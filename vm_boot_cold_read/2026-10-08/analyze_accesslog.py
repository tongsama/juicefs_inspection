"""accesslog から VM イメージ inode の read 並列度・待ち時間・ブロック局所性を集計する。"""
import re,sys,datetime as dt,collections
INO=sys.argv[2]; BS=4<<20
pat=re.compile(r'^(\S+ \S+) \[.*?pid:(\d+)\] (\w+) \((\d+),?([^)]*)\)(.*)<([\d.]+)>')
ev=[]
for l in open(sys.argv[1],errors='replace'):
    m=pat.match(l)
    if not m or m.group(4)!=INO: continue
    t=dt.datetime.strptime(m.group(1),'%Y.%m.%d %H:%M:%S.%f').timestamp()
    d=float(m.group(7)); a=m.group(5).split(',')
    ev.append(dict(op=m.group(3),end=t,start=t-d,dur=d,pid=m.group(2),args=a))
ev.sort(key=lambda e:e['start'])
t0=ev[0]['start']
fw=min((e['start'] for e in ev if e['op'] in('write','fallocate')),default=None)
print('first event',dt.datetime.fromtimestamp(t0),'first write +%.1fs'%(fw-t0) if fw else '')
rd=[e for e in ev if e['op']=='read']
for e in rd: e['size']=int(e['args'][0]); e['off']=int(e['args'][1])
print('reads',len(rd),'last read +%.1fs'%(rd[-1]['end']-t0))
def stats(rs,name):
    if not rs: return
    ds=sorted(e['dur'] for e in rs); n=len(ds)
    slow=[e for e in rs if e['dur']>0.05]
    tot=sum(ds)
    print(f'--- {name}: n={n} bytes={sum(e["size"] for e in rs)/2**20:.1f}MiB dur p50={ds[n//2]*1e3:.2f}ms p90={ds[int(n*.9)]*1e3:.1f}ms p99={ds[int(n*.99)]*1e3:.0f}ms max={ds[-1]:.2f}s sum={tot:.1f}s')
    print(f'    slow(>50ms) n={len(slow)} sum={sum(e["dur"] for e in slow):.1f}s')
    # 並列度: slow read が処理中の時間における同時 slow read 数の時間加重分布
    pts=sorted([(e['start'],1) for e in slow]+[(e['end'],-1) for e in slow])
    cur=0;last=None;hist=collections.Counter()
    for t,d in pts:
        if last is not None and cur>0: hist[cur]+=t-last
        cur+=d; last=t
    busy=sum(hist.values())
    print('    slow-read busy wall %.1fs; concurrency dist:'%busy, {k:round(v/busy*100,1) for k,v in sorted(hist.items())} if busy else '')
    # size 分布
    sz=collections.Counter(e['size'] for e in rs); print('    sizes',sz.most_common(6))
    sz=collections.Counter(e['size'] for e in slow); print('    slow sizes',sz.most_common(6))
    # ブロック局所性: slow read の 4MiB ブロック番号
    blks=[e['off']//BS for e in slow]
    ub=set(blks); print('    slow distinct 4MiB blocks',len(ub),'repeat-slow on same block',len(blks)-len(ub))
    seen=set();nxt=0;near=0
    for b in blks:
        if b-1 in seen: nxt+=1
        if any(b+k in seen for k in range(-4,5) if k): near+=1
        seen.add(b)
    print(f'    slow block whose prev block was already slow-fetched: {nxt}; within ±4 blocks: {near}')
    return slow
pre=[e for e in rd if fw is None or e['start']<fw]; post=[e for e in rd if fw and e['start']>=fw]
stats(rd,'all'); stats(pre,'before first write'); stats(post,'after first write')
# 時間推移(10s毎)
b=collections.defaultdict(lambda:[0,0,0.0])
for e in rd:
    k=int((e['start']-t0)//10); b[k][0]+=1; b[k][1]+= e['dur']>0.05; b[k][2]+=e['dur']
print('per10s: t n slow sumdur'); 
for k in sorted(b): print(k*10,*b[k][:2],round(b[k][2],1))
import pickle; pickle.dump(rd,open(sys.argv[1]+'.pkl','wb'))
