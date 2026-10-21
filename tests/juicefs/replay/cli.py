"""jfsreplay の sub-command（record／stop／mark／verify／testvol／seed／run）。

record は <mountpoint>/.accesslog を読むだけで、mount には何も書かない。"""
import argparse
import json
import os
import re
import signal
import sys
import time

from lib import config, metrics_io
from replay import accesslog, trace

FUSE_COUNT = "juicefs_fuse_written_size_bytes_count"
FUSE_SUM = "juicefs_fuse_written_size_bytes_sum"
FUSE_INCLUDE = re.compile(r"^juicefs_fuse_written_size_bytes_(sum|count)")


def _scrape_fuse(url):
    """url の /metrics から fuse_written_size_bytes の _count／_sum を {"count":, "sum":} で返す。取れなければ None。"""
    if not url:
        return None
    try:
        m = metrics_io.scrape(url, FUSE_INCLUDE, timeout=10)
        return {"count": m[FUSE_COUNT], "sum": m[FUSE_SUM], "t": time.time()}
    except (OSError, KeyError, ValueError) as e:
        print("警告: metrics を取得できません（%s）。verify は metrics.tsv の補間に切り替わります" % type(e).__name__, file=sys.stderr)
        return None


def check_new_output(out):
    """record の出力が新しいことを確かめる。trace に中身があるか <out>.meta.json があれば FileExistsError
    （黙って追記すると前の記録と混ざり、meta の数と合わなくなるため）。空の trace ファイルはよい。"""
    try:
        if os.path.getsize(out) > 0:
            raise FileExistsError("記録の出力 %s に中身がある（上書き・追記はしない。別の名前にする）" % out)
    except FileNotFoundError:
        pass
    if os.path.lexists(out + ".meta.json"):
        raise FileExistsError("%s.meta.json がすでにある（前の記録。別の名前にする）" % out)


def pid_is_record(pid):
    """pid が生きた `jfsreplay record` か（/proc/<pid>/cmdline で確かめる）。別の process に再利用された pid は False。"""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return False
    return any(os.path.basename(a) == b"jfsreplay" for a in args) and b"record" in args


def stop_record(out):
    """<out>.pid の record を SIGTERM で止める。pid が record でなければ signal を送らず、古い pidfile だけ消して
    ProcessLookupError。pidfile が読めなければ OSError／ValueError。"""
    pidfile = out + ".pid"
    with open(pidfile) as f:
        pid = int(f.read())
    if not pid_is_record(pid):
        os.unlink(pidfile)
        raise ProcessLookupError("pid %d は jfsreplay record ではない（古い pidfile を消した。signal は送っていない）" % pid)
    os.kill(pid, signal.SIGTERM)
    return pid


class _Stop(Exception):
    """signal で記録を止めるための内部の例外。"""


