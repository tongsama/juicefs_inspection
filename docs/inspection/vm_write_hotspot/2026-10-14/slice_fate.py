"""For writer slices of inode 627902 frozen before END, report their eventual
fate over the whole log: uploaded (PUT) or retired by compaction before any PUT."""
import re, sys
log, end = sys.argv[1], sys.argv[2]
fre = re.compile(r'slice freeze inode=627902 .*slice=(\d+) ')
pre = re.compile(r'PUT chunks/\d+/\d+/(\d+)_(\d+)_\d+ .*err: <nil>')
rre = re.compile(r'compaction GC local retirement slice=(\d+) .*err=<nil>')
frz, first_put, first_ret, puts = set(), {}, {}, {}
n = 0
last = ''
for l in open(log, errors='replace'):
    n += 1
    m = fre.search(l)
    if m:
        if l[:19] < end:
            frz.add(int(m.group(1)))
        continue
    m = pre.search(l)
    if m:
        s = int(m.group(1))
        first_put.setdefault(s, n)
        puts[s] = puts.get(s, 0) + 1
        last = l[:19]
        continue
    m = rre.search(l)
    if m:
        first_ret.setdefault(int(m.group(1)), n)
up = {s for s in frz if s in first_put and (s not in first_ret or first_put[s] < first_ret[s])}
rb = {s for s in frz if s in first_ret and (s not in first_put or first_ret[s] < first_put[s])}
pend = frz - up - rb
print(f'frozen={len(frz)} uploaded={len(up)} ({len(up)/len(frz):.0%}) PUTs={sum(puts[s] for s in up)} retired_before_upload={len(rb)} ({len(rb)/len(frz):.0%}) still_pending={len(pend)} log_end={last}')
