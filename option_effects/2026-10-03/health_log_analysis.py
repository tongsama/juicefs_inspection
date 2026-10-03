"""Stream fixed diagnostic prefixes once to summarize health and DEBUG volume without secrets."""
import json,re,csv,collections,hashlib,statistics,math,time
from pathlib import Path
OUT=Path(__file__).parent
DURATION=re.compile(r'([\d.]+)(ms|µs|ns|h|m|s)')

def seconds(text):
    """Convert a Go duration to seconds."""
    return sum(float(n)*{'h':3600,'m':60,'s':1,'ms':1e-3,'µs':1e-6,'ns':1e-9}[u] for n,u in DURATION.findall(text))

def stats(values):
    """Summarize numeric latency with conventional median and nearest-rank percentiles."""
    if not values:return {'count':0}
    v=sorted(values);return {'count':len(v),'sum':sum(v),'mean':statistics.mean(v),'median':statistics.median(v),'p95':v[math.ceil(.95*len(v))-1],'p99':v[math.ceil(.99*len(v))-1],'max':v[-1]}

def safe(text):
    """Remove URLs and credentials from any selected diagnostic event before saving it."""
    text=re.sub(r'(?:https?|redis|rediss|mysql|postgres|sqlite3)://\S+','<redacted-url>',text)
    return re.sub(r'(?i)(password|passwd|secret|token|access.?key)([=: ]+)\S+',r'\1\2<redacted>',text)

