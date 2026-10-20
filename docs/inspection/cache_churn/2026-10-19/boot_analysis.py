#!/usr/bin/env python3
"""VM 起動の区間に GET で補充したブロックが、以前キャッシュにあって消えたもの（追い出し）かを判定する。

key_tracker の snapshot と events から、各 key がキャッシュにあった記録をたどる。
補充（fill）は、区間内に現れた key のうち、区間開始の直前の snapshot の最大 slice id 以下のもの（既存の slice）とする。
使い方: boot_analysis.py <区間開始 'YYYY-MM-DD HH:MM:SS'> <区間終了> <直前の snapshot> [古い snapshot ...]
"""
import collections
import sys
import time

fmt = "%Y-%m-%d %H:%M:%S"
t0 = int(time.mktime(time.strptime(sys.argv[1], fmt)))
t1 = int(time.mktime(time.strptime(sys.argv[2], fmt)))
pre_snap = sys.argv[3]
old_snaps = sys.argv[4:]
G = 1 << 30
hm = lambda x: time.strftime("%m/%d %H:%M", time.localtime(x))


def load_snap(p):
    """snapshot を {key: (atime, ctime, size)} で読む。"""
    d = {}
    for i, line in enumerate(open(p)):
        if i == 0:
            continue
        k, a, c, s = line.rstrip("\n").split("\t")
        d[k] = (int(a), int(c), int(s))
    return d


pre = load_snap(pre_snap)
max_id = max(int(k.split("_")[0]) for k in pre)
seen = {}  # key -> 最後にキャッシュにあったと分かる情報 (時刻, atime)
for p in old_snaps:
    ts = int(p.rsplit("-", 1)[1].split(".")[0])
    for k, (a, c, s) in load_snap(p).items():
        seen[k] = ("snap", ts, a)
gone = {}  # key -> del の時刻と atime
fills = []
for line in open("keys-events.tsv"):
    t, kind, key, a, c, s = line.rstrip("\n").split("\t")
    t, a, s = int(t), int(a), int(s)
    if kind == "del":
        gone[key] = (t, a)
    elif t0 <= t <= t1 + 60:
        if int(key.split("_")[0]) <= max_id and key not in pre:
            fills.append((t, key, s))
    if kind == "add":
        seen.setdefault(key, ("add", t, a))

cls = collections.Counter()
evict_at = collections.Counter()
last_acc = collections.Counter()
per_min = collections.defaultdict(collections.Counter)
for t, key, s in fills:
    if key in gone and gone[key][0] < t:
        c = "evicted"
        dt, a = gone[key]
        evict_at[time.strftime("%H:00", time.localtime(dt))] += s
        last_acc[time.strftime("%m/%d %H:00", time.localtime(a))] += s
    elif key in seen:
        c = "evicted-before-tracking"  # 古い snapshot にあり、直前の snapshot には無い
    else:
        c = "never-seen"
    cls[c] += s
    per_min[time.strftime("%H:%M", time.localtime(t))][c] += s

tot = sum(cls.values())
print("window %s - %s, max slice id before: %d" % (hm(t0), hm(t1), max_id))
print("GET fills: %d files, %.2f GiB" % (len(fills), tot / G))
for c in ("evicted", "evicted-before-tracking", "never-seen"):
    print("  %-24s %6.2f GiB  %5.1f%%" % (c, cls[c] / G, 100 * cls[c] / max(tot, 1)))
print("\nwhen the evicted ones were removed (hour):", {k: round(v / G, 2) for k, v in sorted(evict_at.items())})
print("their last access before removal (hour):", {k: round(v / G, 2) for k, v in sorted(last_acc.items())})
print("\nper minute (GiB): evicted / before-tracking / never-seen")
for m in sorted(per_min):
    c = per_min[m]
    print("%s %6.2f %6.2f %6.2f" % (m, c["evicted"] / G, c["evicted-before-tracking"] / G, c["never-seen"] / G))
