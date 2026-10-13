"""Read a fixed original-log prefix to summarize recurrence evidence without production mutations."""
import re,json,hashlib,collections,csv
from pathlib import Path
LOG=Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-014922.log');N=813182468;OUT=Path(__file__).parent

def rows():
    """Yield decoded lines from the shared complete-line cutoff only."""
    with LOG.open('rb') as f:
        while f.tell()<N:yield f.readline(N-f.tell())

def main():
    """Collect operational errors, categorized warnings, request failures and retry evidence."""
    h=hashlib.sha256();count=0;first=None;last=None;warn=collections.Counter();warning_samples=collections.defaultdict(list);events=[];failedkeys=set();requests=[];pids=collections.Counter();errdone=[];versions=[];warnlines=[]
    for b in rows():
        h.update(b);count+=1;s=b.decode('utf8','replace').rstrip();ts=s[:26] if s.startswith('2026/') else None
        if ts:first=first or ts;last=ts
        pid=re.search(r'juicefs\[(\d+)\]',s)
        if pid:pids[pid[1]]+=1
        if 'JuiceFS version' in s or 'Mounting volume' in s:versions.append(s)
        level=re.search(r'<(ERROR|FATAL|WARNING)>',s);tag=re.search(r'\[([^\[\]]+@[^\[\]]+)\]$',s)
        if level:
            kind=level[1]+':'+(tag[1] if tag else 'untagged');warn[kind]+=1
            if len(warning_samples[kind])<8:warning_samples[kind].append(s)
            if level[1] in ('ERROR','FATAL'):events.append({'category':level[1],'line_number':count,'line':s})
            warnlines.append((count,s))
        if 'slow operation:' in s:events.append({'category':'vfs_slow_operation','line_number':count,'line':s})
        if 'errno=' in s:
            m=re.search(r'errno=(.*?)(?: \[|$)',s)
            if m and m[1] not in ('errno 0','0'):errdone.append(s);events.append({'category':'nonzero_errno','line_number':count,'line':s})
        m=re.search(r'\b(PUT|GET|DELETE) (\S+)(?: payload_bytes=(\d+))? \(req_id: .*?, err: (.*), cost: ([^)]+)\) \[logRequest@',s)
        if m:
            op,key,payload,err,cost=m.groups()
            if err!='<nil>':
                events.append({'category':op+'_failure','line_number':count,'line':s,'key':key,'error':err,'cost':cost});failedkeys.add((op,key))
            requests.append((op,key,ts,err,cost,count))
        if any(x in s.lower() for x in ('checksum mismatch','checksum error','corrupt','no such key','nosuchkey','staging failure','staging failed','panic:','fatal error:')):events.append({'category':'corruption_missing_or_panic_keyword','line_number':count,'line':s})
    retries=[]
    for op,key in sorted(failedkeys):
        rr=[r for r in requests if r[0]==op and r[1]==key]
        retries.append({'op':op,'key':key,'requests':[{'timestamp':r[2],'error':r[3],'cost':r[4],'line_number':r[5]} for r in rr]})
    summary={'log':str(LOG),'prefix_bytes':N,'sha256':h.hexdigest(),'line_count':count,'first_timestamp':first,'last_timestamp':last,'file_size_after':LOG.stat().st_size,'pid_lines':dict(pids),'versions_mounts':versions,'warning_error_counts':dict(warn),'warning_samples':dict(warning_samples),'event_category_counts':dict(collections.Counter(e['category'] for e in events)),'nonzero_errno_count':len(errdone),'events':events,'failed_key_retry_evidence':retries}
    (OUT/'incident_log_analysis.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
    with (OUT/'incident_events.txt').open('w') as f:
        for e in events:f.write(str(e['line_number'])+' '+e['category']+' '+e['line']+'\n')
    with (OUT/'all_warning_error_lines.txt').open('w') as f:
        for no,s in warnlines:f.write(str(no)+' '+s+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('events','failed_key_retry_evidence')},indent=2,ensure_ascii=False))

if __name__=='__main__':main()
