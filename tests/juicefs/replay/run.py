"""jfsreplay run: 1 つの compaction の設定で、記録を試験の volume に 1 回流す（設計書 §5.4 の 1〜8）。

手順:
  0. 安全の確かめ（本番と重ならないこと）。試験の mount が上がっていれば外す。
     --age N なら、既定の設定で mount → 種を clone → 記録を N 回流す → 落ち着く → mount を外す（差分を育てる）。
     温めたキャッシュの写し（warmcache）が無いか、種が変わっていれば、先に作る（ensure_warm: 既定の設定で mount →
     種を clone → 記録の read の範囲を Drive から 1 回読む → 複製を消す → mount を外す → 写す）。
  1. 試験の mount を外した状態でキャッシュ（試験のキャッシュだけ）を空にし、温めたキャッシュの写しを戻してから、
     その回の設定で mount する（各回は同じ温まったキャッシュから始まる。Drive から温め直さない）。
  2. 種のファイルを `juicefs clone` で per-run の path に複製する（--age のときは育てた複製を使う）。
  3. （温めは写しを作るときだけ。--head-seconds N なら記録の最初の N 秒だけを流す）
  4. 計測の suite（test）を <out> に始める。
  5. 記録を流す（player.play）。Drive の rate limit・mount の停止を見張り、出たら止める。
  6. 落ち着くまで待つ（staging_blocks 0、object_request_uploading 0、compact_size_histogram_bytes_count が
     settle 秒変わらない。上限 settle_max 秒で「未収束」）。
  7. 計測の材料（capture end）を取って計測を止め、play.json を書き、run.json に流し直しの情報を足す。
  8. 複製を消し、削除の後始末が落ち着くのを待ってから、mount を外す。
EIO などのエラー・rate limit・mount の停止は、その回を「失敗」として結果に出す（次の設定へ進まない判断は呼ぶ側）。"""
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import tomllib
import traceback

from lib import config, metrics_io
from lib.resp import Resp
from measure import capture as mcap
from measure import cli as mcli
from replay import player, testvol, trace, warmcache

SUITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(SUITE_DIR, "settings.toml")
JFSMEASURE = os.path.join(SUITE_DIR, "jfsmeasure")
DEFAULT_SETTINGS = {"name": "default", "mode": None, "flags": {}}  # mount_flags.txt のまま
STAGING = "juicefs_staging_blocks"
UPLOADING = "juicefs_object_request_uploading"
COMPACT = "juicefs_compact_size_histogram_bytes_count"
OBJ_ERRORS = "juicefs_object_request_errors"
PROD_INCLUDE = re.compile(r"^juicefs_object_request_errors")  # 本番の /metrics から読む系列（読み取りの GET だけ）
WATCH_INCLUDE = re.compile(r"^juicefs_(staging_blocks|object_request_uploading|object_request_errors|"
                           r"compact_size_histogram_bytes_count|object_request_durations_histogram_seconds_count)")
# Drive（rclone serve s3）の回数制限の行。429 は too many／rate／limit と一緒のときだけ（inode などの数字と区別する）
RATE_LIMIT_RE = re.compile(r"\b429\b.*(too many|rate|limit)|too many requests|\b403\b.*rate ?limit|"
                           r"ratelimitexceeded", re.I)
SETTLED, NOT_SETTLED = "落ち着いた", "未収束"


class RunError(Exception):
    """試験を続けられないとき（種が無い、staging に上がっていない block がある、clone の失敗など）。"""


def load_settings(name, path=SETTINGS_PATH):
    """settings.toml（[[settings]] の name／mode／flags）から name の設定を返す。
    ファイルに無い "default" は mount_flags.txt のままの設定。見つからなければ KeyError。"""
    items = []
    if os.path.exists(path):
        with open(path, "rb") as f:
            items = tomllib.load(f).get("settings", [])
    for s in items:
        if s.get("name") == name:
            return {"name": name, "mode": s.get("mode"), "flags": dict(s.get("flags") or {})}
    if name == "default":
        return dict(DEFAULT_SETTINGS, flags={})
    raise KeyError("未知の設定: %s（%s）" % (name, path))


class Settle:
    """落ち着いたかの判定。staging_blocks と object_request_uploading が 0 で、
    compact_size_histogram_bytes_count が quiet_s 秒変わらなければ真。系列が無ければ偽。"""

    def __init__(self, quiet_s):
        self.quiet_s = quiet_s
        self.last = None
        self.since = None

    def update(self, m, now):
        """metrics の値 m（系列名 → 値）を時刻 now で与え、落ち着いたかを返す。"""
        c = m.get(COMPACT)
        if c is None or m.get(STAGING) is None or m.get(UPLOADING) is None:
            self.last = self.since = None
            return False
        if self.last is None or c != self.last:
            self.last, self.since = c, now
        if m[STAGING] != 0 or m[UPLOADING] != 0:
            self.since = now  # 書き出し中は数え直す
            return False
        return now - self.since >= self.quiet_s


# 試験の mount のログの行（logRequest@cached_store.go と finishStagingUpload@cached_store.go の形）
LOG_TIME_RE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)(\.\d+)?")
ERR_REQ_RE = re.compile(r"slow request: (\w+) (\S+) .*?\berr: (?!<nil>)")  # err が <nil> でない object の要求
ABANDONED_RE = re.compile(r"\bKey (\S+) is not needed, abandoned")
ABANDON_WINDOW_S = 30.0  # PUT の誤りの後、同じ key の abandoned を待つ秒数（初回の実走では 5〜11 秒後）
UNLOGGED_GRACE_S = 5.0  # metrics の誤りに対応する行がログに出るのを待つ秒数
ABANDON_SKEW_S = 1.0  # abandoned の行が誤りの行より先に出た場合の許し（ログの書き込みの前後）


def _log_time(line):
    """ログの行頭の時刻（現地時刻）を UNIX 秒にする。無ければ None。"""
    m = LOG_TIME_RE.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y/%m/%d %H:%M:%S")) + float(m.group(2) or 0)
    except (ValueError, OverflowError):
        return None


