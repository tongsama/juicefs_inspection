"""全 rclone ログ（読み取りのみ）で openRange→最初の Read 間隔の分布を求め、~30s の上限が Drive 側にあるかを見る。"""
import sys, re, collections, datetime as dt
pat=re.compile(r'^(\S+ \S+) DEBUG : buckets/juicefs-data/(chunks/\S+): ChunkedReader\.(openRange at 0|Read at 0 )')
for p in sys.argv[1:]:
    pend=collections.defaultdict(list); hist=collections.Counter(); big=[]
    for l in open(p,errors='replace'):
        if 'ChunkedReader' not in l: continue
        m=pat.match(l)
        if not m: continue
        t=dt.datetime.strptime(m.group(1),'%Y/%m/%d %H:%M:%S').timestamp()
        k=m.group(2)
        if m.group(3).startswith('openRange'): pend[k].append(t)
        elif pend[k]:
            t0=pend[k].pop(0); d=int(t-t0); hist[min(d,999)]+=1
            if d>=31: big.append((m.group(1),k,d))
    n=sum(hist.values())
    print(p.split('/')[-1], 'n=',n, '>=15s',sum(v for k,v in hist.items() if k>=15),'>=25s',sum(v for k,v in hist.items() if k>=25),'>=31s',sum(v for k,v in hist.items() if k>=31),'max',max(hist) if hist else None, big[:5])
