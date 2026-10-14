"""Summarize a kaz block-compaction run of inode 627902 so it compares with the
2026-10-14 runs A-E (compare_runs.py, slice_fate.py, comp_puts.py).

Differences from those scripts: block compaction logs one `phase=object` line
per compaction (slice= is only the first new slice) and one
`compaction block-object ... slice=` line per new block slice, so compaction
output slices are collected from both. Totals follow the report's definition:
writer-slice PUTs of slices frozen in the window, followed to their final fate,
plus compaction PUTs in the window and the following AFTER minutes, per guest
MiB written in the window (accesslog).

The summary line gained a force= field in 5bb23494; both forms are accepted.

Usage: block_run.py LOG ACCESSLOG START END [AFTER_END]
Times are 'YYYY/MM/DD HH:MM:SS'.
"""
import collections
import re
import statistics
import sys

INO = '627902'
COBJ = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) slice=(\d+) phase=object slices=(\d+) bytes=(\d+)')
CBLK = re.compile(r'compaction block-object inode=' + INO + r' chunk=\d+ slice=(\d+) pos=\d+ bytes=(\d+)')
CSUM = re.compile(r'compaction block inode=' + INO + r' chunk=(\d+) mode=(\w+)(?: force=\w+)? removed=(\d+) kept=(\d+) blocks=(\d+) mask=(\d+) hidden=\S+ slices=(\d+) total=(\S+)')
FRZ = re.compile(r'slice freeze inode=' + INO + r' .*slice=(\d+) ')
PUT = re.compile(r'PUT chunks/\d+/\d+/(\d+)_(\d+)_\d+ .*err: <nil>')
RET = re.compile(r'compaction GC local retirement slice=(\d+) .*err=<nil>')
DEL = re.compile(r'DELETE chunks/\S+ .*err: <nil>')


def ts(s):
    """Normalize the first 19 characters of a JuiceFS or accesslog timestamp."""
    return s[:10].replace('.', '/') + s[10:19]


def secs(v):
    """Parse a Go duration such as 1.5s, 12ms or 300µs into seconds."""
    for unit, f in (('ms', 1e-3), ('µs', 1e-6), ('ns', 1e-9), ('m', 60), ('s', 1)):
        if v.endswith(unit) and (unit != 'm' or not v.endswith('ms')):
            try:
                return float(v[:-len(unit)]) * f
            except ValueError:
                pass
    return float('nan')


def main():
    """Print window counters, compaction stats, slice fates and PUT totals."""
    log, acc, a, b = sys.argv[1:5]
    after = sys.argv[5] if len(sys.argv) > 5 else None
    r = collections.Counter()
    comp, frz = set(), set()
    puts = []                 # (time, slice) of successful PUTs
    first_put, first_ret, nput = {}, {}, collections.Counter()
    starts, durs, blocks = [], [], []
    modes = collections.Counter()
    n = 0
    last = ''
    for line in open(log, errors='replace'):
        n += 1
        if len(line) < 26:
            continue
        t = ts(line)
        last = t
        m = COBJ.search(line)
        if m:
            comp.add(int(m.group(2)))
            if a <= t < b:
                r['compactions'] += 1
                starts.append(int(m.group(3)))
                r['compaction_bytes'] += int(m.group(4))
            continue
        m = CBLK.search(line)
        if m:
            comp.add(int(m.group(1)))
            continue
        m = CSUM.search(line)
        if m:
            if a <= t < b:
                modes[m.group(2)] += 1
                blocks.append(int(m.group(5)))
                durs.append(secs(m.group(8)))
                r['masks'] += int(m.group(6))
            continue
        m = FRZ.search(line)
        if m:
            if a <= t < b:
                frz.add(int(m.group(1)))
            continue
        m = PUT.search(line)
        if m:
            s = int(m.group(1))
            puts.append((t, s))
            first_put.setdefault(s, n)
            nput[s] += 1
            continue
        m = RET.search(line)
        if m:
            first_ret.setdefault(int(m.group(1)), n)
            continue
        if a <= t < b and DEL.search(line):
            r['deletes'] += 1
        if a <= t < b and 'slow compaction' in line and 'inode=' + INO in line:
            r['slow_compactions'] += 1
    for t, s in puts:
        kind = 'compaction' if s in comp else ('writer' if s in frz else 'other')
        if a <= t < b:
            r['put_' + kind] += 1
        elif after and b <= t < after and kind == 'compaction':
            r['put_compaction_after'] += 1
    up = {s for s in frz if s in first_put and (s not in first_ret or first_put[s] < first_ret[s])}
    rb = {s for s in frz if s in first_ret and (s not in first_put or first_ret[s] < first_put[s])}
    pend = frz - up - rb
    wb = 0
    for line in open(acc, errors='replace'):
        m = re.match(r'^(\S+ \S+) \[[^\]]*\] (read|write|fallocate) \(' + INO + r',(\d+),(\d+),', line)
        if not m or not (a <= ts(m.group(1)) < b):
            continue
        r['acc_' + m.group(2)] += 1
        if m.group(2) == 'write':
            wb += int(m.group(3))
    wmib = wb / 2**20
    print(f'window {a} .. {b}  log_end={last}  guest write {r["acc_write"]} ops {wmib:.1f} MiB, read {r["acc_read"]} ops, fallocate {r["acc_fallocate"]}')
    print(f' compactions={r["compactions"]} modes={dict(modes)} blocks/compaction mean={statistics.mean(blocks) if blocks else 0:.2f} '
          f'max_slices_at_compaction={max(starts) if starts else 0} compaction_bytes={r["compaction_bytes"]} masks={r["masks"]} slow={r["slow_compactions"]}')
    if durs:
        d = sorted(durs)
        print(f' compaction duration median={statistics.median(d):.2f}s p90={d[int(0.9 * (len(d) - 1))]:.2f}s max={d[-1]:.2f}s')
    print(f' PUT in window: writer={r["put_writer"]} compaction={r["put_compaction"]} other={r["put_other"]}; compaction PUT after window={r["put_compaction_after"]}; deletes={r["deletes"]}')
    wput = sum(nput[s] for s in up)
    print(f' frozen={len(frz)} uploaded={len(up)} ({len(up) / max(len(frz), 1):.0%}) writer_PUTs={wput} '
          f'retired_before_upload={len(rb)} ({len(rb) / max(len(frz), 1):.0%}) still_pending={len(pend)}')
    if wmib:
        cp = r['put_compaction'] + r['put_compaction_after']
        print(f' per guest MiB: writer_put={wput / wmib:.1f} compaction_put={cp / wmib:.1f} total={(wput + cp) / wmib:.1f}')


if __name__ == '__main__':
    main()
