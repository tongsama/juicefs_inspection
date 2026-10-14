"""Classify slice freezes of the target inode by origin and check whether a
Read-triggered freeze was immediately followed by a contiguous slice (a write
that could have been appended to the frozen slice)."""
import re, sys, collections, datetime
KV = re.compile(r'(\w+)=(\S+)')
TS = lambda s: datetime.datetime.strptime(s, '%Y/%m/%d %H:%M:%S.%f')
win = {'boot': ('2026/10/13 20:02:00.000000', '2026/10/13 20:04:00.000000'),
       'apps': ('2026/10/13 20:04:00.000000', '2026/10/13 20:18:05.000000')}
fr = []
byino = collections.Counter()
for line in open(sys.argv[1], errors='replace'):
    i = line.find('slice freeze inode=')
    if i < 0:
        continue
    kv = dict(KV.findall(line[i:]))
    t = TS(line[:26])
    byino[kv['inode']] += 1
    if kv['inode'] != '627902':
        continue
    fr.append((t, int(kv['chunk']), int(kv['off']), int(kv['raw_length']), kv['reason'],
               kv.get('origin', '-'), int(kv['started_unix_ns'])))
print('freeze by inode (all window):', byino.most_common(5))
for name, (a, b) in win.items():
    a, b = TS(a), TS(b)
    sel = [f for f in fr if a <= f[0] < b]
    c = collections.Counter((f[4], f[5]) for f in sel)
    print('==', name, 'target freezes', len(sel))
    for k, v in c.most_common():
        print('  ', k, v)
    # continuation: a later slice in the same chunk whose off == off+raw_length,
    # started within 1s after the freeze.
    for org in ('vfs.Read.prelock', 'vfs.Read', 'vfs.Fallocate.prelock', 'none'):
        rs = [f for f in sel if f[5] == org]
        if not rs:
            continue
        hits = 0; small = 0
        for f in rs:
            end = f[2] + f[3]
            t_ns = f[0].timestamp() * 1e9
            if f[3] <= 65536:
                small += 1
            for g in fr:
                if g[1] == f[1] and g[2] == end and 0 <= g[6] - t_ns <= 1e9:
                    hits += 1
                    break
        raws = sorted(f[3] for f in rs)
        print(f'   origin={org}: n={len(rs)} raw p50={raws[len(raws)//2]} <=64KiB={small} contiguous_next_within_1s={hits}')
