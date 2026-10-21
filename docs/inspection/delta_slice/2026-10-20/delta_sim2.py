#!/usr/bin/env python3
"""差分 slice 方式の変形（段分け・部分畳み込み・record 上限）を、write_watch.py の記録で模擬する（読み取りのみ）。

モデル（chunk ごと、書き込みを時刻順に適用）:
- chunk は「base（4 MiB ブロックの record、数は数えない）」の上に、差分の段（tier）が古い順に重なる。
  各段は範囲の列で、範囲 1 つが record 1 つ。後の段に完全に隠れた record は compaction のときに外す。
- 新しい書き込みの record が TRIGGER 個たまったら compaction を 1 回行う。
  - resend: 差分を 1 段だけ持ち、毎回その段全体（見えている範囲＋新しい書き込み）を送り直す。
  - tier:   新しい書き込みだけを gap でつないで新しい段として PUT する。段が K を超えたら、全段を 1 段にまとめ直す
            （PUT は差分の見えている範囲の合計）。
- record（差分の record ＋ base の 16）が MAXREC を超えたら畳み込む。
  - full:    差分が触れたブロックを全部 4 MiB ずつ書き直し、差分を空にする（今の見積もりと同じ）。
  - partial: 差分の record が一番多いブロックから 1 つずつ書き直し、record が LOW 以下になるまで続ける。
             書き直したブロックの中だけにある record を外す（ブロックをまたぐ record は残る）。
- resend のときは、差分の大きさが FOLD（4 MiB）を超えても畳み込む（今の見積もりと同じ）。
出力: PUT の量と回数、1 回の compaction の最大 PUT、record の最大と時間平均、段の最大（読み込みの GET の目安）。
使い方: delta_sim2.py writes.tsv
"""
import collections
import sys

BS = 4 << 20
BASE = 16


def merge(ranges, gap):
    """範囲の列を開始順に並べ、隙間 gap 以下をつなげた列にして返す。"""
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def covered(r, cov):
    """範囲 r が、つなげ済みの範囲の列 cov のどれか 1 つに完全に含まれるか。"""
    return any(a <= r[0] and r[1] <= b for a, b in cov)


def visible_bytes(tiers):
    """全段の和集合のバイト数（まとめ直しや送り直しで PUT する量）。"""
    return sum(b - a for a, b in merge([r for t in tiers for r in t], 0))


def drop_hidden(tiers):
    """後の段に完全に隠れた record を外す。"""
    out = []
    later = []
    for t in reversed(tiers):
        cov = merge(later, 0)
        keep = [r for r in t if not covered(r, cov)]
        later += t
        if keep:
            out.append(keep)
    return list(reversed(out))


def nrec(tiers):
    """差分の record の数。"""
    return sum(len(t) for t in tiers)


