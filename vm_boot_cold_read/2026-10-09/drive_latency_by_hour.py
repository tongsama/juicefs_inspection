"""JuiceFS の DEBUG ログから抜き出した GET の所要時間（4MiB ブロックのみ）を、日付と時刻ごと、および時刻（時）ごとに集計する。"""
import collections,sys
use=lambda f: f>='vm-io-20261008-070529.log'
rows=[]
for l in open(sys.argv[1]):
    p=l.split(' ',6)
    if len(p)<7 or not use(p[0]) or p[3]!='4194304': continue
    v=float(p[4])*{'s':1,'ms':1e-3,'µs':1e-6}[p[5]]
    err=p[6].strip()
    kind='ok' if err=='<nil>' else ('hdr' if 'no response headers' in err else 'err')
    rows.append((p[1],p[2][:2],v,kind))
def stat(vs):
    c=sorted(x[0] for x in vs); n=len(c)
    q=lambda r:c[min(n-1,int(n*r))]
    return n,q(.5),q(.9),q(.99),sum(1 for x in vs if x[0]>=8 or x[1]=='hdr')/n*100,sum(1 for x in vs if x[0]>=15)/n*100,sum(1 for x in vs if x[1]=='hdr'),sum(1 for x in vs if x[1]=='err')
by=collections.defaultdict(list); hod=collections.defaultdict(list)
for d,h,v,k in rows: by[(d,h)].append((v,k)); hod[h].append((v,k))
print('date       hh     n   p50   p90    p99  >=8s%  >=15s%  hdrTO  err')
for key in sorted(by):
    s=stat(by[key])
    if s[0]<30: continue
    print('%s %s %5d %5.2f %5.2f %6.2f %6.1f %6.1f %5d %4d'%(key[0],key[1],*s))
print('\nhour-of-day (all days)')
for h in sorted(hod):
    s=stat(hod[h])
    if s[0]<30: continue
    print('%s %5d %5.2f %5.2f %6.2f %6.1f %6.1f %5d %4d'%(h,*s))
