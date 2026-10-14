"""Summarize per-64MiB-chunk read/write/fallocate activity of inode 627902
from the captured accesslog, and freeze counts per chunk from the debug log."""
import re, sys, collections
CH = 64 << 20
op_re = re.compile(r'^(\S+ \S+) \[[^\]]*\] (read|write|fallocate) \(627902,(\d+),(\d+),(\d+)')
stats = {k: collections.defaultdict(lambda: [0, 0]) for k in ('read', 'write', 'fallocate')}
tot = collections.Counter(); totb = collections.Counter()
rw_chunks_time = collections.defaultdict(set)
for line in open('accesslog.txt', errors='replace'):
    m = op_re.match(line)
    if not m:
        continue
    op = m.group(2)
    if op == 'fallocate':
        mode, off, size = int(m.group(3)), int(m.group(4)), int(m.group(5))
    else:
        size, off = int(m.group(3)), int(m.group(4))
    c = off // CH
    stats[op][c][0] += 1; stats[op][c][1] += size
    tot[op] += 1; totb[op] += size
fz = collections.Counter(); fz_read = collections.Counter()
for line in open('log.prefix', errors='replace'):
    i = line.find('slice freeze inode=627902 ')
    if i < 0:
        continue
    c = int(re.search(r'chunk=(\d+)', line[i:]).group(1))
    fz[c] += 1
    if 'origin=vfs.Read' in line:
        fz_read[c] += 1
def top(counter, n, total):
    s = sorted(counter.items(), key=lambda kv: -kv[1])
    acc = 0; out = []
    for k, v in s[:n]:
        acc += v; out.append((k, v))
    return out, acc / total if total else 0
for op in ('read', 'write', 'fallocate'):
    cnt = {c: v[0] for c, v in stats[op].items()}
    print(f'== {op}: ops={tot[op]} bytes={totb[op]/2**20:.1f}MiB chunks={len(cnt)}')
    for n in (1, 5, 10, 20):
        _, share = top(cnt, n, tot[op])
        print(f'   top{n} chunks share of ops: {share:.1%}')
    t, _ = top(cnt, 8, tot[op])
    print('   top8 (chunk:ops, offset GiB):', [(c, v, round(c * 64 / 1024, 2)) for c, v in t])
w = set(stats['write']); r = set(stats['read'])
print('chunks written', len(w), 'read', len(r), 'both', len(w & r))
rw_reads = sum(stats['read'][c][0] for c in w & r)
print('reads landing in written chunks:', rw_reads, f'{rw_reads/tot["read"]:.1%}')
print('freeze chunks', len(fz), 'total', sum(fz.values()))
for n in (1, 5, 10):
    _, s = top(fz, n, sum(fz.values())); print(f'   freeze top{n} share {s:.1%}')
print('   freeze top8:', top(fz, 8, 1)[0])
print('   read-origin freeze top8:', top(fz_read, 8, 1)[0], 'chunks', len(fz_read))
