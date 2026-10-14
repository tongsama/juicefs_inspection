"""Replay commits and reads of inode 627902 and compare background-compaction
policies offline (event-driven model of the kaz.5 code path).

Model (simplified; see the report for its limits):
- Triggers (source: base.go):
  * commit: compactionWanted(prev, now) -- crossing the every-th slice, or more
    than cap slices (base.go:2185, 2259).
  * read: Meta.Read on a chunk-cache miss requests compaction when the chunk has
    >= read_min slices (base.go:2139). The open-file chunk cache is invalidated
    by commits; mtime changes drop all chunks of the file on the next GetAttr, so
    a read is treated as a miss when the file had any commit since the previous
    miss of that chunk, or the chunk was compacted since then.
- Scheduler (compaction_scheduler.go, `priority`): one active compaction per
  inode; pending chunks are served from the highest count bucket (>=1000,
  >=100, else) in FIFO order; a request for the active chunk re-queues it after
  completion (generation change).
- A compaction takes DUR seconds (observed median), merges the slices present
  at its start into one, and keeps the commits that arrived meanwhile.
- Cost per compaction: PUT = ceil(chunk_bytes / 4 MiB) (observed bytes of that
  chunk, else 64 MiB); DELETE = merged slices (small slices are one block each).
- Policies can defer requests until the chunk has been idle `cooldown` seconds
  (forced at `force` slices), raise read_min, or change every/cap.
Deferred work at the end of the window is reported, since deferring postpones
rather than removes a compaction.
"""
import collections
import datetime
import heapq
import math
import re
import statistics
import sys

CH = 64 << 20
BLOCK = 4 << 20
INO = '627902'


def ts(s):
    """Parse a JuiceFS (2026/10/13 ...) or accesslog (2026.10.13 ...) timestamp."""
    return datetime.datetime.strptime(s[:10].replace('.', '/') + s[10:26], '%Y/%m/%d %H:%M:%S.%f').timestamp()


def load_debug(path):
    """Extract commits, initial counts, observed compactions and their durations."""
    single = re.compile(r'metadata write inode=' + INO + r' chunk=(\d+) slice=\d+ phase=stat slices=(\d+)')
    batch = re.compile(r'metadata write batch inode=' + INO + r' chunk=(\d+) first_slice=\d+ count=(\d+) phase=done .*slices=(\d+)')
    cstart = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) phase=read')
    cobj = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) slice=\d+ phase=object slices=(\d+) bytes=(\d+)')
    cdone = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) slice=\d+ phase=done')
    commits, init = [], {}
    obs, obs_bytes, started, durs = collections.Counter(), {}, {}, []
    for line in open(path, errors='replace'):
        if INO not in line:
            continue
        m = single.search(line) or batch.search(line)
        if m:
            c = int(m.group(1))
            if m.re is single:
                n, after = 1, int(m.group(2))
            else:
                n, after = int(m.group(2)), int(m.group(3))
            init.setdefault(c, max(after - n, 0))
            commits.append((ts(line[:26]), c, n))
            continue
        m = cstart.search(line)
        if m:
            started[int(m.group(1))] = ts(line[:26])
            continue
        m = cobj.search(line)
        if m:
            obs[int(m.group(1))] += 1
            obs_bytes[int(m.group(1))] = int(m.group(3))
            continue
        m = cdone.search(line)
        if m and int(m.group(1)) in started:
            durs.append(ts(line[:26]) - started.pop(int(m.group(1))))
    return commits, init, obs, obs_bytes, durs


