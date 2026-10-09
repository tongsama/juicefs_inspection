"""API 推定負荷（前後 5 秒平均）でクラス分けし、GET の lookup／download open（<5s のみ）平均を比べる。"""
import sys, collections, statistics
sys.argv=['x']
import analyze as A
gets, api, _, _ = A.parse_rclone('rclone_proc.log')
per=collections.Counter(int(t) for t,_ in api if t>=A.WIN_START)
cls=collections.defaultdict(lambda: ([],[]))
for g in gets:
    if g['t_req']<A.WIN_START or 't_r0' not in g or 't_or' not in g: continue
    s=int(g['t_or']); load=sum(per.get(x,0) for x in range(s-5,s+6))/11
    c='<5/s' if load<5 else '5-12/s' if load<12 else '12-18/s' if load<18 else '>=18/s'
    dl=g['t_r0']-g['t_or']
    if dl<5: cls[c][1].append(dl)
    if not g['cached']: cls[c][0].append(g['t_open']-g['t_req'])
for c in ['<5/s','5-12/s','12-18/s','>=18/s']:
    lk,dl=cls[c]
    print(f'{c:8s} lookup(GETのうちlookupあり) n={len(lk)} mean={statistics.mean(lk):.2f}s | dlopen(<5s) n={len(dl)} mean={statistics.mean(dl):.2f}s')
