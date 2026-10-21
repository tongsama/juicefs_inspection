"""jfsreplay の sub-command（record／stop／mark／verify）。

record は <mountpoint>/.accesslog を読むだけで、mount には何も書かない。"""
import argparse
import json
import os
import signal
import sys
import time

from lib import config, metrics_io
from replay import accesslog, trace

STAGING_SERIES = "juicefs_staging_write_bytes"


class _Stop(Exception):
    """signal で記録を止めるための内部の例外。"""


def record(out, inode, accesslog_path, exit_at_eof=False, poll_s=0.1):
    """accesslog_path を読み続け、対象 inode の Op だけを out に追記する。行を読んだらすぐ絞って書く
    （mount 側の buffer 10,240 行を溢れさせないため）。止まるときに out.meta.json へ行の総数と Unknown の数を書く。
    戻り値は meta の dict。"""
    f = open(accesslog_path, "rb")  # 開けること自体が開始時の確認になる（失敗は OSError）
    meta = {"inode": inode, "lines_total": 0, "unknown": 0, "ops": 0, "unknown_samples": [],
            "started": time.time()}
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
                meta["lines_total"] += 1
                r = accesslog.parse_line(line, inode)
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


def verify(out, outdir, limit):
    """記録の write のバイトを、同じ区間の staging_write_bytes の差と比べる。
    区間は、最初の write の開始以前の最後のサンプルから、最後の write の終了以後の最初のサンプルまで（記録の区間を外側で挟む）。
    戻り値は (差の割合 or None, 表示の行のリスト)。差の割合は |metrics − trace| / trace。"""
    ops = [o for o in trace.load(out) if o.kind == "write" and o.status == "OK"]
    if not ops:
        return None, ["記録に成功した write がありません"]
    series = metrics_io.load(os.path.join(outdir, "metrics.tsv")).get(STAGING_SERIES, [])
    t0 = min(o.start_us for o in ops) / 1e6
    t1 = max(o.end_us for o in ops) / 1e6
    ts = [t for t, _ in series]
    before = [t for t in ts if t <= t0]
    after = [t for t in ts if t >= t1]
    if not before or not after:
        return None, ["区間 [%d, %d] を挟む %s のサンプルがありません（前 %d 個、後 %d 個）" % (t0, t1, STAGING_SERIES, len(before), len(after))]
    a, b = max(before), min(after)  # staging への書き込みは遅れて起きるので、記録の区間を外側のサンプルで挟む
    tb = sum(o.size for o in ops)
    mb = metrics_io.delta(series, a, b)
    if tb == 0:
        return None, ["区間内の write のバイトが 0 です"]
    ratio = abs(mb - tb) / tb
    lines = ["区間 %d - %d（%.0f 秒）" % (a, b, b - a),
             "記録: write %d 回 %d バイト" % (len(ops), tb),
             "metrics: %s の差 %.0f バイト" % (STAGING_SERIES, mb),
             "差の割合: %.2f%%（上限 %.2f%%）%s" % (ratio * 100, limit * 100, "" if ratio <= limit else " 超過")]
    return ratio, lines


def _meta_lines(out):
    """verify の表示に、record の meta.json の取りこぼし（Unknown）を足す。"""
    try:
        with open(out + ".meta.json", encoding="utf-8") as f:
            m = json.load(f)
    except OSError:
        return ["meta.json なし（record が正常に止まらなかった可能性）"]
    msg = "accesslog の行 %d、解析できなかった行 %d" % (m["lines_total"], m["unknown"])
    return [msg + (" ← 0 でないので内容を確認すること" if m["unknown"] else "")]


def main(argv=None):
    """jfsreplay の入口。"""
    ap = argparse.ArgumentParser(prog="jfsreplay", description="JuiceFS の write の記録と流し直し")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("record", help=".accesslog から対象 inode の操作を記録する（Ctrl-C か stop で止まる）")
    r.add_argument("out")
    r.add_argument("--inode", type=int, required=True)
    r.add_argument("--mountpoint", default="~/mnt_juicefs")
    r.add_argument("--accesslog", help="（試験用）.accesslog の代わりに読む path")
    r.add_argument("--exit-at-eof", action="store_true", help="（試験用）EOF で止まる")
    s = sp.add_parser("stop", help="動いている record を止める")
    s.add_argument("out")
    m = sp.add_parser("mark", help="今の時刻をラベル付きで記録する")
    m.add_argument("out")
    m.add_argument("label")
    v = sp.add_parser("verify", help="記録の write のバイトを計測の staging_write_bytes と比べる")
    v.add_argument("out")
    v.add_argument("outdir")
    a = ap.parse_args(argv)
    if a.cmd == "record":
        path = a.accesslog or os.path.join(os.path.expanduser(a.mountpoint), ".accesslog")
        try:
            meta = record(a.out, a.inode, path, a.exit_at_eof)
        except OSError as e:
            print("エラー: %s を開けません（%s）" % (path, e), file=sys.stderr)
            return 2
        print("記録 %d 操作、行 %d、解析できなかった行 %d" % (meta["ops"], meta["lines_total"], meta["unknown"]))
        return 0
    if a.cmd == "stop":
        try:
            with open(a.out + ".pid") as f:
                os.kill(int(f.read()), signal.SIGTERM)
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
