#!/usr/bin/env python3
"""jfsreplay suite の回（結果のディレクトリ）を並べ、summary.md より細かい比べの表を出す。

使い方: python3 -I compare_ana.py 名前=<結果のディレクトリ> [名前=<ディレクトリ> ...]
材料はどれも各回のディレクトリの中にある記録（metrics.tsv、metrics-end-*.txt、play.json、breakdown-end-*.txt）。
試験の記録を読むだけで、何も書き換えない。
"""
import glob
import json
import os
import re
import sys
from collections import defaultdict


def load_series(path):
    """metrics.tsv（時刻、ISO 時刻、系列名、値）を {系列名: [(時刻, 値), ...]} にする。"""
    out = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 4:
                continue
            try:
                out[p[2]].append((int(p[0]), float(p[3])))
            except ValueError:
                continue
    return out


def delta(s, name):
    """カウンタの最初のサンプルから最後のサンプルまでの増分（系列が無ければ None）。
    記録の途中で初めて現れた系列（label 付きの counter は最初の増加まで出ない）は、現れる前を 0 とみなす。
    mount は回ごとに新しい process なので、記録の始まりの値は 0 か、始めからある系列の値。"""
    v = s.get(name)
    if not v:
        return None
    t0 = min(x[0][0] for x in s.values() if x)
    return v[-1][1] - (0.0 if v[0][0] > t0 else v[0][1])


def gauge_max(s, name):
    """ゲージの区間内の最大値。"""
    v = s.get(name)
    return max(x for _, x in v) if v else None


def drain_seconds(s, name, start):
    """start（記録を流し終えた時刻）から、ゲージ name が最後に 0 になるまでの秒数。0 にならなければ None。"""
    v = s.get(name) or []
    last_nonzero = None
    for t, x in v:
        if x > 0:
            last_nonzero = t
    if last_nonzero is None:
        return 0.0
    after = [t for t, x in v if t > last_nonzero and x == 0]
    return max(0.0, after[0] - start) if after else None


