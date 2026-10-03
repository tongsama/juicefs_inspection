"""Classify fixed-prefix restart-run errors while distinguishing real operations from prefetch and GC."""
import json,re,hashlib,collections
from pathlib import Path
LOG=Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-145808.log');N=385860940;OUT=Path(__file__).parent

def main():
    """Parse all complete lines in the shared snapshot and correlate failed object retries."""
    h=hashlib.sha256();count=0;first=None;last=None;events=[];counts=collections.Counter();recent=collections.Counter();writer=collections.Counter();openbarriers={};requests=collections.defaultdict(list);badkeys=set();checkcount=0;checks=[];warn=collections.Counter();mounts=[];reader=collections.Counter()
    with LOG.open('rb') as f:
        while f.tell()<N:
            b=f.readline(N-f.tell());h.update(b);count+=1;s=b.decode('utf8','replace').rstrip();ts=s[:26] if s.startswith('2026/') else ''
            if ts:first=first or ts;last=ts
            cats=[]
            if 'JuiceFS version' in s or 'Mounting volume' in s:mounts.append(s)
            if '<WARNING>' in s:
                tag=re.search(r'\[([^\[\]]+)\]$',s);warn[tag[1] if tag else 'untagged']+=1
            if '<ERROR>' in s:cats.append('ERROR')
            if '<FATAL>' in s:cats.append('FATAL')
            if any(x in s.lower() for x in ('panic:','fatal error:','no such key','nosuchkey','input/output error','i/o error')):cats.append('panic_missing_eio_text')
            m=re.search(r'writer flush inode=(\d+) origin=(\S+) barrier=(\d+) phase=(begin|end)(?: errno=(\d+))?',s)
            if m:
                inode,origin,barrier,phase,eno=m.groups();writer[phase]+=1
                if phase=='begin':openbarriers[barrier]=s
                else:
                    openbarriers.pop(barrier,None);writer['end_errno_'+str(eno)]+=1
                    if eno!='0':cats.append('writer_flush_real_nonzero')
            if 'slow operation:' in s or 'SlowOperation' in s:
                counts['vfs_slow_all']+=1
                if ' - OK ' not in s:cats.append('vfs_slow_failed')
            if 'errno=' in s and 'writer flush ' not in s:
                m=re.search(r'errno=(.*?)(?: elapsed=| \[|$)',s)
                if m and m[1] not in ('errno 0','0'):cats.append('metadata_nonzero_errno')
            if 'fail to read sliceId' in s:
                kind='context_canceled' if 'context canceled' in s else 'other';reader[kind]+=1
                if kind=='other':cats.append('slice_read_non_cancel_failure')
            if 'interrupted' in s and not ('Transaction failed' in s):cats.append('interrupt_text')
            if 'checksum' in s:
                m=re.search(r'checksum (\d+), expected (\d+)',s)
                if m:
                    checkcount+=1
                    if m[1]!=m[2]:checks.append(s);cats.append('checksum_mismatch')
            m=re.search(r'\b(PUT|GET|DELETE) (\S+)(?: payload_bytes=(\d+))? \(req_id: .*?, err: (.*), cost: ([^)]+)\) \[logRequest@',s)
            if m:
                op,key,payload,err,cost=m.groups();requests[(op,key)].append({'timestamp':ts,'error':err,'cost':cost,'line_number':count})
                if err!='<nil>':cats.append(op+'_failure');badkeys.add((op,key))
            for cat in cats:
                counts[cat]+=1
                if ts>='2026/10/02 15:40:00':recent[cat]+=1
                events.append({'line_number':count,'category':cat,'line':s})
    retries=[]
    for op,key in sorted(badkeys):
        rr=requests[(op,key)];failures=[r for r in rr if r['error']!='<nil>'];succ=[r for r in rr if r['error']=='<nil>' and r['timestamp']>failures[-1]['timestamp']]
        retries.append({'op':op,'key':key,'last_failure':failures[-1]['timestamp'],'later_success':succ[0]['timestamp'] if succ else None,'requests':rr})
    d={'log':str(LOG),'prefix_bytes':N,'sha256':h.hexdigest(),'line_count':count,'first_timestamp':first,'last_timestamp':last,'file_size_after':LOG.stat().st_size,'mounts_versions':mounts,'counts':dict(counts),'after1540_counts':dict(recent),'writer_counts':dict(writer),'unended_writer_barriers':openbarriers,'reader_failure_counts':dict(reader),'checksum_comparisons':checkcount,'checksum_mismatches':checks,'warning_tag_counts':dict(warn),'events':events,'retry_evidence':retries}
    (OUT/'restart_log_analysis.json').write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n')
    (OUT/'restart_events.txt').write_text('\n'.join(str(e['line_number'])+' '+e['category']+' '+e['line'] for e in events)+'\n')
    print(json.dumps({k:v for k,v in d.items() if k not in ('events','retry_evidence','unended_writer_barriers')},indent=2,ensure_ascii=False));print('unended writer barriers',len(openbarriers))

if __name__=='__main__':main()
