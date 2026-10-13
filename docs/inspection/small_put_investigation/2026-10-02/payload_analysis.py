"""Analyze PUT payloads from a fixed, complete-line prefix of the stable diagnostic log."""
import os,re,json,hashlib,csv,collections,statistics,math
from pathlib import Path
LOG=Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-014922.log')
OUT=Path(__file__).parent
CUTOFF='2026/10/02 01:54:30'
FIXED_PREFIX=179156595  # Common complete-line boundary selected by the coordinating analysis.

def select_prefix():
    """Freeze fstat size and omit the unfinished last line at that byte boundary."""
    with LOG.open('rb') as f:
        st=os.fstat(f.fileno());end=st.st_size
        f.seek(max(0,end-1048576));tail=f.read(end-f.tell())
        final_newline=tail.rfind(b'\n')
        if final_newline<0:raise RuntimeError('No full line in last MiB')
        complete=end-len(tail)+final_newline+1
        if FIXED_PREFIX is not None:
            if FIXED_PREFIX>complete:raise RuntimeError('Common prefix exceeds available complete lines')
            f.seek(FIXED_PREFIX-1)
            if f.read(1)!=b'\n':raise RuntimeError('Common prefix is not a complete-line boundary')
            complete=FIXED_PREFIX
        return {'fstat_bytes':end,'prefix_bytes':complete,'bytes_after_fixed_prefix_at_selection':end-complete, 'excluded_partial_bytes':0 if FIXED_PREFIX is not None else end-complete,'inode':st.st_ino,'mtime_ns_at_selection':st.st_mtime_ns}

def lines(limit):
    """Read complete lines without consuming any bytes beyond the frozen prefix."""
    with LOG.open('rb') as f:
        while f.tell()<limit:yield f.readline(limit-f.tell())

def numeric(values):
    """Return size totals, average, conventional median, and nearest-rank percentiles."""
    v=sorted(values)
    if not v:return {'count':0,'sum':0}
    return {'count':len(v),'sum':sum(v),'mean':statistics.mean(v),'median':statistics.median(v),'min':v[0],'p95':v[math.ceil(.95*len(v))-1],'p99':v[math.ceil(.99*len(v))-1],'max':v[-1]}

def bucket(n):
    """Assign byte lengths to explicit, mutually exclusive histogram buckets."""
    return '<1024' if n<1024 else '1024..4095' if n<4096 else '4096..16383' if n<16384 else '16384..65535' if n<65536 else '65536..1048575' if n<1048576 else '1048576..4194303' if n<4194304 else '>=4194304'

def summarize(events):
    """Summarize PUT attempts, successful requests and failures without deduplicating retries."""
    result={}
    for status in ('all','success','failure'):
        rr=[r for r in events if status=='all' or r['status']==status]
        raw=[r['raw_bytes'] for r in rr];payload=[r['payload_bytes'] for r in rr if r['payload_bytes'] is not None]
        result[status]={'events':len(rr),'distinct_keys':len({r['key'] for r in rr}),'raw_bytes':numeric(raw),'payload_bytes':numeric(payload),'payload_missing':len(rr)-len(payload),'payload_histogram':dict(collections.Counter(bucket(n) for n in payload)),'raw_histogram':dict(collections.Counter(bucket(n) for n in raw)),'payload_lt1024':numeric([n for n in payload if n<1024]),'raw_exact_top20':collections.Counter(raw).most_common(20),'payload_exact_top20':collections.Counter(payload).most_common(20)}
        if raw and len(payload)==len(raw):result[status]['payload_sum_over_raw_sum']=sum(payload)/sum(raw)
    return result

