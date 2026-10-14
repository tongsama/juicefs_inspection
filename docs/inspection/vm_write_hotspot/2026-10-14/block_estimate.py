"""Estimate how many PUTs block-level compaction would save for inode 627902.

For every compaction that reached the object phase, count the 4 MiB blocks of
that chunk touched since the previous compaction of the same chunk started:
writer slices from `slice freeze` lines (chunk, off, raw_length) in the DEBUG
log and zero-range/punch fallocates from the accesslog.  Compares:
  full  : observed bytes of the compaction / 4 MiB (today's behaviour)
  range : block-aligned hull of the touched blocks (one contiguous new slice)
  block : number of touched blocks (packed new slice, one entry per block)
The first compaction of a chunk may also merge slices committed before the log
started (positions unknown); those are reported separately ("first").
Usage: block_estimate.py LOGPREFIX ACCESSLOG [END]  (END bounds the window).
"""
import collections
import math
import re
import sys

INO = '627902'
CH = 64 << 20
BLOCK = 4 << 20
FRE = re.compile(r'slice freeze inode=' + INO + r' chunk=(\d+) slice=\d+ off=(\d+) raw_length=(\d+)')
CREAD = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) phase=read')
COBJ = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) slice=\d+ phase=object slices=(\d+) bytes=(\d+)')
FAL = re.compile(r'fallocate \(' + INO + r',(\d+),(\d+),(\d+)\) - OK')


def blocks(off, length):
    """Return the block indexes inside a chunk covered by [off, off+length)."""
    if length <= 0:
        return set()
    return set(range(off // BLOCK, (off + length - 1) // BLOCK + 1))


def load_events(log, acc, end):
    """Merge freezes, fallocates and compaction phases into one time-ordered list."""
    ev = []
    for line in open(log, errors='replace'):
        if INO not in line:
            continue
        t = line[:26]
        if end and t >= end:
            break
        m = FRE.search(line)
        if m:
            ev.append((t, 'w', int(m.group(1)), blocks(int(m.group(2)), int(m.group(3)))))
            continue
        m = CREAD.search(line)
        if m:
            ev.append((t, 'r', int(m.group(1)), None))
            continue
        m = COBJ.search(line)
        if m:
            ev.append((t, 'o', int(m.group(1)), (int(m.group(2)), int(m.group(3)))))
    for line in open(acc, errors='replace'):
        m = FAL.search(line)
        if not m:
            continue
        t = line[:26].replace('.', '/', 2)
        if end and t >= end:
            continue
        off, ln = int(m.group(2)), int(m.group(3))
        # a fallocate may span chunks; split it per chunk
        while ln > 0:
            c, o = off // CH, off % CH
            n = min(ln, CH - o)
            ev.append((t, 'f', c, blocks(o, n)))
            off += n
            ln -= n
    ev.sort(key=lambda e: e[0])
    return ev


def main():
    """Replay events and print the per-mode PUT totals and distributions."""
    log, acc = sys.argv[1], sys.argv[2]
    end = sys.argv[3] if len(sys.argv) > 3 else None
    touched = collections.defaultdict(set)   # blocks touched since the current epoch
    falloc_only = collections.defaultdict(set)
    snap = {}                                 # chunk -> touched set at compaction read
    seen = set()
    rows = []
    for t, k, c, v in load_events(log, acc, end):
        if k == 'w':
            touched[c] |= v
            falloc_only[c] -= v
        elif k == 'f':
            falloc_only[c] |= v - touched[c]
            touched[c] |= v
        elif k == 'r':
            snap[c] = (touched[c], falloc_only[c])
            touched[c], falloc_only[c] = set(), set()
        elif k == 'o' and c in snap:
            tb, fo = snap.pop(c)
            nslices, nbytes = v
            first = c not in seen
            seen.add(c)
            full = math.ceil(nbytes / BLOCK)
            hull = (max(tb) - min(tb) + 1) if tb else 0
            rows.append((c, first, nslices, full, hull, len(tb), len(fo)))
    def summary(name, rs):
        """Print totals for one subset of compactions."""
        if not rs:
            return
        f = sum(r[3] for r in rs)
        h = sum(min(r[4], r[3]) for r in rs)
        b = sum(min(r[5], r[3]) for r in rs)
        fo = sum(r[6] for r in rs)
        print(f'{name}: compactions={len(rs)} full={f} range={h} ({h/f-1:+.0%}) block={b} ({b/f-1:+.0%}) falloc-only-blocks={fo}')
    summary('all', rows)
    summary('not-first', [r for r in rows if not r[1]])
    summary('first', [r for r in rows if r[1]])
    dist = collections.Counter(min(r[5], 16) for r in rows if not r[1])
    print('touched-block distribution (not-first):', dict(sorted(dist.items())))
    hd = collections.Counter(min(r[4], 16) for r in rows if not r[1])
    print('hull-block distribution (not-first):', dict(sorted(hd.items())))
    per = collections.defaultdict(list)
    for r in rows:
        per[r[0]].append(r)
    for c, rs in sorted(per.items(), key=lambda x: -len(x[1]))[:5]:
        print(f' chunk {c}: n={len(rs)} touched={[r[5] for r in rs]} hull={[r[4] for r in rs]}')


if __name__ == '__main__':
    main()
