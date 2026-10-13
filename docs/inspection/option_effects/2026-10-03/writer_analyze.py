"""Stream manifest prefixes once; retain bounded histograms and outstanding identities only."""
import collections
import csv
import datetime
import heapq
import json
import math
import pathlib
import re
import time

ROOT = pathlib.Path(__file__).parent
FIELDS = re.compile(r'(\w+)=([^ ]+)')
PID = re.compile(rb'juicefs\[(\d+)\]')
DURATION = re.compile(r'([\d.]+)(ns|µs|us|ms|s|m|h)')
SCALE = {'ns': 1e-9, 'µs': 1e-6, 'us': 1e-6, 'ms': 1e-3, 's': 1, 'm': 60, 'h': 3600}
ZONE = datetime.timezone(datetime.timedelta(hours=9))
LIMIT = 200000


def seconds(value):
    """Convert Go duration text to seconds, including negative idle ages."""
    v = sum(float(n)*SCALE[u] for n, u in DURATION.findall(value))
    return -v if value.startswith('-') else v


class Histogram:
    """Bound quantile memory to log2 buckets with 64 bins per doubling (about 1.1% width)."""
    def __init__(self):
        """Initialize exact counters and sparse fixed-range buckets."""
        self.n = 0
        self.total = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf
        self.bins = collections.Counter()
        self.below = collections.Counter()

    def add(self, value):
        """Record one duration; extrema and configured threshold counts remain exact."""
        self.n += 1
        self.total += value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        bucket = -8192 if value <= 0 else max(-8191, min(8191, math.floor(math.log2(value)*64)))
        self.bins[bucket] += 1
        for threshold in (.001, .01, .1, 1, 10, 15, 30, 60, 300, 900):
            if value < threshold:
                self.below[str(threshold)] += 1

    def export(self):
        """Report exact counts and quantile bucket intervals, never false exact quantiles."""
        if not self.n:
            return {'n': 0}
        result = {'n': self.n, 'sum': self.total, 'min': self.minimum, 'max': self.maximum,
                  'below_seconds': dict(self.below)}
        ordered = sorted(self.bins.items())
        for name, p in (('p50', .5), ('p95', .95), ('p99', .99)):
            rank = math.ceil(self.n*p)
            cumulative = 0
            for index, n in ordered:
                cumulative += n
                if cumulative >= rank:
                    result[name+'_interval'] = [0, 0] if index == -8192 else [2**(index/64), 2**((index+1)/64)]
                    break
        return result


def freeze_group():
    """Allocate bounded accumulators for a freeze reason or PID."""
    return {'count': 0, 'raw_bytes': 0, 'raw_sizes': collections.Counter(), 'age': Histogram(), 'idle': Histogram(),
            'age_15_to_16': 0, 'age_30_to_31': 0, 'first': None, 'last': None, 'origin': collections.Counter()}


def export_freeze(group):
    """Serialize a freeze accumulator into JSON-compatible output."""
    result = {k: v.export() if isinstance(v, Histogram) else dict(v) if isinstance(v, collections.Counter) else v
              for k, v in group.items()}
    sizes = sorted(group['raw_sizes'].items())
    quantiles = {}
    for name, ratio in (('p50', .5), ('p95', .95), ('p99', .99)):
        rank = math.ceil(group['count']*ratio)
        seen = 0
        for size, count in sizes:
            seen += count
            if seen >= rank:
                quantiles[name] = size
                break
    result['raw_length_quantiles_exact'] = quantiles
    return result