def load_reads(path, t0, t1):
    """Return [(t, chunk)] reads of the target inode inside [t0, t1]."""
    r = re.compile(r'^(\S+ \S+) \[[^\]]*\] read \(' + INO + r',(\d+),(\d+),')
    out = []
    for line in open(path, errors='replace'):
        m = r.match(line)
        if m:
            t = ts(m.group(1))
            if t0 <= t <= t1:
                out.append((t, int(m.group(3)) // CH))
    return out


class Policy:
    """Knobs of one compaction policy (defaults reproduce the current code)."""

    def __init__(self, name, every=100, cap=350, read_min=5, cooldown=0, force=2500):
        self.name, self.every, self.cap, self.read_min = name, every, cap, read_min
        self.cooldown, self.force = cooldown, force

    def commit_wanted(self, prev, now):
        """Mirror compactionWanted with a configurable period and cap."""
        if now > self.cap:
            return True
        return any(n % self.every == self.every - 1 for n in range(max(prev + 1, 0), now + 1))


def bucket(count):
    """Mirror compactionPriority."""
    return 2 if count >= 1000 else (1 if count >= 100 else 0)


def simulate(p, commits, init, obs_bytes, reads, dur):
    """Run one policy; return totals, per-chunk counts, pending work, read samples."""
    events = [(t, 0, c, n) for t, c, n in commits] + [(t, 1, c, 0) for t, c in reads]
    events.sort()
    count = collections.defaultdict(int, init)
    last_commit = {}
    file_commit_seq = 0
    seen_seq = {}                 # chunk -> file_commit_seq at its last Meta.Read
    compacted_since = set()       # chunks compacted since their last Meta.Read
    pending = {}                  # chunk -> (bucket, enqueue order) waiting to run
    deferred = set()              # chunks with a request held back by cooldown
    order = 0
    active = None                 # (chunk, end_time, new_during)
    rerun = False
    res, per = collections.Counter(), collections.Counter()
    read_samples = []

    def request(c, t):
        """Register a compaction request for chunk c at time t."""
        nonlocal order, rerun
        if p.cooldown and count[c] < p.force:
            deferred.add(c)
            return
        if active and active[0] == c:
            rerun = True
            return
        if c not in pending:
            order += 1
            pending[c] = order

    def start_next(t):
        """Start the highest-priority pending chunk if the inode is idle."""
        nonlocal active
        if active or not pending:
            return
        c = min(pending, key=lambda k: (-bucket(count[k]), pending[k]))
        del pending[c]
        res['compactions'] += 1
        res['put'] += math.ceil(obs_bytes.get(c, CH) / BLOCK)
        res['delete'] += count[c]
        res['merged_lt99'] += count[c] < 99
        per[c] += 1
        active = [c, t + dur, 0]

    def finish_until(t):
        """Complete the active compaction(s) that end before t, chaining the queue."""
        nonlocal active, rerun
        while active and active[1] <= t:
            c, end, new = active
            count[c] = 1 + new
            compacted_since.add(c)
            active = None
            if rerun:
                rerun = False
                request(c, end)
            # Release deferred chunks that are idle long enough by `end`.
            release_deferred(end)
            start_next(end)

    def release_deferred(t):
        """Turn cooled-down deferred requests into real ones."""
        nonlocal order
        for c in list(deferred):
            if t - last_commit.get(c, -1e18) >= p.cooldown:
                deferred.discard(c)
                if not (active and active[0] == c) and c not in pending:
                    order += 1
                    pending[c] = order

    # Timer ticks so cooled-down requests fire even without new events.
    tick = 5.0
    t_first = events[0][0]
    t_last = events[-1][0]
    ticks = [t_first + k * tick for k in range(int((t_last - t_first) / tick) + 2)]
    merged = sorted(events + [(x, 2, -1, 0) for x in ticks])
    for t, kind, c, n in merged:
        finish_until(t)
        if kind == 2:
            if p.cooldown:
                release_deferred(t)
                start_next(t)
            continue
        if kind == 0:
            prev = count[c]
            count[c] += n
            if active and active[0] == c:
                active[2] += n
            last_commit[c] = t
            file_commit_seq += 1
            if count[c] >= p.force or p.commit_wanted(prev, count[c]):
                if count[c] >= p.force:
                    deferred.discard(c)
                    if not (active and active[0] == c) and c not in pending:
                        res['forced'] += 1
                        pending[c] = -1   # forced work goes first
                else:
                    request(c, t)
        else:
            read_samples.append(count[c])
            miss = seen_seq.get(c) != file_commit_seq or c in compacted_since
            if miss:
                seen_seq[c] = file_commit_seq
                compacted_since.discard(c)
                if count[c] >= p.read_min:
                    request(c, t)
        start_next(t)
    finish_until(t_last)
    res['pending_end'] = len(pending) + len(deferred) + (1 if active else 0)
    res['end_slices'] = sum(count.values())
    return res, per, read_samples


def q(xs, p):
    """Return the p-quantile of a non-empty sorted list."""
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def main():
    """Validate the model against the log and print the policy comparison."""
    log, acc = sys.argv[1], sys.argv[2]
    commits, init, obs, obs_bytes, durs = load_debug(log)
    t0, t1 = commits[0][0], commits[-1][0]
    reads = load_reads(acc, t0, t1)
    dur = statistics.median(durs)
    print(f'commits={len(commits)} slices={sum(n for _, _, n in commits)} chunks={len(init)} reads={len(reads)} '
          f'window={t1 - t0:.0f}s observed compactions={sum(obs.values())} chunk1={obs[1]} '
          f'chunk1920={obs[1920]} chunk1930={obs[1930]} chunk168={obs[168]} median dur={dur:.1f}s')
    policies = [
        Policy('current (read>=5, every100, cap350)'),
        Policy('read>=50', read_min=50),
        Policy('read>=100', read_min=100),
        Policy('read off (commit trigger only)', read_min=10**9),
        Policy('read>=100, every200 cap700', read_min=100, every=200, cap=700),
        Policy('read>=100, every300 cap1000', read_min=100, every=300, cap=1000),
        Policy('cooldown30s', cooldown=30),
        Policy('cooldown1m', cooldown=60),
        Policy('cooldown3m', cooldown=180),
        Policy('cooldown1m force1000', cooldown=60, force=1000),
        Policy('cooldown3m force1000', cooldown=180, force=1000),
        Policy('read>=20', read_min=20),
        Policy('read>=100 + cooldown1m force700', read_min=100, cooldown=60, force=700),
        Policy('read>=100 + cooldown1m force1000', read_min=100, cooldown=60, force=1000),
        Policy('read>=100 + cooldown3m force1000', read_min=100, cooldown=180, force=1000),
    ]
    print(f'{"policy":36s} {"comp":>4s} {"<99":>4s} {"PUT":>5s} {"DEL":>5s} {"DEL/c":>5s} {"ch1":>3s} {"ch1920":>6s} '
          f'{"forced":>6s} {"pend":>4s} {"slices@end":>10s}  slices-at-read mean/p50/p95/p99/max')
    for p in policies:
        res, per, rs = simulate(p, commits, init, obs_bytes, reads, dur)
        rs.sort()
        print(f'{p.name:36s} {res["compactions"]:4d} {res["merged_lt99"]:4d} {res["put"]:5d} {res["delete"]:5d} '
              f'{res["delete"] / max(res["compactions"], 1):5.0f} {per[1]:3d} {per[1920]:6d} {res["forced"]:6d} {res["pending_end"]:4d} {res["end_slices"]:10d}  '
              f'{statistics.mean(rs):.0f}/{q(rs, .5)}/{q(rs, .95)}/{q(rs, .99)}/{rs[-1]}')


if __name__ == '__main__':
    main()
