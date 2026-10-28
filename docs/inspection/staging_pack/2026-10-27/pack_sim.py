"""staging のアップロード待ちの列で、同じ chunk の小さい block をまとめたときの PUT 回数を模擬する。

入力: trace.tsv（write の行だけを使う）。出力: 方式ごとの PUT 回数と片付けた（retire）数。
単純化（推論の材料であり、実装の挙動の再現ではない）:
- slice: 同じ chunk の開いた slice の範囲 [start, end] に触れる write は延長、ほかは新しい slice。
  最後の write から idle 秒、または最初の write から wait 秒で freeze する。
- freeze した slice は 4 MiB の block ごとに staging の item になる（4 MiB 未満を「小さい」とする）。
- 列は FIFO、同時 workers、PUT 1 回 service 秒で一定。compaction 自身の PUT は数えない。
- compaction: chunk ごとに freeze した slice が trigger 個たまるたびに、その chunk の待ち中の小さい item を片付ける。
- pack: worker が小さい item を取ったとき、同じ chunk の待ち中の小さい item を cap バイトまで 1 PUT にまとめる。
"""
import collections
import heapq
import sys

C = 64 << 20
B = 4 << 20


def load(path):
    """trace.tsv から (時刻 秒, offset, 長さ) の write の列を読む。"""
    ws = []
    with open(path) as f:
        for line in f:
            p = line.split('\t')
            if p[3] == 'write':
                ws.append((int(p[0]) / 1e6, int(p[4]), int(p[5])))
    ws.sort()
    return ws


def slices(ws, idle=16.0, wait=30.0):
    """write の列から freeze した slice の (freeze 時刻, chunk, start, end) を作る。"""
    open_ = collections.defaultdict(list)  # chunk -> [first, last, start, end]
    out = []

    def expire(now):
        for c, lst in list(open_.items()):
            keep = []
            for s in lst:
                if now - s[1] >= idle or now - s[0] >= wait:
                    out.append((min(s[1] + idle, s[0] + wait), c, s[2], s[3]))
                else:
                    keep.append(s)
            open_[c] = keep

    last_exp = 0
    for t, off, n in ws:
        if t - last_exp > 0.5:
            expire(t)
            last_exp = t
        c, o = off // C, off % C
        for s in open_[c]:
            if s[2] <= o <= s[3]:
                s[1] = t
                s[3] = max(s[3], o + n)
                break
        else:
            open_[c].append([t, t, o, o + n])
    expire(float('inf'))
    out.sort()
    return out


def items(sl):
    """slice を 4 MiB の block ごとの staging の item (時刻, chunk, slice 番号, 長さ) に分ける。"""
    res = []
    for i, (t, c, s, e) in enumerate(sl):
        b = s // B
        while b * B < e:
            lo, hi = max(s, b * B), min(e, (b + 1) * B)
            res.append((t, c, i, hi - lo))
            b += 1
    return res


def run(sl, workers=18, service=3.7, trigger=30, pack=False, cap=4 << 20):
    """FIFO の列を模擬して (PUT 回数, 片付けた item 数, まとめた item 数) を返す。"""
    its = items(sl)
    pending = collections.OrderedDict()  # idx -> item
    bychunk = collections.defaultdict(set)
    newrec = collections.Counter()
    seen_slice = set()
    free = [0.0] * workers  # 各 worker の空く時刻
    heapq.heapify(free)
    puts = retired = packed = 0
    k = 0

    def dispatch(now):
        nonlocal puts, packed
        while pending and free[0] <= now:
            st = heapq.heappop(free)
            idx, it = pending.popitem(last=False)
            bychunk[it[1]].discard(idx)
            puts += 1
            if pack and it[3] < B:
                size = it[3]
                for j in sorted(bychunk[it[1]]):
                    o = pending[j]
                    if o[3] >= B or size + o[3] > cap:
                        continue
                    size += o[3]
                    del pending[j]
                    bychunk[it[1]].discard(j)
                    packed += 1
            heapq.heappush(free, max(st, it[0]) + service)

    for it in its:
        t = it[0]
        # 空いた worker から順に、この時刻までに始められる PUT を進める
        while pending and free[0] <= t:
            dispatch(free[0])
        if not pending and free[0] <= t:
            # 空いた worker があれば即座にアップロード（upload-delay 0 の経路）
            st = heapq.heappop(free)
            heapq.heappush(free, t + service)
            puts += 1
        else:
            pending[k] = it
            bychunk[it[1]].add(k)
        k += 1
        if it[2] not in seen_slice:
            seen_slice.add(it[2])
            newrec[it[1]] += 1
            if newrec[it[1]] >= trigger:
                newrec[it[1]] = 0
                for j in list(bychunk[it[1]]):
                    if pending[j][3] < B:
                        del pending[j]
                        bychunk[it[1]].discard(j)
                        retired += 1
    while pending:
        dispatch(free[0])
    return puts, retired, packed, len(its)


if __name__ == '__main__':
    ws = load(sys.argv[1])
    sl = slices(ws)
    print('slices', len(sl), 'items', len(items(sl)),
          'small', sum(1 for x in items(sl) if x[3] < B))
    for trig in (99, 30):
        for workers, service in ((18, 3.7), (18, 2.0), (36, 3.7)):
            base = run(sl, workers, service, trig)
            for cap in (1 << 20, 4 << 20, 16 << 20):
                pk = run(sl, workers, service, trig, pack=True, cap=cap)
                print(f'trigger={trig} workers={workers} svc={service} '
                      f'base PUT={base[0]} retired={base[1]} | pack cap={cap >> 20}MiB '
                      f'PUT={pk[0]} retired={pk[1]} packed={pk[2]} '
                      f'({(pk[0] - base[0]) / base[0] * 100:+.0f}%)')
