"""Summarize completed writer barriers and successful slow VFS operations in the fixed restart snapshot."""
import re,json,collections,statistics,math
from pathlib import Path
LOG=Path('/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-145808.log');N=385860940;OUT=Path(__file__).parent

def seconds(s):
    """Convert a Go duration to seconds."""
    return sum(float(n)*{'h':3600,'m':60,'s':1,'ms':1e-3,'µs':1e-6,'ns':1e-9}[u] for n,u in re.findall(r'([\d.]+)(ms|µs|ns|h|m|s)',s))

def stats(v):
    """Return completed-barrier latency statistics and the longest observation."""
    if not v:return {'count':0}
    ss=sorted(x[0] for x in v);return {'count':len(v),'median_s':statistics.median(ss),'p95_s':ss[math.ceil(.95*len(ss))-1],'max_s':ss[-1],'max_line':max(v,key=lambda x:x[0])[1]}

def main():
    """Collect completed writer barrier durations separately for all and recent periods."""
    v=collections.defaultdict(list);recent=collections.defaultdict(list);slow=[]
    with LOG.open('rb') as f:
        while f.tell()<N:
            b=f.readline(N-f.tell())
            if b'writer flush ' in b and b'phase=end' in b:
                s=b.decode().rstrip();m=re.search(r'origin=(\S+).*?elapsed=(\S+)',s)
                if m:
                    val=(seconds(m[2]),s);v[m[1]].append(val)
                    if s[:26]>='2026/10/02 15:40:00':recent[m[1]].append(val)
            elif b'slow operation:' in b:slow.append(b.decode().rstrip())
    result={'all':{k:stats(vals) for k,vals in v.items()},'after1540':{k:stats(vals) for k,vals in recent.items()},'slow_vfs_lines':slow}
    (OUT/'writer_latency_analysis.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n');print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
