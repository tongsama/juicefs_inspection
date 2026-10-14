"""Summarize one measurement window of inode 627902 (DEBUG log + accesslog) so
that runs of different lengths can be compared per minute and per guest MiB.

Usage: compare_runs.py LOG ACCESSLOG START END [START END ...]
Times are 'YYYY/MM/DD HH:MM:SS'.
"""
import collections
import re
import sys

INO = '627902'


def ts(s):
    """Normalize the first 19 characters of a JuiceFS or accesslog timestamp."""
    return s[:10].replace('.', '/') + s[10:19]


def summarize(log, acc, a, b):
    """Return counters for the window [a, b)."""
    r = collections.Counter()
    put_re = re.compile(r'PUT chunks/\d+/\d+/(\d+)_(\d+)_(\d+) .*err: <nil>')
    del_re = re.compile(r'DELETE chunks/\S+ .*err: <nil>')
    cobj = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) slice=(\d+) phase=object slices=(\d+) bytes=(\d+)')
    frz = re.compile(r'slice freeze inode=' + INO + r' .*slice=(\d+) .*raw_length=(\d+) reason=(\S+).*?(?:origin=(\S+))?')
    flush = re.compile(r'writer flush inode=' + INO + r' .*phase=end .*origin=(vfs\.Read\S*) .*elapsed=(\S+)')
    comp_slices, writer_slices = set(), set()
    starts = []
    waits = []
    puts = []
    for line in open(log, errors='replace'):
        if len(line) < 26:
            continue
        t = ts(line)
        m = cobj.search(line)
        if m:
            comp_slices.add(int(m.group(2)))
            if a <= t < b:
                r['compactions'] += 1
                r['compaction_bytes'] += int(m.group(4))
                r['compaction_merged'] += int(m.group(3))
                starts.append(int(m.group(3)))
            continue
        m = frz.search(line)
        if m:
            writer_slices.add(int(m.group(1)))
            if a <= t < b:
                r['freezes'] += 1
                o = re.search(r'origin=(\S+)', line)
                r['freeze_origin_' + (o.group(1) if o else 'none')] += 1
            continue
        if not (a <= t < b):
            continue
        if 'phase=compact slices=' in line and 'inode=' + INO in line:
            r['sync_compactions'] += 1
        m = put_re.search(line)
        if m and line.find('PUT chunks/') >= 0:
            puts.append(int(m.group(1)))
            continue
        if del_re.search(line):
            r['deletes'] += 1
            continue
        m = flush.search(line)
        if m:
            v = m.group(2)
            sec = float(v[:-2]) / 1e3 if v.endswith('ms') else float(v[:-2]) / 1e6 if v.endswith('µs') else float(v[:-2]) / 1e9 if v.endswith('ns') else float(v[:-1])
            waits.append(sec)
    for sid in puts:
        r['put'] += 1
        r['put_compaction' if sid in comp_slices else ('put_writer' if sid in writer_slices else 'put_other')] += 1
    for line in open(acc, errors='replace'):
        m = re.match(r'^(\S+ \S+) \[[^\]]*\] (read|write|fallocate) \(' + INO + r',(\d+),(\d+),', line)
        if not m or not (a <= ts(m.group(1)) < b):
            continue
        op = m.group(2)
        r['acc_' + op] += 1
        if op != 'fallocate':
            r['acc_' + op + '_bytes'] += int(m.group(3))
    waits.sort()
    r['read_flush_sum_s'] = round(sum(waits), 1)
    r['read_flush_p99_ms'] = round(waits[int(0.99 * len(waits))] * 1e3, 1) if waits else 0
    r['max_slices_at_compaction'] = max(starts) if starts else 0
    return r


def minutes(a, b):
    """Return the length of [a, b) in minutes."""
    import datetime
    f = lambda s: datetime.datetime.strptime(s, '%Y/%m/%d %H:%M:%S')
    return (f(b) - f(a)).total_seconds() / 60


def main():
    """Print the summary and normalized rates for each window."""
    log, acc = sys.argv[1], sys.argv[2]
    wins = sys.argv[3:]
    for i in range(0, len(wins), 2):
        a, b = wins[i], wins[i + 1]
        r = summarize(log, acc, a, b)
        mins = minutes(a, b)
        wmib = r['acc_write_bytes'] / 2**20
        print(f'== {a} .. {b} ({mins:.1f} min) guest write {r["acc_write"]} ops {wmib:.1f} MiB, read {r["acc_read"]} ops, fallocate {r["acc_fallocate"]}')
        for k in sorted(r):
            print(f'   {k} = {r[k]}')
        if wmib:
            print(f'   per guest MiB: compaction_GiB={r["compaction_bytes"] / 2**30 / wmib:.4f} put={r["put"] / wmib:.1f} put_compaction={r["put_compaction"] / wmib:.1f} deletes={r["deletes"] / wmib:.1f}')
        print(f'   per minute: compactions={r["compactions"] / mins:.2f} put={r["put"] / mins:.1f} put_compaction={r["put_compaction"] / mins:.1f} deletes={r["deletes"] / mins:.1f} guest_write_ops={r["acc_write"] / mins:.0f}')


if __name__ == '__main__':
    main()
