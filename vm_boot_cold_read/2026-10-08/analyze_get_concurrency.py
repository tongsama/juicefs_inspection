"""GET の並列度と所要時間・合計スループットの関係、および同一 slice 内の次ブロック取得率（先読みの当たり見込み）を調べる。"""
import pickle,collections,re
d=pickle.load(open(__import__('sys').argv[1],'rb')); g=sorted(d['gets'])
print('errors:',[(x[2],x[3][:60]) for x in g if x[3]!='<nil>'][:8])
# 各 GET の開始時点での同時 GET 数と所要時間
by=collections.defaultdict(list)
for s,e,k,err in g:
    c=sum(1 for s2,e2,_,_ in g if s2<=s<e2)
    by[min(c,8)].append(e-s)
for c in sorted(by): v=sorted(by[c]); print('conc',c,'n',len(v),'p50 %.2fs'%v[len(v)//2],'MiB/s per GET %.2f'%(4/v[len(v)//2]))
# 1 秒ごとの合計ダウンロード量の分布（帯域の天井確認）
sec=collections.Counter()
for s,e,k,err in g:
    sz=int(k.rsplit('_',1)[1]); dur=max(e-s,1e-3)
    t=int(s)
    while t<e: sec[t]+=sz/dur*(min(e,t+1)-max(s,t))/2**20; t+=1
v=sorted(sec.values()); print('aggregate MiB/s per active sec p50 %.1f p90 %.1f max %.1f'%(v[len(v)//2],v[int(len(v)*.9)],v[-1]))
# 次ブロック先読みの当たり見込み
keys=[(k.split('/')[-1].split('_')) for _,_,k,_ in g]
seen=set();hit1=hit2=0;slices=collections.Counter()
for sid,idx,sz in keys:
    idx=int(idx)
    if (sid,idx-1) in seen: hit1+=1
    if (sid,idx-1) in seen or (sid,idx-2) in seen: hit2+=1
    seen.add((sid,idx)); slices[sid]+=1
print('GETs',len(keys),'distinct slices',len(slices),'GETs whose prev block(same slice) fetched earlier',hit1,'within prev 2',hit2)
print('blocks per slice dist',collections.Counter(min(v,10) for v in slices.values()))