def record(out, inode, accesslog_path, exit_at_eof=False, poll_s=0.1, metrics_url=None):
    """accesslog_path を読み続け、対象 inode の Op だけを out に追記する。行を読んだらすぐ絞って書く
    （mount 側の buffer 10,240 行を溢れさせないため）。metrics_url があれば、.accesslog を開いた直後と閉じる直前の fuse_written_size_bytes を meta に保存する（verify の厳密な突き合わせ用）。
    止まるときに out.meta.json へ行の総数と Unknown の数を書く。
    出力に前の記録（中身のある trace か meta.json）があれば、何も開かずに FileExistsError。
    戻り値は meta の dict。"""
    check_new_output(out)
    f = open(accesslog_path, "rb")  # 開けること自体が開始時の確認になる（失敗は OSError）
    meta = {"inode": inode, "lines_total": 0, "keepalive": 0, "unknown": 0, "ops": 0, "unknown_samples": [],
            "all_write_ops": 0, "all_write_bytes": 0, "started": time.time()}
    meta["metrics_start"] = _scrape_fuse(metrics_url)
    pidfile = out + ".pid"
    with open(pidfile, "w") as pf:
        pf.write(str(os.getpid()))
    old = {s: signal.signal(s, _raise_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with open(out, "a", encoding="utf-8") as o:
            buf = b""
            while True:
                chunk = f.readline()
                if not chunk:
                    if exit_at_eof:
                        break
                    time.sleep(poll_s)
                    continue
                buf += chunk
                if not buf.endswith(b"\n"):
                    continue  # 行の途中（次の読みで続きが来る）
                line, buf = buf.decode("utf-8", "replace"), b""
                if accesslog.is_keepalive(line):
                    meta["keepalive"] += 1  # idle 時の "#" 行。行の総数にも Unknown にも入れない
                    continue
                meta["lines_total"] += 1
                ino, r = accesslog.parse_any(line)
                if (isinstance(r, accesslog.Op) and r.kind == "write" and r.status == "OK"
                        and ino < accesslog.MIN_INTERNAL_NODE):
                    # 全 inode の成功した write（vfs.go が written_size を observe する条件と同じ）。verify の突き合わせ用
                    meta["all_write_ops"] += 1
                    meta["all_write_bytes"] += r.size
                if isinstance(r, accesslog.Op) and ino != inode:
                    r = None
                if isinstance(r, accesslog.Unknown):
                    meta["unknown"] += 1
                    if len(meta["unknown_samples"]) < 5:
                        meta["unknown_samples"].append(r.line[:200])
                elif r is not None:
                    o.write(trace.format_op(r))
                    o.flush()
                    meta["ops"] += 1
    except (_Stop, KeyboardInterrupt):
        pass
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        meta["metrics_stop"] = _scrape_fuse(metrics_url)
        f.close()
        meta["stopped"] = time.time()
        with open(out + ".meta.json", "w", encoding="utf-8") as m:
            json.dump(meta, m, ensure_ascii=False, indent=1)
        try:
            os.unlink(pidfile)
        except OSError:
            pass
    return meta


def _raise_stop(signum, frame):
    """record の signal handler。読み待ちの中でも止められるよう例外にする。"""
    raise _Stop()


def mark(out, label, now=None):
    """今の時刻（UNIX マイクロ秒、ISO、ラベル）を <out>.marks.tsv に足す。"""
    t = time.time() if now is None else now
    with open(out + ".marks.tsv", "a", encoding="utf-8") as f:
        f.write("%d\t%s\t%s\n" % (int(t * 1e6), time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)), label))


def _cum(series):
    """counter の系列を、mount のし直しの reset を足し合わせた累積 [(時刻, 累積の増分)] にする。"""
    out, c = [], 0.0
    for i, (t, v) in enumerate(series):
        if i:
            c += v - series[i - 1][1] if v >= series[i - 1][1] else v
        out.append((t, c))
    return out


def _delta_between(series, t0, t1):
    """時刻 t0〜t1 の counter の増分。サンプルの間は線形に補間する（60 秒間隔でも記録の区間に合わせるため）。
    区間がサンプルの範囲に収まらなければ None。"""
    cum = _cum(series)
    if len(cum) < 2 or t0 < cum[0][0] or t1 > cum[-1][0]:
        return None

    def at(t):
        for (ta, ca), (tb, cb) in zip(cum, cum[1:]):
            if ta <= t <= tb:
                return ca if tb == ta else ca + (cb - ca) * (t - ta) / (tb - ta)
    return at(t1) - at(t0)


