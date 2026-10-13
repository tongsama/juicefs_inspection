"""JuiceFS 側にログの無い rclone GET（未照合 GET）を抽出し、読み出し量と broken pipe の関係を見る。"""
import sys, collections
sys.argv=['x','rclone_proc.log','../debug.part']
import analyze as A
gets, api, s3ops, errors = A.parse_rclone('rclone_proc.log')
jfs = A.parse_juicefs('../debug.part')
END = A.dt.datetime(2026,10,8,21,48,13).timestamp()
W=[g for g in gets if A.WIN_START<=g['t_req']<=END]
byk=collections.defaultdict(list)
for g in W: byk[g['key']].append(g)
for j in jfs:
    if j['op']!='GET': continue
    c=[g for g in byk.get(j['key'],[]) if abs(g['t_req']-j['start'])<=2 and not g.get('_m')]
    if c:
        g=min(c,key=lambda g:abs(g['t_req']-j['start'])); g['_m']=True; g['jcost']=j['cost']
um=[g for g in W if not g.get('_m')]
print('窓内(〜21:48:13) rclone GET',len(W),'未照合',len(um))
# 未照合 key が JuiceFS 側で他の文脈で出るか
keys=set(g['key'] for g in um)
seen=collections.Counter()
for line in open('../debug.part',errors='replace'):
    if 'chunks/' in line:
        i=line.find('chunks/'); k=line[i:].split()[0].rstrip(':,)')
        if k in keys: seen[k]+=1
print('未照合 key のうち debug.part に何らかの言及がある key', len(seen), '/', len(keys))
# 未照合 GET の同一 key の照合済み GET が直後にあるか
dup=0
for g in um:
    if any(h.get('_m') and 0<=h['t_req']-g['t_req']<=60 for h in byk[g['key']]): dup+=1
print('未照合 GET のうち 60s 以内に同じ key の照合済み GET がある数', dup)
ex=um[:5]
for g in ex: print(' ', g['key'], A.hms(g['t_req']), {k:A.hms(v) for k,v in g.items() if k.startswith('t_')})
def d(gs,a,b): return [g[b]-g[a] for g in gs if a in g and b in g]
for nm,gs in (('未照合(キャンセル推定)',um),('照合済み',[g for g in W if g.get('_m')])):
    dl=d(gs,'t_or','t_r0'); bd=d(gs,'t_r0','t_rl'); lk=d(gs,'t_req','t_open')
    print(nm,len(gs),'dlopen hist',sorted(collections.Counter(int(x) for x in dl).items())[:12],' body max',max(bd),' lookup mean %.2f'%(sum(lk)/len(lk)))
# 未照合 GET の最後の Read オフセット
import re
last=collections.Counter()
