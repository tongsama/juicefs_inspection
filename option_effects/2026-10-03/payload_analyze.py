"""Read each manifest prefix once; aggregate PUT and GC by PID without mutating source logs."""
import json,re,datetime,collections,csv,time,array,os
BASE=os.path.dirname(__file__)
manifest=json.load(open(BASE+'/manifest.json'))
header=re.compile(rb'^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)\.\d+ juicefs\[(\d+)\]')
keypat=re.compile(rb'chunks/[^ /]+/[^ /]+/(\d+)_(\d+)_(\d+)')
putpat=re.compile(rb'\b(PUT|DELETE) (chunks/\S+) .*?err: (.*?), cost: ([^ )]+)')
slicepat=re.compile(rb'slice=(\d+)')
lengthpat=re.compile(rb'raw_length=(\d+)')
payloadpat=re.compile(rb'payload_bytes=(\d+)')
units={'ns':1e-9,'us':1e-6,'µs':1e-6,'ms':1e-3,'s':1,'m':60,'h':3600}

def duration(raw):
    """Decode Go durations used by request logging."""
    return sum(float(x)*units[u] for x,u in re.findall(r'([\d.]+)(ns|us|µs|ms|s|m|h)',raw.decode()))

def hist_value(n):
    """Use bounded histogram bins while retaining exact threshold counts."""
    previous=0
    for threshold in [1024,4096,16384,65536,262144,1048576,4194304]:
        if n<threshold:return str(previous)+'..'+str(threshold-1)
        previous=threshold
    return '>=4194304'

