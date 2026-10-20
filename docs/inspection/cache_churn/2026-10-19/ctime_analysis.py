#!/usr/bin/env python3
"""cache_breakdown.py の files CSV から、キャッシュのファイルが作られた時刻（ctime）を時間帯ごとに集計する。

ctime の時点ですでに存在した slice（その時点までに見えた最大の slice id より十分小さい id）のファイルは、
read のミスで GET して補充したもの（fill）とみなし、新しい slice（書き込み・compaction の出力）と分ける。
最終アクセス（atime）の時間帯も合わせて出す。
"""
import csv
import collections
import sys
import time

rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
for r in rows:
    for k in ("id", "indx", "size", "real", "atime", "ctime"):
        r[k] = int(r[k])
rows.sort(key=lambda r: r["ctime"])

LAG = 600  # この秒数より前に作られたファイルの最大 id を、その時点の「既存の slice の上限」とする
hist = []  # (ctime, running max id)
fill = collections.defaultdict(collections.Counter)
newer = collections.defaultdict(collections.Counter)
mx, j = 0, 0
for r in rows:
    while j < len(rows) and rows[j]["ctime"] < r["ctime"] - LAG:
        mx = max(mx, rows[j]["id"])
        j += 1
    hour = time.strftime("%m/%d %H", time.localtime(r["ctime"]))
    target = fill if mx and r["id"] < mx else newer
    target[hour]["bytes"] += r["real"]
    target[hour]["files"] += 1
    target[hour]["inode634040"] += r["real"] if "634040" in r["inodes"] else 0
    r["kind"] = "fill" if target is fill else "new"

G = 1 << 30
print("== cache files by creation hour (ctime): fill = old slice (GET), new = new slice ==")
print("hour        fill_GiB files  new_GiB files")
for h in sorted(set(fill) | set(newer)):
    print("%-10s %8.2f %5d %8.2f %5d" % (h, fill[h]["bytes"] / G, fill[h]["files"], newer[h]["bytes"] / G, newer[h]["files"]))

print("\n== fill files by last access hour (atime) ==")
acc = collections.Counter()
for r in rows:
    if r["kind"] == "fill":
        acc[time.strftime("%m/%d %H", time.localtime(r["atime"]))] += r["real"]
for h in sorted(acc):
    print("%-10s %8.2f" % (h, acc[h] / G))

print("\n== all files by last access hour (atime), by kind ==")
acc2 = collections.defaultdict(collections.Counter)
for r in rows:
    acc2[time.strftime("%m/%d %H", time.localtime(r["atime"]))][r["kind"]] += r["real"]
for h in sorted(acc2):
    print("%-10s fill %6.2f  new %6.2f" % (h, acc2[h]["fill"] / G, acc2[h]["new"] / G))
