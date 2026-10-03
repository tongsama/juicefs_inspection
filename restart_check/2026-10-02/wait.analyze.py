"""Correlate writer barriers and commit phases from a fixed log prefix."""
import collections
import datetime
import hashlib
import json
import math
import pathlib
import re

SOURCE = pathlib.Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-145808.log')
PREFIX = 385860940
OUT = pathlib.Path(__file__).parent
FIELDS = re.compile(r'(\w+)=([^ ]+)')
DURATION = re.compile(r'([\d.]+)(ns|µs|us|ms|s|m|h)')
SCALE = {'ns': 1e-9, 'µs': 1e-6, 'us': 1e-6, 'ms': 1e-3, 's': 1, 'm': 60, 'h': 3600}


def seconds(value):
    """Convert Go duration units into seconds."""
    return sum(float(n) * SCALE[u] for n, u in DURATION.findall(value))


def distribution(values):
    """Summarize durations with nearest-rank percentiles."""
    a = sorted(values)
    if not a:
        return {'n': 0}
    return {'n': len(a), 'min': a[0], 'p50': a[math.ceil(len(a)*.5)-1],
            'p95': a[math.ceil(len(a)*.95)-1], 'p99': a[math.ceil(len(a)*.99)-1], 'max': a[-1],
            'above_1s': sum(x >= 1 for x in a), 'above_10s': sum(x >= 10 for x in a),
            'above_60s': sum(x >= 60 for x in a), 'above_300s': sum(x >= 300 for x in a)}


active = {}
completed = []
metadata = {}
compaction = {}
slow = collections.defaultdict(list)
thresholds = []
warnings = []
unmatched = []
line_count = 0
hashing = hashlib.sha256()
read_bytes = 0
first_time = None
last_time = None
with SOURCE.open('rb') as stream:
    while read_bytes < PREFIX:
        raw = stream.readline(PREFIX-read_bytes)
        assert raw and raw.endswith(b'\n'), 'Prefix must end on a complete line'
        read_bytes += len(raw)
        hashing.update(raw)
        line_count += 1
        line = raw.decode(errors='replace').rstrip()
        timestamp = line[:26]
        if re.match(r'\d{4}/', line):
            first_time = first_time or timestamp
            last_time = timestamp
        if 'writer flush ' in line:
            f = dict(FIELDS.findall(line))
            key = int(f['barrier'])
            if f['phase'] == 'begin':
                assert key not in active
                active[key] = {'begin': timestamp, 'inode': int(f['inode']), 'origin': f['origin'], 'barrier': key}
            else:
                b = active.pop(key, None)
                if b is None:
                    unmatched.append(line)
                else:
                    assert b['inode'] == int(f['inode']) and b['origin'] == f['origin']
                    b.update(end=timestamp, errno=int(f['errno']), elapsed=seconds(f['elapsed']))
                    completed.append(b)
        elif 'metadata write ' in line and 'phase=' in line:
            f = dict(FIELDS.findall(line))
            key = (int(f['inode']), int(f['chunk']), int(f['slice']))
            if f['phase'] == 'done':
                metadata.pop(key, None)
            else:
                metadata[key] = {'time': timestamp, 'fields': f, 'line': line}
                if f['phase'] == 'compact':
                    thresholds.append(line)
        elif 'compaction inode=' in line and 'phase=' in line:
            f = dict(FIELDS.findall(line))
            key = (int(f['inode']), int(f['chunk']))
            if f['phase'] == 'done':
                compaction.pop(key, None)
            else:
                compaction[key] = {'time': timestamp, 'fields': f, 'line': line}
        elif 'slow compaction ' in line:
            f = dict(FIELDS.findall(line))
            slow['compaction'].append({'time': timestamp, 'fields': f, 'line': line,
                                      **{k: seconds(f[k]) for k in ('total', 'queue_wait', 'object', 'metadata')}})
        elif 'slow metadata write ' in line:
            f = dict(FIELDS.findall(line))
            slow['metadata'].append({'time': timestamp, 'fields': f, 'line': line,
                                    **{k: seconds(f[k]) for k in ('total', 'lock_wait', 'doWrite', 'stat', 'compact')}})
        elif 'slow slice commit ' in line:
            f = dict(FIELDS.findall(line))
            slow['commit'].append({'time': timestamp, 'fields': f, 'line': line, 'metadata': seconds(f['metadata'])})
        elif 'slow operation:' in line:
            match = re.search(r'slow operation: (\w+).*<([\d.]+)>', line)
            if match:
                slow['operation'].append({'time': timestamp, 'method': match[1], 'duration': float(match[2]), 'line': line})
        if re.search(r'still waiting|flush \d+ interrupted|flush \d+ timeout|All goroutines', line):
            warnings.append(line)

last = datetime.datetime.strptime(last_time, '%Y/%m/%d %H:%M:%S.%f')
for b in active.values():
    b['outstanding_seconds_at_cutoff'] = (last-datetime.datetime.strptime(b['begin'], '%Y/%m/%d %H:%M:%S.%f')).total_seconds()
origins = sorted({b['origin'] for b in completed} | {b['origin'] for b in active.values()})
summary = {'source': str(SOURCE), 'prefix_bytes': PREFIX, 'sha256': hashing.hexdigest(), 'line_count': line_count,
           'first_time': first_time, 'last_time': last_time, 'completed_count': len(completed),
           'unmatched_ends': unmatched, 'outstanding_barriers': list(active.values()),
           'outstanding_metadata': list(metadata.values()), 'unclosed_compaction_phase_logs': list(compaction.values()),
           'threshold_2500_routes': thresholds, 'warning_lines': warnings,
           'barriers_by_origin': {o: {'completed': sum(b['origin']==o for b in completed),
                                      'errno_counts': dict(collections.Counter(b['errno'] for b in completed if b['origin']==o)),
                                      'elapsed_seconds': distribution([b['elapsed'] for b in completed if b['origin']==o]),
                                      'longest': sorted([b for b in completed if b['origin']==o], key=lambda b:b['elapsed'], reverse=True)[:10]}
                                  for o in origins},
           'slow': dict(slow)}
(OUT/'wait.results.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False)+'\n')
print(json.dumps({k:summary[k] for k in ('prefix_bytes','sha256','first_time','last_time','completed_count','outstanding_barriers','outstanding_metadata','unclosed_compaction_phase_logs','threshold_2500_routes','warning_lines')}, indent=2, ensure_ascii=False))
for origin, values in summary['barriers_by_origin'].items():
    print(origin, json.dumps({k:v for k,v in values.items() if k!='longest'}))
for kind, rows in slow.items():
    key = 'total' if kind in ('compaction','metadata') else 'metadata' if kind=='commit' else 'duration'
    print('SLOW', kind, distribution([r[key] for r in rows]))
    for r in sorted(rows,key=lambda r:r[key],reverse=True)[:5]: print(r['line'])