def classify_errors(errors, abandoned, now, window_s=ABANDON_WINDOW_S):
    """ログの object の要求の誤り errors（(method, key, 時刻) の list）を、abandoned（key → 時刻の list）で分ける。
    - 説明できる（explained）: PUT で、同じ key の abandoned が誤りの時刻の ABANDON_SKEW_S 前から window_s 後までにある
      （compaction が staging の slice を retire し、上げ途中の PUT が打ち切られた。試験している効果そのもの）。
    - 待ち中（pending）: PUT で、まだ window_s が過ぎていない。
    - 説明できない（unexplained）: それ以外（PUT 以外の誤り、窓の中に abandoned が来なかった PUT）。
    戻り値は {explained, pending, unexplained, first_unexplained（最初の説明できない誤りの文字列 or None）}。"""
    c = {"explained": 0, "pending": 0, "unexplained": 0, "first_unexplained": None}
    for method, key, t in errors:
        if method == "PUT":
            if any(t - ABANDON_SKEW_S <= a <= t + window_s for a in abandoned.get(key, ())):
                c["explained"] += 1
                continue
            if now < t + window_s:
                c["pending"] += 1
                continue
        c["unexplained"] += 1
        if c["first_unexplained"] is None:
            c["first_unexplained"] = "%s %s（%s）" % (method, key, time.strftime("%H:%M:%S", time.localtime(t)))
    return c


class Guard:
    """流し直しと待ちの間の見張り。試験の mount のログの 429／403 の rate limit、mount の停止（mountpoint が mount でない）、
    metrics の 3 回続けての取得の失敗、説明できない object の要求の誤りを、check() で理由の文字列にして返す。
    試験の object_request_errors の増加は、そのまま止める理由にはしない。compaction が staging の slice を retire すると、
    上げ途中の PUT が打ち切られて誤りになり、同じ key の `Key … is not needed, abandoned` がログに出る（初回の実走、
    2026-10-21）。これは試験している効果なので数えない。説明できない誤り（abandoned の来ない PUT、PUT 以外、
    ログに行の無い metrics の誤り）が tolerance を超えたら止める（classify_errors と counts を参照）。
    prod_scrape（本番の /metrics を読み取りの GET で取る関数）があれば、本番の object_request_errors の増加でも止める
    （rclone と Drive は本番と共有なので、試験の負荷が本番の失敗を起こしたら続けない）。本番の値が取れないときは
    その項目だけ見張れない（prod_seen が偽のまま。呼ぶ側が警告に残す）。"""

    def __init__(self, scrape, log_path, mounted, prod_scrape=None, tolerance=0, window_s=ABANDON_WINDOW_S,
                 unlogged_grace_s=UNLOGGED_GRACE_S, clock=time.time):
        self.scrape, self.log_path, self.mounted = scrape, log_path, mounted
        self.prod_scrape = prod_scrape
        self.tolerance, self.window_s, self.unlogged_grace_s, self.clock = tolerance, window_s, unlogged_grace_s, clock
        self.base = None
        self.prod_base = None
        self.prod_seen = False
        self.fail = 0
        self.pos = 0
        self.errors = []  # ログの誤り (method, key, 時刻)
        self.abandoned = {}  # key → abandoned の時刻の list
        self.history = []  # (時計の時刻, metrics の誤りの増分)
        self.total = 0.0  # metrics の誤りの増分（最新）
        self.last = classify_errors([], {}, 0)
        self.final = False  # finalize の後は、待ち中と猶予中の誤りも説明できない誤りとして数える
        try:
            self.pos = os.path.getsize(log_path)  # 始める前の行は見ない
        except OSError:
            pass
        self._base()

    def _base(self):
        """object_request_errors の基準の値を取る。"""
        try:
            self.base = self.scrape().get(OBJ_ERRORS)
        except (OSError, ValueError):
            self.base = None
        self.prod_base = self._prod_errors()

    def _prod_errors(self):
        """本番の object_request_errors の今の値。prod_scrape が無いか、取れなければ None。"""
        if self.prod_scrape is None:
            return None
        try:
            v = self.prod_scrape().get(OBJ_ERRORS)
        except (OSError, ValueError):
            return None
        if v is not None:
            self.prod_seen = True
        return v

    def _scan_log(self):
        """ログの増えた分を読み、誤りの行と abandoned の行を控える。rate limit の行があれば最初の 1 行（200 文字まで）を返す。"""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return None
        cut = data.rfind(b"\n") + 1  # 書きかけの行は次回に回す
        self.pos += cut
        hit = None
        for line in data[:cut].decode("utf-8", "replace").splitlines():
            if hit is None and RATE_LIMIT_RE.search(line):
                hit = line.strip()[:200]
            m = ERR_REQ_RE.search(line)
            if m:
                t = _log_time(line)
                self.errors.append((m.group(1), m.group(2), self.clock() if t is None else t))
                continue
            m = ABANDONED_RE.search(line)
            if m:
                t = _log_time(line)
                self.abandoned.setdefault(m.group(1), []).append(self.clock() if t is None else t)
        return hit

    def _unlogged(self, now):
        """unlogged_grace_s より前の metrics の誤りの増分のうち、ログに行の無い数（遅くない誤りは行が出ない）。"""
        old = [d for t, d in self.history if t <= now - self.unlogged_grace_s]
        return max(0, int((old[-1] if old else 0) - len(self.errors)))

    def counts(self):
        """object の要求の誤りの内訳（run.json に残す）: errors_total（metrics の増分）、logged（ログの誤りの行）、
        abandoned_explained、pending、unexplained（ログの説明できない誤り + unlogged）、unlogged、tolerance、finalized。
        finalize の後は、待ち中（pending）を説明できない誤りに入れ（pending は 0）、unlogged は猶予なしで数える。"""
        if self.final:
            unlogged = max(0, int(self.total - len(self.errors)))
            unexplained = self.last["unexplained"] + self.last["pending"] + unlogged
            pending = 0
        else:
            unlogged = self._unlogged(self.clock())
            unexplained, pending = self.last["unexplained"] + unlogged, self.last["pending"]
        return {"errors_total": self.total, "logged": len(self.errors), "abandoned_explained": self.last["explained"],
                "pending": pending, "unexplained": unexplained, "unlogged": unlogged, "tolerance": self.tolerance,
                "finalized": self.final}

    def _refresh(self):
        """ログと試験の metrics を読み直して分類を更新する（mount の確かめはしない）。rate limit の行があれば返す。"""
        hit = self._scan_log()
        now = self.clock()
        try:
            v = self.scrape().get(OBJ_ERRORS)
        except (OSError, ValueError):
            v = None
        if v is not None:
            if self.base is None:
                self.base = v
            else:
                self.total = max(0.0, v - self.base)
                self.history.append((now, self.total))
        self.last = classify_errors(self.errors, self.abandoned, now, self.window_s)
        return hit

    def _quiet(self, now):
        """待ち中の PUT が無く、猶予（unlogged_grace_s）の間に metrics の誤りが増えていなければ真（判定を確定してよい）。"""
        old = [d for t, d in self.history if t <= now - self.unlogged_grace_s]
        return self.last["pending"] == 0 and (old[-1] if old else 0) >= self.total

    def finalize(self, wait=True, poll_s=1.0, sleep=time.sleep):
        """Guard を捨てる前（流し直しと待ちの終わり、run の終わり）に判定を確定する。wait なら、待ち中の PUT の窓と
        unlogged の猶予が過ぎるまで（上限 window_s + unlogged_grace_s + 1 秒）ログと metrics を読み直す。その後、
        残った待ち中と猶予中の誤りは説明できない誤りとして数える（counts の finalized）。
        戻り値は止める（失敗にする）理由（説明できない誤りが tolerance を超えた、または rate limit の行）か None。"""
        if self.final:
            return None
        deadline = self.clock() + (self.window_s + self.unlogged_grace_s + 1.0 if wait else 0.0)
        hit = None
        while True:
            hit = self._refresh() or hit
            now = self.clock()
            if not wait or now >= deadline or self._quiet(now):
                break
            sleep(max(0.0, min(poll_s, deadline - now)))
        self.final = True
        if hit:
            return "試験の mount のログに rate limit（429／403）: %s" % hit
        c = self.counts()
        if c["unexplained"] > self.tolerance:
            what = self.last["first_unexplained"] or "待ち中・ログに行の無い誤り"
            return ("流し直しの終わりの確定: 試験の object_request_errors に、retire（abandoned）で説明できない誤りが"
                    " %d 件（許容 %d）: %s（誤り %g 件、abandoned で説明 %d 件、窓の中に abandoned が来なかった"
                    "待ち中 %d 件、ログに行の無い誤り %d 件）"
                    % (c["unexplained"], self.tolerance, what, c["errors_total"], c["abandoned_explained"],
                       self.last["pending"], c["unlogged"]))
        return None

    def check(self):
        """止める理由があれば文字列、無ければ None。"""
        if not self.mounted():
            return "試験の mount が落ちた（mountpoint が mount ではない）"
        hit = self._scan_log()
        if hit:
            return "試験の mount のログに rate limit（429／403）: %s" % hit
        pv = self._prod_errors()
        if self.prod_base is None:
            self.prod_base = pv
        elif pv is not None and pv > self.prod_base:
            return "本番の object_request_errors が増えた（%g → %g）。本番への影響を避けるため止めます" % (self.prod_base, pv)
        now = self.clock()
        try:
            m = self.scrape()
            self.fail = 0
        except (OSError, ValueError):
            self.fail += 1
            return "試験の metrics を 3 回続けて取得できない" if self.fail >= 3 else None
        v = m.get(OBJ_ERRORS)
        if self.base is None:
            self.base = v
        elif v is not None:
            self.total = max(0.0, v - self.base)
            self.history.append((now, self.total))
        self.last = classify_errors(self.errors, self.abandoned, now, self.window_s)
        c = self.counts()
        if c["unexplained"] > self.tolerance:
            what = self.last["first_unexplained"] or "ログに行の無い誤り %d 件" % c["unlogged"]
            return ("試験の object_request_errors に、retire（abandoned）で説明できない誤りが %d 件（許容 %d）: %s"
                    "（誤り %g 件、abandoned で説明 %d 件、待ち中 %d 件）"
                    % (c["unexplained"], self.tolerance, what, c["errors_total"], c["abandoned_explained"], c["pending"]))
        return None