def analyze(spec):
    """Scan one fixed byte prefix, excluding a final partial line and preserving PID boundaries."""
    start = time.monotonic()
    size = spec['prefix_bytes']
    read_bytes = 0
    complete_bytes = 0
    lines = 0
    first = last = None
    reasons = collections.defaultdict(lambda: collections.defaultdict(freeze_group))
    barriers = collections.defaultdict(lambda: collections.defaultdict(lambda: {'begin': 0, 'end': 0, 'errno': collections.Counter(), 'duration': Histogram(), 'longest': []}))
    pending = collections.OrderedDict()
    zero = collections.OrderedDict()
    cohorts = collections.Counter()
    hours = collections.defaultdict(lambda: {'freeze': collections.Counter(), 'created_observed_freezes': 0,
                                            'finish': 0, 'barrier_begin': collections.Counter(), 'barrier_end': collections.Counter()})
    zero_matched = zero_dropped = pending_dropped = unknown_end = zero_count = malformed = 0
    pid_counts = collections.Counter()
    next_progress = 256 << 20
    with pathlib.Path(spec['path']).open('rb') as stream:
        while read_bytes < size:
            raw = stream.readline(size-read_bytes)
            if not raw:
                break
            read_bytes += len(raw)
            if not raw.endswith(b'\n'):
                break
            complete_bytes = read_bytes
            lines += 1
            if raw[:4].isdigit() and raw[4:5] == b'/':
                timestamp = raw[:26].decode('ascii', errors='replace')
                first = first or timestamp
                last = timestamp
            if read_bytes >= next_progress:
                print(spec['role'], read_bytes, '/', size, 'elapsed', round(time.monotonic()-start, 1), flush=True)
                next_progress += 256 << 20
            if b'slice freeze ' not in raw and b'slice finish ' not in raw and b'writer flush ' not in raw:
                continue
            match = PID.search(raw)
            if not match:
                malformed += 1
                continue
            pid = match[1].decode()
            pid_counts[pid] += 1
            line = raw.decode(errors='replace')
            timestamp = line[:26]
            hour = timestamp[:13]
            f = dict(FIELDS.findall(line))
            if 'slice freeze ' in line:
                r = reasons[pid][f['reason']]
                length = int(f['raw_length'])
                age, idle = seconds(f['age']), seconds(f['idle'])
                r['count'] += 1
                r['raw_bytes'] += length
                r['raw_sizes'][length] += 1
                r['age'].add(age)
                r['idle'].add(idle)
                r['age_15_to_16'] += 15 <= age < 16
                r['age_30_to_31'] += 30 <= age < 31
                r['first'] = r['first'] or timestamp
                r['last'] = timestamp
                r['origin'][f.get('origin', 'absent')] += 1
                hours[(pid, hour)]['freeze'][f['reason']] += 1
                creation = datetime.datetime.fromtimestamp(int(f['started_unix_ns'])/1e9, ZONE).strftime('%Y/%m/%d %H')
                hours[(pid, creation)]['created_observed_freezes'] += 1
                if int(f['slice']) == 0:
                    zero_count += 1
                    key = (pid, f['inode'], f['chunk'], f['started_unix_ns'])
                    zero[key] = {'freeze_time': timestamp, 'reason': f['reason']}
                    if len(zero) > LIMIT:
                        zero.popitem(last=False)
                        zero_dropped += 1
            elif 'slice finish ' in line:
                hours[(pid, hour)]['finish'] += 1
                key = (pid, f['inode'], f['chunk'], f['started_unix_ns'])
                if key in zero:
                    del zero[key]
                    zero_matched += 1
            else:
                origin = f['origin']
                group = barriers[pid][origin]
                key = (pid, f['barrier'])
                if f['phase'] == 'begin':
                    group['begin'] += 1
                    pending[key] = {'time': timestamp, 'inode': f['inode'], 'origin': origin}
                    hours[(pid, hour)]['barrier_begin'][origin] += 1
                    if len(pending) > LIMIT:
                        pending.popitem(last=False)
                        pending_dropped += 1
                elif f['phase'] == 'end':
                    begin = pending.pop(key, None)
                    unknown_end += begin is None
                    group['end'] += 1
                    group['errno'][f['errno']] += 1
                    elapsed = seconds(f['elapsed'])
                    group['duration'].add(elapsed)
                    hours[(pid, hour)]['barrier_end'][origin] += 1
                    record = {'time': timestamp, 'begin': begin, 'inode': f['inode'], 'barrier': f['barrier'],
                              'errno': f['errno'], 'elapsed_seconds': elapsed}
                    item = (elapsed, group['end'], record)
                    if len(group['longest']) < 16:
                        heapq.heappush(group['longest'], item)
                    elif elapsed > group['longest'][0][0]:
                        heapq.heapreplace(group['longest'], item)
    assert complete_bytes <= size and read_bytes == size
    for pid in pid_counts:
        hour_start = datetime.datetime.strptime(first, '%Y/%m/%d %H:%M:%S.%f').replace(minute=0, second=0, microsecond=0)
        hour_stop = datetime.datetime.strptime(last, '%Y/%m/%d %H:%M:%S.%f').replace(minute=0, second=0, microsecond=0)
        while hour_start <= hour_stop:
            hours[(pid, hour_start.strftime('%Y/%m/%d %H'))]
            hour_start += datetime.timedelta(hours=1)
    summary = {'manifest': spec, 'complete_prefix_bytes': complete_bytes, 'excluded_partial_bytes': size-complete_bytes,
               'lines': lines, 'first': first, 'last': last, 'event_pid_counts': dict(pid_counts),
               'quantile_method': 'Nearest rank log2 histogram, 64 buckets per doubling; quantiles are intervals, extrema/counts exact',
               'slice_creation_method': 'Creation-hour cohorts of observed freezes only, derived from started_unix_ns; not all NewSlice allocations',
               'freeze_by_pid': {pid: {reason: export_freeze(g) for reason, g in groups.items()} for pid, groups in reasons.items()},
               'barriers_by_pid': {pid: {origin: {'begin': g['begin'], 'end': g['end'], 'errno': dict(g['errno']),
                                                'duration_seconds': g['duration'].export(),
                                                'longest': [r for _, _, r in sorted(g['longest'], reverse=True)]}
                                        for origin, g in groups.items()} for pid, groups in barriers.items()},
               'id_zero': {'freeze': zero_count, 'finish_matched': zero_matched, 'outstanding': len(zero), 'evicted': zero_dropped},
               'barrier_correlations': {'outstanding': len(pending), 'unmatched_end': unknown_end, 'evicted': pending_dropped,
                                        'outstanding_examples': list(pending.items())[:100]}, 'malformed_events': malformed}
    stem = pathlib.Path(spec['path']).stem
    (ROOT / ('writer_'+stem+'.json')).write_text(json.dumps(summary, indent=2, ensure_ascii=False)+'\n')
    with (ROOT / ('writer_'+stem+'_hours.csv')).open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['pid', 'hour_JST', 'creation_cohort_observed_freezes', 'finish', 'freeze_total', 'explicit_flush', 'writable_window', 'idle', 'age', 'slice_pressure', 'commit_age', 'full_slice', 'barrier_begin', 'barrier_end', 'explicit_flush_pct', 'writable_window_pct', 'timer_pct'])
        for (pid, hour), g in sorted(hours.items()):
            writer.writerow([pid, hour, g['created_observed_freezes'], g['finish'], sum(g['freeze'].values()),
                             *[g['freeze'][r] for r in ('explicit_flush', 'writable_window', 'idle', 'age', 'slice_pressure', 'commit_age', 'full_slice')],
                             sum(g['barrier_begin'].values()), sum(g['barrier_end'].values()),
                             100*g['freeze']['explicit_flush']/sum(g['freeze'].values()) if sum(g['freeze'].values()) else '',
                             100*g['freeze']['writable_window']/sum(g['freeze'].values()) if sum(g['freeze'].values()) else '',
                             100*sum(g['freeze'][r] for r in ('age', 'idle', 'commit_age'))/sum(g['freeze'].values()) if sum(g['freeze'].values()) else ''])
    print('DONE', spec['role'], lines, 'seconds', round(time.monotonic()-start, 1), 'pids', dict(pid_counts), flush=True)


if __name__ == '__main__':
    manifest = json.loads((ROOT/'manifest.json').read_text())
    for file_spec in manifest['files']:
        analyze(file_spec)
