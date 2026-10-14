"""Estimate how PUT count would change with an 8MiB block size, grouping the
observed PUTs (20:02-20:18:05) by slice and source (compaction or writer)."""
import re, collections
M4, M8 = 4 << 20, 8 << 20
put = re.compile(r'^(\S+ \S+) .*?PUT chunks/\d+/\d+/(\d+)_(\d+)_(\d+) .*err: <nil>')
comp = re.compile(r'compaction inode=\d+ chunk=\d+ slice=(\d+) phase=object slices=\d+ bytes=(\d+)')
frz = re.compile(r'slice freeze inode=\d+ .*slice=(\d+) off=\d+ raw_length=(\d+)')
A, B = '2026/10/13 20:02:00', '2026/10/13 20:18:05'
comp_ids, writer_len = {}, {}
blocks = collections.defaultdict(dict)  # slice -> {indx: size}
for line in open('log.prefix', errors='replace'):
    m = comp.search(line)
    if m: comp_ids[int(m.group(1))] = int(m.group(2)); continue
    m = frz.search(line)
    if m: writer_len[int(m.group(1))] = int(m.group(2)); continue
    m = put.match(line)
    if m and A <= m.group(1)[:19] < B:
        blocks[int(m.group(2))][int(m.group(3))] = int(m.group(4))
res = collections.defaultdict(lambda: [0, 0, 0, collections.Counter()])
for sid, bl in blocks.items():
    src = 'compaction' if sid in comp_ids else ('writer' if sid in writer_len else 'unknown(before window/staging)')
    mx = max(bl)
    L = mx * M4 + bl[mx]                 # slice length implied by last block
    n8 = -(-L // M8)
    r = res[src]
    r[0] += 1; r[1] += len(bl); r[2] += n8
    r[3]['<=4MiB' if L <= M4 else ('4-8MiB' if L <= M8 else ('8-32MiB' if L <= 32 << 20 else '>32MiB'))] += 1
tot4 = tot8 = 0
for src, (ns, n4, n8, dist) in res.items():
    tot4 += n4; tot8 += n8
    print(f'{src:32s} slices={ns:5d} PUT@4MiB={n4:5d} PUT@8MiB(est)={n8:5d} ({(n8-n4)/n4:+.0%}) len dist={dict(dist)}')
print(f'total PUT@4MiB={tot4} est@8MiB={tot8} ({(tot8-tot4)/tot4:+.1%})')