def sum_counts(guards):
    """複数の Guard の counts を足す（--age の回と本番の回）。Guard が無ければ None。"""
    if not guards:
        return None
    out = {}
    for g in guards:
        for k, v in g.counts().items():
            if k == "tolerance":
                out[k] = v
            elif k == "finalized":
                out[k] = out.get(k, True) and v
            else:
                out[k] = out.get(k, 0) + v
    return out


def _watch(guard, stop, poll_s, reasons):
    """guard を poll_s ごとに確かめる thread を起こす。理由が出たら reasons に足して stop を立てる。"""
    done = threading.Event()

    def loop():
        """stop か done が立つまで見張る。"""
        while not (stop.is_set() or done.is_set()):
            r = guard.check()
            if r:
                reasons.append(r)
                stop.set()
                return
            done.wait(poll_s)
    t = threading.Thread(target=loop, name="replay-guard", daemon=True)
    t.start()

    def finish():
        """見張りを止める。"""
        done.set()
        t.join()
    return finish


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _under(a, b):
    """a が b と同じか、b の下にあるか（どちらも解決済みの絶対 path）。"""
    return a == b or a.startswith(b.rstrip("/") + "/")


def _rm_tree(p):
    """p（実のディレクトリ）の中身を、symlink をたどらずに消して p も消す。symlink はリンク自体だけ消す。消したファイルの数。"""
    n = 0
    for r, ds, fs in os.walk(p, topdown=False, followlinks=False):
        for f in fs:
            os.unlink(os.path.join(r, f))
            n += 1
        for d in ds:
            q = os.path.join(r, d)
            if os.path.islink(q):
                os.unlink(q)  # ディレクトリへの symlink（先は消さない）
                n += 1
            else:
                os.rmdir(q)
    os.rmdir(p)
    return n


def check_cache_dir(cache_dir, mountpoint, prod, state_dir):
    """消してよい試験のキャッシュの場所かを確かめ、解決した絶対 path を返す。だめなら SafetyError。
    空・None・相対 path は不可。realpath で解決し、本番のキャッシュ・本番の mountpoint・試験の state_dir（Redis の AOF）・
    試験の mountpoint・suite のディレクトリと重なるか、$HOME 自体かその上なら止める。"""
    if not cache_dir or not isinstance(cache_dir, str) or not os.path.isabs(cache_dir):
        raise testvol.SafetyError("キャッシュの場所が空か相対 path なので消さない: %r" % (cache_dir,))
    # 本番の mountpoint は FUSE の root に stat を送らないよう文字列で比べる（realpath の前に）
    if testvol._overlaps(cache_dir, prod.mountpoint) or testvol._overlaps(mountpoint, prod.mountpoint):
        raise testvol.SafetyError("キャッシュを消す対象が本番の mountpoint と重なるので止めます")
    real = os.path.realpath(cache_dir)
    home = os.path.realpath(os.path.expanduser("~"))
    if _under(home, real):
        raise testvol.SafetyError("キャッシュの場所が $HOME 自体かその上なので消さない: %s" % real)
    for label, other in (("本番のキャッシュ", prod.cache_dir), ("試験の state（Redis の AOF）", state_dir),
                         ("試験の mountpoint", mountpoint), ("suite のディレクトリ", SUITE_DIR)):
        if not other:
            continue
        o = os.path.realpath(os.path.expanduser(other)) if label != "試験の mountpoint" else os.path.abspath(other)
        if _under(real, o) or _under(o, real):
            raise testvol.SafetyError("キャッシュを消す対象が%sと重なるので止めます: %s" % (label, real))
    return real


