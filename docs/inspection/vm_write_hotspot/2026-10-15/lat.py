"""Guest I/O latency (accesslog) and freeze reasons (DEBUG log) for one inode and window.
usage: lat.py INO ACCESSLOG START END [LOG]"""
import re, sys, collections, statistics
ino, acc, a, b = sys.argv[1:5]
lat = collections.defaultdict(list)
R = re.compile(r'^(\S+ \S+) \[[^\]]*\] (read|write|fallocate|fsync|flush) \(' + ino + r'[,)].*<([\d.]+)>')
for l in open(acc, errors='replace'):
    m = R.match(l)
    if not m: continue
    t = m.group(1)[:19].replace('.', '/', 2)
    if a <= t < b:
        lat[m.group(2)].append(float(m.group(3)) * 1000)
for op, v in sorted(lat.items()):
    v.sort()
    print(f'{op}: n={len(v)} mean={statistics.mean(v):.1f}ms p50={v[len(v)//2]:.2f} p99={v[int(len(v)*.99)]:.1f} max={v[-1]:.0f} over1s={sum(x>1000 for x in v)} total={sum(v)/1000:.1f}s')
if len(sys.argv) > 5:
    c = collections.Counter()
    F = re.compile(r'slice freeze inode=' + ino + r' .*reason=(\S+) .*origin=(\S+)')
    for l in open(sys.argv[5], errors='replace'):
        if l[:19] < a or l[:19] >= b or 'slice freeze inode=' + ino + ' ' not in l: continue
        m = F.search(l)
        if m: c[(m.group(1), m.group(2).split('.prelock')[0])] += 1
    n = sum(c.values()); print('freeze', n, {f'{k[0]}/{k[1]}': f'{v} ({v/n:.0%})' for k, v in c.most_common()})
