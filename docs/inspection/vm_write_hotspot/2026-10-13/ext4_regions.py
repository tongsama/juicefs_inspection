"""Classify every guest write of inode 627902 (accesslog, host offsets) into ext4
regions of the guest root filesystem, using qemu-img map (host -> guest offset)
and the ext4 layout reported from inside the guest.

Inputs (current directory): accesslog.txt, map.json.
Guest layout (2026-10-13/14, after a reboot): /dev/vda2 starts at 2 MiB, 4 KiB
blocks, 32768 blocks per group, flex_bg 16, jbd2 journal = inode 8 blocks
38830079-39092223, orphan file = inode 12 blocks 9293-9804, group 0 metadata
from block 1063, journald file extents from filefrag (post-reboot, so files
rotated before the reboot are not covered and fall into "other data").
"""
import bisect
import collections
import json
import re

PART, BS, GPB = 2 << 20, 4096, 32768
CH = 64 << 20
# Physical (ext4 block) ranges of the journald files, inclusive, from filefrag.
SYSJ = [(15256064, 15256064), (29565, 31611), (16332, 18379)]
USRJ = [(15240708, 15240708), (31623679, 31625725), (44838912, 44840959), (51341312, 51343359)]


def load_map(path):
    """Return data extents of qemu-img map sorted by host offset."""
    m = [e for e in json.load(open(path)) if e.get('offset') is not None and e['data']]
    m.sort(key=lambda e: e['offset'])
    return m, [e['offset'] for e in m]


MAP, HS = load_map('map.json')


def h2g(h):
    """Translate a host (qcow2 file) offset into a guest disk offset, or None."""
    i = bisect.bisect_right(HS, h) - 1
    if i >= 0 and h < MAP[i]['offset'] + MAP[i]['length']:
        return MAP[i]['start'] + (h - MAP[i]['offset'])
    return None


def inr(b, ranges):
    """Report whether block b is inside any inclusive range."""
    return any(a <= b <= z for a, z in ranges)


def region(b):
    """Name the ext4 region that owns filesystem block b."""
    if inr(b, SYSJ):
        return 'journald system.journal'
    if inr(b, USRJ):
        return 'journald user-1000.journal'
    if 9293 <= b <= 9804:
        return 'ext4 orphan file'
    if 38830079 <= b <= 39092223:
        return 'ext4 jbd2 journal'
    if b <= 1062:
        return 'superblock/GDT'
    g = b // GPB
    if g % 16 == 0:
        base = 1063 if g == 0 else g * GPB
        if base <= b < base + 16:
            return 'flex block bitmaps'
        if base + 16 <= b < base + 32:
            return 'flex inode bitmaps'
        if base + 32 <= b < base + 32 + 16 * 512:
            return 'flex inode tables'
    return 'other data'


def main():
    """Print write counts per ext4 region, per hot host chunk and per block group."""
    deltas = collections.Counter(e['offset'] - e['start'] for e in MAP)
    print('data extents', len(MAP), 'distinct host-guest deltas', len(deltas),
          'top (MiB)', [(d / 2**20, c) for d, c in deltas.most_common(5)])
    r = re.compile(r'^\S+ \S+ \[[^\]]*\] write \(627902,(\d+),(\d+),')
    cnt, byt = collections.Counter(), collections.Counter()
    hot = collections.defaultdict(collections.Counter)
    other, grp = collections.Counter(), collections.defaultdict(collections.Counter)
    for line in open('accesslog.txt', errors='replace'):
        x = r.match(line)
        if not x:
            continue
        size, off = int(x.group(1)), int(x.group(2))
        g = h2g(off)
        if g is None:
            k = 'qcow2 meta/unmapped'
        elif g < PART:
            k = 'before vda2'
        else:
            b = (g - PART) // BS
            k = region(b)
            grp[b // GPB][b] += 1
            if k == 'other data':
                other[b // GPB] += 1
        cnt[k] += 1
        byt[k] += size
        hot[off // CH][k] += 1
    total = sum(cnt.values())
    for k, v in cnt.most_common():
        print(f'{k:28s} {v:5d} ({v / total:5.1%}) {byt[k] / 2**20:7.1f} MiB')
    for h in (1, 1920, 1930, 2384, 168, 167, 151, 0):
        print(' host chunk', h, dict(hot[h]))
    print('other data by block group top:', other.most_common(8))
    for G in (0, 2, 960, 965, 1368, 1566):
        bl = sorted(grp[G])
        print(' group', G, 'writes', sum(grp[G].values()), 'blocks', bl[0], '-', bl[-1],
              'top', grp[G].most_common(3))


if __name__ == '__main__':
    main()
