"""delta_sim2 の case I/D の状態から、読み込み 1 回あたりの GET の目安を数える（読み取りのみ）。
各 compaction の直後に、(1) 差分のあるブロックごとに、見えているデータを持つ段の数、
(2) 書き込みがあった位置を 64 KiB で読むとき、重なる段の数、を数えて平均と最大を出す。"""
import collections, sys
sys.path.insert(0, ".")
import delta_sim2 as d  # 同じディレクトリの delta_sim2.py

def run(ws, k, maxrec, gap=16384, trig=85):
    """simulate と同じ手順で段を作り、段の重なりを数える。"""
    tiers, pending = [], []
    per_block, per_read = [], []
    for t, pos, ln in ws:
        pending.append((pos, pos + ln))
        if len(pending) < trig:
            continue
        tiers = d.drop_hidden(tiers + [d.merge(pending, gap)])
        if len(tiers) > k:
            tiers = [d.merge([r for tt in tiers for r in tt], gap)]
        pending = []
        if d.nrec(tiers) + d.BASE > maxrec:
            while d.nrec(tiers) + d.BASE > maxrec - 50 and tiers:
                cnt = collections.Counter(a // d.BS for tt in tiers for a, b in tt if a // d.BS == (b - 1) // d.BS)
                if not cnt: break
                blk = cnt.most_common(1)[0][0]
                tiers = [tt2 for tt2 in ([r for r in tt if not (r[0] // d.BS == blk and (r[1]-1)//d.BS == blk)] for tt in tiers) if tt2]
        blocks = collections.defaultdict(set)
        for i, tt in enumerate(tiers):
            for a, b in tt:
                for x in range(a // d.BS, (b - 1) // d.BS + 1):
                    blocks[x].add(i)
        per_block += [len(v) for v in blocks.values()]
        for a, b in pending or [r for tt in tiers[-1:] for r in tt]:
            lo = a // 65536 * 65536; hi = lo + 65536
            per_read.append(sum(1 for tt in tiers if any(x < hi and y > lo for x, y in tt)))
    return per_block, per_read

writes, _ = d.load(sys.argv[1])
for k, m in ((4, 300), (8, 500)):
    pb, pr = run(writes[1920], k, m)
    h = collections.Counter(pr)
    print("段 %d・上限 %d: ブロックごとの段の数 平均 %.1f 最大 %d／64KiB 読み込みの重なる段 平均 %.2f 最大 %d 分布 %s"
          % (k, m, sum(pb)/len(pb), max(pb), sum(pr)/len(pr), max(pr), dict(sorted(h.items()))))
