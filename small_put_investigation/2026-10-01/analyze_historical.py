"""Analyze an immutable byte-prefix read of the historical JuiceFS diagnostic log."""
import os,re,json,hashlib,collections
from pathlib import Path
P='/home/kwatanabe/.juicefs/diagnostics/vm-io-20261001-183557.log'
N=144316877

def rows():
    """Yield decoded lines only from the fixed original prefix."""
    with open(P,'rb') as f:
        while f.tell()<N:
            yield f.readline(N-f.tell()).decode('utf8','replace')

def describe(vals):
    """Summarize numeric observations with empirical nearest-rank percentiles."""
    v=sorted(vals)
    if not v:return {'count':0}
    import math
    return {'count':len(v),'sum':sum(v),'min':v[0],'median':v[(len(v)-1)//2],'p95':v[math.ceil(len(v)*.95)-1],'p99':v[math.ceil(len(v)*.99)-1],'max':v[-1]}

comp=set(); normal=set(); compinfo={}; commit=collections.Counter(); pending={}; cache=collections.Counter(); comp_phases=collections.Counter(); first=None; last=None; linecount=0
for s in rows():
    linecount+=1
    if re.match(r'2026/\d\d/\d\d ',s):
        first=first or s[:26];last=s[:26]
    m=re.search(r'compaction inode=(\d+) chunk=(\d+) slice=(\d+) phase=(\w+)',s)
    if m:
        inode,ch,sl,phase=m.groups();comp.add(int(sl));comp_phases[phase]+=1
        if phase=='object':compinfo[sl]={'inode':int(inode),'chunk':int(ch),'time':s[:26],'line':s.rstrip()}
    m=re.search(r'metadata write inode=(\d+) chunk=(\d+) slice=(\d+) phase=(\w+)',s)
    if m:normal.add(int(m[3]));commit[m[4]]+=1
    m=re.search(r'pending slice (\d+)-(\d+): \{id:(\d+) .*? length:(\d+) soff:(\d+) slen:(\d+)',s)
    if m:pending[m[3]]={'inode':int(m[1]),'chunk':int(m[2]),'length':int(m[4]),'soff':int(m[5]),'slen':int(m[6])}
    m=re.search(r'Cache file read data start (\d+) end (\d+)',s)
    if m:cache[int(m[2])-int(m[1])]+=1

puts=[]; otherputs=[]; ops=collections.Counter(); hist=collections.defaultdict(collections.Counter); errors=collections.defaultdict(collections.Counter); keys=collections.defaultdict(set); costs=collections.defaultdict(list); rawsum=collections.Counter(); bucket=collections.defaultdict(collections.Counter)
for s in rows():
    m=re.search(r'(?:slow request: )?(PUT|GET|DELETE|HEAD) (\S+) \(req_id: .*?err: (.*), cost: ([^)]+)\) \[logRequest@',s)
    if not m:continue
    op,key,err,cost=m.groups();ops[op]+=1
    if op!='PUT':continue
    k=re.search(r'/(\d+)_(\d+)_(\d+)$',key)
    if not k:otherputs.append(s.rstrip());continue
    sl,block,raw=map(int,k.groups())
    cat='compaction' if sl in comp else 'normal_metadata_write' if sl in normal else 'unclassified'
    if sl in comp and sl in normal:cat='both'
    hist[cat][raw]+=1;keys[cat].add(key);errors[cat][err]+=1;rawsum[cat]+=raw
    sec=0
    for num,unit in re.findall(r'([\d.]+)(h|ms|m|µs|ns|s)',cost):sec+=float(num)*{'h':3600,'m':60,'s':1,'ms':1e-3,'µs':1e-6,'ns':1e-9}[unit]
    costs[cat].append(sec)
    bucket[cat]['<=4KiB' if raw<=4096 else '<=8KiB' if raw<=8192 else '<=16KiB' if raw<=16384 else '<=64KiB' if raw<=65536 else '<1MiB' if raw<1048576 else '<4MiB' if raw<4194304 else '4MiB' if raw==4194304 else '>4MiB']+=1
    puts.append((cat,key,sl,raw,err,s[:26]))
# Hash only the analyzed byte prefix and report file size without changing original.
h=hashlib.sha256()
with open(P,'rb') as f:
    left=N
    while left:
        b=f.read(min(left,1048576))
        if not b: raise EOFError("Historical log no longer contains the complete recorded prefix")
        h.update(b);left-=len(b)
out={'path':P,'prefix_bytes':N,'sha256_prefix':h.hexdigest(),'size_after':os.stat(P).st_size,'line_count':linecount,'first_timestamp':first,'last_timestamp':last,'op_events':ops,'compaction_output_slices':len(comp),'normal_metadata_slices':len(normal),'intersection':sorted(comp&normal),'metadata_phases':commit,'compaction_phases':comp_phases,'put_by_class':{cat:{'events':sum(v.values()),'distinct_keys':len(keys[cat]),'raw_suffix_sum_per_event':rawsum[cat],'histogram':v,'size_buckets':bucket[cat],'errors':errors[cat],'cost_seconds':describe(costs[cat])} for cat,v in hist.items()},'other_puts':otherputs,'cache_read_size_histogram':cache,'cache_read_events':sum(cache.values()),'cache_read_observed_bytes':sum(k*v for k,v in cache.items()),'pending_distinct_slices':len(pending),'pending_length_histogram':collections.Counter(v['length'] for v in pending.values()),'pending_slen_histogram':collections.Counter(v['slen'] for v in pending.values()),'compaction_details':compinfo}
with open(str(Path(__file__).with_name('result.json')),'w') as f:json.dump(out,f,indent=2,ensure_ascii=False)
with open(str(Path(__file__).with_name('put-events.tsv')),'w') as f:
    f.write('class\tkey\tslice\traw_suffix_bytes\terr\ttimestamp\n')
    for r in puts:f.write('\t'.join(map(str,r))+'\n')
print(json.dumps({k:v for k,v in out.items() if k not in ('compaction_details','cache_read_size_histogram')},indent=2,ensure_ascii=False))
