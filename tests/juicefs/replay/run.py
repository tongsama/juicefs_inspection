"""jfsreplay run: 1 つの compaction の設定で、記録を試験の volume に 1 回流す（設計書 §5.4 の 1〜8）。

手順:
  0. 安全の確かめ（本番と重ならないこと）。試験の mount が上がっていれば外す。
     --age N なら、既定の設定で mount → 種を clone → 記録を N 回流す → 落ち着く → mount を外す（差分を育てる）。
  1. 試験の mount を外した状態でキャッシュ（試験のキャッシュだけ）を空にし、その回の設定で mount する。
  2. 種のファイルを `juicefs clone` で per-run の path に複製する（--age のときは育てた複製を使う）。
  3. キャッシュを温める: 記録の中の read の範囲を 1 回読み、page cache は捨てる。
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
from replay import player, testvol, trace

SUITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(SUITE_DIR, "settings.toml")
JFSMEASURE = os.path.join(SUITE_DIR, "jfsmeasure")
DEFAULT_SETTINGS = {"name": "default", "mode": None, "flags": {}}  # mount_flags.txt のまま
STAGING = "juicefs_staging_blocks"
UPLOADING = "juicefs_object_request_uploading"
COMPACT = "juicefs_compact_size_histogram_bytes_count"
OBJ_ERRORS = "juicefs_object_request_errors"
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


class Guard:
    """流し直しと待ちの間の見張り。object_request_errors の増加、試験の mount のログの 429／403 の rate limit、
    mount の停止（mountpoint が mount でない）、metrics の 3 回続けての取得の失敗を、check() で理由の文字列にして返す。"""

    def __init__(self, scrape, log_path, mounted):
        self.scrape, self.log_path, self.mounted = scrape, log_path, mounted
        self.base = None
        self.fail = 0
        self.pos = 0
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

    def _log_hits(self):
        """ログの増えた分から rate limit の行を探し、最初の 1 行（200 文字まで）を返す。"""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return None
        cut = data.rfind(b"\n") + 1  # 書きかけの行は次回に回す
        self.pos += cut
        for line in data[:cut].decode("utf-8", "replace").splitlines():
            if RATE_LIMIT_RE.search(line):
                return line.strip()[:200]
        return None

    def check(self):
        """止める理由があれば文字列、無ければ None。"""
        if not self.mounted():
            return "試験の mount が落ちた（mountpoint が mount ではない）"
        hit = self._log_hits()
        if hit:
            return "試験の mount のログに rate limit（429／403）: %s" % hit
        try:
            m = self.scrape()
            self.fail = 0
        except (OSError, ValueError):
            self.fail += 1
            return "試験の metrics を 3 回続けて取得できない" if self.fail >= 3 else None
        v = m.get(OBJ_ERRORS)
        if self.base is None:
            self.base = v
        elif v is not None and v > self.base:
            return "object_request_errors が増えた（%g → %g）" % (self.base, v)
        return None


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


def _toml(v):
    """profiles.toml に書く値（文字列・数・真偽）。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)


def write_measure_profiles(out, spec, inode, common, intervals=None):
    """計測の suite に渡す <out>/profiles.toml（test だけ）を書く。inode は複製の inode、Redis は試験の Redis の直接の
    port と db、間隔は intervals（metrics／llen／keys の秒）で上書きできる。接続の鍵は入れない。path を返す。"""
    c = dict(common)
    for k, key in (("metrics", "interval_metrics_s"), ("llen", "interval_llen_s"), ("keys", "interval_cache_churn_s")):
        if intervals and k in intervals:
            c[key] = intervals[k]
    test = {"metrics_url": "http://%s:%d/metrics" % spec.metrics_addr, "inode": inode, "cache_dir": spec.cache_dir,
            "mountpoint": spec.mountpoint, "redis_source": "test", "redis_addr": "%s:%d" % spec.redis_addr,
            "redis_db": spec.db, "log_glob": os.path.join(spec.state_dir, "*.log"), "bucket": spec.bucket_name}
    path = os.path.join(out, "profiles.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# jfsreplay run が書いた計測用のプロファイル（複製の inode と試験の資源）\n[test]\n")
        f.writelines("%s = %s\n" % (k, _toml(v)) for k, v in test.items())
        f.write("\n[common]\n")
        f.writelines("%s = %s\n" % (k, _toml(v)) for k, v in c.items())
    return path


