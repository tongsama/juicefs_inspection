"""試験の volume（一時の Redis、RTT の中継、juicefs format／mount）を立てる・下ろす・消す。

本番と同じ mount のオプション（mount_flags.txt）で別の mount を立て、compaction の設定だけを差し替える。
どの操作も、最初に safety_check で、Redis・バケット・mountpoint・キャッシュ・metrics の port が
本番と重ならないことを確かめ、重なれば何もせずに SafetyError で止まる。

接続の鍵（rclone）は実行時に読み、juicefs format の環境変数（ACCESS_KEY／SECRET_KEY）にだけ渡す。
表示・ログ・例外の文言には出さない（juicefs の出力を例外に含めるときも伏せる）。"""
import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

from lib import config, secrets
from lib.resp import Resp

SUITE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAGS_PATH = os.path.join(SUITE_DIR, "mount_flags.txt")
RTT_PROXY_PATH = os.path.join(SUITE_DIR, "replay", "rtt_proxy.py")
# 本番の接続情報を持つスクリプト。内容は実行時に読むだけで表示しない
MOUNT_SCRIPT = os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh")
RCLONE_SCRIPT = os.path.expanduser("~/utilities/cloudfs/rclone_s3_start.sh")
STATE_DIR = os.path.expanduser("~/.juicefs/test-suite")
PROD_BUCKET = "buckets"
# 本番の format の値（2026-10-21 の .config の写し: BlockSize 4096 KiB、zstd）。本番の .config を読めないときに使う
PROD_FORMAT_FALLBACK = (4096, "zstd")
VOLUME_NAME = "juicefs-kaz-test"
META_DB = 1
COMPACTION_PREFIX = "kaz-compaction-"


class SafetyError(Exception):
    """試験の設定が本番と重なるときの例外。これが出たら何も変えずに止まる。"""


class CreateBucketRequired(SafetyError):
    """s3（本物の Drive）の上に volume を新しく作るのに、--create-bucket が無いときの例外（Ruling 9）。
    何も起動・作成する前に投げる。"""


CREATE_BUCKET_MSG = ("試験の volume がまだ無いので、up は本物の Google Drive（rclone serve s3）の上に"
                     "バケット %s と volume を新しく作ることになります。作ってよいときだけ "
                     "`jfsreplay testvol up --create-bucket` で実行してください（local の file storage では不要）")


@dataclasses.dataclass(frozen=True)
class Spec:
    """試験の volume の資源の置き場所と port。既定は profiles.toml の test と common から作る（spec_from_profile）。
    storage は "s3"（rclone serve s3）か "file"（結合の試験用）。bucket は空なら s3 の URL を実行時に組み立てる。"""
    binary: str
    compose_file: str
    compose_project: str
    redis_addr: tuple
    proxy_addr: tuple
    one_way_ms: float
    metrics_addr: tuple
    mountpoint: str
    cache_dir: str
    state_dir: str
    storage: str = "s3"
    bucket: str = ""
    bucket_name: str = VOLUME_NAME
    name: str = VOLUME_NAME
    db: int = META_DB

    def meta_url(self, direct=False):
        """juicefs に渡す metadata の URL。通常は中継（RTT あり）、direct=True なら Redis へ直接（管理の命令用）。"""
        host, port = self.redis_addr if direct else self.proxy_addr
        return "redis://%s:%d/%d" % (host, port, self.db)

    def path(self, name):
        """state_dir の下の path（pid、log、Redis のデータ）。"""
        return os.path.join(self.state_dir, name)


def _addr(s):
    """"host:port" を (host, port) にする。"""
    host, sep, port = str(s).rpartition(":")
    if not sep or not host:
        raise ValueError("host:port の形ではない: %s" % s)
    return host, int(port)


def _url_addr(url):
    """http://host:port/... の (host, port)。"""
    u = urlparse(url)
    return u.hostname, u.port or 80


