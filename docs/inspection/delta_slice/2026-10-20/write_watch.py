#!/usr/bin/env python3
"""VM ディスクの chunk に新しく積まれる書き込みの record を、Redis から読み取りだけで記録し続ける。

差分 slice 方式の見積もり（delta_sim.py）の材料にする。一定間隔で対象 chunk の LRANGE を取り、
前回までに見ていない slice id の record を、書き込み（list の末尾に追加されたもの）と
compaction の出力（list の先頭側に置かれたもの）に分けて TSV に追記する。書き込み・削除は一切しない。
接続情報は cache_breakdown.py の connect_from_script（MOUNT_FROM を読み、表示しない）を使う。
使い方: write_watch.py <出力TSV> [--interval 15] [--hours 2] [--chunks 1920,0,2810] [--top 10]
出力列: unix 時刻 / chunk / 種別(write|compact) / pos / id / size / off / len / その時点の record 数
"""
import argparse
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cache_churn", "2026-10-19"))
from cache_breakdown import connect_from_script  # noqa: E402  読み取り専用 RESP クライアント


def lrange(r, inode, ch):
    """chunk の record 列を (pos, id, size, off, len) のリストで返す。"""
    vals = r.call("LRANGE", f"c{inode}_{ch}", 0, -1) or []
    return [struct.unpack(">IQIII", v) for v in vals if len(v) == 24]


def main():
    """対象 chunk を一定間隔で読み、新しい record を TSV に追記する。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--script", default=os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh"))
    ap.add_argument("--inode", type=int, default=634040)
    ap.add_argument("--nchunks", type=int, default=4800)
    ap.add_argument("--chunks", default="1920,0,2810,1985,2807")
    ap.add_argument("--top", type=int, default=10, help="開始時に record 数の多い chunk をこの数だけ足す")
    ap.add_argument("--interval", type=int, default=15)
    ap.add_argument("--hours", type=float, default=2.0)
    a = ap.parse_args()
    r = connect_from_script(a.script)
    chunks = [int(x) for x in a.chunks.split(",") if x]
    r.send(*[("LLEN", f"c{a.inode}_{i}") for i in range(a.nchunks)])
    lens = [(r.read(), i) for i in range(a.nchunks)]
    for _, i in sorted(lens, reverse=True)[: a.top]:
        if i not in chunks:
            chunks.append(i)
    seen = {ch: set(x[1] for x in lrange(r, a.inode, ch)) for ch in chunks}
    out = open(a.out, "a", buffering=1)
    out.write("# start %d chunks %s\n" % (time.time(), ",".join(map(str, chunks))))
    end = time.time() + a.hours * 3600
    while time.time() < end:
        time.sleep(a.interval)
        now = int(time.time())
        for ch in chunks:
            ss = lrange(r, a.inode, ch)
            last_seen = -1
            for idx, s in enumerate(ss):
                if s[1] in seen[ch]:
                    last_seen = idx
            for idx, s in enumerate(ss):
                if s[1] in seen[ch] or s[1] == 0:
                    continue
                kind = "write" if idx > last_seen else "compact"
                out.write("%d\t%d\t%s\t%d\t%d\t%d\t%d\t%d\t%d\n" % ((now, ch, kind) + s + (len(ss),)))
            seen[ch] = set(x[1] for x in ss) | seen[ch]


if __name__ == "__main__":
    main()
