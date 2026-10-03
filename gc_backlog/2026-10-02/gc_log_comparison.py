"""Compare GC transport volume and classify PUT/DELETE lineage using fixed log prefixes."""
import argparse,collections,csv,hashlib,json,re,statistics,math
from pathlib import Path
OUT=Path(__file__).parent

def seconds(s):
    """Parse a Go duration without confusing milliseconds and minutes."""
    return sum(float(n)*{'h':3600,'m':60,'s':1,'ms':1e-3,'µs':1e-6,'ns':1e-9}[u] for n,u in re.findall(r'([\d.]+)(ms|µs|ns|h|m|s)',s))

def numeric(values):
    """Describe observed per-request elapsed seconds."""
    v=sorted(values)
    if not v:return {'count':0}
    return {'count':len(v),'sum':sum(v),'mean':statistics.mean(v),'median':statistics.median(v),'p95':v[math.ceil(.95*len(v))-1],'max':v[-1]}

def lines(path,n):
    """Yield only the complete-line prefix agreed with the coordinating analysis."""
    with path.open('rb') as f:
        f.seek(n-1)
        if f.read(1)!=b'\n':raise RuntimeError('Incomplete line boundary')
        f.seek(0)
        while f.tell()<n:yield f.readline(n-f.tell())

def analyze(path,n,name):
    """Correlate metadata slice IDs, startup staging, object requests, and cleanup clues."""
    h=hashlib.sha256();comp=set();normal=set();stage=set();count=0;first=None;last=None;cleanup=[];writes={};phasecounts=collections.Counter()
    for b in lines(path,n):
        h.update(b);count+=1;s=b.decode('utf8','replace').rstrip()
        if s.startswith('2026/'):first=first or s[:26];last=s[:26]
        m=re.search(r'compaction .*?slice=(\d+) phase=object',s)
        if m:comp.add(int(m[1]))
        m=re.search(r'metadata write .*?slice=(\d+) phase=(\w+)',s)
        if m:
            sl=int(m[1]);normal.add(sl);writes.setdefault(sl,s[:26]);phasecounts[m[2]]+=1
        m=re.search(r'Found staging block: .*?/rawstaging/(chunks/\S+) ',s)
        if m:stage.add(m[1])
        if any(x in s for x in ('Cleanup delayed slices:','nextCleanupSlices','Cleanup slices','cleanupSlices','not needed,','is not needed,','CompactionGC','compaction GC')):cleanup.append(s)
    minute=collections.defaultdict(list);records=[];totals=collections.Counter();classes=collections.Counter();failurekinds=collections.Counter();drop=collections.Counter();deleteknown=[]
    for b in lines(path,n):
        s=b.decode('utf8','replace').rstrip()
        if 'is not needed,' in s or 'not needed, abandoned' in s:
            drop['drop' if 'drop it' in s else 'abandoned']+=1
        if '[logRequest@' not in s:continue
        m=re.search(r'\b(PUT|DELETE|GET) (\S+)(?: payload_bytes=(\d+))? \(req_id: .*?, err: (.*), cost: ([^)]+)\) \[logRequest@',s)
        if not m:continue
        op,key,payload,err,cost=m.groups();km=re.search(r'/(\d+)_(\d+)_(\d+)$',key);sl=int(km[1]) if km else None;raw=int(km[3]) if km else None
        ev={'timestamp':s[:26],'op':op,'key':key,'slice_id':sl,'raw_bytes':raw,'payload_bytes':int(payload) if payload else None,'success':err=='<nil>','cost_s':seconds(cost)}
        lineage=[]
        if sl in comp:lineage.append('compaction_output')
        if sl in normal:lineage.append('normal_metadata_write')
        if key in stage:lineage.append('recovered_startup_staging')
        ev['lineage']='+'.join(lineage) if lineage else 'unclassified';records.append(ev);totals[op+('_success' if ev['success'] else '_failure')]+=1;classes[(op,ev['lineage'],'success' if ev['success'] else 'failure')]+=1;minute[(s[:16],op)].append(ev)
        if not ev['success']:failurekinds[(op,'timeout' if 'timeout' in err else 'HTTP500' if 'StatusCode: 500' in err else 'other')]+=1
        if op=='DELETE' and ev['success'] and sl in writes and writes[sl]<=ev['timestamp']:deleteknown.append(ev)
    rows=[]
    for (mn,op),rr in sorted(minute.items()):
        costs=numeric([r['cost_s'] for r in rr]);rows.append({'minute':mn,'op':op,'count':len(rr),'success':sum(r['success'] for r in rr),'failure':sum(not r['success'] for r in rr),'payload_sum':sum(r['payload_bytes'] or 0 for r in rr),**{'cost_'+k:v for k,v in costs.items() if k!='count'}})
    with (OUT/(name+'_per_minute.csv')).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (OUT/(name+'_cleanup_clues.txt')).write_text('\n'.join(cleanup)+'\n')
    summary={'log':str(path),'prefix_bytes':n,'sha256':h.hexdigest(),'line_count':count,'first_timestamp':first,'last_timestamp':last,'compaction_output_slices':len(comp),'normal_metadata_slices':len(normal),'startup_staging_keys':len(stage),'metadata_phases':dict(phasecounts),'object_totals':dict(totals),'object_lineage_counts':[{'op':op,'lineage':cl,'status':st,'count':num} for (op,cl,st),num in sorted(classes.items())],'request_failure_kinds':[{'op':op,'kind':kind,'count':num} for (op,kind),num in failurekinds.items()],'unneeded_staging_events':dict(drop),'cleanup_clue_count':len(cleanup),'normal_written_slice_ids_with_later_successful_delete':len(set(e['slice_id'] for e in deleteknown)),'normal_write_later_delete_events':len(deleteknown),'delete_costs':numeric([r['cost_s'] for r in records if r['op']=='DELETE']),'max_delete_minute':max([r for r in rows if r['op']=='DELETE'],key=lambda r:r['count']) if any(r['op']=='DELETE' for r in rows) else None,'recent1540':dict(collections.Counter(r['op']+('_success' if r['success'] else '_failure') for r in records if r['timestamp']>='2026/10/02 15:40:00'))}
    normalput={r['slice_id'] for r in records if r['op']=='PUT' and r['success'] and r['slice_id'] in normal}
    normaldelete={r['slice_id'] for r in records if r['op']=='DELETE' and r['success'] and r['slice_id'] in normal}
    summary['normal_slice_remote_observations']={'successful_put_slice_ids':len(normalput),'successful_delete_slice_ids':len(normaldelete),'put_and_delete_slice_ids':len(normalput&normaldelete),'neither_successful_put_nor_delete_slice_ids':len(normal-normalput-normaldelete)}
    lineage_minutes=collections.Counter((r['timestamp'][:16],r['op'],r['lineage'],'success' if r['success'] else 'failure') for r in records)
    with (OUT/(name+'_lineage_per_minute.csv')).open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['minute','op','lineage','status','count'])
        for row,num in sorted(lineage_minutes.items()):w.writerow([*row,num])
    (OUT/(name+'_summary.json')).write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n');print(json.dumps(summary,indent=2,ensure_ascii=False))

def main():
    """Run a selected prefix, leaving all original logs untouched."""
    parser=argparse.ArgumentParser();parser.add_argument('log');parser.add_argument('bytes',type=int);parser.add_argument('name');args=parser.parse_args();analyze(Path(args.log),args.bytes,args.name)

if __name__=='__main__':main()