def clear_cache(cache_dir, mountpoint, prod, state_dir, volume_uuid=None):
    """試験の mount が外れているときだけ、試験のキャッシュ（<cache_dir>/<UUID>/ の rawstaging 以外）を消す。
    場所は check_cache_dir で確かめる（だめなら SafetyError）。1 段目は UUID の形の名前の実のディレクトリだけを対象にし
    （volume_uuid があればそれだけ）、symlink やほかの名前には触れない。mount 中か、rawstaging に上がっていない block が
    あれば RunError（何も消さない）。戻り値は消したファイルの数。"""
    real = check_cache_dir(cache_dir, mountpoint, prod, state_dir)
    if os.path.ismount(mountpoint):
        raise RunError("試験の mount が上がっている間はキャッシュを消さない: %s" % mountpoint)
    try:
        st0 = os.lstat(real)
    except FileNotFoundError:
        return 0
    if not stat.S_ISDIR(st0.st_mode):
        raise testvol.SafetyError("キャッシュの場所がディレクトリではない: %s" % real)
    vols = []
    for name in os.listdir(real):
        p = os.path.join(real, name)
        if not UUID_RE.match(name) or (volume_uuid and name != volume_uuid):
            continue
        if stat.S_ISDIR(os.lstat(p).st_mode):  # symlink（lstat で LNK）は対象にしない
            vols.append(p)
    for v in vols:  # 先に全部確かめる（途中まで消してから止まらない）
        st = os.path.join(v, "rawstaging")
        if os.path.lexists(st):
            if not stat.S_ISDIR(os.lstat(st).st_mode):
                raise RunError("rawstaging が実のディレクトリではないので、キャッシュを消さない: %s" % st)
            # 空のディレクトリ（書き出し済みの後に残る chunks/...）は良い。ファイルか symlink があれば止める
            if any(fs or any(os.path.islink(os.path.join(r, x)) for x in ds) for r, ds, fs in os.walk(st)):
                raise RunError("staging に上がっていない block が残っているので、キャッシュを消さない: %s" % st)
    n = 0
    for v in vols:
        for name in os.listdir(v):
            if name == "rawstaging":
                continue
            p = os.path.join(v, name)
            if stat.S_ISDIR(os.lstat(p).st_mode):
                n += _rm_tree(p)
            else:
                os.unlink(p)  # ファイルと symlink（リンク自体）
                n += 1
    return n


def warm(path, ops):
    """記録の read の範囲（重複は 1 回）を path から 1 回読む。その後 page cache を捨てて、流し直しの read が
    JuiceFS のキャッシュに届くようにする。戻り値は {ranges, bytes, seconds}。"""
    rng = sorted({(o.off, o.size) for o in ops if o.kind == "read" and o.status == "OK" and o.size > 0})
    t = time.monotonic()
    total = 0
    fd = os.open(path, os.O_RDONLY)
    try:
        for off, size in rng:
            total += len(os.pread(fd, size, off))
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    return {"ranges": len(rng), "bytes": total, "seconds": round(time.monotonic() - t, 3)}


def clone(binary, src, dst):
    """`juicefs clone src dst`（メタデータだけの複製）。失敗は RunError。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        p = subprocess.run([binary, "clone", src, dst], capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RunError("juicefs clone が %d 秒で終わらない: %s" % (600, dst)) from None
    if p.returncode != 0:
        raise RunError("juicefs clone が失敗（終了 %d）:\n%s" % (p.returncode, (p.stderr or p.stdout)[-1500:]))
    return os.stat(dst).st_ino


def _scan_any(r, pattern):
    """試験の Redis に pattern の key が 1 つでもあるか（SCAN を最後まで回す）。"""
    cur = b"0"
    while True:
        cur, keys = r.call("SCAN", cur, "MATCH", pattern, "COUNT", 1000)
        if keys:
            return True
        if cur in (b"0", "0", 0):
            return False


def _deletes(m):
    """metrics の値から object storage の DELETE の回数の合計。"""
    return sum(v for k, v in m.items() if k.startswith("juicefs_object_request_durations_histogram_seconds_count")
               and 'method="DELETE"' in k)


def wait_deleted(spec, inode, scrape, quiet_s, max_s, poll_s, clock=time.monotonic):
    """複製を消した後、試験の Redis に c<inode>_* と delfiles が無くなり、DELETE の回数が quiet_s 秒変わらなく
    なるまで待つ（上限 max_s）。戻り値は {state, seconds}。"""
    t0 = clock()
    last, since = None, t0
    with Resp(spec.redis_addr[0], spec.redis_addr[1], db=spec.db, readonly=True, timeout=10) as r:
        while True:
            now = clock()
            gone = not _scan_any(r, "c%d_*" % inode) and not _scan_any(r, "delfiles")
            try:
                d = _deletes(scrape())
            except (OSError, ValueError):
                d = None
            if d != last or not gone:
                last, since = d, now
            if gone and now - since >= quiet_s:
                return {"state": SETTLED, "seconds": round(now - t0, 1)}
            if now - t0 >= max_s:
                return {"state": NOT_SETTLED, "seconds": round(now - t0, 1)}
            time.sleep(poll_s)


def settle(scrape, guard, quiet_s, max_s, poll_s, clock=time.monotonic):
    """落ち着くまで待つ。戻り値は {state（落ち着いた／未収束／止めた）, seconds, reason}。"""
    st = Settle(quiet_s)
    t0 = clock()
    while True:
        now = clock()
        reason = guard.check() if guard else None
        if reason:
            return {"state": "止めた", "seconds": round(now - t0, 1), "reason": reason}
        try:
            m = scrape()
        except (OSError, ValueError):
            m = {}
        if st.update(m, now):
            return {"state": SETTLED, "seconds": round(now - t0, 1)}
        if now - t0 >= max_s:
            return {"state": NOT_SETTLED, "seconds": round(now - t0, 1),
                    "last": {k: m.get(k) for k in (STAGING, UPLOADING, COMPACT)}}
        time.sleep(poll_s)


FLUSH_MAX_S = 6 * 3600  # 種の書き出しを待つ上限（32 GiB の種でも Drive に上げ切れるよう大きく取る）


def wait_flushed(scrape, max_s=FLUSH_MAX_S, poll_s=5.0, progress_s=60.0, log=print, clock=time.monotonic):
    """staging_blocks と object_request_uploading が 2 回続けて 0 になるまで待つ（書き出しの完了）。
    progress_s ごとに今の値を log に出す。戻り値は {state（落ち着いた／未収束）, seconds, last}。
    系列が取れない間は 0 とみなさずに待ち続ける（上限 max_s）。"""
    t0 = clock()
    zeros, last_log, m = 0, None, {}
    while True:
        now = clock()
        try:
            m = scrape()
        except (OSError, ValueError):
            m = {}
        st, up = m.get(STAGING), m.get(UPLOADING)
        zeros = zeros + 1 if st == 0 and up == 0 else 0
        if zeros >= 2:
            return {"state": SETTLED, "seconds": round(now - t0, 1), "last": {STAGING: st, UPLOADING: up}}
        if last_log is None or now - last_log >= progress_s:
            log("書き出しを待つ: staging_blocks=%s、object_request_uploading=%s（経過 %d 秒、上限 %d 秒）"
                % (st, up, now - t0, max_s))
            last_log = now
        if now - t0 >= max_s:
            return {"state": NOT_SETTLED, "seconds": round(now - t0, 1), "last": {STAGING: st, UPLOADING: up}}
        time.sleep(poll_s)


def _toml(v):
    """profiles.toml に書く値（文字列・数・真偽）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)