def verify(out, outdir, limit):
    """record の meta.json（全 inode の write の回数とバイト、記録の開始・終了時刻）を、同じ区間の
    juicefs_fuse_written_size_bytes の _count と _sum の増分と比べる。回数とバイトの両方を limit で判定する。
    対象 inode だけの write の合計は参考として表示する。戻り値は (差の割合の大きい方 or None, 表示の行)。"""
    try:
        with open(out + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None, ["meta.json を読めません（record が正常に止まっていません）"]
    if not meta.get("all_write_ops"):
        return None, ["記録に全 inode の write がありません（all_write_ops が 0）"]
    t0, t1 = meta["started"], meta["stopped"]
    ms, me = meta.get("metrics_start"), meta.get("metrics_stop")
    if ms and me:
        dc, ds = me["count"] - ms["count"], me["sum"] - ms["sum"]
        if dc < 0 or ds < 0:
            return None, ["記録中に mount が再起動した可能性があります（counter が減少）。取り直してください"]
        src = "record の開始・終了時に取得した値"
    else:
        m = metrics_io.load(os.path.join(outdir, "metrics.tsv"))
        dc = _delta_between(m.get(FUSE_COUNT, []), t0, t1)
        ds = _delta_between(m.get(FUSE_SUM, []), t0, t1)
        src = "metrics.tsv のサンプル間の線形補間（meta に開始・終了の値なし）"
    if dc is None or ds is None:
        return None, ["記録の区間 [%d, %d] が %s／%s のサンプルの範囲に収まりません" % (t0, t1, FUSE_COUNT, FUSE_SUM)]
    ops = [o for o in trace.load(out) if o.kind == "write" and o.status == "OK"]
    lines = ["区間 %.0f - %.0f（%.0f 秒）、metrics の出所: %s" % (t0, t1, t1 - t0, src)]
    ratios = []
    for name, rec, met in (("回数", meta["all_write_ops"], dc), ("バイト", meta["all_write_bytes"], ds)):
        r = abs(met - rec) / rec if rec else None
        if r is None:
            return None, lines + ["記録の write の%sが 0 です" % name]
        ratios.append(r)
        lines.append("全 inode の write %s: 記録 %d、metrics の差 %.0f、差の割合 %.2f%%（上限 %.2f%%）%s" % (
            name, rec, met, r * 100, limit * 100, "" if r <= limit else " 超過"))
    lines.append("（参考）対象 inode %s の write: %d 回 %d バイト" % (meta.get("inode"), len(ops), sum(o.size for o in ops)))
    return max(ratios), lines


def _meta_lines(out):
    """verify の表示に、record の meta.json の取りこぼし（Unknown）を足す。"""
    try:
        with open(out + ".meta.json", encoding="utf-8") as f:
            m = json.load(f)
    except OSError:
        return ["meta.json なし（record が正常に止まらなかった可能性）"]
    msg = "accesslog の行 %d（keepalive %d は除く）、解析できなかった行 %d" % (m["lines_total"], m.get("keepalive", 0), m["unknown"])
    return [msg + (" ← 0 でないので内容を確認すること" if m["unknown"] else "")]


def _parse_value(v):
    """--set の値を bool／int／float／文字列にする。"""
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return v


def _testvol(a):
    """jfsreplay testvol の本体。安全の確かめで止まったら終了 3、失敗は 2。"""
    from replay import testvol
    profile = config.load_profile("test")
    try:
        if a.action == "up":
            flags = {}
            for kv in a.set:
                k, sep, v = kv.partition("=")
                if not sep:
                    print("エラー: --set は 名前=値 の形にする: %s" % kv, file=sys.stderr)
                    return 2
                flags[k.lstrip("-")] = _parse_value(v)
            info = testvol.up(profile, {"name": "cli", "mode": a.mode, "flags": flags},
                              create_bucket=a.create_bucket)
            for k in ("meta", "mountpoint", "metrics", "started_redis", "started_proxy", "formatted"):
                print("%s: %s" % (k, info[k]))
            print("mount のオプション: %s" % " ".join(info["mount_args"]))
        elif a.action == "down":
            testvol.down(profile=profile)
            print("試験の mount を外し、中継を止めました（Redis は残しています）")
        elif a.action == "status":
            st = testvol.status()
            print("  ".join("%s=%s" % (k, "yes" if v else "no") for k, v in st.items()))
        else:
            print("消しました" if testvol.destroy(profile) else "何もしませんでした")
    except testvol.SafetyError as e:
        print("停止: %s" % e, file=sys.stderr)
        return 3
    except (RuntimeError, OSError, ValueError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 2
    return 0


def _seed(a):
    """jfsreplay seed の本体。安全の確かめで止まったら 3、それ以外の失敗は 2。"""
    from replay import seed, testvol
    try:
        limit = seed.parse_size(a.limit) if a.limit else None
        length = seed.parse_size(a.length) if a.length else None
        info = seed.seed(a.trace, config.load_profile("test"), limit=limit, length=length, create_bucket=a.create_bucket,
                         written_only=a.written_only)
    except testvol.SafetyError as e:
        print("停止: %s" % e, file=sys.stderr)
        return 3
    except (seed.SeedError, RuntimeError, OSError, ValueError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 2
    print("seed inode: %d" % info["inode"])
    print("seed path: %s" % info["path"])
    return 0


def _run(a):
    """jfsreplay run の本体。成功は 0、失敗（EIO、rate limit、mount の停止など）は 1、続けられないエラーは 2、
    安全の確かめで止まったら 3。"""
    from replay import run as runmod, testvol
    out = a.out or os.path.join(runmod.SUITE_DIR, "results", time.strftime("%Y%m%d-%H%M%S"), a.settings)
    try:
        res = runmod.run(a.trace, a.settings, out, config.load_profile("test"), age=a.age)
    except testvol.SafetyError as e:
        print("停止: %s" % e, file=sys.stderr)
        return 3
    except (KeyError, runmod.RunError, RuntimeError, OSError, ValueError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 2
    print("結果: %s（%s）" % ("成功" if res["status"] == "ok" else "失敗", out))
    if res.get("settle"):
        print("落ち着きの判定: %s（%s 秒）" % (res["settle"]["state"], res["settle"]["seconds"]))
    for r in res["reasons"]:
        print("理由: %s" % r, file=sys.stderr)
    for w in res.get("warnings") or []:
        print("警告: %s" % w, file=sys.stderr)
    return 0 if res["status"] == "ok" else 1


def _suite(a):
    """jfsreplay suite の本体。全部成功は 0、失敗で止まったら 1、続けられないエラーは 2。"""
    from replay import suite
    try:
        names = [x["name"] for x in suite.select(a.settings_file or suite.runmod.SETTINGS_PATH, a.only)]
        if a.repeat < 1:
            raise ValueError("--repeat は 1 以上")
        sf = os.path.abspath(a.settings_file) if a.settings_file else None
        res = suite.run_suite(a.trace, names, repeat=a.repeat, age=a.age,
                              runner=suite.make_runner(a.create_bucket, sf))
    except (KeyError, OSError, ValueError) as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 2
    print("表: %s" % res["summary"])
    if res["status"] != "ok":
        print("失敗した設定があり、そこで止めました。表の「失敗・注意」を見てください", file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    """jfsreplay の入口。"""
    ap = argparse.ArgumentParser(prog="jfsreplay", description="JuiceFS の write の記録と流し直し")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("record", help=".accesslog から対象 inode の操作を記録する（Ctrl-C か stop で止まる）")
    r.add_argument("out")
    r.add_argument("--inode", type=int, required=True)
    r.add_argument("--mountpoint", default="~/mnt_juicefs")
    r.add_argument("--profile", default="prod", help="metrics URL を取るプロファイル（既定 prod）")
    r.add_argument("--metrics-url", help="metrics URL を直接指定（'' で取得しない）")
    r.add_argument("--accesslog", help="（試験用）.accesslog の代わりに読む path")
    r.add_argument("--exit-at-eof", action="store_true", help="（試験用）EOF で止まる")
    s = sp.add_parser("stop", help="動いている record を止める")
    s.add_argument("out")
    m = sp.add_parser("mark", help="今の時刻をラベル付きで記録する")
    m.add_argument("out")
    m.add_argument("label")
    v = sp.add_parser("verify", help="記録の全 inode の write の回数とバイトを、計測の fuse_written_size_bytes の増分と比べる")
    v.add_argument("out")
    v.add_argument("outdir")
    tv = sp.add_parser("testvol", help="試験の volume（一時の Redis、RTT の中継、format／mount）を操作する")
    tv.add_argument("action", choices=("up", "down", "status", "destroy"))
    tv.add_argument("--mode", help="up: compaction の mode（delta／block など）")
    tv.add_argument("--set", action="append", default=[], metavar="名前=値",
                    help="up: compaction の値（例 kaz-compaction-delta-trigger=30）。何回でも指定できる")
    tv.add_argument("--create-bucket", action="store_true",
                    help="up: 試験の volume が無いとき、本物の Drive（rclone serve s3）にバケットと volume を作ってよい")
    sd = sp.add_parser("seed", help="流し直しが clone する種のファイルを試験の volume に作る")
    sd.add_argument("trace")
    sd.add_argument("--limit", default=None, help="種の上限（既定は profiles.toml の seed_limit_gib、例 32G）")
    sd.add_argument("--length", default=None, help="種の長さ（既定は trace の最大 off+size を 64 MiB に切り上げ）")
    sd.add_argument("--create-bucket", action="store_true", help="試験の volume が無いとき、本物の Drive に作ってよい")
    sd.add_argument("--written-only", action="store_true",
                    help="write が触れた chunk だけを種にする（read だけの範囲は穴のまま。種が小さく速いが、読み込みの GET の比較は弱くなる）")
    rn = sp.add_parser("run", help="1 つの compaction の設定で、記録を試験の volume に 1 回流す（設計書 §5.4）")
    rn.add_argument("trace")
    rn.add_argument("--settings", required=True, help="settings.toml の設定の名前（default は mount_flags.txt のまま）")
    rn.add_argument("--age", type=int, default=0, help="計測の前に既定の設定で記録を流して差分を育てる回数（既定 0）")
    rn.add_argument("--out", help="結果の置き場所（既定 tests/juicefs/results/<日時>/<設定の名前>）")
    su = sp.add_parser("suite", help="settings.toml の設定の組を順に流し、1 枚の比較の表（summary.md）を作る（設計書 §5.5）")
    su.add_argument("trace")
    su.add_argument("--only", help="流す設定の名前（カンマ区切り。既定は settings.toml の全部）")
    su.add_argument("--repeat", type=int, default=1, help="同じ設定を流す回数（ばらつきを見る。既定 1）")
    su.add_argument("--age", type=int, default=0, help="各回の計測の前に既定の設定で記録を流して差分を育てる回数（既定 0）")
    su.add_argument("--create-bucket", action="store_true",
                    help="試験の volume が無いとき、本物の Drive にバケットと volume を作ってよい（run の前に seed などで作る場合は不要）")
    su.add_argument("--settings-file", default=None, help="設定の組のファイル（既定 tests/juicefs/settings.toml）")
    a = ap.parse_args(argv)
    if a.cmd == "suite":
        return _suite(a)
    if a.cmd == "run":
        return _run(a)
    if a.cmd == "testvol":
        return _testvol(a)
    if a.cmd == "seed":
        return _seed(a)
    if a.cmd == "record":
        path = a.accesslog or os.path.join(os.path.expanduser(a.mountpoint), ".accesslog")
        url = a.metrics_url if a.metrics_url is not None else config.load_profile(a.profile).metrics_url
        try:
            meta = record(a.out, a.inode, path, a.exit_at_eof, metrics_url=url)
        except FileExistsError as e:
            print("エラー: %s" % e, file=sys.stderr)
            return 2
        except OSError as e:
            print("エラー: %s を開けません（%s）" % (path, e), file=sys.stderr)
            return 2
        print("記録 %d 操作、行 %d、keepalive %d、解析できなかった行 %d" % (meta["ops"], meta["lines_total"], meta["keepalive"], meta["unknown"]))
        return 0
    if a.cmd == "stop":
        try:
            stop_record(a.out)
        except (OSError, ValueError) as e:
            print("エラー: record を止められません（%s）" % e, file=sys.stderr)
            return 2
        return 0
    if a.cmd == "mark":
        mark(a.out, a.label)
        return 0
    limit = config.load_common()["loss_tolerance"]
    ratio, lines = verify(a.out, a.outdir, limit)
    print("\n".join(lines + _meta_lines(a.out)))
    if ratio is None:
        return 2
    return 0 if ratio <= limit else 1
