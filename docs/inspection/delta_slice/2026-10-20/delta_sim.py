#!/usr/bin/env python3
"""write_watch.py の記録から、差分 slice 方式で compaction した場合の量を見積もる（読み取りのみ）。

chunk ごとに、書き込みの record（種別 write）を時刻順に適用して、次を模擬する。
- 差分: 書かれたバイト範囲の和集合（同じ位置の書き直しは 1 回分）。隙間が gap 以下の範囲はつなげる
  （つなげた隙間は元のデータをコピーするので、差分の大きさに含める）。
- 前回の差分 compaction 以降の新しい書き込みの record が TRIGGER 個たまったら、差分 compaction を 1 回行い、
  差分全体を 1 つの slice として PUT し直す（record の総数ではなく新しい record の数で起動する。総数で起動すると、
  差分の範囲の数が多いときに毎回の書き込みで起動してしまうため）。record の総数は「範囲の数」として別に出す。
- 差分の大きさが FOLD を超えるか、差分の範囲の数が MAXRUNS を超えたら、今の方式と同じく
  差分が触れたブロックを 4 MiB ずつ書き直して差分を空にする（畳み込み）。
今の方式の実績として、同じ期間に観測した compaction の出力（種別 compact）の合計も並べる。
1 MiB 以上の record は書き込みではなく compaction の出力として扱う。
使い方: delta_sim.py writes.tsv [--gap 0,4096,16384,65536] [--trigger 99] [--fold-mib 4] [--maxruns 300]
"""
import argparse
import collections

BS = 4 << 20
BASE = 15


def merge(runs, s, e, gap):
    """範囲の和集合 runs（開始順の [s,e) のリスト）に [s,e) を足し、隙間 gap 以下はつなげて返す。"""
    out = []
    for a, b in sorted(runs + [(s, e)]):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def simulate(ws, gap, trigger, fold, maxruns):
    """1 つの chunk の書き込み列 ws=[(t,pos,len)] に差分方式を当てはめ、量を返す。"""
    runs, new = [], 0
    put_bytes = put_cnt = folds = compactions = 0
    peak_d = peak_n = 0
    for t, pos, ln in ws:
        runs = merge(runs, pos, pos + ln, gap)
        new += 1
        if new >= trigger:
            d = sum(b - a for a, b in runs)
            if d > fold or len(runs) > maxruns:
                blocks = {x // BS for a, b in runs for x in range(a // BS * BS, b, BS)}
                put_bytes += len(blocks) * BS
                put_cnt += len(blocks)
                folds += 1
                runs = []
            else:
                put_bytes += d
                put_cnt += max(1, -(-d // BS))
            compactions += 1
            new = 0
        peak_d = max(peak_d, sum(b - a for a, b in runs))
        peak_n = max(peak_n, len(runs))
    return dict(put_mib=put_bytes / 2**20, puts=put_cnt, compactions=compactions, folds=folds,
                peak_delta_kib=peak_d / 1024, peak_runs=peak_n,
                end_delta_kib=sum(b - a for a, b in runs) / 1024, end_runs=len(runs))


def main():
    """記録を読み、chunk ごと・gap ごとの見積もりと、今の方式の実績を表示する。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("tsv")
    ap.add_argument("--gap", default="0,4096,16384,65536")
    ap.add_argument("--trigger", type=int, default=99)
    ap.add_argument("--fold-mib", type=float, default=4)
    ap.add_argument("--maxruns", type=int, default=300)
    a = ap.parse_args()
    writes = collections.defaultdict(list)
    actual = collections.Counter()
    actual_n = collections.Counter()
    t0 = t1 = None
    for line in open(a.tsv):
        if line.startswith("#"):
            continue
        t, ch, kind, pos, sid, size, off, ln, nrec = line.rstrip("\n").split("\t")
        t, ch, pos, ln = int(t), int(ch), int(pos), int(ln)
        t0 = t if t0 is None else min(t0, t)
        t1 = t if t1 is None else max(t1, t)
        # 1 MiB 以上の record は compaction の出力とみなす（write_watch の位置による分類は
        # kaz の置き換えでは出力を末尾側に置くことがあり、当てにならないため）
        if kind == "write" and ln < (1 << 20):
            writes[ch].append((t, pos, ln))
        else:
            actual[ch] += ln
            actual_n[ch] += 1
    if t0 is None:
        print("no records yet")
        return
    print("期間 %.1f 分、chunk %d 個" % ((t1 - t0) / 60, len(writes)))
    gaps = [int(g) for g in a.gap.split(",")]
    tot = collections.defaultdict(collections.Counter)
    for ch in sorted(writes, key=lambda c: -len(writes[c])):
        ws = writes[ch]
        wbytes = sum(x[2] for x in ws)
        print("\nchunk %d: 書き込み %d 個 %.0f KiB、今の方式の実績 compaction 出力 %.1f MiB（record %d）"
              % (ch, len(ws), wbytes / 1024, actual[ch] / 2**20, actual_n[ch]))
        tot["now"]["put_mib"] += actual[ch] / 2**20
        tot["now"]["write_kib"] += wbytes / 1024
        for g in gaps:
            r = simulate(ws, g, a.trigger, a.fold_mib * 2**20, a.maxruns)
            print("  gap %6d: PUT %.2f MiB / %d 回、compaction %d（畳み込み %d）、差分 最大 %.0f KiB・範囲 最大 %d 個、終了時 %.0f KiB・%d 個"
                  % (g, r["put_mib"], r["puts"], r["compactions"], r["folds"], r["peak_delta_kib"], r["peak_runs"], r["end_delta_kib"], r["end_runs"]))
            tot[g]["put_mib"] += r["put_mib"]
            tot[g]["puts"] += r["puts"]
    print("\n合計: 書き込み %.0f KiB、今の方式の実績 %.1f MiB" % (tot["now"]["write_kib"], tot["now"]["put_mib"]))
    for g in gaps:
        print("  gap %6d: 差分方式 PUT %.1f MiB / %d 回" % (g, tot[g]["put_mib"], tot[g]["puts"]))


if __name__ == "__main__":
    main()