results=[]; binrows=[]
for f in manifest['files']:
    epochs={};puts=array.array('q'); laststamp=b''; ts=0
    def epoch(pid):
        """Keep epoch-local compact maps and counters."""
        if pid not in epochs:
            epochs[pid]={'start':None,'end':None,'normal':{},'comp':set(),'recovered':set(),'deletekeys':set(),'dropkeys':set(),'retired':set(),'counters':collections.Counter(),'delete_latency':[], 'firstputs':set()}
        return epochs[pid]
    with open(f['path'],'rb') as inp:
        left=f['prefix_bytes']; lines=0
        while left>0:
            line=inp.readline(left);left-=len(line)
            if not line:break
            lines+=1
            h=header.match(line)
            if not h:continue
            stamp=h[1];pid=int(h[2]);e=epoch(pid)
            if stamp!=laststamp:
                ts=int(datetime.datetime.strptime(stamp.decode(),'%Y/%m/%d %H:%M:%S').timestamp());laststamp=stamp
            if e['start'] is None:e['start']=ts
            e['end']=ts
            if b'slice finish ' in line:
                sm=slicepat.search(line);lm=lengthpat.search(line)
                if sm and lm:e['normal'][int(sm[1])]=int(lm[1])
            elif b'compaction ' in line and b'phase=object' in line:
                sm=slicepat.search(line)
                if sm:e['comp'].add(int(sm[1]))
            elif b'Found staging block:' in line:
                k=keypat.search(line)
                if k:e['recovered'].add(tuple(map(int,k.groups())))
            elif b'slice commit ' in line and b'phase=metadata' in line:e['counters']['metadata_started']+=1
            if b'compaction GC local retirement' in line:
                sm=slicepat.search(line)
                if b'err=<nil>' in line:
                    e['counters']['retirement_success']+=1
                    if sm:e['retired'].add(int(sm[1]))
                else:
                    e['counters']['retirement_error']+=1
                    if b'in flight' in line or b'in-flight' in line:e['counters']['retirement_busy']+=1
            if b'compaction GC remote deferred' in line:e['counters']['remote_deferred']+=1
            if b'is not needed' in line:
                k=keypat.search(line)
                if b'drop it' in line:e['counters']['notneeded_drop_lines']+=1
                if b'abandoned' in line:e['counters']['abandoned_lines']+=1
                if k:e['dropkeys'].add(tuple(map(int,k.groups())))
            if b'PUT chunks/' not in line and b'DELETE chunks/' not in line:continue
            p=putpat.search(line);k=keypat.search(line)
            if not p or not k:e['counters']['request_parse_missing']+=1;continue
            sid,idx,raw=map(int,k.groups());ok=p[3]==b'<nil>';cost=int(duration(p[4])*1e6)
            if p[1]==b'DELETE':
                e['counters']['delete_success' if ok else 'delete_failed']+=1
                e['delete_latency'].append(cost)
                if ok:e['deletekeys'].add((sid,idx,raw))
            else:
                pm=payloadpat.search(line);payload=int(pm[1]) if pm else -1
                puts.extend([pid,ts,sid,idx,raw,payload,int(ok),cost])
    summaries={};bins={}
    for i in range(0,len(puts),8):
        pid,ts,sid,idx,raw,payload,ok,cost=puts[i:i+8];e=epochs[pid];key=(sid,idx,raw)
        cats=[]
        if sid in e['normal']:cats.append('normal')
        if sid in e['comp']:cats.append('compaction')
        if key in e['recovered']:cats.append('startup_staging')
        cat='+'.join(cats) or 'unclassified'
        c=summaries.setdefault((pid,cat),collections.Counter())
        c['success' if ok else 'failed']+=1;c['raw_bytes_attempted']+=raw;c['payload_bytes_attempted']+=max(payload,0);c['latency_us_total']+=cost
        if payload<0:c['payload_missing']+=1
        else:c['payload_range_'+hist_value(payload)]+=1
        if ok:
            c['success_raw_bytes']+=raw;c['success_payload_bytes']+=max(payload,0)
            if 0<=payload<1024:c['success_payload_lt1KiB']+=1
            if raw==4096:c['success_raw_4KiB']+=1
        if key not in e['firstputs']:
            c['unique_put_keys']+=1;e['firstputs'].add(key)
        b=bins.setdefault((pid,ts//60*60,cat),collections.Counter());b['calls']+=1;b['success']+=ok;b['raw_bytes']+=raw;b['payload_bytes']+=max(payload,0)
    fileout={'file':f,'lines':lines,'epochs':[]}
    for pid,e in epochs.items():
        secs=max(e['end']-e['start'],1);lat=sorted(e['delete_latency']);q=lambda p:lat[min(int(len(lat)*p),len(lat)-1)]/1e6 if lat else None
        sizes=list(e['normal'].values());normalputs=summaries.get((pid,'normal'),{})
        fileout['epochs'].append({'pid':pid,'start':datetime.datetime.fromtimestamp(e['start']).isoformat(),'end':datetime.datetime.fromtimestamp(e['end']).isoformat(),'seconds':secs,'normal_finish_ids':len(sizes),'normal_raw_slice_bytes':sum(sizes),'normal_slice_lt1KiB':sum(x<1024 for x in sizes),'normal_slice_4KiB':sum(x==4096 for x in sizes),'compaction_output_ids':len(e['comp']),'startup_stage_keys':len(e['recovered']),'counters':dict(e['counters']),'delete_latency_seconds':{'p50':q(.5),'p95':q(.95),'p99':q(.99),'max':q(1)},'put_categories':{cat:dict(c) for (p,cat),c in summaries.items() if p==pid},'normal_put_calls_per_finish':(normalputs.get('success',0)+normalputs.get('failed',0))/len(sizes) if sizes else None,'correlations':{'put_and_success_delete_keys_unordered':len(e['firstputs']&e['deletekeys']),'notneeded_without_any_put_keys':len(e['dropkeys']-e['firstputs']),'retired_normal_ids_without_put':len((e['retired']&set(e['normal']))-{k[0] for k in e['firstputs']}),'startup_stage_without_put_keys':len(e['recovered']-e['firstputs'])}})
    results.append(fileout)
    for (pid,minute,cat),c in bins.items():binrows.append([os.path.basename(f['path']),pid,datetime.datetime.fromtimestamp(minute).isoformat(),cat,c['calls'],c['success'],c['raw_bytes'],c['payload_bytes']])
    print(os.path.basename(f['path']),lines,'PUTrecords',len(puts)//8,flush=True)
json.dump({'method':'single prefix streaming pass; PID epochs; request completion timestamps; attempted bytes include errors/retries; unordered key correlations do not establish event order; no workload write-byte counter present','files':results},open(BASE+'/payload_summary.json','w'),indent=2)
with open(BASE+'/payload_minute_bins.csv','w') as out:
    w=csv.writer(out);w.writerow(['file','pid','minute_local','category','calls','success','raw_bytes_attempted','payload_bytes_attempted']);w.writerows(binrows)