def write_measure_profiles(out, spec, inode, common, intervals=None, *, cache_dir):
    """計測の suite に渡す <out>/profiles.toml（test だけ）を書く。inode は複製の inode、Redis は試験の Redis の直接の
    port と db、間隔は intervals（metrics／llen／keys の秒）で上書きできる。接続の鍵は入れない。path を返す。
    cache_dir は volume のキャッシュの実の場所（<spec.cache_dir>/<UUID>。JuiceFS は --cache-dir の下の UUID の
    ディレクトリに置くので、親の spec.cache_dir では keys・内訳・kaz-cache-state が空になる）。
    log_glob は試験の mount の juicefs のログだけ（Redis や中継のログは含めない）。"""
    c = dict(common)
    for k, key in (("metrics", "interval_metrics_s"), ("llen", "interval_llen_s"), ("keys", "interval_cache_churn_s")):
        if intervals and k in intervals:
            c[key] = intervals[k]
    test = {"metrics_url": "http://%s:%d/metrics" % spec.metrics_addr, "inode": inode, "cache_dir": cache_dir,
            "mountpoint": spec.mountpoint, "redis_source": "test", "redis_addr": "%s:%d" % spec.redis_addr,
            "redis_db": spec.db, "log_glob": spec.path("juicefs.log"), "bucket": spec.bucket_name}
    path = os.path.join(out, "profiles.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# jfsreplay run が書いた計測用のプロファイル（複製の inode と試験の資源）\n[test]\n")
        f.writelines("%s = %s\n" % (k, _toml(v)) for k, v in test.items())
        f.write("\n[common]\n")
        f.writelines("%s = %s\n" % (k, _toml(v)) for k, v in c.items())
    return path