def spec_from_profile(profile, common=None):
    """試験のプロファイルと common の値から Spec を作る。"""
    c = common or config.load_common()
    return Spec(
        binary=c["test_binary"], compose_file=c["redis_compose_file"], compose_project=c["redis_compose_project"],
        redis_addr=_addr(profile.redis_addr), proxy_addr=_addr(c["rtt_proxy_addr"]),
        one_way_ms=float(c["rtt_one_way_ms"]), metrics_addr=_url_addr(profile.metrics_url),
        mountpoint=profile.mountpoint, cache_dir=profile.cache_dir, state_dir=STATE_DIR,
        bucket_name=profile.bucket or VOLUME_NAME)


# ---- 安全の確かめ ----

_LOOPBACK = {"localhost", "::1", "ip6-localhost"}


def _norm_host(h):
    """host を比べやすい形にする（大文字小文字、loopback の別名をまとめる）。"""
    h = (h or "").strip().strip("[]").lower()
    return "loopback" if h in _LOOPBACK or h.startswith("127.") else h


def _same_redis(addr, prod):
    """addr が本番の Redis と重なるか。port が同じ、または loopback 以外で host が同じなら重なるとみなす。"""
    h, p = _norm_host(addr[0]), int(addr[1])
    ph, pp = _norm_host(prod[0]), int(prod[1])
    return p == pp or (h == ph and h != "loopback")


def _overlaps(a, b):
    """2 つの path が同じか、片方がもう片方の下にあるか。FUSE の root に stat を送らないよう文字列だけで比べる。"""
    a = os.path.abspath(os.path.expanduser(a.strip()))
    b = os.path.abspath(os.path.expanduser(b.strip()))
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def _prod_redis_addr(mount_script):
    """本番の Redis の (host, port) を mount スクリプトから読む。値は表示しない。読めなければ SafetyError。"""
    try:
        u = urlparse(secrets.redis_url_from_mount_script(mount_script).value)
        return u.hostname, u.port or 6379
    except (OSError, ValueError) as e:
        raise SafetyError("本番の Redis の接続先を確かめられないので止めます（%s）" % type(e).__name__) from None


def safety_check(profile, spec=None, prod=None, prod_redis=None, mount_script=MOUNT_SCRIPT):
    """試験のプロファイルと Spec が本番と重ならないことを確かめる。重なれば理由を並べて SafetyError。
    確かめる項目: Redis（プロファイル、Spec の Redis と中継）の host／port、バケットの名前、
    mountpoint、キャッシュ、state の置き場所、metrics の port。prod_redis は試験で偽の値を渡すためのもの。"""
    prod = prod or config.load_profile("prod")
    spec = spec or spec_from_profile(profile)
    if prod_redis is None:
        prod_redis = _prod_redis_addr(mount_script)
    bad = []
    if profile.redis_source != "test":
        bad.append("プロファイルの redis_source が test ではない（%s）" % profile.redis_source)
    try:
        addrs = [("プロファイルの Redis", _addr(profile.redis_addr)),
                 ("Spec の Redis", spec.redis_addr), ("RTT の中継", spec.proxy_addr)]
    except ValueError:
        addrs = []
        bad.append("プロファイルの redis_addr が host:port の形ではない")
    for label, a in addrs:
        if _same_redis(a, prod_redis):
            bad.append("%s が本番の Redis と同じ host か port" % label)
    for label, b in (("プロファイルのバケット", profile.bucket), ("Spec のバケット", spec.bucket_name)):
        if b.strip().lower() in (PROD_BUCKET, prod.bucket.strip().lower()):
            bad.append("%s が本番のバケット（%s）と同じ" % (label, PROD_BUCKET))
    if spec.storage == "s3" and spec.bucket and urlparse(spec.bucket).path.strip("/").lower() == PROD_BUCKET:
        bad.append("Spec のバケットの URL が本番のバケットを指す")
    for label, p in (("プロファイルの mountpoint", profile.mountpoint), ("Spec の mountpoint", spec.mountpoint),
                     ("プロファイルのキャッシュ", profile.cache_dir), ("Spec のキャッシュ", spec.cache_dir),
                     ("Spec の state", spec.state_dir)):
        for plabel, pp in (("本番の mountpoint", prod.mountpoint), ("本番のキャッシュ", prod.cache_dir)):
            if _overlaps(p, pp):
                bad.append("%s が%sと重なる" % (label, plabel))
    pm = _url_addr(prod.metrics_url)[1]
    for label, port in (("プロファイルの metrics", _url_addr(profile.metrics_url)[1]),
                        ("Spec の metrics", spec.metrics_addr[1])):
        if port == pm:
            bad.append("%s の port が本番（%d）と同じ" % (label, pm))
    if bad:
        raise SafetyError("試験の設定が本番と重なるので止めます: " + "; ".join(bad))