def end_metrics(d):
    """metrics-end-*.txt を {系列名（instance などのラベルを除く）: 値} にする。"""
    path = sorted(glob.glob(os.path.join(d, "metrics-end-*.txt")))
    out = {}
    if not path:
        return out
    with open(path[-1], encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or " " not in line:
                continue
            k, v = line.rsplit(" ", 1)
            k = re.sub(r'(instance|juicefs_version|mp|vol_name|storage_class)="[^"]*",?', "", k)
            k = k.replace(",}", "}").replace("{}", "")
            try:
                out[k] = out.get(k, 0.0) + float(v)
            except ValueError:
                pass
    return out


def breakdown(d):
    """breakdown-end-*.txt から、参照されている slice の数と、キャッシュの区分ごとの GiB を読む。"""
    path = sorted(glob.glob(os.path.join(d, "breakdown-end-*.txt")))
    out = {}
    if not path:
        return out
    text = open(path[-1], encoding="utf-8").read()
    m = re.search(r"referenced slice ids (\d+)", text)
    if m:
        out["slices"] = int(m.group(1))
    m = re.search(r"chunks scanned (\d+)", text)
    if m:
        out["chunks"] = int(m.group(1))
    for cat in ("full", "partial", "hidden", "dead", "new"):
        m = re.search(r"^%s\s+([\d.]+) GiB\s+[\d.]+%%\s+(\d+) files" % cat, text, re.M)
        if m:
            out[cat] = (float(m.group(1)), int(m.group(2)))
    return out


def ratio(a, b):
    """a / b（b が 0 か None なら None）。"""
    return a / b if a is not None and b else None


def analyze(d):
    """1 回分の値を {行の名前: 値} にする。"""
    s = load_series(os.path.join(d, "metrics.tsv"))
    play = json.load(open(os.path.join(d, "play.json"), encoding="utf-8"))
    run = json.load(open(os.path.join(d, "run.json"), encoding="utf-8"))
    em = end_metrics(d)
    bd = breakdown(d)
    r = {}
    m = 'juicefs_object_request_durations_histogram_seconds_%s{method="%s"}'
    for meth in ("PUT", "GET", "DELETE"):
        c = delta(s, m % ("count", meth))
        r[meth + " 回数"] = c
        r[meth + " 平均 ms"] = ratio((delta(s, m % ("sum", meth)) or 0) * 1000, c)
    r["PUT＋DELETE"] = (r["PUT 回数"] or 0) + (r["DELETE 回数"] or 0)
    r["PUT＋GET＋DELETE"] = r["PUT＋DELETE"] + (r["GET 回数"] or 0)
    stg = delta(s, "juicefs_staging_write_bytes")
    r["staging 書込 GiB"] = stg / 2**30 if stg else None
    r["PUT 1 回あたりの staging MiB"] = ratio(stg / 2**20 if stg else None, r["PUT 回数"])
    r["待ち行列の待ちの合計 秒"] = delta(s, "juicefs_staging_block_delay_seconds")
    r["staging の block 数 最大"] = gauge_max(s, "juicefs_staging_blocks")
    sb = gauge_max(s, "juicefs_staging_block_bytes")
    r["staging の量 最大 MiB"] = sb / 2**20 if sb is not None else None
    end_play = run["started"] + play.get("duration_s", 0)
    r["流し終えてから staging が空になるまで 秒"] = drain_seconds(s, "juicefs_staging_blocks", end_play)
    r["落ち着くまで 秒（suite の判定）"] = (play.get("settle") or {}).get("seconds")
    h, mi = delta(s, "juicefs_blockcache_hits"), delta(s, "juicefs_blockcache_miss")
    r["キャッシュ hit 率（回数）"] = ratio(h, (h or 0) + (mi or 0))
    hb, mb = delta(s, "juicefs_blockcache_hit_bytes"), delta(s, "juicefs_blockcache_miss_bytes")
    r["キャッシュ hit 率（バイト）"] = ratio(hb, (hb or 0) + (mb or 0))
    r["キャッシュ miss 回数"] = mi
    r["キャッシュ evict 回数"] = delta(s, "juicefs_blockcache_evicts")
    cw = delta(s, "juicefs_blockcache_write_bytes")
    r["キャッシュ書込 GiB"] = cw / 2**30 if cw else None
    r["FUSE ops"] = delta(s, "juicefs_fuse_ops_durations_histogram_seconds_count")
    r["FUSE 平均 ms"] = ratio((delta(s, "juicefs_fuse_ops_durations_histogram_seconds_sum") or 0) * 1000, r["FUSE ops"])
    r["meta ops"] = delta(s, "juicefs_meta_ops_durations_histogram_seconds_count")
    r["meta 平均 ms"] = ratio((delta(s, "juicefs_meta_ops_durations_histogram_seconds_sum") or 0) * 1000, r["meta ops"])
    r["transaction 回数"] = delta(s, "juicefs_transaction_durations_histogram_seconds_count")
    r["transaction 平均 ms"] = ratio((delta(s, "juicefs_transaction_durations_histogram_seconds_sum") or 0) * 1000,
                                     r["transaction 回数"])
    r["compaction 出力 slice 数"] = delta(s, "juicefs_compact_size_histogram_bytes_count")
    cs = delta(s, "juicefs_compact_size_histogram_bytes_sum")
    r["compaction 出力 GiB"] = cs / 2**30 if cs else None
    r["最後に参照されている slice 数"] = bd.get("slices")
    r["chunk 数"] = bd.get("chunks")
    r["slice ÷ chunk"] = ratio(bd.get("slices"), bd.get("chunks"))
    for cat in ("full", "partial", "hidden", "new"):
        if cat in bd:
            r["キャッシュの中身 %s GiB" % cat] = bd[cat][0]
    lat = play.get("latency") or {}
    for op in ("write", "read", "flush"):
        for k in ("p50_ms", "p99_ms", "max_ms"):
            r["%s %s" % (op, k.replace("_ms", " ms"))] = (lat.get(op) or {}).get(k)
    lag = play.get("lag") or {}
    r["予定からの遅れ p99 ms"] = lag.get("p99_ms")
    r["予定からの遅れ max ms"] = lag.get("max_ms")
    r["play のエラー"] = len(play.get("errors") or []) if isinstance(play.get("errors"), list) else play.get("errors")
    r["staging の書込エラー"] = delta(s, "juicefs_staging_block_errors")
    for k, v in sorted(em.items()):
        if k.startswith("juicefs_compaction_gc_events_total"):
            r["片付け " + k.split("{", 1)[-1].rstrip("}")] = v
    return r


def fmt(v):
    """表の値の書き方。"""
    if v is None:
        return "-"
    if isinstance(v, float):
        if v.is_integer() and (abs(v) >= 100 or v == 0):
            return "{:,}".format(int(v))
        return "{:,.3f}".format(v) if abs(v) < 1 else "{:,.1f}".format(v)
    return "{:,}".format(v) if isinstance(v, int) else str(v)


def main(argv):
    """引数の回を並べて Markdown の表を出す。"""
    runs = [a.split("=", 1) for a in argv]
    res = [(n, analyze(d)) for n, d in runs]
    keys = []
    for _, r in res:
        keys += [k for k in r if k not in keys]
    print("| 項目 | " + " | ".join(n for n, _ in res) + " |")
    print("|---|" + "---:|" * len(res))
    for k in keys:
        print("| %s | %s |" % (k, " | ".join(fmt(r.get(k)) for _, r in res)))


if __name__ == "__main__":
    main(sys.argv[1:])
