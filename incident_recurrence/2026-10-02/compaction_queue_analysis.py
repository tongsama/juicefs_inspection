"""Correlate compaction targets with Redis transaction and DELETE observations."""
import re,json,collections
from pathlib import Path
LOG=Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-014922.log');OUT=Path(__file__).parent;N=813182468

def seconds(text):
    """Parse a Go duration into seconds."""
    return sum(float(n)*{'h':3600,'m':60,'s':1,'ms':.001,'µs':.000001,'ns':.000000001}[u] for n,u in re.findall(r'([\d.]+)(ms|µs|ns|h|m|s)',text))

def main():
    """Extract target timelines and per-minute deletion activity from the shared prefix."""
    events=[];deletes=collections.defaultdict(list);allslow=[];capacity=[]
    with LOG.open('rb') as f:
        while f.tell()<N:
            s=f.readline(N-f.tell()).decode('utf8','replace').rstrip()
            if ('compaction inode=' in s or 'metadata write inode=' in s or 'slice commit inode=' in s) and any(x in s for x in ('inode=596153','inode=596154')):
                if 'compaction' in s or 'slice=4110001' in s or 'slice=4123567' in s or 'phase=compact' in s or 'slow metadata write' in s:events.append(s)
            if 'slow metadata transaction' in s:allslow.append(s)
            if 'metadata transaction key=' in s and any(x in s for x in ('c596153_', 'c596154_')):events.append(s)
            if any(x in s.lower() for x in ('dslices','enqueue','deletion queue')):capacity.append(s)
            if 'DELETE ' in s and '[logRequest@' in s:
                m=re.search(r'err: (.*), cost: ([^)]+)',s)
                if m:deletes[s[:16]].append((m[1]=='<nil>',seconds(m[2])))
    (OUT/'compaction_target_timeline.txt').write_text('\n'.join(events)+'\n');(OUT/'all_slow_redis_txn.txt').write_text('\n'.join(allslow)+'\n');(OUT/'queue_instrumentation_matches.txt').write_text('\n'.join(capacity)+'\n')
    summary={minute:{'count':len(v),'success':sum(x[0] for x in v),'fail':sum(not x[0] for x in v),'cost_mean_s':sum(x[1] for x in v)/len(v),'cost_max_s':max(x[1] for x in v)} for minute,v in deletes.items()}
    (OUT/'delete_per_minute.json').write_text(json.dumps(summary,indent=2)+'\n')
    print('target events',len(events),'slow txn',len(allslow),'queue text',len(capacity));print('\n'.join(s for s in events if ('slow compaction' in s and ('total=30m' in s or 'total=1h' in s)) or 'slice=4110001' in s or 'slice=4123567' in s));print('\n'.join(allslow))

if __name__=='__main__':main()