# ---- mount のオプション ----

@dataclasses.dataclass(frozen=True)
class MountFlag:
    """mount_flags.txt の 1 行。name は先頭の - を除いた名前、value は値（無ければ None）、
    eq は --name=value の形か、mark は "compaction"／"test"／""。"""
    name: str
    value: object
    eq: bool
    mark: str
    dash: str = "--"

    def tokens(self, value=None):
        """引数の列にする。value を渡すとその値で置き換える（True は値なし、False は行を消す）。"""
        v = self.value if value is None else value
        if v is False:
            return []
        flag = self.dash + self.name
        if v is True or v is None:
            return [flag]
        v = _fmt(v)
        return [flag + "=" + v] if self.eq else [flag, v]


def _fmt(v):
    """settings の値を引数の文字列にする。"""
    if isinstance(v, float):
        return repr(v)
    return str(v)


def read_mount_flags(path=FLAGS_PATH):
    """mount_flags.txt を MountFlag の list にする。# 以降は注釈で、[compaction]／[test] の印を読む。
    行の続き（\\）や、1 行に複数のオプションは ValueError（今回の mount スクリプトの件の再発防止）。"""
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            body, _, comment = line.partition("#")
            mark = "compaction" if "[compaction]" in comment else "test" if "[test]" in comment else ""
            if "\\" in body:
                raise ValueError("%s:%d: 行の続き（\\）は使えない" % (path, n))
            toks = shlex.split(body)
            if not toks:
                continue
            first = toks[0]
            if not first.startswith("-") or len(toks) > 2 or (len(toks) == 2 and toks[1].startswith("-")):
                raise ValueError("%s:%d: 1 行に 1 つのオプション（--名前 [値]）にする" % (path, n))
            dash = "--" if first.startswith("--") else "-"
            name, eq, val = first[len(dash):].partition("=")
            value = val if eq else (toks[1] if len(toks) == 2 else None)
            if eq and len(toks) == 2:
                raise ValueError("%s:%d: --名前=値 の後に余分な値がある" % (path, n))
            out.append(MountFlag(name, value, bool(eq), mark, dash))
    return out


def _settings_flags(settings):
    """settings（name、mode、flags）から、置き換える compaction の値の dict を作る。"""
    flags = dict((settings or {}).get("flags") or {})
    mode = (settings or {}).get("mode")
    if mode:
        flags.setdefault("kaz-compaction-mode", mode)
    return flags


def build_mount_args(path, settings, test_values=None):
    """mount_flags.txt と settings から juicefs mount のオプションの列を作る（--background、--log、
    --cache-dir、META、mountpoint は含めない）。settings で変えられるのは [compaction] の行と、
    本番の mount に無い kaz-compaction-* だけ（末尾に足す）。test_values は [test] の行だけを置き換える。"""
    entries = read_mount_flags(path)
    comp = _settings_flags(settings)
    test_values = dict(test_values or {})
    by_name = {e.name: e for e in entries}
    for k in comp:
        e = by_name.get(k)
        if e is not None and e.mark != "compaction":
            raise ValueError("compaction 以外の設定は settings で変えられない: %s" % k)
        if e is None and not k.startswith(COMPACTION_PREFIX):
            raise ValueError("compaction の設定ではない: %s" % k)
    for k in test_values:
        e = by_name.get(k)
        if e is None or e.mark != "test":
            raise ValueError("[test] の印の無い行は試験の値で変えられない: %s" % k)
    args = []
    for e in entries:
        args += e.tokens(comp.get(e.name) if e.mark == "compaction" else test_values.get(e.name))
    for k, v in comp.items():
        if k not in by_name:
            args += MountFlag(k, None, False, "compaction").tokens(v)
    return args


