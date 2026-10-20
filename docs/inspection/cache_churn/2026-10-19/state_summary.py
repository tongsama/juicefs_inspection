#!/usr/bin/env python3
"""kaz-cache-state.v1（kaz-2q の管理ファイル）を読み、区分ごとに由来・ヒット回数・最後に読んだ時刻の内訳を出す（読み取りのみ）。

形式は pkg/chunk/cache_state_kaz.go の通り（little endian、magic "KAZ2QST1"、末尾 CRC32）。
- version 1: 項目 31 バイト（id u64, indx u32, size i32, seg u8, origin u8, hits u8, atime u32, inserted u32, order u32）、
  追い出し済みリスト 16 バイト（id u64, indx u32, size u32）
- version 2: 項目の末尾に降格の印 1 バイト、追い出し済みリストの末尾に追い出す前の区分 1 バイト
  （0 = 不明、1 = 常連用に上がらなかった、2 = 降格を経た、3 = 常連用、4 = compaction の出力で常連用に上がらなかった）
- version 3: 項目の末尾にさらに読まれた実績の印 1 バイト（常連用だけが 1 になりうる）
使い方: state_summary.py <kaz-cache-state.v1>
"""
import collections
import struct
import sys
import time
import zlib

SEG = {1: "probation", 2: "protected"}
ORG = {0: "legacy", 1: "write", 2: "fill", 3: "compaction"}
PRIOR = {0: "unknown", 1: "never-protected", 2: "demoted", 3: "protected", 4: "compaction"}
ITEM = "<QIiBBBIII"
GHOST = "<QII"
G = 1 << 30


def hits_bucket(h):
    """ヒット回数をおおまかな区分に分ける。"""
    return "0" if h == 0 else "1" if h == 1 else "2-4" if h < 5 else "5-19" if h < 20 else "20+"


def main():
    """管理ファイルを読んで、区分ごとの内訳と追い出し済みリストの内訳を表示する。"""
    data = open(sys.argv[1], "rb").read()
    body, crc = data[:-4], struct.unpack("<I", data[-4:])[0]
    assert data[:8] == b"KAZ2QST1" and zlib.crc32(body) == crc, "bad file"
    ver, saved = struct.unpack_from("<II", data, 8)
    assert ver in (1, 2, 3), "unsupported version %d" % ver
    extra = {1: 0, 2: 1, 3: 2}[ver]  # 項目の末尾の追加バイト数
    gextra = 1 if ver >= 2 else 0  # 追い出し済みリストの末尾の追加バイト数
    off = 16
    (n,) = struct.unpack_from("<I", data, off)
    off += 4
    by = collections.defaultdict(collections.Counter)
    hits = collections.defaultdict(collections.Counter)
    age = collections.defaultdict(collections.Counter)
    demoted = collections.Counter()
    read = collections.Counter()
    for _ in range(n):
        sid, indx, size, seg, org, h, at, ins, order = struct.unpack_from(ITEM, data, off)
        mark = data[off + struct.calcsize(ITEM)] if extra >= 1 else 0
        rmark = data[off + struct.calcsize(ITEM) + 1] if extra >= 2 else 0
        off += struct.calcsize(ITEM) + extra
        if size <= 0:
            continue
        s = SEG.get(seg, "?")
        by[s][ORG.get(org, "?")] += size
        hits[s][hits_bucket(h)] += size
        age[s][time.strftime("%m/%d %H:00", time.localtime(at))] += size
        if mark:
            demoted[s] += size
        if rmark:
            read[s] += size
    (g,) = struct.unpack_from("<I", data, off)
    off += 4
    ghost = collections.Counter()
    for _ in range(g):
        sid, indx, size = struct.unpack_from(GHOST, data, off)
        prior = data[off + struct.calcsize(GHOST)] if gextra else 0
        off += struct.calcsize(GHOST) + gextra
        ghost[PRIOR.get(prior, "?")] += size
    print("saved %s, version %d, entries %d, ghosts %d" % (time.strftime("%F %T", time.localtime(saved)), ver, n, g))
    for s in ("protected", "probation"):
        print("\n[%s] %.2f GiB（降格の印あり %.2f GiB、読まれた実績の印あり %.2f GiB）" % (s, sum(by[s].values()) / G, demoted[s] / G, read[s] / G))
        print("  origin:", {k: round(v / G, 2) for k, v in by[s].most_common()})
        print("  hits:  ", {k: round(hits[s][k] / G, 2) for k in ("0", "1", "2-4", "5-19", "20+")})
        print("  last read (hour):", {k: round(v / G, 2) for k, v in sorted(age[s].items())})
    print("\n[ghost] %.2f GiB by prior:" % (sum(ghost.values()) / G), {k: round(v / G, 2) for k, v in ghost.most_common()})


if __name__ == "__main__":
    main()
