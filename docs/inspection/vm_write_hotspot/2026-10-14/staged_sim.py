"""Replay one run of inode 627902 and estimate the TOTAL number of PUTs under
several compaction policies, modelling the writeback upload queue.

Model (simplified; calibrated against the observed share of frozen writer
slices retired by compaction before upload):
- Every frozen writer slice (DEBUG 'slice freeze') is staged at its freeze time
  as ceil(raw_length / 4 MiB) blocks and committed at the same time.
- Uploads: FIFO by staging time, K parallel workers, each PUT takes D seconds.
- Compaction (one at a time per inode, DUR seconds, priority buckets as in the
  kaz scheduler) merges the chunk's slices present at its start; at its end the
  merged slices that have not started uploading are retired (no PUT), and the
  output (ceil(bytes / 4 MiB) blocks, observed per chunk, else 16) is staged.
- Triggers: commit (compactionWanted: 99th slice, >350), read on a chunk-cache
  miss with >= read_min slices (a read misses when the file had any commit since
  that chunk's previous miss, or the chunk was compacted), cooldown/force as in
  the kaz policy, and optionally a staging trigger: request compaction when the
  chunk has >= stage_min slices still waiting for upload (cooldown is bypassed).
"""
import collections
import datetime
import heapq
import math
import re
import sys

INO = '627902'
UPLOAD_DELAY = 0.0  # seconds a staged block waits before it may be uploaded (--upload-delay)
BLOCK = 4 << 20
CH = 64 << 20


def ts(s):
    """Parse a JuiceFS or accesslog timestamp into epoch seconds."""
    return datetime.datetime.strptime(s[:10].replace('.', '/') + s[10:26], '%Y/%m/%d %H:%M:%S.%f').timestamp()


