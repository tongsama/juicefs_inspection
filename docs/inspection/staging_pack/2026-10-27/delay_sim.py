"""pack_sim に upload-delay を足した模擬。事象を時刻順に処理する。

item は freeze 時刻 t に staging され、t+delay から列に並べる（delay>0 では即時の経路を使わない）。
compaction の片付けは freeze の時点で数える。pack は worker が取り出したときに、
同じ chunk の待ち中の小さい item を集める（respect=True なら delay を過ぎたものだけ）。
"""
import collections, heapq, sys
sys.path.insert(0, '.')
from pack_sim import load, slices, items, B


def run(sl, delay, pack, respect, workers=18, service=3.7, trigger=30, cap=4 << 20):
    """(PUT 回数, 片付けた数) を返す。"""
    its = items(sl)
    ev = []  # (時刻, 種類 0=freeze 1=eligible, idx)
    for i, it in enumerate(its):
        heapq.heappush(ev, (it[0], 0, i))
    alive = {}            # idx -> item（staging にあって未 PUT）
    eligible = set()
    queue = collections.deque()  # 列（eligible の順）
    enq = {}              # idx -> 列に並んだ時刻
    bychunk = collections.defaultdict(set)
    newrec = collections.Counter(); seen = set()
    free = [0.0] * workers
    puts = retired = 0

    def take(now):
        nonlocal puts
        while queue and free[0] <= now:
            i = queue.popleft()
            if i not in alive:
                continue
            st = heapq.heappop(free)
            it = alive.pop(i); bychunk[it[1]].discard(i)
            puts += 1
            if pack and it[3] < B:
                size = it[3]
                for j in sorted(bychunk[it[1]]):
                    o = alive[j]
                    if o[3] >= B or size + o[3] > cap or (respect and j not in eligible):
                        continue
                    size += o[3]; del alive[j]; bychunk[it[1]].discard(j)
            heapq.heappush(free, max(st, enq[i]) + service)

    while ev:
        t, kind, i = heapq.heappop(ev)
        while queue and free[0] <= t:
            take(free[0])
        it = its[i]
        if kind == 0:
            if delay == 0 and not queue and free[0] <= t:
                st = heapq.heappop(free); heapq.heappush(free, t + service); puts += 1
            else:
                alive[i] = it; bychunk[it[1]].add(i)
                heapq.heappush(ev, (t + delay, 1, i))
            if it[2] not in seen:
                seen.add(it[2]); newrec[it[1]] += 1
                if newrec[it[1]] >= trigger:
                    newrec[it[1]] = 0
                    for j in list(bychunk[it[1]]):
                        if alive[j][3] < B:
                            del alive[j]; bychunk[it[1]].discard(j); retired += 1
        else:
            if i in alive:
                eligible.add(i); queue.append(i); enq[i] = t
    while queue:
        take(free[0])
    return puts, retired


if __name__ == '__main__':
    sl = slices(load(sys.argv[1]))
    for d in (0, 10, 30, 60, 120):
        b = run(sl, d, False, False)
        r = run(sl, d, True, True)
        a = run(sl, d, True, False)
        print(f'delay={d:>3}s base PUT={b[0]} retired={b[1]} | pack(delay を守る) PUT={r[0]} ({(r[0]-b[0])/b[0]*100:+.0f}%) | pack(若いものも取る) PUT={a[0]} ({(a[0]-b[0])/b[0]*100:+.0f}%)')