def simulate(ws, mode, gap, trigger, maxrec, fold_mode, k=4, low_margin=50, fold_bytes=4 << 20):
    """1 つの chunk の書き込み列 ws=[(t,pos,len)] を模擬し、量を返す。"""
    tiers, pending = [], []
    st = collections.Counter()
    peak_rec = peak_tiers = max_put = 0
    rec_time = 0.0
    last_t = ws[0][0] if ws else 0
    for t, pos, ln in ws:
        rec_time += (nrec(tiers) + len(pending) + BASE) * (t - last_t)
        last_t = t
        pending.append((pos, pos + ln))
        peak_rec = max(peak_rec, nrec(tiers) + len(pending) + BASE)
        if len(pending) < trigger:
            continue
        put = 0
        if mode == "resend":
            tiers = [merge([r for tt in tiers for r in tt] + pending, gap)]
            d = sum(b - a for a, b in tiers[0])
            if d > fold_bytes:
                blocks = {x // BS for a, b in tiers[0] for x in range(a // BS * BS, b, BS)}
                put += len(blocks) * BS
                st["puts"] += len(blocks)
                st["folds"] += 1
                tiers = []
            else:
                put += d
                st["puts"] += max(1, -(-d // BS))
        else:
            new = merge(pending, gap)
            put += sum(b - a for a, b in new)
            st["puts"] += 1
            tiers = drop_hidden(tiers + [new])
            if len(tiers) > k:
                d = visible_bytes(tiers)
                put += d
                st["puts"] += max(1, -(-d // BS))
                st["merges"] += 1
                tiers = [merge([r for tt in tiers for r in tt], gap)]
        pending = []
        # record の上限による畳み込み
        if nrec(tiers) + BASE > maxrec:
            st["folds"] += 1
            if fold_mode == "full":
                blocks = {x // BS for tt in tiers for a, b in tt for x in range(a // BS * BS, b, BS)}
                put += len(blocks) * BS
                st["puts"] += len(blocks)
                st["fold_blocks"] += len(blocks)
                tiers = []
            else:
                while nrec(tiers) + BASE > maxrec - low_margin and tiers:
                    cnt = collections.Counter()
                    for tt in tiers:
                        for a, b in tt:
                            if a // BS == (b - 1) // BS:
                                cnt[a // BS] += 1
                    if not cnt:
                        break
                    blk = cnt.most_common(1)[0][0]
                    put += BS
                    st["puts"] += 1
                    st["fold_blocks"] += 1
                    tiers = [[r for r in tt if not (r[0] // BS == blk and (r[1] - 1) // BS == blk)] for tt in tiers]
                    tiers = [tt for tt in tiers if tt]
        st["put_bytes"] += put
        st["compactions"] += 1
        max_put = max(max_put, put)
        peak_tiers = max(peak_tiers, len(tiers))
    dur = max(1, ws[-1][0] - ws[0][0]) if ws else 1
    return dict(put_mib=st["put_bytes"] / 2**20, puts=st["puts"], comp=st["compactions"], folds=st["folds"],
                fold_blocks=st["fold_blocks"], merges=st["merges"], max_put_mib=max_put / 2**20,
                peak_rec=peak_rec, avg_rec=rec_time / dur, peak_tiers=peak_tiers)


def load(path):
    """write_watch.py の記録を読み、chunk ごとの書き込み列と今の方式の実績の量を返す。"""
    writes = collections.defaultdict(list)
    actual = collections.Counter()
    for line in open(path):
        if line.startswith("#"):
            continue
        t, ch, kind, pos, sid, size, off, ln, nrec_ = line.rstrip("\n").split("\t")
        t, ch, pos, ln = int(t), int(ch), int(pos), int(ln)
        if kind == "write" and ln < (1 << 20):
            writes[ch].append((t, pos, ln))
        else:
            actual[ch] += ln
    return writes, actual


CASES = [
    # (名前, mode, gap, trigger, maxrec, fold_mode, k)
    ("A 今の見積もり resend/full 300", "resend", 16384, 85, 300, "full", 0),
    ("B resend/partial 300", "resend", 16384, 85, 300, "partial", 0),
    ("C tier4/full 300", "tier", 16384, 85, 300, "full", 4),
    ("D tier4/partial 300", "tier", 16384, 85, 300, "partial", 4),
    ("E tier8/partial 300", "tier", 16384, 85, 300, "partial", 8),
    ("F tier4/partial 500", "tier", 16384, 85, 500, "partial", 4),
    ("G tier4/partial 800", "tier", 16384, 85, 800, "partial", 4),
    ("H tier4/partial 500 gap4K", "tier", 4096, 85, 500, "partial", 4),
    ("I tier8/partial 500", "tier", 16384, 85, 500, "partial", 8),
    ("J resend/partial 500", "resend", 16384, 85, 500, "partial", 0),
]


def main():
    """全 case を chunk 1920 と全 chunk の合計で表示する。"""
    writes, actual = load(sys.argv[1])
    t0 = min(w[0][0] for w in writes.values())
    t1 = max(w[-1][0] for w in writes.values())
    print("期間 %.1f 分、chunk %d 個、今の方式の実績: chunk 1920 %.0f MiB、合計 %.0f MiB"
          % ((t1 - t0) / 60, len(writes), actual[1920] / 2**20, sum(actual.values()) / 2**20))
    hdr = "%-30s %9s %6s %6s %7s %8s %8s %7s %7s %5s" % (
        "case", "PUT MiB", "PUT回", "畳込", "畳込blk", "最大PUT", "rec最大", "rec平均", "まとめ", "段")
    for label, chs in (("chunk 1920", [1920]), ("全 chunk 合計（rec は最大の chunk）", sorted(writes))):
        print("\n== %s ==" % label)
        print(hdr)
        for name, mode, gap, trig, maxrec, fm, k in CASES:
            agg = collections.Counter()
            pk = collections.Counter()
            for ch in chs:
                r = simulate(writes[ch], mode, gap, trig, maxrec, fm, k=k or 4)
                for key in ("put_mib", "puts", "folds", "fold_blocks", "merges"):
                    agg[key] += r[key]
                for key in ("max_put_mib", "peak_rec", "avg_rec", "peak_tiers"):
                    pk[key] = max(pk[key], r[key])
            print("%-30s %9.0f %6d %6d %7d %8.1f %8d %7.0f %7d %5d" % (
                name, agg["put_mib"], agg["puts"], agg["folds"], agg["fold_blocks"], pk["max_put_mib"],
                pk["peak_rec"], pk["avg_rec"], agg["merges"], pk["peak_tiers"]))


if __name__ == "__main__":
    main()
