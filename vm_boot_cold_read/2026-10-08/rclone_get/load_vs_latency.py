"""分ごとの Drive API 推定負荷と、GET の lookup／download open 時間・PUT 系の関係を見る。"""
import sys, collections, statistics
sys.argv=['x']
import analyze as A
gets, api, s3ops, errors = A.parse_rclone('rclone_proc.log')
W=[g for g in gets if g['t_req']>=A.WIN_START and 't_r0' in g and 't_or' in g]
per=collections.Counter(int(t) for t,_ in api if t>=A.WIN_START)
bym=collections.defaultdict(list)
for g in W: bym[A.hms(g['t_or'])[:5]].append(g)
apm=collections.Counter(A.hms(t)[:5] for t,_ in api if t>=A.WIN_START)
print('minute api/s nGET dlopen_mean dlopen_p50 dlopen_p90 n>=15s lookup_mean')
for m in sorted(bym):
    gs=bym[m]; dl=[g['t_r0']-g['t_or'] for g in gs]; lk=[g['t_open']-g['t_req'] for g in gs]
    print(f"{m} {apm[m]/60:5.2f} {len(gs):4d} {statistics.mean(dl):5.2f} {A.pct(dl,.5):3.0f} {A.pct(dl,.9):3.0f} {sum(x>=15 for x in dl):3d} {statistics.mean(lk):4.2f}")
# 遅い GET の待ち区間中の API 負荷
print('\nslow GET (dlopen>=15s): 待ち区間の API 推定/s、同区間に開始→完了した他の GET の dlopen')
for g in sorted([g for g in W if g['t_r0']-g['t_or']>=15], key=lambda g:g['t_or']):
    a,b=int(g['t_or']),int(g['t_r0'])
    load=sum(per.get(s,0) for s in range(a,b+1))/(b-a+1)
    others=[h['t_r0']-h['t_or'] for h in W if h is not g and a<=h['t_or'] and h['t_r0']<=b]
    print(f"  {A.hms(g['t_or'])}-{A.hms(g['t_r0'])} {g['key'][-26:]:>26} wait={b-a:2d}s api={load:5.2f}/s others n={len(others)} max={max(others) if others else '-'} mean={statistics.mean(others) if others else 0:.2f}")
