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

FUSE_COUNT = "juicefs_fuse_written_size_bytes_count"
FUSE_SUM = "juicefs_fuse_written_size_bytes_sum"


class _Stop(Exception):
    """signal で記録を止めるための内部の例外。"""


def record(out, inode, accesslog_path, exit_at_eof=False, poll_s=0.1):
    """accesslog_path を読み続け、対象 inode の Op だけを out に追記する。行を読んだらすぐ絞って書く
    （mount 側の buffer 10,240 行を溢れさせないため）。止まるときに out.meta.json へ行の総数と Unknown の数を書く。
    戻り値は meta の dict。"""
    f = open(accesslog_path, "rb")  # 開けること自体が開始時の確認になる（失敗は OSError）
    meta = {"inode": inode, "lines_total": 0, "keepalive": 0, "unknown": 0, "ops": 0, "unknown_samples": [],
            "all_write_ops": 0, "all_write_bytes": 0, "started": time.time()}
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
    m = metrics_io.load(os.path.join(outdir, "metrics.tsv"))
    dc = _delta_between(m.get(FUSE_COUNT, []), t0, t1)
    ds = _delta_between(m.get(FUSE_SUM, []), t0, t1)
    if dc is None or ds is None:
        return None, ["記録の区間 [%d, %d] が %s／%s のサンプルの範囲に収まりません" % (t0, t1, FUSE_COUNT, FUSE_SUM)]
    ops = [o for o in trace.load(out) if o.kind == "write" and o.status == "OK"]
    lines = ["区間 %.0f - %.0f（%.0f 秒）" % (t0, t1, t1 - t0)]
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
        print("記録 %d 操作、行 %d、keepalive %d、解析できなかった行 %d" % (meta["ops"], meta["lines_total"], meta["keepalive"], meta["unknown"]))
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