# ---- 子の process ----

def _scrub(text, hidden):
    """text の中の隠す値（Secret の実値）を <redacted> に替える。"""
    for s in hidden:
        v = getattr(s, "value", s)
        if v:
            text = text.replace(v, "<redacted>")
    return text


def _run(cmd, env=None, timeout=300, hidden=()):
    """コマンドを実行し、失敗なら stderr の末尾（隠す値は伏せる）を付けて RuntimeError。stdout を返す。
    出力は pipe ではなく一時ファイルで受ける（mount --background の daemon が pipe を握ったまま残っても待たないため）。"""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            p = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("%s %s が %d 秒で終わらない" % (os.path.basename(cmd[0]), cmd[1], timeout)) from None
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read().decode(errors="replace"), err.read().decode(errors="replace")
    if p.returncode != 0:
        tail = _scrub(stderr[-2000:], hidden)
        raise RuntimeError("%s %s が失敗（終了 %d）:\n%s" % (os.path.basename(cmd[0]), cmd[1], p.returncode, tail))
    return stdout


def _pid_alive(pidfile, expect):
    """pidfile の process が生きていて、cmdline に expect を含めば pid を返す。違えば None。"""
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, ValueError):
        return None
    return pid if expect in cmd else None


def _stop_pid(pidfile, expect, timeout=10):
    """pidfile の process を SIGTERM で止め、終わるまで待つ（待ちきれなければ SIGKILL）。pidfile は消す。
    この process が起動した子なら wait で回収する（既に終わっていても回収する）。"""
    try:
        with open(pidfile) as f:
            fpid = int(f.read().strip())
    except (OSError, ValueError):
        fpid = None
    pid = _pid_alive(pidfile, expect)
    if pid:
        os.kill(pid, signal.SIGTERM)
        end = time.time() + timeout
        while time.time() < end and _pid_alive(pidfile, expect):
            time.sleep(0.05)
        if _pid_alive(pidfile, expect):
            os.kill(pid, signal.SIGKILL)
    child = _CHILDREN.pop(fpid, None)
    if child is not None:
        child.wait(timeout)
    try:
        os.unlink(pidfile)
    except OSError:
        pass


# この process が起動した子（pid → Popen）。止めるときに wait して、zombie と ResourceWarning を残さない
_CHILDREN = {}


def _private_dir(path):
    """path を mode 0700 で作る。既にあれば 0700 に直す。"""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.stat(path).st_mode & 0o777 != 0o700:
        os.chmod(path, 0o700)


def prepare_state_dir(spec):
    """state_dir と Redis のデータの置き場所を 0700 にする（Ruling 8: juicefs format が rclone の鍵を
    volume の Format に保存し、それが試験の Redis の AOF に入るため、他の user から読めないようにする）。"""
    _private_dir(spec.state_dir)
    _private_dir(spec.path("redis"))


def _spawn(cmd, pidfile, log):
    """cmd を session を分けて起動し、pid を pidfile に書いて Popen を返す（出力は log へ）。"""
    _private_dir(os.path.dirname(pidfile))
    with open(log, "ab") as lf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True, cwd=SUITE_DIR)
    with open(pidfile, "w") as f:
        f.write(str(p.pid))
    _CHILDREN[p.pid] = p
    return p


