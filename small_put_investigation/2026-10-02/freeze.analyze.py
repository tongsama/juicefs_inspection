"""Analyze a fixed, complete-line prefix without changing the source log."""
import collections
import datetime
import hashlib
import json
import math
import pathlib
import re

SOURCE = pathlib.Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-014922.log')
PREFIX = 179156595
OUT = pathlib.Path(__file__).parent
CUTOFF = '2026/10/02 01:54:30'
FIELDS = re.compile(r'(\w+)=([^ ]+)')
DURATION = re.compile(r'([\d.]+)(ns|µs|us|ms|s|m|h)')
SCALE = {'ns': 1e-9, 'µs': 1e-6, 'us': 1e-6, 'ms': 1e-3, 's': 1, 'm': 60, 'h': 3600}
DELETE = re.compile(r'DELETE chunks/\S*/(\d+)_(\d+)_(\d+).*err: <nil>')
PUT = re.compile(r'PUT chunks/\S*/(\d+)_(\d+)_(\d+) payload_bytes=(\d+).*err: <nil>')


def seconds(value):
    """Convert the positive Go duration format to seconds."""
    return sum(float(n) * SCALE[u] for n, u in DURATION.findall(value))


def distribution(values):
    """Return count, sum, extrema, and nearest-rank quantiles."""
    ordered = sorted(values)
    if not ordered:
        return {'n': 0}
    def quantile(p):
        """Select the nearest-rank quantile from a nonempty ordered list."""
        return ordered[max(0, math.ceil(len(ordered) * p) - 1)]
    return {'n': len(ordered), 'sum': sum(ordered), 'min': ordered[0], 'p50': quantile(.5),
            'p90': quantile(.9), 'p95': quantile(.95), 'p99': quantile(.99), 'max': ordered[-1]}


def summarize(records):
    """Aggregate freeze reasons, sizes, durations, and linked storage events."""
    result = {'count': len(records), 'reasons': dict(collections.Counter(r['reason'] for r in records)),
              'raw_length_counts': dict(collections.Counter(r['raw_length'] for r in records)),
              'raw_length': distribution([r['raw_length'] for r in records]),
              'age_seconds': distribution([r['age_s'] for r in records]),
              'idle_seconds': distribution([r['idle_s'] for r in records]),
              'id_zero_at_freeze': sum(r['initial_id'] == 0 for r in records),
              'finish_matched': sum(r['finish_time'] is not None for r in records),
              'metadata_entry_matched': sum(r['commit_time'] is not None for r in records),
              'successful_put_matched': sum(bool(r['puts']) for r in records),
              'metadata_done_matched': sum(r['metadata_done_time'] is not None for r in records),
              'delete_matched': sum(bool(r['deletes']) for r in records),
              'raw_4KiB_freeze_reasons': dict(collections.Counter(r['reason'] for r in records if r['raw_length'] == 4096))}
    result['by_reason'] = {}
    for reason in sorted(result['reasons']):
        rr = [r for r in records if r['reason'] == reason]
        result['by_reason'][reason] = {
            'count': len(rr), 'raw_length_counts': dict(collections.Counter(r['raw_length'] for r in rr)),
            'raw_length': distribution([r['raw_length'] for r in rr]),
            'age_seconds': distribution([r['age_s'] for r in rr]),
            'idle_seconds': distribution([r['idle_s'] for r in rr]),
            'age_below_30s': sum(r['age_s'] < 30 for r in rr),
            'age_below_10s': sum(r['age_s'] < 10 for r in rr),
            'age_below_1s': sum(r['age_s'] < 1 for r in rr),
            'age_below_100ms': sum(r['age_s'] < .1 for r in rr),
            'age_below_10ms': sum(r['age_s'] < .01 for r in rr),
            'idle_below_10s': sum(r['idle_s'] < 10 for r in rr),
            'age_and_idle_below_configured_timers': sum(r['age_s'] < 30 and r['idle_s'] < 10 for r in rr)}
    fourk = [r for r in records if any(p['raw'] == 4096 for p in r['puts'])]
    fourkputs = [(r, p) for r in records for p in r['puts'] if p['raw'] == 4096]
    result['matched_raw_4KiB'] = {
        'slices': len(fourk), 'puts': len(fourkputs),
        'reason_counts_by_put': dict(collections.Counter(r['reason'] for r, p in fourkputs)),
        'reason_counts_by_slice': dict(collections.Counter(r['reason'] for r in fourk)),
        'payload_bytes': distribution([p['payload'] for r, p in fourkputs]),
        'payload_below_1KiB': sum(p['payload'] < 1024 for r, p in fourkputs),
        'age_seconds': distribution([r['age_s'] for r in fourk]),
        'idle_seconds': distribution([r['idle_s'] for r in fourk])}
    explicit = [r for r in records if r['reason'] == 'explicit_flush']
    result['explicit_by_inode'] = dict(collections.Counter(r['inode'] for r in explicit))
    return result


with SOURCE.open('rb') as stream:
    prefix = stream.read(PREFIX)
