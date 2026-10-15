"""Estimate what --slice-flush-idle 31s (with --slice-flush-wait 60s) would have
changed for idle-frozen slices of the target inode in the 2026-10-13 log.

For each slice that was frozen with reason=idle, keep it open in a counterfactual:
later writes (accesslog) to the same chunk whose position falls in the writable
tail range of findWritableSlice (pos in [off+flushoff, off+slen]) are absorbed,
extending the slice and refreshing lastMod. The slice closes at the earliest of
  lastMod+IDLE, started+WAIT, or an explicit_flush freeze on the same chunk
(range scope freezes every pending slice of the chunk).
Not modelled: writable_window eviction, slice_pressure, chains between extended
slices, writes crossing chunk boundaries. Each idle slice is simulated alone.

usage: idle_extend.py log.prefix accesslog.txt
"""
import re, sys, bisect, collections, datetime

INO = '627902'
CHUNK = 64 << 20
BLOCK = 4 << 20
WAIT, IDLE = 60.0, 31.0
KV = re.compile(r'(\w+)=(\S+)')
WIN = {'boot': ('2026/10/13 20:02:00', '2026/10/13 20:04:00'),
       'apps': ('2026/10/13 20:04:00', '2026/10/13 20:18:05')}


def ts(s, fmt):
    """Parse a log timestamp to a unix float (local time, same tz for both logs)."""
    return datetime.datetime.strptime(s, fmt).timestamp()


def dur(s):
    """Parse a Go duration such as 16.03s / 850ms to seconds."""
    m = re.fullmatch(r'([\d.]+)(ms|s|µs|us|m)', s)
    if not m:
        return float('nan')
    v, u = float(m.group(1)), m.group(2)
    return v * {'s': 1, 'ms': 1e-3, 'µs': 1e-6, 'us': 1e-6, 'm': 60}[u]


# --- freezes of the target inode
idle, explicit = [], collections.defaultdict(list)
slice_start = {}  # (chunk, off, started_s) -> freeze reason, for "absorbed slice" matching
for line in open(sys.argv[1], errors='replace'):
    i = line.find('slice freeze inode=' + INO + ' ')
    if i < 0:
        continue
    kv = dict(KV.findall(line[i:]))
    t = ts(line[:26], '%Y/%m/%d %H:%M:%S.%f')
    ch, off, ln = int(kv['chunk']), int(kv['off']), int(kv['raw_length'])
    st = int(kv['started_unix_ns']) / 1e9
    slice_start[(ch, off)] = slice_start.get((ch, off), []) + [(st, kv['reason'])]
    if kv['reason'] == 'explicit_flush':
        explicit[ch].append(t)
    elif kv['reason'] == 'idle':
        idle.append(dict(t=t, ch=ch, off=off, len=ln, st=st, lastmod=t - dur(kv['idle'])))
for v in explicit.values():
    v.sort()

# --- writes of the target inode, per chunk (in-chunk pos)
writes = collections.defaultdict(list)
for line in open(sys.argv[2], errors='replace'):
    if ' write (' + INO + ',' not in line or ' OK ' not in line:
        continue
    t = ts(line[:26], '%Y.%m.%d %H:%M:%S.%f')
    a = line.split(' write (', 1)[1].split(')', 1)[0].split(',')
    size, foff = int(a[1]), int(a[2])
    writes[foff // CHUNK].append((t, foff % CHUNK, size))
for v in writes.values():
    v.sort()

for name, (a, b) in WIN.items():
    a, b = ts(a, '%Y/%m/%d %H:%M:%S'), ts(b, '%Y/%m/%d %H:%M:%S')
    sel = [s for s in idle if a <= s['t'] < b]
    grew = absorbed_w = absorbed_b = absorbed_in_gap = 0
    absorbed_slices = set()  # real slices whose first write would have joined the idle slice
    close = collections.Counter()
    for s in sel:
        end, last = s['off'] + s['len'], s['lastmod']
        ex = explicit.get(s['ch'], [])
        nxt_ex = ex[bisect.bisect_right(ex, s['t'])] if bisect.bisect_right(ex, s['t']) < len(ex) else float('inf')
        ws = writes.get(s['ch'], [])
        j = bisect.bisect_right(ws, (s['t'], 1 << 62, 0))
        n = 0
        reason = None
        while True:
            deadline = min(last + IDLE, s['st'] + WAIT)
            if j >= len(ws) or ws[j][0] >= min(deadline, nxt_ex):
                reason = 'explicit' if nxt_ex < deadline and (j >= len(ws) or nxt_ex <= ws[j][0]) else (
                    'age60' if s['st'] + WAIT <= last + IDLE else 'idle31')
                break
            t, pos, size = ws[j]
            flushoff = (end - s['off']) // BLOCK * BLOCK
            if s['off'] + flushoff <= pos <= end:
                n += 1
                absorbed_b += size
                if t - s['lastmod'] <= IDLE:  # within 16-31s of the original last write
                    absorbed_in_gap += 1
                # was this write the first write of a real slice?
                for st, r in slice_start.get((s['ch'], pos), []):
                    if abs(st - t) < 0.01:
                        absorbed_slices.add((s['ch'], pos, st, r))
                end, last = max(end, pos + size), t
            j += 1
        close[reason] += 1
        if n:
            grew += 1
            absorbed_w += n
    print(f'== {name}: idle freezes {len(sel)}')
    print(f'   grew with >=1 write: {grew} ({grew / max(len(sel), 1):.1%}), absorbed writes {absorbed_w},'
          f' bytes {absorbed_b}, first absorbed write in 16-31s gap {absorbed_in_gap}')
    print(f'   counterfactual close: {dict(close)}')
    rc = collections.Counter(r for *_, r in absorbed_slices)
    print(f'   real slices whose first write would be absorbed: {len(absorbed_slices)} by reason {dict(rc)}')

# --- upper-bound context: same-chunk writes at any position / near (+-64KiB) in (freeze, lastMod+IDLE]
for name, (a, b) in WIN.items():
    a, b = ts(a, '%Y/%m/%d %H:%M:%S'), ts(b, '%Y/%m/%d %H:%M:%S')
    sel = [s for s in idle if a <= s['t'] < b]
    anyw = near = 0
    for s in sel:
        ws = [w for w in writes.get(s['ch'], []) if s['t'] < w[0] <= s['lastmod'] + IDLE]
        anyw += bool(ws)
        near += any(s['off'] - 65536 <= w[1] <= s['off'] + s['len'] + 65536 for w in ws)
    print(f'== {name} upper bound: same-chunk write in gap {anyw}, within +-64KiB {near} of {len(sel)}')
