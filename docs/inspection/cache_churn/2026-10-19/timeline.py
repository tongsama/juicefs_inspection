#!/usr/bin/env python3
"""metrics.tsv と keys-events.tsv を区間ごとに集計する（読み取りのみ）。

区間ごとに、キャッシュへの流入（write_bytes の増分）、staging・compaction の内訳、ミスの量、GET の件数、
追い出しの件数を出す。あわせて、消えたブロックのうち最終アクセスが指定時刻（既定は 10/19 の起動）以降のものと、
新しく現れたブロックのうち、以前に消えていたもの（再取得）を数える。
使い方: timeline.py [区間の分(既定10)] [起動時刻 'YYYY-MM-DD HH:MM']
"""
import collections
import sys
import time

step = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else 600
boot = int(time.mktime(time.strptime(sys.argv[2] if len(sys.argv) > 2 else "2026-10-19 09:55", "%Y-%m-%d %H:%M")))
want = {
    "juicefs_blockcache_write_bytes": "in",
    "juicefs_staging_write_bytes": "stage",
    "juicefs_compact_size_histogram_bytes_sum": "compact",
    "juicefs_blockcache_miss_bytes": "miss",
    "juicefs_object_request_durations_histogram_seconds_count{method=\"GET\"}": "get",
    "juicefs_blockcache_evicts": "evict",
    "juicefs_blockcache_hit_bytes": "hit",
}
series = collections.defaultdict(dict)
for line in open("metrics.tsv"):
    t, _, name, v = line.rstrip("\n").split("\t")
    if name in want:
        series[int(t)][want[name]] = float(v)
ts = sorted(series)
G = 1 << 30

ev_del = collections.Counter()
ev_del_boot = collections.Counter()
ev_add = collections.Counter()
ev_readd = collections.Counter()
deleted = set()
events = []
for line in open("keys-events.tsv"):
    t, kind, key, at, ct, size = line.rstrip("\n").split("\t")
    events.append((int(t), kind, key, int(at), int(size)))
for t, kind, key, at, size in events:
    b = t  # 事象の時刻そのもの（行の区間 (prev, t] に入れる）
    if kind == "del":
        deleted.add(key)
        ev_del[b] += size
        if at >= boot:
            ev_del_boot[b] += size
    else:
        ev_add[b] += size
        if key in deleted:
            ev_readd[b] += size

print("%-11s %6s %6s %6s %6s %6s %5s %6s | %6s %6s %6s %6s" % (
    "until", "in", "stage", "cmpct", "miss", "hit", "GET", "evict", "add", "del", "delBt", "readd"))
prev = ts[0]
for t in ts[1:]:
    if t // step == prev // step and t != ts[-1]:
        continue
    a, b = series[prev], series[t]
    d = lambda k: b.get(k, 0) - a.get(k, 0)
    s = lambda c: sum(v for x, v in c.items() if prev < x <= t)
    print("%-11s %6.2f %6.2f %6.2f %6.2f %6.2f %5d %6d | %6.2f %6.2f %6.2f %6.2f" % (
        time.strftime("%m/%d %H:%M", time.localtime(t)), d("in") / G, d("stage") / G, d("compact") / G,
        d("miss") / G, d("hit") / G, d("get"), d("evict"),
        s(ev_add) / G, s(ev_del) / G, s(ev_del_boot) / G, s(ev_readd) / G))
    prev = t