assert len(prefix) == PREFIX and prefix.endswith(b'\n'), 'The shared prefix must end in a complete line'
lines = prefix.decode('utf-8', errors='replace').splitlines()
records = []
by_key = {}
finishes = []
puts = []
commits = {}
metadata_done = {}
deletes = []
for line in lines:
    timestamp = line[:26]
    if 'slice freeze ' in line:
        fields = dict(FIELDS.findall(line))
        r = {'time': timestamp, 'inode': int(fields['inode']), 'chunk': int(fields['chunk']),
             'initial_id': int(fields['slice']), 'slice_id': int(fields['slice']),
             'off': int(fields['off']), 'raw_length': int(fields['raw_length']), 'reason': fields['reason'],
             'age_s': seconds(fields['age']), 'idle_s': seconds(fields['idle']),
             'started_unix_ns': int(fields['started_unix_ns']), 'finish_time': None,
             'commit_time': None, 'metadata_done_time': None, 'puts': [], 'deletes': []}
        key = (r['inode'], r['chunk'], r['started_unix_ns'])
        assert key not in by_key, 'Duplicate freeze identity'
        records.append(r)
        by_key[key] = r
    elif 'slice finish ' in line:
        fields = dict(FIELDS.findall(line))
        finishes.append((timestamp, fields))
    elif 'slice commit ' in line:
        fields = dict(FIELDS.findall(line))
        commits[int(fields['slice'])] = timestamp
    elif 'metadata write ' in line and 'phase=done' in line:
        fields = dict(FIELDS.findall(line))
        metadata_done[int(fields['slice'])] = timestamp
    elif 'DELETE chunks/' in line:
        match = DELETE.search(line)
        if match:
            sid, block, raw = map(int, match.groups())
            deletes.append({'time': timestamp, 'slice_id': sid, 'block': block, 'raw': raw})
    elif 'PUT chunks/' in line:
        match = PUT.search(line)
        if match:
            sid, block, raw, payload = map(int, match.groups())
            puts.append({'time': timestamp, 'slice_id': sid, 'block': block, 'raw': raw, 'payload': payload})

unmatched_finish = 0
for timestamp, f in finishes:
    key = (int(f['inode']), int(f['chunk']), int(f['started_unix_ns']))
    r = by_key.get(key)
    if r is None:
        unmatched_finish += 1
        continue
    assert r['off'] == int(f['off']) and r['raw_length'] == int(f['raw_length'])
    assert r['initial_id'] in (0, int(f['slice']))
    r['slice_id'] = int(f['slice'])
    r['finish_time'] = timestamp
by_id = {r['slice_id']: r for r in records if r['slice_id']}
assert len(by_id) == sum(bool(r['slice_id']) for r in records), 'Duplicate allocated slice IDs'
for p in puts:
    if p['slice_id'] in by_id:
        by_id[p['slice_id']]['puts'].append(p)
for d in deletes:
    if d['slice_id'] in by_id:
        by_id[d['slice_id']]['deletes'].append(d)
for r in records:
    r['metadata_done_time'] = metadata_done.get(r['slice_id'])
    r['commit_time'] = commits.get(r['slice_id'])

result = {'source': str(SOURCE), 'prefix_bytes': PREFIX, 'sha256': hashlib.sha256(prefix).hexdigest(),
          'line_count': len(lines), 'first_line_time': lines[0][:26], 'last_line_time': lines[-1][:26],
          'first_freeze_time': records[0]['time'] if records else None,
          'last_freeze_time': records[-1]['time'] if records else None,
          'cutoff': CUTOFF, 'unmatched_finish': unmatched_finish,
          'successful_put_entries': len(puts),
          'successful_put_entries_matched_to_freeze': sum(len(r['puts']) for r in records),
          'full': summarize(records), 'after_startup_5min': summarize([r for r in records if r['time'] >= CUTOFF]),
          'freeze_minute_reason_counts': {minute: dict(collections.Counter(r['reason'] for r in records if r['time'][:16] == minute))
                                         for minute in sorted({r['time'][:16] for r in records})},
          'successful_put_slice_id_range': [min(p['slice_id'] for p in puts), max(p['slice_id'] for p in puts)],
          'freeze_slice_id_range': [min(by_id), max(by_id)],
          'by_inode': {str(inode): summarize([r for r in records if r['inode'] == inode])
                       for inode in sorted({r['inode'] for r in records})}}
(OUT / 'freeze.results.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
with (OUT / 'freeze.correlations.jsonl').open('w') as stream:
    for r in records:
        stream.write(json.dumps(r, ensure_ascii=False) + '\n')
print(json.dumps({k: result[k] for k in ('prefix_bytes', 'sha256', 'line_count', 'first_freeze_time',
                                       'last_freeze_time', 'unmatched_finish', 'successful_put_entries',
                                       'successful_put_entries_matched_to_freeze')}, indent=2))
for label in ('full', 'after_startup_5min'):
    print(label, json.dumps({key: result[label][key] for key in ('count', 'reasons', 'raw_4KiB_freeze_reasons', 'metadata_done_matched', 'delete_matched')}, ensure_ascii=False))