def _seed_path(trace_path, spec):
    """trace の隣の seed.json から、試験の mountpoint の上の種の path を返す。無ければ RunError。
    seed.json の name が trace の名前（拡張子を除く）と違えば、別の trace の種なので RunError。"""
    p = os.path.join(os.path.dirname(os.path.abspath(trace_path)), "seed.json")
    try:
        with open(p, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        raise RunError("seed.json が無い（先に jfsreplay seed を実行する）: %s" % p) from None
    name = os.path.splitext(os.path.basename(trace_path))[0]
    if info.get("name") != name:
        raise RunError("seed.json の種（%r）は trace（%r）のものではない。jfsreplay seed %s で作り直す: %s"
                       % (info.get("name"), name, trace_path, p))
    return os.path.join(spec.mountpoint, os.path.relpath(info["path"], info["mountpoint"])), info


def _mcli(*args):
    """計測の suite の CLI（jfsmeasure）を子の process で呼ぶ（sampler の Popen をこの process に残さないため）。
    0 以外は RunError（出力の末尾を付ける）。"""
    name = next((x for x in args if x in ("start", "stop", "capture")), args[0])
    try:
        p = subprocess.run([sys.executable, "-I", JFSMEASURE] + list(args), capture_output=True, text=True,
                           timeout=300, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RunError("jfsmeasure %s が %d 秒で終わらない" % (name, 300)) from None
    if p.returncode != 0:
        raise RunError("jfsmeasure %s が失敗（終了 %d）:\n%s" % (name, p.returncode, (p.stderr + p.stdout)[-1500:]))
    return p.stdout + p.stderr


def _play_guarded(path, ops, guard, poll_s, reasons):
    """見張り付きで 1 回流す。戻り値は PlayResult。"""
    stop = threading.Event()
    finish = _watch(guard, stop, poll_s, reasons)
    try:
        return player.play(path, ops, time.monotonic() + 1.0, stop=stop)
    finally:
        finish()


def ensure_warm(trace_path, ops, spec, profile, prod, up_kw, default, scrape, *, force=False, delete_quiet_s=30,
                max_s=600, poll_s=5.0, tsr=None, log=print):
    """温めたキャッシュの写しが今の種で使えればそのまま返し、無いか種が変わっていれば（force なら必ず）作る。
    作る手順: 試験の mount を外す → キャッシュを空にする → 既定の設定で mount → 種を clone → 記録の read の範囲を
    1 回読む（warm。Drive から取る）→ 複製を消して後始末を待つ → mount を外す（キャッシュと kaz-2q の状態が揃う）→
    warmcache.save。途中の失敗でも、複製を消して mount を外してから例外を上げる。戻り値は (meta, 作ったか)。"""
    tsr = tsr or testvol._run
    seed_mp_path, seed_info = _seed_path(trace_path, spec)
    ident = warmcache.seed_identity(trace_path, seed_info)
    d = warmcache.warm_dir(spec, trace_path)
    root = warmcache.warm_root(spec)
    warmcache.check_warm_dir(d, root, cache_dir=spec.cache_dir, mountpoint=spec.mountpoint, prod=prod)
    meta = warmcache.load_meta(d)
    ok, why = warmcache.is_valid(meta, ident, d)
    if ok and not force:
        log("温めたキャッシュの写しを使う: %s（%s 作成、%d ファイル、%d バイト）"
            % (d, meta.get("saved_at"), meta.get("files", 0), meta.get("bytes", 0)))
        return meta, False
    log("温めたキャッシュの写しを作る（%s）: %s" % ("--force" if ok else why, d))
    if os.path.ismount(spec.mountpoint):
        testvol.umount(spec, run=tsr)
    n = clear_cache(spec.cache_dir, spec.mountpoint, prod, spec.state_dir)
    log("試験のキャッシュを空にした（%d ファイル）。既定の設定で mount して Drive から温める" % n)
    clone_path = ino = None
    ok_body = False
    try:
        testvol.up(profile, default, spec=spec, **up_kw)
        uuid = _volume_uuid(spec)
        if not uuid:
            raise RunError("試験の mount の .config から volume の UUID を読めない（写すキャッシュの場所が決まらない）")
        clone_path, ino = _make_clone(spec, seed_mp_path, "warm", log)
        w = warm(clone_path, ops)
        log("キャッシュを温めた: %(ranges)d 範囲、%(bytes)d バイト、%(seconds).1f 秒" % w)
        os.unlink(clone_path)
        clone_path = None
        dl = wait_deleted(spec, ino, scrape, delete_quiet_s, max_s, poll_s)
        testvol.umount(spec, run=tsr)
        meta = warmcache.save(spec.cache_dir, uuid, d, {"seed": ident, "trace": os.path.abspath(trace_path),
                                                        "settings": default.get("name"), "warm": w, "delete": dl},
                              mountpoint=spec.mountpoint, prod=prod, state_dir=spec.state_dir, root=root)
        log("温めたキャッシュを写した: %s（%d ファイル、%d バイト、%.1f 秒）"
            % (d, meta["files"], meta["bytes"], meta["save_seconds"]))
        ok_body = True
        return meta, True
    finally:
        # 失敗の後始末（複製と mount を残さない）。本体が成功した後には何もしない。後始末の失敗は元の例外を隠さない
        if not ok_body:
            try:
                if clone_path and os.path.ismount(spec.mountpoint):
                    os.unlink(clone_path)
                    wait_deleted(spec, ino, scrape, delete_quiet_s, max_s, poll_s)
            except (OSError, RuntimeError, ConnectionError) as e:
                log("警告: 温め用の複製を消せない（手で確かめる）: %s: %s" % (clone_path, e))
            try:
                testvol.umount(spec, run=tsr)
            except (OSError, RuntimeError) as e:
                log("警告: 試験の mount を外せない: %s" % e)


def _common_setup(profile, spec, up_kw):
    """run／warm_only の共通の準備: common、spec、prod、安全の確かめ。戻り値は (common, spec, up_kw, prod)。"""
    common = config.load_common()
    spec = spec or testvol.spec_from_profile(profile, common)
    up_kw = dict(up_kw or {})
    prod = up_kw.get("prod") or config.load_profile("prod")
    up_kw["prod"] = prod
    testvol.safety_check(profile, spec=spec, prod=prod, prod_redis=up_kw.get("prod_redis"))
    return common, spec, up_kw, prod


def warm_only(trace_path, profile, spec=None, up_kw=None, force=False, delete_quiet_s=30, poll_s=5.0,
              settings_path=SETTINGS_PATH, log=print):
    """jfsreplay warm: 温めたキャッシュの写しを（無いか種が変わっていれば。force なら必ず）作る。戻り値は (meta, 作ったか)。"""
    common, spec, up_kw, prod = _common_setup(profile, spec, up_kw)
    ops = trace.load(trace_path)
    if not ops:
        raise RunError("trace に操作が無い: %s" % trace_path)
    url = "http://%s:%d/metrics" % spec.metrics_addr
    return ensure_warm(trace_path, ops, spec, profile, prod, up_kw, load_settings("default", settings_path),
                       lambda: metrics_io.scrape(url, WATCH_INCLUDE, timeout=10), force=force,
                       delete_quiet_s=delete_quiet_s, max_s=common["settle_max_seconds"], poll_s=poll_s,
                       tsr=up_kw.get("run"), log=log)


def run(trace_path, settings_name, out, profile, spec=None, age=0, up_kw=None, settle_s=None, settle_max_s=None,
        delete_quiet_s=30, poll_s=5.0, intervals=None, settings_path=SETTINGS_PATH, log=print, prod_scrape=None,
        head_s=None):
    """1 回の試験を行い、結果の dict（status は ok／failed、reasons、clone、clone_inode、settle、out）を返す。
    結果の files は <out>/ の run.json、metrics.tsv など（計測の suite）、play.json、config.json。
    prod_scrape は本番の object_request_errors を読む関数（既定は本番の metrics_url への読み取りの GET。試験で偽に差し替える）。
    head_s は記録の最初の head_s 秒だけを流す（--head-seconds。None なら全部）。温めたキャッシュの写しは記録の全体で作る。"""
    common, spec, up_kw, prod = _common_setup(profile, spec, up_kw)
    if testvol._overlaps(out, prod.cache_dir) or testvol._overlaps(out, prod.mountpoint):
        raise testvol.SafetyError("出力の場所が本番と重なるので止めます: %s" % out)
    settings = load_settings(settings_name, settings_path)
    default = load_settings("default", settings_path)
    settle_s = common["settle_seconds"] if settle_s is None else settle_s
    settle_max_s = common["settle_max_seconds"] if settle_max_s is None else settle_max_s
    all_ops = trace.load(trace_path)
    ops = trace.head(all_ops, head_s)
    if not ops:
        raise RunError("trace に操作が無い: %s" % trace_path)
    os.makedirs(out, exist_ok=True)
    tsr = testvol._run if "run" not in up_kw else up_kw["run"]
    url = "http://%s:%d/metrics" % spec.metrics_addr

    def scrape():
        """試験の mount の見張り用の系列を取る。"""
        return metrics_io.scrape(url, WATCH_INCLUDE, timeout=10)

    def mounted():
        """試験の mount が生きているか。"""
        return os.path.ismount(spec.mountpoint)

    if prod_scrape is None:
        def prod_scrape():
            """本番の /metrics から object_request_errors だけを読む（読み取りの GET のみ、本番には何も書かない）。"""
            return metrics_io.scrape(prod.metrics_url, PROD_INCLUDE, timeout=10)
    guards = []

    def guard():
        """試験と本番の両方を見張る Guard を作る（本番の値が読めたかを後で確かめるため控える）。"""
        g = Guard(scrape, spec.path("juicefs.log"), mounted, prod_scrape=prod_scrape,
                  tolerance=common.get("object_error_tolerance", 0),
                  window_s=common.get("abandon_window_seconds", ABANDON_WINDOW_S))
        guards.append(g)
        return g

    res = {"status": "ok", "reasons": [], "settings": settings, "age": age, "out": os.path.abspath(out),
           "clone": None, "clone_inode": None, "settle": None, "cleanup": {}, "warnings": [], "head_seconds": head_s,
           "ops": len(ops), "ops_total": len(all_ops)}
    plays, age_plays, measuring = [], [], False
    seed_info = None
    try:
        if os.path.ismount(spec.mountpoint):
            res["volume_uuid"] = _volume_uuid(spec)
            log("試験の mount を外す（設定を入れ替えるため）")
            testvol.umount(spec, run=tsr)
        wmeta, created = ensure_warm(trace_path, all_ops, spec, profile, prod, up_kw, default, scrape,
                                     delete_quiet_s=delete_quiet_s, max_s=settle_max_s, poll_s=poll_s, tsr=tsr, log=log)
        wdir = warmcache.warm_dir(spec, trace_path)
        res["warm_cache"] = {"dir": wdir, "created": created, "saved_at": wmeta.get("saved_at"),
                             "uuid": wmeta.get("uuid"), "files": wmeta.get("files"), "bytes": wmeta.get("bytes")}
        res["warm"] = wmeta.get("warm")  # 写しを作ったときの温めの数（各回は温め直さない）
        seed_mp_path = None
        if age:
            log("--age %d: 既定の設定で差分を育てる" % age)
            testvol.up(profile, default, spec=spec, **up_kw)
            res["volume_uuid"] = _volume_uuid(spec) or res.get("volume_uuid")
            seed_mp_path, seed_info = _seed_path(trace_path, spec)
            res["clone"], res["clone_inode"] = _make_clone(spec, seed_mp_path, settings_name, log)
            ag = guard()  # 育てる全ての回と、その後の待ちで 1 つの Guard を使う（回の境目の誤りも判定する）
            for i in range(age):
                pr = _play_guarded(res["clone"], ops, ag, poll_s, res["reasons"])
                age_plays.append(pr.to_dict())
                if pr.failed or pr.stopped:
                    res["reasons"].append("育てる %d 回目: %s" % (i + 1, pr.stop_reason))
                    raise RunError("差分を育てる途中で止まった")
            s = settle(scrape, ag, settle_s, settle_max_s, poll_s)
            res["age_settle"] = s
            if s["state"] == "止めた":
                res["reasons"].append(s["reason"])
                raise RunError("差分を育てた後の待ちで止まった")
            r = ag.finalize(poll_s=min(poll_s, 1.0))  # 終わりの直前の誤り（abandoned の窓・猶予の中）も判定する
            if r:
                res["reasons"].append(r)
                raise RunError("差分を育てた後の誤りの確定で止まった")
            _warn_tolerated(res, ag)
            testvol.umount(spec, run=tsr)
        n = clear_cache(spec.cache_dir, spec.mountpoint, prod, spec.state_dir, res.get("volume_uuid"))
        log("試験のキャッシュを空にした（%d ファイル）。温めたキャッシュの写しを戻す" % n)
        rs = warmcache.restore(wdir, spec.cache_dir, mountpoint=spec.mountpoint, prod=prod, state_dir=spec.state_dir,
                               root=warmcache.warm_root(spec))
        res["warm_cache"]["restore"] = rs
        log("温めたキャッシュを戻した（%(files)d ファイル、%(bytes)d バイト、%(seconds).1f 秒）" % rs)
        log("設定 %s で mount する" % settings["name"])
        info = testvol.up(profile, settings, spec=spec, **up_kw)
        res["mount_args"] = info["mount_args"]
        uuid = _volume_uuid(spec)
        if not uuid:  # キャッシュの実の場所（<cache_dir>/<UUID>）が決まらないと、キャッシュの計測が空になる
            raise RunError("試験の mount の .config から volume の UUID を読めない（キャッシュの計測の場所が決まらない）")
        res["volume_uuid"] = uuid
        if uuid != rs["uuid"]:  # volume を作り直した（format し直した）後の古い写し。戻したキャッシュが使われない
            raise RunError("温めたキャッシュの写し（UUID %s）は今の試験の volume（UUID %s）のものではない。"
                           "jfsreplay warm --force %s で作り直す" % (rs["uuid"], uuid, trace_path))
        if not res["clone"]:
            seed_mp_path, seed_info = _seed_path(trace_path, spec)
            res["clone"], res["clone_inode"] = _make_clone(spec, seed_mp_path, settings_name, log)
        res["seed"] = {"path": seed_mp_path, "inode": seed_info.get("inode")}
        mprof = write_measure_profiles(out, spec, res["clone_inode"], common, intervals,
                                       cache_dir=os.path.join(spec.cache_dir, uuid))
        _mcli("--profiles", mprof, "start", "test", out)
        measuring = True
        log("記録を流す: %d 操作、%.0f 秒%s" % (len(ops), (ops[-1].start_us - ops[0].start_us) / 1e6,
                                          "（--head-seconds %g、全 %d 操作のうち）" % (head_s, len(all_ops)) if head_s else ""))
        g = guard()
        pr = _play_guarded(res["clone"], ops, g, poll_s, res["reasons"])
        plays.append(pr)
        if pr.failed or pr.stopped:
            res["status"] = "failed"
            res["reasons"].append("流し直しが止まった: %s" % pr.stop_reason)
        else:
            log("落ち着くまで待つ（%d 秒変化なし、上限 %d 秒）" % (settle_s, settle_max_s))
            res["settle"] = settle(scrape, g, settle_s, settle_max_s, poll_s)
            if res["settle"]["state"] == "止めた":
                res["status"] = "failed"
                res["reasons"].append(res["settle"]["reason"])
        # 流し直しと待ち（未収束で上限に達した場合も）の終わりに、窓・猶予の中の誤りも判定する。失敗済みなら待たない
        r = g.finalize(wait=res["status"] == "ok", poll_s=min(poll_s, 1.0))
        if r:
            _fail(res, r)
        else:
            _warn_tolerated(res, g)
        if mounted():
            # 終わりの材料（/metrics、キャッシュの内訳、kaz-cache-state の写し）。取れなかった物は warnings に残して表示する
            res["warnings"].extend(mcap.capture(config.load_profile("test", mprof), out, "end"))
    except Exception as e:  # 想定外の例外も含め、その回を失敗として結果に残す（後始末は finally で必ず行う）
        _fail(res, "%s: %s" % (type(e).__name__, e))
        if not isinstance(e, (RunError, RuntimeError, OSError, ValueError)):
            res["traceback"] = traceback.format_exc()[-3000:]
    except BaseException as e:  # 中断（KeyboardInterrupt など）: 失敗として残し、後始末の後に投げ直す
        _fail(res, "中断された: %s" % type(e).__name__)
        raise
    finally:
        for gg in guards:  # 例外で途中で捨てた Guard も、待たずに確定して数える（待ち中は説明できない誤りに入る）
            if not gg.final:
                pend = gg.counts()["pending"]
                if gg.finalize(wait=False) and pend:
                    res["warnings"].append("途中で止まったため、abandoned を待ち切れなかった PUT の誤り %d 件を説明できない誤りに数えた" % pend)
        res["object_errors"] = sum_counts(guards)
        if guards and not any(g.prod_seen for g in guards):
            res["warnings"].append("本番の metrics（%s）を読めなかったので、本番の object_request_errors は見張れていない"
                                   % prod.metrics_url)
        if measuring:
            _step(res, "計測の停止", lambda: _mcli("stop", out))
        _step(res, "後始末（複製の削除・umount）",
              lambda: _cleanup(spec, res, scrape, delete_quiet_s, settle_max_s, poll_s, tsr, log))
        if res["cleanup"].get("errors"):
            res["status"] = "failed"
        _step(res, "play.json の書き出し", lambda: _write_play(out, plays, age_plays, res))
        _step(res, "run.json の書き出し", lambda: _write_run_json(out, res))
    return res


def _warn_tolerated(res, g):
    """説明できない誤りが許容（object_error_tolerance）以内で止めなかったとき、その数を warnings に残す（隠さない）。"""
    c = g.counts()
    if c["unexplained"]:
        res["warnings"].append("試験の object_request_errors に説明できない誤りが %d 件（許容 %d 以内なので続けた）: %s"
                               % (c["unexplained"], c["tolerance"], g.last["first_unexplained"] or "ログに行の無い誤り"))


def _fail(res, reason):
    """res を失敗にして理由を足す。"""
    res["status"] = "failed"
    res["reasons"].append(reason)


def _step(res, label, fn):
    """finally の 1 つの手順を、ほかの手順と独立に行う。失敗は res に残し、ほかの手順と元のエラーを隠さない。"""
    try:
        fn()
    except Exception as e:
        _fail(res, "%sの失敗: %s: %s" % (label, type(e).__name__, e))


def _volume_uuid(spec):
    """試験の mount の .config から volume の UUID を読む（キャッシュの 1 段目の名前）。読めなければ None。"""
    try:
        u = testvol.read_mount_config(spec.mountpoint).get("Format", {}).get("UUID")
    except (OSError, ValueError, AttributeError):
        return None
    return u if isinstance(u, str) and UUID_RE.match(u) else None


def _make_clone(spec, src, settings_name, log):
    """種を per-run の path（<mountpoint>/replay/<種>-<設定>-<時刻>-<pid>.img）へ clone し、(path, inode) を返す。"""
    if not os.path.exists(src):
        raise RunError("種のファイルが試験の volume に無い: %s" % src)
    name = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(spec.mountpoint, "replay", "%s-%s-%s-%d.img" % (
        name, re.sub(r"[^\w.-]", "_", settings_name), time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
    ino = clone(spec.binary, src, dst)
    log("種を clone した: %s（inode %d）" % (dst, ino))
    return dst, ino


def _write_play(out, plays, age_plays, res):
    """play.json（kind ごとの待ち時間、予定からの遅れの最大、エラー、落ち着きの判定）を書く。"""
    d = plays[-1].to_dict() if plays else {"latency": {}, "max_lag_ms": None, "errors": [], "ops_done": 0,
                                           "failed": True, "stop_reason": "流し直しの前に止まった"}
    d.update({"status": res["status"], "reasons": res["reasons"], "settings": res["settings"],
              "settle": res["settle"], "warm": res.get("warm"), "warnings": res.get("warnings", []), "age": res["age"], "age_plays": age_plays,
              "object_errors": res.get("object_errors"), "head_seconds": res.get("head_seconds"),
              "warm_cache": res.get("warm_cache")})
    with open(os.path.join(out, "play.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _cleanup(spec, res, scrape, quiet_s, max_s, poll_s, tsr, log):
    """手順 8: 複製を消し、後始末が落ち着くのを待って mount を外す。失敗は res["cleanup"]["errors"] に残す。"""
    errs = res["cleanup"].setdefault("errors", [])
    if res["clone"] and os.path.ismount(spec.mountpoint):
        try:
            os.unlink(res["clone"])
            log("複製を消した。後始末を待つ")
            res["cleanup"]["delete"] = wait_deleted(spec, res["clone_inode"], scrape, quiet_s, max_s, poll_s)
        except (OSError, RuntimeError, ConnectionError) as e:
            errs.append("複製を消せない: %s" % e)
    elif res["clone"]:
        errs.append("mount が無いので複製を消せない（手で確かめる）: %s" % res["clone"])
    try:
        testvol.umount(spec, run=tsr)
    except (RuntimeError, OSError) as e:
        errs.append("mount を外せない: %s" % e)
    if errs:
        res["reasons"].extend(errs)


def _write_run_json(out, res):
    """計測の suite の run.json に、流し直しの情報（replay）を足す。run.json が無ければ作る。"""
    run = mcli.read_run(out) or {"profile": "test"}
    run["replay"] = {k: res.get(k) for k in ("status", "reasons", "settings", "age", "clone", "clone_inode", "seed",
                                             "mount_args", "warm", "warm_cache", "settle", "age_settle", "cleanup", "warnings",
                                             "volume_uuid", "object_errors", "head_seconds", "ops", "ops_total", "traceback")}
    mcli.write_run(out, run)
