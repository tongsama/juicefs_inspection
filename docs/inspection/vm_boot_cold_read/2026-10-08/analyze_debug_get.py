"""DEBUG ログ断片から GET の並列度・所要時間、Read 前 flush 待ち、キャッシュ追い出しと再 GET を集計する。"""
import re,sys,datetime as dt,collections,pickle
ts=lambda s: dt.datetime.strptime(s,'%Y/%m/%d %H:%M:%S.%f').timestamp()
def cost(s):
    m=re.match(r'([\d.]+)(ms|µs|s)',s); v=float(m.group(1)); return v*{'s':1,'ms':1e-3,'µs':1e-6}[m.group(2)]
gets=[];flush=[];evict=[];cancel=0;getkeys=collections.Counter()
for l in open(sys.argv[1],errors='replace'):
    p=l.split(' ',4)
    if len(p)<5: continue
    if re.search(r'(: |request: )GET chunks/', l) and 'cost:' in l:
        m=re.search(r'GET (chunks/\S+) .*?err: (.*?), cost: (\S+)\)',l)
        t=ts(p[0]+' '+p[1]); c=cost(m.group(3)); gets.append((t-c,t,m.group(1),m.group(2)))
    elif 'origin=vfs.Read' in l and 'phase=end' in l:
        m=re.search(r'origin=(\S+) .*elapsed=(\S+)',l); flush.append((ts(p[0]+' '+p[1]),m.group(1),cost(m.group(2))))
    elif 'remove ' in l and 'from cache, age' in l:
        m=re.search(r'remove (\S+) from cache, age: (\d+)s',l); evict.append((ts(p[0]+' '+p[1]),m.group(1),int(m.group(2))))
    elif 'context canceled' in l and 'fail to read' in l: cancel+=1
print('GET n',len(gets),'errs',sum(g[3]!='<nil>' for g in gets))
cs=sorted(g[1]-g[0] for g in gets); n=len(cs)
print('GET cost p50 %.2fs p90 %.2fs max %.2fs sum %.0fs'%(cs[n//2],cs[int(n*.9)],cs[-1],sum(cs)))
sizes=collections.Counter(int(g[2].rsplit('_',1)[1])>=4<<20 for g in gets); print('GET full-4MiB-blocks?',sizes)
pts=sorted([(g[0],1) for g in gets]+[(g[1],-1) for g in gets]); cur=0;last=None;h=collections.Counter()
for t,d in pts:
    if last is not None and cur>0: h[cur]+=t-last
    cur+=d;last=t
b=sum(h.values()); print('GET busy wall %.0fs conc:'%b,{k:round(v/b*100,1) for k,v in sorted(h.items())})
k=collections.Counter(g[2] for g in gets); print('GET repeated keys',sum(v-1 for v in k.values()))
fl=[f for f in flush if f[2]>0.05]; print('Read-flush waits >50ms: n',len(fl),'sum %.0fs'%sum(f[2] for f in fl),'max %.1fs'%max((f[2] for f in flush),default=0), collections.Counter(f[1] for f in fl))
print('evictions',len(evict),'age p50',sorted(e[2] for e in evict)[len(evict)//2] if evict else '-', 'age<600s',sum(e[2]<600 for e in evict))
ek=set(e[1].split('_')[0] for e in evict); print('GET of slices evicted earlier in window:', sum(1 for g in gets if g[2].split('/')[-1].split('_')[0] in ek))
print('canceled slice reads',cancel)
pickle.dump(dict(gets=gets,flush=flush,evict=evict),open(sys.argv[1]+'.pkl','wb'))
