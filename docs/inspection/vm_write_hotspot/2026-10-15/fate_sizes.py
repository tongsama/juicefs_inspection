"""Split frozen writer slices of one inode (env JFS_INO, default 627902) in [A, B) by raw length and fate
(uploaded before retirement, or retired before any PUT) to see whether large
slices kept by block compaction drive uploads. Usage: fate_sizes.py LOG A B"""
import collections, os, re, sys
INO = os.environ.get('JFS_INO', '627902')  # 2026-10-15: raw image support
log, a, b = sys.argv[1:4]
fre = re.compile(r'slice freeze inode=' + INO + r' .*slice=(\d+) off=\d+ raw_length=(\d+)')
pre = re.compile(r'PUT chunks/\d+/\d+/(\d+)_\d+_\d+ .*err: <nil>')
rre = re.compile(r'compaction GC local retirement slice=(\d+) .*err=<nil>')
size, fp, fr = {}, {}, {}
n = 0
for l in open(log, errors='replace'):
    n += 1
    t = l[:19]
    m = fre.search(l)
    if m:
        if a <= t < b:
            size[int(m.group(1))] = int(m.group(2))
        continue
    m = pre.search(l)
    if m:
        fp.setdefault(int(m.group(1)), n); continue
    m = rre.search(l)
    if m:
        fr.setdefault(int(m.group(1)), n)
bins = [(0, 64 << 10, '<64K'), (64 << 10, 1 << 20, '64K-1M'), (1 << 20, 4 << 20, '1M-4M'), (4 << 20, 1 << 40, '>=4M')]
c = collections.Counter(); byt = collections.Counter()
for s, L in size.items():
    lab = next(x[2] for x in bins if x[0] <= L < x[1])
    up = s in fp and (s not in fr or fp[s] < fr[s])
    rb = s in fr and (s not in fp or fr[s] < fp[s])
    k = 'up' if up else 'ret' if rb else 'pend'
    c[(lab, k)] += 1; byt[(lab, k)] += L
for _, _, lab in bins:
    print(f'{lab:7s} uploaded={c[(lab,"up")]:6d} ({byt[(lab,"up")]/2**20:8.1f} MiB) retired={c[(lab,"ret")]:6d} ({byt[(lab,"ret")]/2**20:8.1f} MiB) pending={c[(lab,"pend")]}')