def _tail(path, n=1500):
    """log の末尾（起動の失敗の報告用）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def _redis_dbsize(addr, db, timeout=1):
    """試験の Redis の db の DBSIZE。接続できなければ None。"""
    try:
        with Resp(addr[0], addr[1], db=db, readonly=True, timeout=timeout) as r:
            return r.call("DBSIZE")
    except (OSError, RuntimeError, ConnectionError):
        return None


def _wait(cond, timeout, proc=None):
    """cond() が真になるまで待つ。proc が先に終わったら False。"""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.05)
    return cond()


# compose で起動した Redis が応答するまでの待ち時間（秒）
REDIS_START_TIMEOUT = 60


def _compose_cmd(spec, *args):
    """試験の Redis の docker compose の命令の列（固定の project 名と compose file を毎回明示する）。"""
    return ["docker", "compose", "-p", spec.compose_project, "-f", spec.compose_file] + list(args)


def _compose_env(spec):
    """compose file が読む環境変数（公開 port、AOF の host dir、container の user）。親の環境は引き継ぐ。"""
    return dict(os.environ, JFS_TEST_REDIS_PORT=str(spec.redis_addr[1]), JFS_TEST_REDIS_DIR=spec.path("redis"),
                UID=str(os.getuid()), GID=str(os.getgid()))


def ensure_redis(spec, run=_run):
    """試験の Redis が動いていなければ docker compose で起動する（AOF は state_dir/redis に保存）。
    公開 port 越しに応答するまで待つ。起動したら True。state_dir は compose の前に 0700 で作る。"""
    if _redis_dbsize(spec.redis_addr, spec.db) is not None:
        return False
    prepare_state_dir(spec)
    run(_compose_cmd(spec, "up", "-d", "redis"), env=_compose_env(spec), timeout=180)
    if not _wait(lambda: _redis_dbsize(spec.redis_addr, spec.db) is not None, REDIS_START_TIMEOUT):
        raise RuntimeError("試験の Redis（compose project %s）が起動しない" % spec.compose_project)
    return True


def ensure_proxy(spec):
    """RTT の中継が動いていなければ起動する。起動したら True。"""
    pidfile = spec.path("rtt_proxy.pid")
    if _pid_alive(pidfile, "rtt_proxy"):
        return False
    log = spec.path("rtt_proxy.log")
    p = _spawn([sys.executable, "-I", RTT_PROXY_PATH, "--listen", "%s:%d" % spec.proxy_addr,
                "--target", "%s:%d" % spec.redis_addr, "--one-way-ms", str(spec.one_way_ms)], pidfile, log)
    if not _wait(lambda: _redis_dbsize(spec.proxy_addr, spec.db) is not None, 15, p):
        p.kill()
        raise RuntimeError("RTT の中継が起動しない:\n%s" % _tail(log))
    return True


def prod_format_values(prod=None):
    """本番の format の (block size KiB, 圧縮) を、本番の mount の .config から読む（読み取りのみ。他の項目は使わない）。
    本番の Redis には問い合わせない（juicefs status は許可外の命令を送るため）。読めなければ記録の値を使う。"""
    prod = prod or config.load_profile("prod")
    try:
        with open(os.path.join(prod.mountpoint, ".config"), encoding="utf-8") as f:
            fmt = json.load(f)["Format"]
        return int(fmt["BlockSize"]), str(fmt["Compression"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("警告: 本番の .config を読めないので、記録の値 %s を使います（%s）" % (PROD_FORMAT_FALLBACK, type(e).__name__),
              file=sys.stderr)
        return PROD_FORMAT_FALLBACK


def is_formatted(spec):
    """試験の Redis の db に volume があるか（DBSIZE > 0）。"""
    return bool(_redis_dbsize(spec.redis_addr, spec.db))


def _likely_formatted(spec):
    """副作用なしで volume があるかを推す: Redis が動いていれば DBSIZE > 0、止まっていれば AOF に中身があるか。"""
    n = _redis_dbsize(spec.redis_addr, spec.db)
    if n is not None:
        return n > 0
    d = os.path.join(spec.path("redis"), "appendonlydir")
    try:
        return any(os.path.getsize(os.path.join(d, f)) > 0 for f in os.listdir(d))
    except OSError:
        return False


def ensure_format(spec, prod_format=None, run=_run, create_bucket=False):
    """volume が無ければ juicefs format する（block size と圧縮は本番の値、trash-days は 0）。
    s3 は create_bucket=True のときだけ（無ければ鍵を読む前に CreateBucketRequired）。s3 なら rclone の鍵を
    環境変数で渡し、バケットが無ければ juicefs format が作る。format したら True。"""
    if is_formatted(spec):
        return False
    if spec.storage == "s3" and not create_bucket:
        raise CreateBucketRequired(CREATE_BUCKET_MSG % spec.bucket_name)
    bs, comp = prod_format or prod_format_values()
    env, hidden, bucket = None, (), spec.bucket
    if spec.storage == "s3":
        ak, sk, port = secrets.rclone_keys(RCLONE_SCRIPT)
        bucket = bucket or "http://127.0.0.1:%d/%s" % (port, spec.bucket_name)
        env = dict(os.environ, ACCESS_KEY=ak.value, SECRET_KEY=sk.value)
        hidden = (ak, sk)
    run([spec.binary, "format", "--storage", spec.storage, "--bucket", bucket, "--block-size", str(bs),
         "--compress", comp, "--trash-days", "0", "--no-update", spec.meta_url(direct=True), spec.name],
        env=env, hidden=hidden)
    return True


def mount_args(spec, settings, flags_path=FLAGS_PATH):
    """試験の mount のオプションの列（mount_flags.txt、settings の compaction の値、試験の metrics の port）。"""
    return build_mount_args(flags_path, settings, test_values={"metrics": "%s:%d" % spec.metrics_addr})


def mount(spec, settings, run=_run, flags_path=FLAGS_PATH, args=None):
    """mount_flags.txt と settings の compaction の値で、試験の mount を --background で立てる。引数の列を返す。"""
    args = args if args is not None else mount_args(spec, settings, flags_path)
    if os.path.ismount(spec.mountpoint):
        raise RuntimeError("試験の mountpoint は既に mount されている（down してから up する）: %s" % spec.mountpoint)
    prepare_state_dir(spec)
    for d in (spec.mountpoint, spec.cache_dir):
        os.makedirs(d, exist_ok=True)
    cmd = ([spec.binary, "mount", "--background", "--log", spec.path("juicefs.log"), "--cache-dir", spec.cache_dir]
           + args + [spec.meta_url(), spec.mountpoint])
    run(cmd, timeout=180)
    if not _wait(lambda: os.path.ismount(spec.mountpoint), 30):
        raise RuntimeError("juicefs mount の後に mountpoint が mount されていない:\n%s" % _tail(spec.path("juicefs.log")))
    return args


# ---- 公開の操作 ----

def up(profile, settings, spec=None, prod=None, prod_redis=None, prod_format=None, run=_run, flags_path=FLAGS_PATH,
       create_bucket=False):
    """試験の volume を立てる: 安全の確かめ → settings の確かめ → Redis → 中継 → (無ければ) format → mount。
    安全・settings・--create-bucket（s3 で volume が無いとき）の確かめは、何かを起動・作成する前に行う。
    Redis が止まっていて AOF に中身があるのに volume が無かった場合だけは、Redis の起動の後に止まる。
    戻り値は表示・記録用の dict（鍵は含まない）。"""
    spec = spec or spec_from_profile(profile)
    safety_check(profile, spec=spec, prod=prod, prod_redis=prod_redis)
    args = mount_args(spec, settings, flags_path)  # 不正な settings はここで ValueError（まだ何も起動していない）
    if not os.access(spec.binary, os.X_OK):
        raise RuntimeError("試験のバイナリが実行できない: %s" % spec.binary)
    if not os.path.exists(spec.compose_file):
        raise RuntimeError("試験の Redis の compose file が無い: %s" % spec.compose_file)
    if spec.storage == "s3" and not create_bucket and not _likely_formatted(spec):
        raise CreateBucketRequired(CREATE_BUCKET_MSG % spec.bucket_name)  # Ruling 9: 副作用の前に止まる
    started_redis = ensure_redis(spec, run=run)
    started_proxy = ensure_proxy(spec)
    formatted = ensure_format(spec, prod_format=prod_format, run=run, create_bucket=create_bucket)
    mount(spec, settings, run=run, args=args)
    return {"meta": spec.meta_url(), "mountpoint": spec.mountpoint, "metrics": "%s:%d" % spec.metrics_addr,
            "started_redis": started_redis, "started_proxy": started_proxy, "formatted": formatted,
            "settings": (settings or {}).get("name", ""), "mount_args": args}


def umount(spec, run=_run):
    """試験の mount を外す（staging の書き出しを待つ）。mount されていなければ何もしない。
    外した後も mount の process は残ることがあり（後始末で長く残る、または待っても終わらない。ユーザーも手動の umount の後に
    いつも kill している）、metrics の port を持ち続けて次の mount が metrics を出せなくなる（2026-10-21 の実走）。
    そこで外す前に、試験の mount 自身の `.config` から Pid／PPid を読み、外した後も残ればその pid だけを止める。
    juicefs の mount の process は cmdline を 1 つの文字列に書き換え 255 文字で切るので、cmdline では mountpoint が分からない。"""
    if not os.path.ismount(spec.mountpoint):
        return False
    pids = mount_pids_from_config(spec)
    run([spec.binary, "umount", "--flush", spec.mountpoint], timeout=600)
    if not _wait(lambda: not os.path.ismount(spec.mountpoint), 30):
        raise RuntimeError("試験の mount が外れない: %s" % spec.mountpoint)
    if not _wait(lambda: not _alive_test_pids(spec, pids), UMOUNT_EXIT_WAIT_S):
        _kill_test_mount(spec, pids)
    if not _wait(lambda: port_free(spec.metrics_addr), 30):
        raise RuntimeError("試験の metrics の port %s:%d が空かない" % spec.metrics_addr)
    return True


UMOUNT_EXIT_WAIT_S = 120  # umount の後、試験の mount の process が自分で終わるのを待つ上限
KILL_WAIT_S = 30  # SIGTERM の後、SIGKILL を送るまで待つ秒数
PROD_MOUNTPOINTS = (os.path.expanduser("~/mnt_juicefs"),)  # この mount の Pid／PPid には、決して signal を送らない


def _config_pids(mountpoint):
    """mountpoint の `.config`（動いている mount の設定の JSON）の Pid と PPid。読めなければ空の集合。"""
    try:
        with open(os.path.join(mountpoint, ".config"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return set()
    return {int(d[k]) for k in ("Pid", "PPid") if isinstance(d.get(k), int) and d.get(k) > 1}


def mount_pids_from_config(spec):
    """試験の mount の process の pid（試験の mount の `.config` の Pid／PPid）。本番の mount の Pid／PPid は除く。"""
    prod = set()
    for mp in PROD_MOUNTPOINTS:
        if os.path.realpath(mp) != os.path.realpath(spec.mountpoint):
            prod |= _config_pids(mp)
    return sorted(_config_pids(spec.mountpoint) - prod)


def _alive_test_pids(spec, pids):
    """pids のうち、まだ生きていて、実行ファイルが試験のバイナリのもの（pid の使い回しで別の process を止めないため）。"""
    want = os.path.realpath(spec.binary)
    out = []
    for pid in pids:
        try:
            exe = os.path.realpath(os.readlink("/proc/%d/exe" % pid))
        except OSError:
            continue
        if exe == want:
            out.append(pid)
    return out


def _kill_test_mount(spec, pids):
    """待っても終わらない試験の mount の process を SIGTERM → SIGKILL で止める。送る直前に毎回確かめ直す
    （生きている、実行ファイルが試験のバイナリ、本番の mount の Pid／PPid ではない）。"""
    prod = set()
    for mp in PROD_MOUNTPOINTS:
        prod |= _config_pids(mp)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        targets = [p for p in _alive_test_pids(spec, pids) if p not in prod]
        if not targets:
            return
        for pid in targets:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if _wait(lambda: not _alive_test_pids(spec, pids), KILL_WAIT_S):
            return
    left = _alive_test_pids(spec, pids)
    if left:
        raise RuntimeError("試験の mount の process が SIGKILL の後も残る（pid %s）" % left)


def port_free(addr):
    """addr（host, port）に誰も listen していなければ True（bind を試して確かめる）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # juicefs（Go の net.Listen）は SO_REUSEADDR を付けるので、TIME_WAIT の接続が残っていても listen できる。同じ条件で確かめる
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(addr)
        return True
    except OSError:
        return False
    finally:
        s.close()