def _seed_path(trace_path, spec):
    """trace の隣の seed.json から、試験の mountpoint の上の種の path を返す。無ければ RunError。"""
    p = os.path.join(os.path.dirname(os.path.abspath(trace_path)), "seed.json")
    try:
        with open(p, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        raise RunError("seed.json が無い（先に jfsreplay seed を実行する）: %s" % p) from None
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


def run(trace_path, settings_name, out, profile, spec=None, age=0, up_kw=None, settle_s=None, settle_max_s=None,
        delete_quiet_s=30, poll_s=5.0, intervals=None, settings_path=SETTINGS_PATH, log=print):
    """1 回の試験を行い、結果の dict（status は ok／failed、reasons、clone、clone_inode、settle、out）を返す。
    結果の files は <out>/ の run.json、metrics.tsv など（計測の suite）、play.json、config.json。"""
    common = config.load_common()
    spec = spec or testvol.spec_from_profile(profile, common)
    up_kw = dict(up_kw or {})
    prod = up_kw.get("prod") or config.load_profile("prod")
    up_kw["prod"] = prod
    testvol.safety_check(profile, spec=spec, prod=prod, prod_redis=up_kw.get("prod_redis"))
    if testvol._overlaps(out, prod.cache_dir) or testvol._overlaps(out, prod.mountpoint):
        raise testvol.SafetyError("出力の場所が本番と重なるので止めます: %s" % out)
    settings = load_settings(settings_name, settings_path)
    default = load_settings("default", settings_path)
    settle_s = common["settle_seconds"] if settle_s is None else settle_s
    settle_max_s = common["settle_max_seconds"] if settle_max_s is None else settle_max_s
    ops = trace.load(trace_path)
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

    res = {"status": "ok", "reasons": [], "settings": settings, "age": age, "out": os.path.abspath(out),
           "clone": None, "clone_inode": None, "settle": None, "cleanup": {}}
    plays, age_plays, measuring = [], [], False
    seed_info = None
    try:
        if os.path.ismount(spec.mountpoint):
            res["volume_uuid"] = _volume_uuid(spec)
            log("試験の mount を外す（設定を入れ替えるため）")
            testvol.umount(spec, run=tsr)
        seed_mp_path = None
        if age:
            log("--age %d: 既定の設定で差分を育てる" % age)
            testvol.up(profile, default, spec=spec, **up_kw)
            res["volume_uuid"] = _volume_uuid(spec) or res.get("volume_uuid")
            seed_mp_path, seed_info = _seed_path(trace_path, spec)
            res["clone"], res["clone_inode"] = _make_clone(spec, seed_mp_path, settings_name, log)
            for i in range(age):
                g = Guard(scrape, spec.path("juicefs.log"), mounted)
                pr = _play_guarded(res["clone"], ops, g, poll_s, res["reasons"])
                age_plays.append(pr.to_dict())
                if pr.failed or pr.stopped:
                    res["reasons"].append("育てる %d 回目: %s" % (i + 1, pr.stop_reason))
                    raise RunError("差分を育てる途中で止まった")
            s = settle(scrape, Guard(scrape, spec.path("juicefs.log"), mounted), settle_s, settle_max_s, poll_s)
            res["age_settle"] = s
            if s["state"] == "止めた":
                res["reasons"].append(s["reason"])
                raise RunError("差分を育てた後の待ちで止まった")
            testvol.umount(spec, run=tsr)
        n = clear_cache(spec.cache_dir, spec.mountpoint, prod, spec.state_dir, res.get("volume_uuid"))
        log("試験のキャッシュを空にした（%d ファイル）。設定 %s で mount する" % (n, settings["name"]))
        info = testvol.up(profile, settings, spec=spec, **up_kw)
        res["mount_args"] = info["mount_args"]
        if not res["clone"]:
            seed_mp_path, seed_info = _seed_path(trace_path, spec)
            res["clone"], res["clone_inode"] = _make_clone(spec, seed_mp_path, settings_name, log)
        res["seed"] = {"path": seed_mp_path, "inode": seed_info.get("inode")}
        res["warm"] = warm(res["clone"], ops)
        log("キャッシュを温めた: %(ranges)d 範囲、%(bytes)d バイト、%(seconds).1f 秒" % res["warm"])
        mprof = write_measure_profiles(out, spec, res["clone_inode"], common, intervals)
        _mcli("--profiles", mprof, "start", "test", out)
        measuring = True
        log("記録を流す: %d 操作、%.0f 秒" % (len(ops), (ops[-1].start_us - ops[0].start_us) / 1e6))
        g = Guard(scrape, spec.path("juicefs.log"), mounted)
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
        if mounted():
            # 終わりの材料（キャッシュの内訳など）。空にした直後は kaz-cache-state が無いこともあるので、取れない物は警告にする
            res["warnings"] = mcap.capture(config.load_profile("test", mprof), out, "end")
    except Exception as e:  # 想定外の例外も含め、その回を失敗として結果に残す（後始末は finally で必ず行う）
        _fail(res, "%s: %s" % (type(e).__name__, e))
        if not isinstance(e, (RunError, RuntimeError, OSError, ValueError)):
            res["traceback"] = traceback.format_exc()[-3000:]
    except BaseException as e:  # 中断（KeyboardInterrupt など）: 失敗として残し、後始末の後に投げ直す
        _fail(res, "中断された: %s" % type(e).__name__)
        raise
    finally:
        if measuring:
            _step(res, "計測の停止", lambda: _mcli("stop", out))
        _step(res, "後始末（複製の削除・umount）",
              lambda: _cleanup(spec, res, scrape, delete_quiet_s, settle_max_s, poll_s, tsr, log))
        if res["cleanup"].get("errors"):
            res["status"] = "failed"
        _step(res, "play.json の書き出し", lambda: _write_play(out, plays, age_plays, res))
        _step(res, "run.json の書き出し", lambda: _write_run_json(out, res))
    return res


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
              "settle": res["settle"], "warm": res.get("warm"), "warnings": res.get("warnings", []), "age": res["age"], "age_plays": age_plays})
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
                                             "mount_args", "warm", "settle", "age_settle", "cleanup", "warnings", "volume_uuid",
                                             "traceback")}
    mcli.write_run(out, run)
