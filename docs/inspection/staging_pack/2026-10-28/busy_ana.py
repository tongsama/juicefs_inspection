"""pack の debug log から、片付けで in-flight になった slice の経緯（commit、PUT、同じ chunk の pack）を集計する。"""
import re, sys, collections, datetime
log = sys.argv[1]
ts_re = re.compile(r'^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\.\d+)')
def ts(line):
    m = ts_re.match(line); return datetime.datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S.%f").timestamp() if m else None
commit = {}; loc = {}; put = {}; busy = {}; packs = collections.defaultdict(list); ok_ret = {}
packsrc_chunks = 0
for line in open(log, errors="replace"):
    if "slice commit inode=" in line:
        m = re.search(r'inode=(\d+) chunk=(\d+) slice=(\d+)', line)
        if m: commit[int(m.group(3))] = ts(line); loc[int(m.group(3))] = (int(m.group(1)), int(m.group(2)))
    elif "<DEBUG>: PUT chunks/" in line:
        m = re.search(r'PUT chunks/\d+/\d+/(\d+)_0_\d+ .*cost: ([\d.]+)(m?s)', line)
        if m:
            c = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
            t = ts(line); put[int(m.group(1))] = (t - c, t)
    elif "compaction GC local retirement" in line:
        m = re.search(r'slice=(\d+) size=\d+ err=(.*?)(;|$)', line)
        if m:
            (ok_ret if m.group(2).startswith("<nil>") else busy)[int(m.group(1))] = ts(line)
    elif "staging pack inode=" in line:
        m = re.search(r'inode=(\d+) chunk=(\d+) slice=(\d+)', line)
        if m: packs[(int(m.group(1)), int(m.group(2)))].append(ts(line))
def q(xs):
    xs = sorted(xs); n = len(xs)
    return "n=%d p10=%.1f p50=%.1f p90=%.1f" % (n, xs[n//10], xs[n//2], xs[n*9//10]) if n else "n=0"
age, inflight_age, near_pack, retire_vs_putend = [], [], 0, []
for s, t in busy.items():
    if s in commit: age.append(t - commit[s])
    if s in put:
        inflight_age.append(t - put[s][0]); retire_vs_putend.append(put[s][1] - t)
    l = loc.get(s)
    if l and any(0 <= t - p <= 10 for p in packs.get(l, [])): near_pack += 1
print("busy", len(busy), "ok", len(ok_ret))
print("commit→片付け 秒", q(age))
print("PUT 開始→片付け 秒", q(inflight_age))
print("片付け→PUT 終了 秒", q(retire_vs_putend))
print("片付けの前 10 秒以内に同じ chunk で pack があった", near_pack)
okage = [t - commit[s] for s, t in ok_ret.items() if s in commit]
print("成功した片付けの commit→片付け 秒", q(okage))
put_single = [s for s in put if s in commit]
print("PUT した slice（commit のあるもの）", len(put_single), "うち後で片付けたもの", sum(1 for s in put_single if s in ok_ret or s in busy))
