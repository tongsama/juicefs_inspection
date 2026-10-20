#!/usr/bin/env python3
"""VM ディスク（inode 634040）の chunk record 列を Redis から読み取りのみで観察する。

- LLEN を全 chunk（0..N-1）に対してパイプラインで取り、record 数の分布を出す。
- 指定 chunk の LRANGE を一定間隔で取り、新しく積まれた record（前回にない slice id）の
  位置・長さ・触れた 4 MiB block を記録する。書き込み・削除は一切しない。
接続情報は cache_breakdown.py の connect_from_script（MOUNT_FROM を読み、表示しない）を使う。
"""
import argparse
import collections
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2026-10-19"))
from cache_breakdown import connect_from_script  # noqa: E402  読み取り専用 RESP クライアント

BS = 4 << 20


def lrange(r, inode, ch):
    """chunk の record 列を (pos, id, size, off, len) のリストで返す。"""
    vals = r.call("LRANGE", f"c{inode}_{ch}", 0, -1) or []
    return [struct.unpack(">IQIII", v) for v in vals if len(v) == 24]


def main():
    """LLEN 分布と、指定 chunk の新規 record の観察結果を表示する。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh"))
    ap.add_argument("--inode", type=int, default=634040)
    ap.add_argument("--nchunks", type=int, default=4800)
    ap.add_argument("--watch", default="1920,2810,0,1985,2807")
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--rounds", type=int, default=10)
    a = ap.parse_args()
    r = connect_from_script(a.script)

    r.send(*[("LLEN", f"c{a.inode}_{i}") for i in range(a.nchunks)])
    lens = [r.read() for _ in range(a.nchunks)]
    print(f"# LLEN at {time.strftime('%F %T')}: chunks>0={sum(1 for x in lens if x)}, "
          f">=17: {sum(1 for x in lens if x >= 17)}, >=50: {sum(1 for x in lens if x >= 50)}, >=99: {sum(1 for x in lens if x >= 99)}")
    print("# top LLEN:", sorted(((x, i) for i, x in enumerate(lens)), reverse=True)[:15])

    watch = [int(x) for x in a.watch.split(",")]
    prev = {c: {s[1] for s in lrange(r, a.inode, c)} for c in watch}
    new = collections.defaultdict(list)
    for k in range(a.rounds):
        time.sleep(a.interval)
        for c in watch:
            ss = lrange(r, a.inode, c)
            ids = {s[1] for s in ss}
            fresh = [s for s in ss if s[1] not in prev[c]]
            new[c].extend((time.time(), s) for s in fresh)
            print(f"{time.strftime('%T')} chunk {c}: records={len(ss)} new={len(fresh)} "
                  f"blocks={sorted({s[0] // BS for s in fresh})} lens={[s[4] for s in fresh][:12]}")
            prev[c] = ids
    dur = a.interval * a.rounds
    print("\n# summary (window %ds)" % dur)
    for c in watch:
        f = [s for _, s in new[c]]
        blocks = collections.Counter(b for s in f for b in range(s[0] // BS, (s[0] + max(s[4], 1) - 1) // BS + 1))
        print(f"chunk {c}: new records {len(f)} ({len(f) * 60 / dur:.1f}/min), bytes {sum(s[4] for s in f)} "
              f"(median len {sorted(s[4] for s in f)[len(f) // 2] if f else 0}), distinct blocks {len(blocks)} {dict(sorted(blocks.items()))}")


if __name__ == "__main__":
    main()


def offsets_report(inode=634040, chunks=(1920, 0), script=os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh")):
    """現在の small record（4 MiB 未満）の位置の重複度を表示する（同じ page の再書き込みがどれだけあるか）。"""
    r = connect_from_script(script)
    for c in chunks:
        small = [s for s in lrange(r, inode, c) if s[4] < BS]
        pos = collections.Counter(s[0] for s in small)
        print(f"chunk {c}: small records {len(small)}, distinct pos {len(pos)}, "
              f"pos written >=2: {sum(1 for v in pos.values() if v >= 2)}, top {pos.most_common(5)}")