def down(spec=None, profile=None, prod=None, prod_redis=None, run=_run):
    """試験の mount を外し、中継を止める。Redis は残す（次の回で使う）。"""
    profile = profile or config.load_profile("test")
    spec = spec or spec_from_profile(profile)
    safety_check(profile, spec=spec, prod=prod, prod_redis=prod_redis)
    umount(spec, run=run)
    _stop_pid(spec.path("rtt_proxy.pid"), "rtt_proxy")


def _stop_redis(spec, run=_run):
    """試験の Redis を止める（自分の compose project だけを docker compose down。data の dir は残る）。"""
    run(_compose_cmd(spec, "down"), env=_compose_env(spec), timeout=120)
    _wait(lambda: _redis_dbsize(spec.redis_addr, spec.db) is None, 10)


def destroy(profile, spec=None, confirm=input, prod=None, prod_redis=None, run=_run):
    """確認の入力（destroy）の後に、試験の volume を消す: umount → juicefs destroy（バケットの中身と
    metadata）→ Redis を止めてデータを消す → 中継を止める。中止なら False。"""
    spec = spec or spec_from_profile(profile)
    safety_check(profile, spec=spec, prod=prod, prod_redis=prod_redis)
    ans = confirm("試験の volume（バケット %s の中身と Redis %s:%d）を消します。続けるなら destroy と入力: "
                  % (spec.bucket_name, spec.redis_addr[0], spec.redis_addr[1]))
    if (ans or "").strip() != "destroy":
        print("中止しました")
        return False
    umount(spec, run=run)
    ensure_redis(spec, run=run)
    if is_formatted(spec):
        st = json.loads(run([spec.binary, "status", spec.meta_url(direct=True)], timeout=120))
        uuid = st["Setting"]["UUID"]
        run([spec.binary, "destroy", "--yes", spec.meta_url(direct=True), uuid], timeout=3600)
    with Resp(spec.redis_addr[0], spec.redis_addr[1], db=spec.db, readonly=False, timeout=10) as r:
        r.call("FLUSHDB")
    _stop_redis(spec, run=run)
    _stop_pid(spec.path("rtt_proxy.pid"), "rtt_proxy")
    d = spec.path("redis")
    if os.path.isdir(d):
        for root, dirs, files in os.walk(d, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for x in dirs:
                os.rmdir(os.path.join(root, x))
        os.rmdir(d)
    return True


def status(spec=None):
    """試験の volume の状態（redis、proxy、mounted、formatted の真偽）を dict で返す。何も変えない。"""
    spec = spec or spec_from_profile(config.load_profile("test"))
    n = _redis_dbsize(spec.redis_addr, spec.db)
    return {"redis": n is not None, "proxy": bool(_pid_alive(spec.path("rtt_proxy.pid"), "rtt_proxy")),
            "mounted": os.path.ismount(spec.mountpoint), "formatted": bool(n)}


def read_mount_config(mountpoint):
    """mount の <mountpoint>/.config を dict で返す（試験の mount の実効値の確認用）。"""
    with open(os.path.join(mountpoint, ".config"), encoding="utf-8") as f:
        return json.load(f)


def cleanup(spec, prod=None):
    """結合の試験の後始末（失敗しても続ける）: mount を外し、中継と Redis を止める。
    本番の mountpoint と重なる Spec には何もしない。"""
    prod = prod or config.load_profile("prod")
    if _overlaps(spec.mountpoint, prod.mountpoint):
        return
    if os.path.ismount(spec.mountpoint):
        try:
            umount(spec)
        except (RuntimeError, OSError):
            subprocess.run(["fusermount3", "-u", "-z", spec.mountpoint], capture_output=True)
    for fn in (lambda: _stop_pid(spec.path("rtt_proxy.pid"), "rtt_proxy"), lambda: _stop_redis(spec)):
        try:
            fn()
        except (OSError, RuntimeError):
            pass
