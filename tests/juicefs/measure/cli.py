"""jfsmeasure の sub-command（start／check／stop／capture／progress／compare／boot）。"""
import argparse
import glob
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

from lib import config
from measure import boot as bootmod
from measure import capture as cap
from measure import report

KINDS = ("metrics", "llen", "keys")
STALE_FACTOR = 3  # 記録の最後の更新がこの間隔の倍数より古ければ警告
# -I では -m が cwd から探せないので、tests/juicefs を足してから measure.samplers を実行する
BOOT = ("import sys,runpy;sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('measure.samplers',run_name='__main__')")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pid_is_sampler(pid, kind=None):
    """pid が生きた sampler（measure.samplers）か。ゾンビや別のプロセスに再利用された pid は False。"""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return False
    return b"measure.samplers" in b" ".join(args) and (kind is None or kind.encode() in args)


def read_run(outdir):
    """run.json を読む。無ければ None。"""
    try:
        with open(os.path.join(outdir, "run.json"), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def write_run(outdir, run):
    """run.json を書く（接続先や鍵は入れない）。"""
    with open(os.path.join(outdir, "run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=1)


def find_binary(profile, common):
    """計測対象のバイナリの path。本番は mount の process の /proc/<pid>/exe、試験は共通設定の test_binary。"""
    if profile.redis_source != "mount_script":
        return common["test_binary"]
    mp = os.path.realpath(profile.mountpoint)
    for d in glob.glob("/proc/[0-9]*"):
        try:
            with open(d + "/cmdline", "rb") as f:
                args = f.read().split(b"\0")
            if b"mount" in args and mp.encode() in [os.path.realpath(a.decode(errors="replace")).encode()
                                                    for a in args if a.startswith(b"/")]:
                return os.readlink(d + "/exe").removesuffix(" (deleted)")
        except OSError:
            continue
    return None


def binary_info(path):
    """バイナリの {path, version, sha256}。取れない項目は None。"""
    info = {"path": path, "version": None, "sha256": None}
    if not path:
        return info
    try:
        with open(path, "rb") as f:
            h = hashlib.sha256()
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        info["sha256"] = h.hexdigest()
        p = subprocess.run([path, "version"], capture_output=True, text=True, timeout=10)
        info["version"] = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else None
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def kaz_log_lines(log_glob, limit=50):
    """log_glob で一番新しいログの末尾から、kaz を含む行を最大 limit 行返す（起動時の設定の記録）。
    "://" を含む行（接続の URL が入りうる）は run.json に残さないよう落とす。"""
    files = sorted(glob.glob(log_glob), key=os.path.getmtime)
    if not files:
        return []
    with open(files[-1], "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - (4 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    return [l for l in lines if "kaz" in l.lower() and "://" not in l][-limit:]


def _under(a, b):
    """a が b と同じか、b の下にあるか（どちらも絶対 path の文字列）。"""
    return a == b or a.startswith(b.rstrip("/") + "/")


def prod_overlap(outdir, prod):
    """出力ディレクトリが本番の mountpoint か本番のキャッシュと同じかその下なら理由の文字列、違えば None。
    先に文字列で比べ（本番の FUSE の root に stat を送らない）、次に realpath で symlink を解いて比べる。
    本番の mountpoint は親だけを realpath で解く（mount の root 自体は stat しない）。"""
    a = os.path.abspath(os.path.expanduser(outdir))
    mp = os.path.abspath(os.path.expanduser(prod.mountpoint))
    cache = os.path.abspath(os.path.expanduser(prod.cache_dir))
    for label, b in (("本番の mountpoint", mp), ("本番のキャッシュ", cache)):
        if _under(a, b):
            return "出力ディレクトリが%sと重なる: %s" % (label, a)
    real = os.path.realpath(a)
    mp_real = os.path.join(os.path.realpath(os.path.dirname(mp)), os.path.basename(mp))
    for label, b in (("本番の mountpoint", mp_real), ("本番のキャッシュ", os.path.realpath(cache))):
        if _under(real, b):
            return "出力ディレクトリが%sと重なる（symlink を解いた先 %s）" % (label, real)
    return None


def load_prod(profiles=None):
    """本番のプロファイル。profiles に prod が無ければ既定の profiles.toml の prod を使う（安全の確かめ用）。"""
    try:
        return config.load_profile("prod", profiles)
    except KeyError:
        return config.load_profile("prod")


def cmd_start(a, profile, common):
    """常駐を起動し、開始の材料（/metrics の全体、キャッシュの内訳、設定の写し）と run.json を作る。"""
    outdir = a.outdir
    old = read_run(outdir)
    if old:
        live = [k for k, p in old.get("pids", {}).items() if pid_is_sampler(p, k)]
        if live:
            print("この出力ディレクトリの計測は動いている（%s）。先に stop する" % ", ".join(live), file=sys.stderr)
            return 1
        os.replace(os.path.join(outdir, "run.json"), os.path.join(outdir, "run-%s.json" % old.get("started", "old")))
    os.makedirs(outdir, exist_ok=True)
    intervals = {"metrics": common["interval_metrics_s"], "llen": common["interval_llen_s"],
                 "keys": common["interval_cache_churn_s"]}
    run = {"profile": profile.name, "profiles_path": a.profiles, "started": int(time.time()),
           "binary": binary_info(find_binary(profile, common)), "kaz_log_lines": kaz_log_lines(profile.log_glob),
           "outdir": os.path.abspath(outdir), "pids": {}, "intervals": intervals}
    write_run(outdir, run)  # sampler が読むので、起動の前に書く
    warn = []
    err = cap.copy_config(profile.mountpoint, outdir)
    if err:
        warn.append(err)
    warn += cap.capture(profile, outdir, "start")
    log = open(os.path.join(outdir, "samplers.log"), "ab")
    for k in KINDS:
        p = subprocess.Popen([sys.executable, "-I", "-c", BOOT, ROOT, k, outdir], cwd=ROOT, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
        run["pids"][k] = p.pid
    log.close()
    write_run(outdir, run)
    print("計測を開始した: %s（pids %s）" % (outdir, run["pids"]))
    for w in warn:
        print("警告: " + w, file=sys.stderr)
    return 0


def latest_is_error(path, kind, tail=65536):
    """記録の末尾を読み、最新の標本が失敗の行か。metrics は最後の時刻の行に _scrape_error があるか、
    llen は最後の行の 3 列目が error か。"""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - tail))
        lines = f.read().decode("utf-8", "replace").splitlines()
    rows = [l.split("\t") for l in lines if l.count("\t") >= 2]
    if not rows:
        return False
    if kind == "llen":
        return rows[-1][2] == "error"
    last = rows[-1][0]
    return any(r[0] == last and r[2] == "_scrape_error" for r in rows)


def cmd_check(a, profile, common):
    """常駐が全部生きているか、各記録の最後の更新、足りない材料を出す。足りなければ 1。"""
    run = read_run(a.outdir)
    if not run:
        print("NG: run.json が無い（start していない）", file=sys.stderr)
        return 1
    problems, now = [], time.time()
    for k in KINDS:
        pid = run.get("pids", {}).get(k)
        if not pid or not pid_is_sampler(pid, k):
            problems.append("%s の常駐が止まっている（pid %s）" % (k, pid))
    for fn, kind in (("metrics.tsv", "metrics"), ("llen.tsv", "llen")):
        path = os.path.join(a.outdir, fn)
        lim = STALE_FACTOR * run["intervals"][kind]
        if not os.path.exists(path):
            problems.append("%s がまだ無い" % fn)
        else:
            age = now - os.path.getmtime(path)
            print("%s: 最後の更新は %d 秒前" % (fn, age))
            if age > lim:
                problems.append("%s の更新が古い（%d 秒前、間隔の %d 倍 = %d 秒を超えた）" % (fn, age, STALE_FACTOR, lim))
            if latest_is_error(path, kind):
                problems.append("%s の最新の記録が取得の失敗（%s）" % (
                    fn, "_scrape_error" if kind == "metrics" else "error"))
    names = os.listdir(a.outdir)
    for pre in ("keys-snapshot-", "metrics-start-", "breakdown-start-"):
        if not any(n.startswith(pre) for n in names):
            problems.append("%s* が無い" % pre)
    for fn in ("keys-events.tsv", "config.json"):
        if fn not in names:
            problems.append("%s が無い" % fn)
    for p in problems:
        print("NG: " + p, file=sys.stderr)
    if not problems:
        print("OK: 常駐は全部生きていて、記録は新しい")
    return 1 if problems else 0


def cmd_stop(a, profile, common):
    """常駐を止める（SIGTERM、5 秒待って残れば SIGKILL）。"""
    run = read_run(a.outdir)
    if not run:
        print("run.json が無い", file=sys.stderr)
        return 1
    live = {k: p for k, p in run.get("pids", {}).items() if pid_is_sampler(p, k)}
    for p in live.values():
        os.kill(p, signal.SIGTERM)
    end = time.time() + 5
    while time.time() < end and any(pid_is_sampler(p) for p in live.values()):
        time.sleep(0.05)
    for p in live.values():
        if pid_is_sampler(p):
            os.kill(p, signal.SIGKILL)
    run["stopped"] = int(time.time())
    write_run(a.outdir, run)
    print("止めた: %s" % (", ".join(live) or "（動いているものは無かった）"))
    return 0


def cmd_capture(a, profile, common):
    """再起動の直前などの材料を、ラベル付きで取る。取れないものがあれば 1。"""
    os.makedirs(a.outdir, exist_ok=True)
    problems = cap.capture(profile, a.outdir, a.label)
    for p in problems:
        print("NG: " + p, file=sys.stderr)
    if not problems:
        print("取得した: %s/*-%s-*" % (a.outdir, a.label))
    return 1 if problems else 0


def _run_report(fn):
    """表を作る関数を呼んで出力する。記録が無い・指定が読めないときは日本語の理由を出して 1。"""
    try:
        print(fn())
    except (report.NoData, ValueError, OSError) as e:
        print("NG: %s" % e, file=sys.stderr)
        return 1
    return 0


def cmd_progress(a, profile, common):
    """progress_kaz.sh と同じ項目に compaction などを足して表示する。"""
    return _run_report(lambda: report.progress(a.outdir, a.start, a.end))


def cmd_compare(a, profile, common):
    """区間を同じ表で並べる。"""
    return _run_report(lambda: report.compare(a.specs))


def cmd_boot(a, profile, common):
    """起動の区間の解析。"""
    marks = {k: v for k, v in zip(bootmod.MARK_KEYS, (a.boot_start, a.login_screen, a.login_done, a.app)) if v}
    return _run_report(lambda: bootmod.analyze(a.outdir, a.start, a.end, marks, a.label))


def main(argv=None):
    """jfsmeasure の入口。"""
    ap = argparse.ArgumentParser(prog="jfsmeasure", description="JuiceFS の計測")
    ap.add_argument("--profiles", help="profiles.toml の path（既定は tests/juicefs/profiles.toml）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="常駐を起動する")
    s.add_argument("profile")
    s.add_argument("outdir")
    s = sub.add_parser("check", help="常駐と記録の状態を確かめる")
    s.add_argument("outdir")
    s = sub.add_parser("stop", help="常駐を止める")
    s.add_argument("outdir")
    s = sub.add_parser("capture", help="ラベル付きの材料を取る")
    s.add_argument("outdir")
    s.add_argument("label")
    s.add_argument("--profile", help="対象のプロファイル（既定は run.json のもの）")
    s = sub.add_parser("progress", help="流入・compaction などを表示する")
    s.add_argument("outdir")
    s.add_argument("start", nargs="?", default="first", help="開始（既定は first）。省略時は全体と直近 1 時間を出す")
    s.add_argument("end", nargs="?", default="now")
    s = sub.add_parser("compare", help="区間を同じ表で並べる")
    s.add_argument("specs", nargs="+", metavar="ラベル:ディレクトリかTSV:開始:終了")
    s = sub.add_parser("boot", help="起動の区間の解析")
    s.add_argument("outdir")
    s.add_argument("start")
    s.add_argument("end")
    s.add_argument("label", nargs="?")
    s.add_argument("--boot-start", help="起動開始の時刻（HH:MM[:SS]）")
    s.add_argument("--login-screen", help="ログイン画面の時刻")
    s.add_argument("--login-done", help="ログイン完了の時刻")
    s.add_argument("--app", help="アプリの時刻")
    a = ap.parse_args(argv)
    profile = common = None
    if a.cmd in ("start", "capture"):
        run = read_run(a.outdir) if a.cmd == "capture" else None
        pname = a.profile or (run or {}).get("profile")
        if not pname:
            print("プロファイルが分からない（--profile を付けるか、start 済みの出力ディレクトリを使う）", file=sys.stderr)
            return 1
        a.profiles = a.profiles or (run or {}).get("profiles_path")
        profile, common = config.load_profile(pname, a.profiles), config.load_common(a.profiles)
        why = prod_overlap(a.outdir, load_prod(a.profiles))
        if why:  # 計測の材料を本番の mount やキャッシュの中に書かない
            print("停止: %s" % why, file=sys.stderr)
            return 3
    return {"start": cmd_start, "check": cmd_check, "stop": cmd_stop, "capture": cmd_capture,
            "progress": cmd_progress, "compare": cmd_compare, "boot": cmd_boot}[a.cmd](a, profile, common)


if __name__ == "__main__":
    sys.exit(main())
