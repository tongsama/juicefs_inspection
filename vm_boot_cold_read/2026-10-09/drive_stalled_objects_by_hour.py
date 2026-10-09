"""時刻（日付＋時）ごとに、8s 以上かかった、または header-timeout で切られた 4MiB の object を重複なしで数える。"""
import sys,collections
keys=collections.defaultdict(set); allk=collections.defaultdict(set)
for l in open(sys.argv[1]):
    p=l.split(' ',5)
    try:
        d,h,k,v,u,err=p; v=float(v)*{'s':1,'ms':1e-3,'µs':1e-6}[u]
    except (ValueError,KeyError):
        continue
    allk[(d,h)].add(k)
    if v>=8 or 'no response headers' in err: keys[(d,h)].add(k)
print('date       hh  objects  stalled(>=8s)  per1000')
for key in sorted(allk):
    a=len(allk[key]); s=len(keys[key])
    if a<30: continue
    print('%s %s %7d %8d %10.1f'%(key[0],key[1],a,s,s/a*1000))