def main():
    """Collect classification evidence and emit reproducible aggregate and event artifacts."""
    snapshot=select_prefix();n=snapshot['prefix_bytes'];h=hashlib.sha256();comp=set();normal=set();stage=set();first=None;last=None;count=0;pidcounts=collections.Counter();versions=[]
    for b in lines(n):
        h.update(b);count+=1;s=b.decode('utf8','replace')
        if re.match(r'2026/\d\d/\d\d ',s):first=first or s[:26];last=s[:26]
        pm=re.search(r'juicefs\[(\d+)\]',s)
        if pm:pidcounts[pm[1]]+=1
        if 'JuiceFS version' in s:versions.append(s.rstrip())
        m=re.search(r'compaction .*?slice=(\d+) phase=object',s)
        if m:comp.add(int(m[1]))
        m=re.search(r'metadata write .*?slice=(\d+) phase=',s)
        if m:normal.add(int(m[1]))
        m=re.search(r'Found staging block: .*?/rawstaging/(chunks/\S+) ',s)
        if m:stage.add(m[1])
    events=[];unmatched=[];minute=collections.defaultdict(lambda: {'put_count':0,'success_count':0,'failure_count':0,'raw_sum':0,'payload_sum':0,'payload_lt1024_count':0})
    for b in lines(n):
        s=b.decode('utf8','replace')
        if not re.search(r'\bPUT ',s) or '[logRequest@' not in s:continue
        m=re.search(r'\bPUT (\S+)(?: payload_bytes=(\d+))? \(req_id: .*?, err: (.*), cost: ([^)]+)\) \[logRequest@',s)
        if not m:unmatched.append(s.rstrip());continue
        key,payload,err,cost=m.groups();k=re.search(r'/(\d+)_(\d+)_(\d+)$',key)
        if not k:unmatched.append(s.rstrip());continue
        slice_id,block,raw=map(int,k.groups());classes=[]
        if slice_id in comp:classes.append('compaction')
        if slice_id in normal:classes.append('normal_metadata_write')
        if key in stage:classes.append('recovered_staging')
        cat=classes[0] if len(classes)==1 else 'unclassified' if not classes else 'ambiguous:'+'+'.join(classes)
        row={'timestamp':s[:26],'class':cat,'key':key,'slice_id':slice_id,'block':block,'raw_bytes':raw,'payload_bytes':int(payload) if payload else None,'status':'success' if err=='<nil>' else 'failure','error':err,'cost':cost}
        events.append(row);minute_key=(s[:16],cat);a=minute[minute_key];a['put_count']+=1;a[row['status']+'_count']+=1;a['raw_sum']+=raw
        if payload is not None:a['payload_sum']+=int(payload);a['payload_lt1024_count']+=int(payload)<1024
    classified=sorted({'compaction','normal_metadata_write','recovered_staging','unclassified'}|{r['class'] for r in events});results={}
    for period in ('all','after_startup_5min'):
        rr=events if period=='all' else [r for r in events if r['timestamp']>=CUTOFF]
        results[period]={'all_classes':summarize(rr),'by_class':{cat:summarize([r for r in rr if r['class']==cat]) for cat in classified}}
    output={'log':str(LOG),'snapshot':snapshot|{'sha256':h.hexdigest(),'line_count':count,'first_timestamp':first,'last_timestamp':last,'file_size_after':LOG.stat().st_size},'after_startup_cutoff':CUTOFF,'versions':versions,'pid_line_counts':dict(pidcounts),'classification_evidence':{'compaction_output_slice_count':len(comp),'normal_metadata_slice_count':len(normal),'startup_staging_key_count':len(stage),'compaction_normal_intersection':sorted(comp&normal)},'unmatched_put_lines':unmatched,'results':results}
    (OUT/'payload_analysis.json').write_text(json.dumps(output,indent=2,ensure_ascii=False)+'\n')
    with (OUT/'payload_put_events.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(events[0]));writer.writeheader();writer.writerows(events)
    with (OUT/'payload_per_minute.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['minute','class','put_count','success_count','failure_count','raw_sum','payload_sum','payload_lt1024_count']);writer.writeheader()
        for (minute_name,cat),values in sorted(minute.items()):writer.writerow({'minute':minute_name,'class':cat}|values)
    print(json.dumps({'snapshot':output['snapshot'],'evidence':output['classification_evidence'],'unmatched':len(unmatched),'brief':{p:{cat:{st:{'events':x[st]['events'],'raw':x[st]['raw_bytes'],'payload':x[st]['payload_bytes'],'lt1024':x[st]['payload_lt1024']} for st in ('success','failure')} for cat,x in v['by_class'].items()} for p,v in results.items()}},indent=2,ensure_ascii=False))

if __name__=='__main__':main()