def analyze(spec):
    """Read and hash one manifest prefix once; classify log bytes and operational latency."""
    path=Path(spec['path']);n=spec['prefix_bytes'];name=path.stem;h=hashlib.sha256();lines=0;first=None;last=None;tags=collections.Counter();tagbytes=collections.Counter();levelcount=collections.Counter();levelbytes=collections.Counter();pids=collections.Counter();checks=0;checkbytes=0;badchecks=0;versions=[];events=[];health=collections.Counter();slow=collections.defaultdict(list);compact=collections.defaultdict(list);writer=collections.Counter();writerorigin=collections.defaultdict(list);syncwrites=[];compactphases=collections.Counter();prev=None;lastbyte=None;clock=time.monotonic()
    with path.open('rb') as f:
        while f.tell()<n:
            b=f.readline(n-f.tell());h.update(b);lines+=1;lb=len(b);lastbyte=b[-1:];ts=b[:26].decode('ascii','replace') if b.startswith(b'2026/') else None
            if ts:first=first or ts;last=ts
            k=b.find(b'juicefs[')
            if k>=0:
                j=b.find(b']',k);pids[b[k+8:j].decode('ascii','replace')]+=1
            lev='untagged'
            for ll in (b'DEBUG',b'INFO',b'WARNING',b'ERROR',b'FATAL'):
                if b'<'+ll+b'>' in b:lev=ll.decode();break
            levelcount[lev]+=1;levelbytes[lev]+=lb
            tag='untagged';tail=b.rstrip();i=tail.rfind(b'[')
            if i>=0 and tail.endswith(b']'):
                t=tail[i+1:-1]
                if b'@' in t:tag=t.rsplit(b':',1)[0].decode('utf8','replace')
            tags[tag]+=1;tagbytes[tag]+=lb
            if b'checksum ' in b and b', expected ' in b:
                checks+=1;checkbytes+=lb;m=re.search(rb'checksum (\d+), expected (\d+)',b)
                if m and m[1]!=m[2]:badchecks+=1
                continue
            if b'JuiceFS version ' in b:versions.append(safe(b.decode().rstrip()))
            noteworthy=[]
            if lev in ('ERROR','FATAL'):health[lev]+=1;noteworthy.append(lev)
            for keyword,cat in ((b'input/output error','EIO_text'),(b'interrupted system call','EINTR_return'),(b'panic:','panic'),(b'fatal error:','panic'),(b'NoSuchKey','NoSuchKey')):
                if keyword in b:health[cat]+=1;noteworthy.append(cat)
            if b'writer flush ' in b and b'phase=end' in b:
                s=b.decode('utf8','replace');m=re.search(r'origin=(\S+).*?errno=(\d+).*?elapsed=(\S+)',s)
                if m:
                    writer['end']+=1;writer['errno_'+m[2]]+=1;writerorigin[m[1]].append(seconds(m[3]))
                    if m[2]!='0':health['writer_nonzero']+=1;noteworthy.append('writer_nonzero')
            elif b'writer flush ' in b and b'phase=begin' in b:writer['begin']+=1
            if b'errno=' in b and b'writer flush ' not in b:
                s=b.decode('utf8','replace');m=re.search(r'errno=(.*?)(?: \[|$)',s)
                if m and m[1] not in ('0','errno 0'):health['nonzero_metadata_errno']+=1;noteworthy.append('nonzero_metadata_errno')
            if b'fail to read sliceId' in b:health['prefetch_context_canceled' if b'context canceled' in b else 'readSlice_other_failure']+=1
            if b'slow operation:' in b:
                s=b.decode('utf8','replace');m=re.search(r'slow operation: (\w+).*<([\d.]+)>',s)
                if m:
                    st='OK' if ' - OK ' in s else 'failed';slow[m[1]+'_'+st].append(float(m[2]));health['slow_operation_'+st]+=1
                    if st!='OK':noteworthy.append('slow_operation_failed')
            if b'compaction inode=' in b and b'phase=' in b:
                m=re.search(rb'phase=(\w+)',b)
                if m:compactphases[m[1].decode()]+=1
            if b'slow compaction inode=' in b:
                s=b.decode('utf8','replace');m=re.search(r'once=(\w+).*?total=(\S+) queue_wait=(\S+) object=(\S+) metadata=(\S+)',s)
                if m:
                    kind='once_'+m[1]
                    for lab,val in zip(('total','queue_wait','object','metadata'),m.groups()[1:]):compact[kind+'_'+lab].append(seconds(val))
                    if seconds(m[2])>=60 or seconds(m[5])>=60:noteworthy.append('compaction_ge60s')
            if b'phase=compact slices=2500' in b:health['sync_compact_trigger_2500']+=1
            if b'slow metadata write ' in b:
                s=b.decode('utf8','replace');m=re.search(r'slices=(\d+).*?total=(\S+).*?compact=(\S+)',s)
                if m and int(m[1])>=2500:syncwrites.append({'timestamp':ts,'slices':int(m[1]),'total_s':seconds(m[2]),'compact_s':seconds(m[3]),'line':safe(s.rstrip())})
            if noteworthy:
                s=safe(b.decode('utf8','replace').rstrip());events.append({'line_number':lines,'timestamp':ts,'categories':noteworthy,'line':s})
            if lines%3000000==0:print(json.dumps({'progress_log':name,'lines':lines,'bytes':f.tell(),'elapsed_s':round(time.monotonic()-clock,1)}),flush=True)
    result={'manifest_spec':spec,'sha256_prefix':h.hexdigest(),'line_count':lines,'last_byte_newline':lastbyte==b'\n','first_timestamp':first,'last_timestamp':last,'pids':dict(pids),'versions':versions,'level_line_counts':dict(levelcount),'level_bytes':dict(levelbytes),'checksum_lines':checks,'checksum_bytes':checkbytes,'checksum_line_share':checks/lines,'checksum_byte_share':checkbytes/n,'checksum_mismatches':badchecks,'health_counts':dict(health),'writer_counts':dict(writer),'writer_origin_latency_seconds':{k:stats(v) for k,v in writerorigin.items()},'slow_operation_latency_seconds':{k:stats(v) for k,v in slow.items()},'compaction_phase_counts':dict(compactphases),'completed_slow_compaction_latency_seconds':{k:stats(v) for k,v in compact.items()},'sync_metadata_writes_ge2500':syncwrites,'top_source_categories':[{'source':k,'lines':tags[k],'bytes':v,'byte_share':v/n} for k,v in tagbytes.most_common(25)],'selected_health_events':events,'file_size_after_analysis':path.stat().st_size,'elapsed_analysis_seconds':time.monotonic()-clock}
    (OUT/('health_'+name+'.json')).write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    with (OUT/('health_'+name+'_categories.csv')).open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['source','lines','bytes','byte_share'])
        for k,v in tagbytes.most_common():w.writerow([k,tags[k],v,v/n])
    with (OUT/('health_'+name+'_events.txt')).open('w') as f:
        for e in events:f.write(str(e['line_number'])+' '+','.join(e['categories'])+' '+e['line']+'\n')
    print(json.dumps({'complete_log':name,'sha256':h.hexdigest(),'lines':lines,'checksum_byte_share':checkbytes/n,'health':dict(health),'pids':dict(pids)},ensure_ascii=False),flush=True)

def main():
    """Process the manifest sequentially, preserving the fixed prefix and PID epochs."""
    manifest=json.loads((OUT/'manifest.json').read_text())
    for spec in manifest['files']:analyze(spec)

if __name__=='__main__':main()