def load(log, acc, end):
    """Return freezes, initial slice counts, observed compaction bytes and reads before end."""
    fre = re.compile(r'slice freeze inode=' + INO + r' chunk=(\d+) slice=(\d+) off=\d+ raw_length=(\d+)')
    single = re.compile(r'metadata write inode=' + INO + r' chunk=(\d+) slice=\d+ phase=stat slices=(\d+)')
    batch = re.compile(r'metadata write batch inode=' + INO + r' chunk=(\d+) first_slice=\d+ count=(\d+) phase=done .*slices=(\d+)')
    cobj = re.compile(r'compaction inode=' + INO + r' chunk=(\d+) slice=\d+ phase=object slices=\d+ bytes=(\d+)')
    freezes, init, obs_bytes = [], {}, {}
    tend = ts(end + '.000000')
    for line in open(log, errors='replace'):
        if INO not in line:
            continue
        t = ts(line[:26])
        if t >= tend:
            break
        m = fre.search(line)
        if m:
            freezes.append((t, int(m.group(1)), int(m.group(2)), max(1, math.ceil(int(m.group(3)) / BLOCK))))
            continue
        m = single.search(line) or batch.search(line)
        if m:
            c = int(m.group(1))
            n, after = (1, int(m.group(2))) if m.re is single else (int(m.group(2)), int(m.group(3)))
            init.setdefault(c, max(after - n, 0))
            continue
        m = cobj.search(line)
        if m:
            obs_bytes[int(m.group(1))] = int(m.group(2))
    t0 = freezes[0][0]
    reads = []
    r = re.compile(r'^(\S+ \S+) \[[^\]]*\] read \(' + INO + r',(\d+),(\d+),')
    for line in open(acc, errors='replace'):
        m = r.match(line)
        if m:
            t = ts(m.group(1))
            if t0 <= t < tend:
                reads.append((t, int(m.group(3)) // CH))
    return freezes, init, obs_bytes, reads, tend


class Policy:
    """Compaction trigger knobs; the defaults reproduce kaz.5 without the policy."""

    def __init__(self, name, read_min=5, cooldown=0, force=2500, stage_min=0):
        self.name, self.read_min, self.cooldown, self.force, self.stage_min = name, read_min, cooldown, force, stage_min


def wanted(prev, now):
    """Mirror compactionWanted."""
    return now > 350 or any(n % 100 == 99 for n in range(max(prev + 1, 0), now + 1))


def simulate(p, freezes, init, obs_bytes, reads, tend, K, D, DUR):
    """Run one policy; return counters."""
    res = collections.Counter()
    # Staged upload items: [enqueue_time, chunk, retired, started]
    queue = collections.deque()
    workers = [0.0] * K
    heapq.heapify(workers)
    staged_by_chunk = collections.defaultdict(list)  # chunk -> items not yet started

    def stage(t, chunk, nblocks, kind):
        for _ in range(nblocks):
            item = {'t': t, 'ready': t + UPLOAD_DELAY, 'chunk': chunk, 'retired': False, 'started': False, 'kind': kind}
            queue.append(item)
            staged_by_chunk[chunk].append(item)

    def advance(t):
        """Start uploads whose worker frees up by t."""
        while queue:
            item = queue[0]
            if item['retired']:
                queue.popleft()
                continue
            w = workers[0]
            start = max(w, item['ready'])
            if start > t:
                return
            heapq.heapreplace(workers, start + D)
            queue.popleft()
            item['started'] = True
            res['put_' + item['kind']] += 1

    def waiting(chunk):
        """Count blocks of the chunk still waiting for upload."""
        lst = [i for i in staged_by_chunk[chunk] if not i['started'] and not i['retired']]
        staged_by_chunk[chunk] = lst
        return len(lst)

    count = collections.defaultdict(int, init)
    last_commit, held = {}, {}
    pending, order = {}, [0]
    active = [None]  # (chunk, end, start_items)
    file_seq, seen_seq, compacted_since = [0], {}, set()
    freeze_iter = iter(freezes)

    def bucket(c):
        return 2 if count[c] >= 1000 else (1 if count[c] >= 100 else 0)

    def enqueue(c):
        if active[0] and active[0][0] == c:
            return
        if c not in pending:
            order[0] += 1
            pending[c] = order[0]

    def request(c, t, from_read):
        if from_read and count[c] < p.read_min:
            return
        if p.stage_min and waiting(c) >= p.stage_min:
            held.pop(c, None)
            enqueue(c)
            return
        if p.cooldown and count[c] < p.force and t - last_commit.get(c, -1e18) < p.cooldown:
            held[c] = t
            return
        held.pop(c, None)
        enqueue(c)

    def start_next(t):
        if active[0] or not pending:
            return
        c = min(pending, key=lambda k: (-bucket(k), pending[k]))
        del pending[c]
        items = [i for i in staged_by_chunk[c] if not i['started'] and not i['retired'] and i['t'] <= t]
        active[0] = (c, t + DUR, items, count[c])
        res['compactions'] += 1
        res['merged'] += count[c]

    def finish_until(t):
        while active[0] and active[0][1] <= t:
            c, end, items, merged = active[0]
            advance(end)
            for i in items:
                if not i['started']:
                    i['retired'] = True
                    res['retired_' + i['kind']] += 1
            new = count[c] - merged
            count[c] = 1 + new
            compacted_since.add(c)
            stage(end, c, math.ceil(obs_bytes.get(c, CH) / BLOCK), 'compaction')
            active[0] = None
            for hc in list(held):
                if end - last_commit.get(hc, -1e18) >= p.cooldown:
                    del held[hc]
                    enqueue(hc)
            start_next(end)

    events = [(t, 0, c, n) for t, c, _, n in freezes] + [(t, 1, c, 0) for t, c in reads]
    t = events[0][0]
    while t < tend:
        events.append((t, 2, -1, 0))
        t += 5
    events.sort()
    for t, kind, c, n in events:
        finish_until(t)
        advance(t)
        if kind == 0:
            stage(t, c, n, 'writer')
            res['frozen_writer_blocks'] += n
            prev = count[c]
            count[c] += 1
            last_commit[c] = t
            file_seq[0] += 1
            if count[c] >= p.force and c in held:
                del held[c]
                enqueue(c)
            elif wanted(prev, count[c]):
                request(c, t, False)
            elif p.stage_min and waiting(c) >= p.stage_min:
                request(c, t, False)
        elif kind == 1:
            if seen_seq.get(c) != file_seq[0] or c in compacted_since:
                seen_seq[c] = file_seq[0]
                compacted_since.discard(c)
                if count[c] >= 5:
                    request(c, t, True)
        else:
            for hc in list(held):
                if t - last_commit.get(hc, -1e18) >= p.cooldown:
                    del held[hc]
                    enqueue(hc)
        start_next(t)
    # Drain: run remaining compactions and uploads to completion (no new writes).
    t = tend
    while active[0] or pending or held:
        for hc in list(held):
            del held[hc]
            enqueue(hc)
        start_next(t)
        if active[0]:
            t = active[0][1]
            finish_until(t)
    advance(1e18)
    res['put_total'] = res['put_writer'] + res['put_compaction']
    return res


def main():
    """Calibrate on the observed retired share and compare policies."""
    log, acc, end, K, D, DUR = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
    freezes, init, obs_bytes, reads, tend = load(log, acc, end)
    print(f'frozen slices={len(freezes)} reads={len(reads)} K={K} D={D}s DUR={DUR}s')
    pols = [
        Policy('current (kaz.5)'),
        Policy('read-min 100', read_min=100),
        Policy('read-min 100 + cooldown 1m force 700', read_min=100, cooldown=60, force=700),
        Policy('read-min 100 + stage>=50', read_min=100, stage_min=50),
        Policy('read-min 100 + stage>=100', read_min=100, stage_min=100),
        Policy('read-min 100 + stage>=200', read_min=100, stage_min=200),
        Policy('read-min 100 + cooldown 1m force 700 + stage>=100', read_min=100, cooldown=60, force=700, stage_min=100),
    ]
    print(f'{"policy":50s} {"comp":>5s} {"PUTw":>6s} {"PUTc":>6s} {"PUT":>6s} {"retired_w":>9s} {"ret%":>5s}')
    for p in pols:
        r = simulate(p, freezes, init, obs_bytes, reads, tend, K, D, DUR)
        share = r['retired_writer'] / max(r['frozen_writer_blocks'], 1)
        print(f'{p.name:50s} {r["compactions"]:5d} {r["put_writer"]:6d} {r["put_compaction"]:6d} {r["put_total"]:6d} {r["retired_writer"]:9d} {share:5.0%}')


if __name__ == '__main__':
    main()
