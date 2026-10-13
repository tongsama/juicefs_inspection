"""Refine completed compaction summaries by force-mode recursive frames versus ordinary background mode."""
import subprocess,re,json,math,statistics
from pathlib import Path
OUT=Path(__file__).parent

def seconds(s):
    """Parse a Go duration into seconds."""
    return sum(float(n)*{'h':3600,'m':60,'s':1,'ms':1e-3,'µs':1e-6,'ns':1e-9}[u] for n,u in re.findall(r'([\d.]+)(ms|µs|ns|h|m|s)',s))

def describe(v):
    """Return the ordinary median and nearest-rank percentiles."""
    q=sorted(v);return {'count':len(q),'median':statistics.median(q),'p95':q[math.ceil(.95*len(q))-1],'p99':q[math.ceil(.99*len(q))-1],'max':q[-1]}

def main():
    """Filter small matching line sets and retain only the already hashed fixed prefix."""
    results={}
    for path in sorted(OUT.glob('health_vm-io-*.json')):
        d=json.loads(path.read_text());group={};text=subprocess.run(['rg','-n','slow compaction inode=',d['manifest_spec']['path']],capture_output=True,text=True).stdout
        for s in text.splitlines():
            no,_,line=s.partition(':')
            if not no.isdigit() or int(no)>d['line_count']:continue
            m=re.search(r'once=(\w+) force=(\w+).*?total=(\S+) queue_wait=(\S+) object=(\S+) metadata=(\S+)',line)
            if not m:continue
            mode='once_'+m[1]+'_force_'+m[2];vals=group.setdefault(mode,{'total':[],'queue_wait':[],'object':[],'metadata':[],'unmeasured_residual':[]})
            nums=[seconds(v) for v in m.groups()[2:]]
            for k,v in zip(('total','queue_wait','object','metadata'),nums):vals[k].append(v)
            vals['unmeasured_residual'].append(max(0,nums[0]-sum(nums[1:])))
        results[path.stem]={mode:{k:describe(v) for k,v in vals.items()} for mode,vals in group.items()}
    (OUT/'health_compaction_modes.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n');print(json.dumps(results,indent=2,ensure_ascii=False))

if __name__=='__main__':main()
