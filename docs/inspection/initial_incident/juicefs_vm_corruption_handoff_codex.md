# JuiceFS CE v1.4.1 + rclone serve s3 + Google Drive
## VM qcow2 I/O corruption / `fsync` EIO investigation handoff for Codex

**Prepared:** 2026-10-01  
**Primary goal:** Continue root-cause analysis and implement safe, testable code changes in the user's JuiceFS CE v1.4.1 working tree.

---

# 1. Executive summary

A VM qcow2 image stored on JuiceFS is experiencing guest-side filesystem corruption / filesystem errors. The strongest evidence now is **not silent corruption in Google Drive itself**, but that JuiceFS is returning real `EIO` to QEMU for the qcow2 file after `fileWriter.flush()` waits 5 minutes for pending slices to be committed.

The affected qcow2 file is:

```text
./proxmox9_01_vol1.qcow2
JuiceFS inode: 596152
```

Confirmed log sequence for inode `596152`:

```text
flush 596152 timeout after waited 5m0s
fsync (596152,1) - input/output error <300s>

flush 596152 timeout after waited 5m0s
fallocate (596152,16,...) - input/output error <300s>
```

This repeats multiple times. This is enough by itself to explain why a VM guest filesystem can journal-abort, remount read-only, or later report corruption: QEMU is receiving host-side disk I/O errors.

At the timeout, all inspected pending slices for inode `596152`, chunk `0`, look like:

```text
freezed:true
done:true
err:0
growing:false
committed:false
dep:<nil>
```

This is a critical clue:

- `done:true` => `flushData()` finished.
- `err:0` => slice data staging/upload path reported success.
- `dep:nil` => not waiting on an inter-chunk dependency.
- `committed:false` => the metadata commit (`Meta.Write`) has not completed.

So the current leading hypothesis is:

> **Data writing has completed, but metadata slice commits are falling behind / blocking.**

The most suspicious source-level design is that `baseMeta.Write()` takes an **inode-wide open-file lock**, while the VFS can have several `chunkWriter.commitThread()` goroutines for different 64 MiB chunks. Therefore metadata commits for the same large qcow2 inode are serialized. This exactly matches upstream JuiceFS issue **#6398**, which reports blocking under **4 KiB random writes to a 10 GiB file**.

The metadata backend is Redis and is accessed over a network/WAN path from the JuiceFS client. The user observed that Redis `SCAN` was extremely slow when executed across the Internet but fast when executed locally on the Redis host. `SCAN` itself is not a valid direct benchmark for `doWrite`, but the observation reinforces that **WAN round-trip cost matters**.

The affected Redis raw chunk list currently has:

```text
LLEN c596152_0
(integer) 682
```

`682` is important:

- It is **above 350**, so JuiceFS's automatic background compaction trigger is active.
- It is **below 2500 (`maxSlices`)**, so the synchronous compaction branch is **not currently triggered merely by this chunk's length**.
- Thus an earlier hypothesis that current `Meta.Write()` is directly blocked by synchronous `compactChunk()` because the list exceeded `maxSlices` must be corrected.
- However background compaction is likely a **major amplifier**: it performs substantial object GET/PUT traffic, and `vfs.Compact()` explicitly disables writeback (`writer.SetWriteback(false)`).

There is also a separate correctness concern in JuiceFS v1.4.1:

```go
_ = v.writer.Flush(ctx, ino)
n, err = h.reader.Read(...)
```

`VFS.Read()` discards a writer flush error. If metadata commit is stuck and `Flush()` returns `EIO`, the read path proceeds anyway. This may expose older metadata / stale data rather than propagating the I/O error. This exact behavior is still present in current upstream `main` as of 2026-10-01.

That should be investigated and probably covered by a regression test before changing semantics.

---

# 2. Environment / topology

Known architecture:

```text
VM / QEMU / qcow2
       |
       v
JuiceFS FUSE (CE v1.4.1 source tree)
       |
       +------ metadata ------> Redis
       |                        (network/WAN from JuiceFS client)
       |
       +------ object API ----> rclone serve s3
                                 |
                                 v
                              Google Drive
```

Relevant versions / facts:

```text
JuiceFS CE: v1.4.1 target/source
rclone:     v1.75.1
Object path: JuiceFS -> rclone serve s3 -> Google Drive
PutTimeout: 60s for the affected PID / reproduction
Affected qcow2 inode: 596152
Redis DB seen in diagnostic command: DB 1
```

The user explicitly confirmed:

```text
--put-timeout = 60s
rclone v1.75.1
```

The goroutine dump also independently supports a 60 second `PutTimeout` because uploader goroutines are blocked in:

```text
utils.WithTimeout(..., 0xdf8475800)
```

and:

```text
0xdf8475800 ns = 60,000,000,000 ns = 60s
```

---

# 3. rclone status: known `serve s3` corruption bug is already fixed

There was a relevant rclone `serve s3` bug in which failed/retried uploads could delete or corrupt the object at a key.

However **rclone v1.75.1** includes the explicit fix:

> `serve s3`: Fix failed uploads deleting or corrupting the object at the key

Changelog:

https://rclone.org/changelog/

Therefore this known bug should be considered **unlikely for writes performed entirely under v1.75.1**.

Caveat:

- If the VM image was modified while running an older affected rclone version, historic damage cannot be excluded.
- Do not spend primary investigation time on this bug unless there is evidence that the problematic image was written using v1.75.0 or older.

Current `serve s3` documentation also states that successful PUT replacement is intended to be atomic and failed/interrupted PUTs should not replace/delete an existing object.

Reference:

https://tip.rclone.org/commands/rclone_serve_s3/

---

# 4. Local JuiceFS working tree has a custom `--slice-*` patch

A small custom patch was added during this investigation to expose slice flush timers as CLI options.

**Important: the VM corruption / EIO problem existed before this patch. Do not treat this patch as the root cause.**

The intended new CLI options are:

```bash
--slice-flush-wait 10s
--slice-flush-idle 3s
```

Default behavior is intended to remain:

```text
slice-flush-wait = 5s
slice-flush-idle = 1s
```

The working tree diff approximately contains:

```diff
cmd/flags.go
+ --slice-flush-wait
+ --slice-flush-idle

cmd/mount.go
+ SliceFlushWait: utils.Duration(c.String("slice-flush-wait"))
+ SliceFlushIdle: utils.Duration(c.String("slice-flush-idle"))

pkg/vfs/vfs.go
+ SliceFlushWait time.Duration
+ SliceFlushIdle time.Duration

pkg/vfs/writer.go
- flushDuration = 5s
+ defaultSliceFlushWait = 5s
+ defaultSliceFlushIdle = 1s

commitThread:
- flushDuration * 2
+ f.w.conf.SliceFlushWait * 2

NewDataWriter:
+ fallback zero values to 5s / 1s

flushAll:
- 5s absolute / 1s idle
+ configured SliceFlushWait / SliceFlushIdle
```

### Warning for Codex

Several assistant-generated `.patch` files had invalid / inaccurate hunk headers. The user finally applied the last patch using:

```bash
patch -p1 < ...
```

and it applied with offsets/fuzz.

**Do not trust those patch files.**

The authoritative state is the current Git working tree:

```bash
git status
git diff
git diff --check
```

Run `gofmt` on modified Go files and inspect actual source.

Also determine the exact base commit:

```bash
git rev-parse HEAD
git describe --tags --always --dirty
git status --short
```

The official upstream v1.4.1 release currently resolves to commit `0b90c7d` in GitHub release metadata, but confirm the user's local repository rather than assuming.

---

# 5. Failure evidence

## 5.1 Earlier process: 30 second object timeouts

An older JuiceFS process (`PID 276092`) logged many 30 second GET/PUT timeouts.

Examples:

```text
Upload chunks/...: timeout after 30s: function timeout
slow request: GET chunks/... timeout after 30s
fail to read sliceId ... timeout after 30s
```

This was from an earlier mount/configuration.

Do **not** confuse these 30 second logs with the later PID that generated the qcow2 `fsync` failure below.

---

## 5.2 Current failure process: inode 596152 gets real `EIO`

Affected process:

```text
juicefs PID 784039
```

Examples:

```text
2026/10/01 15:42:46
flush 596152 timeout after waited 5m0s

fsync (596152,1) - input/output error
elapsed ~300.071 s
```

Then repeatedly:

```text
15:47:46 flush 596152 timeout after waited 5m0s
15:47:46 fallocate (596152,16,73465856,65536) - input/output error

15:52:46 flush 596152 timeout after waited 5m0s
15:52:46 fallocate (...) - input/output error

15:59:06 flush 596152 timeout after waited 5m0s

16:06:54 flush 596152 timeout after waited 5m0s
```

Other nearby VM-related inodes (`596153`, `596154`) also show `fsync` / `flush` EIOs, suggesting this is not just one damaged qcow2 sector or a one-off file.

---

# 6. What `fileWriter.flush()` actually does

Official JuiceFS v1.4.1:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/writer.go

Important source region:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/writer.go#L364-L410

Conceptually:

```go
func (f *fileWriter) flush(ctx meta.Context, writeback bool) syscall.Errno {
    wait := ((maxRetries+2)^2 / 2) seconds
    if wait < 5*time.Minute {
        wait = 5*time.Minute
    }

    deadline := now + wait

    for len(f.chunks) > 0 {
        freeze all pending slices

        wait on f.flushcond

        if deadline exceeded {
            dump pending slices
            dump all goroutines
            return EIO
        }
    }

    if err == 0 {
        err = f.err
    }
    return err
}
```

Important implications:

1. `fsync` waits for **all in-memory chunk writers to disappear**.
2. A chunk is only freed after its `commitThread()` drains all slices.
3. A slice being `done=true` is not enough. It must also pass metadata commit.
4. After 5 minutes (minimum), JuiceFS explicitly returns `EIO`.

The observed 300 second `fsync` failure is therefore exactly explained by this code.

### About `io-retries`

The flush deadline depends on metadata retry count:

```text
wait_seconds = (maxRetries + 2)^2 / 2
minimum      = 300s
```

Because the observed deadline is exactly the minimum 5 minutes, the currently effective metadata retry count is low enough that the calculated value is <= 300 seconds.

Do not assume the current `--io-retries` value without reading the mount command/config.

---

# 7. Critical pending-slice state at the timeout

At `15:42:46`, immediately after:

```text
flush 596152 timeout after waited 5m0s
```

the log dumps many slices from:

```text
inode: 596152
chunk index: 0
```

Typical entries:

```text
{id:3713694
 off:35635200
 length:4096
 slen:4096
 freezed:true
 done:true
 err:0
 growing:false
 committed:false
 dep:<nil>}
```

and many more 4 KiB / 8 KiB / 12 KiB / 32 KiB fragments.

This strongly resembles qcow2/random-write workload.

### Interpretation from `writer.go`

`sliceWriter` state:

```go
type sliceWriter struct {
    ...
    freezed bool
    done bool
    err syscall.Errno

    growing bool
    committed bool
    dep *sliceWriter
}
```

`flushData()`:

```go
prepareID()
writer.Finish()
markDone()
```

Thus:

```text
done=true + err=0
```

means the data side of that slice completed successfully as far as this writer is concerned.

`commitThread()` later does:

```go
wait until s.done

wait for s.dep if any

err := s.err

if err == 0 {
    err = Meta.Write(...)
}

s.committed = true
remove slice from c.slices
```

So:

```text
done:true
err:0
dep:nil
committed:false
```

points to a bottleneck **at or before completion of `Meta.Write()`**, not a currently failed data upload for those slices.

Official source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/writer.go#L174-L219

---

# 8. Metadata commit architecture: likely primary bottleneck

## 8.1 `commitThread()` is per chunk

Each active 64 MiB logical chunk gets a `chunkWriter`.

When the first slice enters a chunk:

```go
go c.commitThread()
```

So a huge qcow2 can have multiple chunk commit threads active concurrently.

---

## 8.2 But `baseMeta.Write()` takes an inode-wide lock

Official v1.4.1:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L2049-L2072

Relevant logic:

```go
func (m *baseMeta) Write(...) syscall.Errno {
    f := m.of.find(inode)
    if f != nil {
        f.Lock()
        defer f.Unlock()
    }

    st := m.en.doWrite(...)

    ...
}
```

That lock belongs to the open file / inode, not the individual 64 MiB chunk.

Therefore:

```text
chunk 0 commitThread ----\
chunk 1 commitThread -----\
chunk 2 commitThread ------> inode-wide f.Lock --> Meta doWrite
chunk 3 commitThread -----/
...
```

Metadata commits for different chunks of the same qcow2 are serialized.

---

# 9. Upstream issue #6398 is extremely relevant

Open JuiceFS issue:

https://github.com/juicedata/juicefs/issues/6398

Title:

> Would it be feasible to reduce the scope of the openfile lock when read or write file meta info?

The report specifically says they tested:

```text
4k random writes
large file (10 GiB)
```

and requests were blocked on:

```go
f.Lock()
```

inside `baseMeta.Write()`.

The issue describes the exact structural mismatch:

- chunk writers can flush data concurrently;
- metadata slice updates must update in order;
- inode-wide lock becomes the bottleneck.

This is the closest upstream match found so far to this VM/qcow2 workload.

As of 2026-10-01 the issue remains open.

### Very important

Do not immediately implement a naive per-chunk lock.

`baseMeta.Write()` also updates:

- inode length
- mtime / ctime
- usage / quota stats
- chunk cache invalidation

Those have cross-chunk / inode-level semantics.

A lock-scope reduction needs careful reasoning and tests.

---

# 10. Redis metadata implementation

User's metadata backend is Redis.

Relevant v1.4.1 implementation:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/redis.go#L3002-L3045

`doWrite()` roughly does:

```go
transaction/watch inode key
    GET inode attr
    quota checks
    update mtime/ctime/length

    TxPipelined:
        RPUSH c<inode>_<chunk> new slice metadata
        SET inode attr
        optional INCR used space

return RPUSH list length as numSlices
```

Specifically:

```go
rpush = pipe.RPush(
    ctx,
    m.chunkKey(inode, indx),
    marshalSlice(...)
)

...
*numSlices = int(rpush.Val())
```

Thus Redis `LLEN` of:

```text
c596152_0
```

is directly relevant to the compaction trigger.

---

# 11. Redis raw slice count: confirmed 682

The user ran this **locally on the Redis host** because running `SCAN` across the Internet was very slow:

```bash
redis-cli -u redis://...@localhost:6379/1 \
  --scan --pattern '*c596152_0'
```

Result:

```text
c596152_0
```

Then:

```bash
redis-cli -u redis://...@localhost:6379/1 \
  LLEN 'c596152_0'
```

Result:

```text
(integer) 682
```

### Interpretation

`682` is the number of raw slice records currently stored in Redis for qcow2 inode `596152`, chunk `0`.

The relevant v1.4.1 constants are:

```go
maxCompactSlices = 1000
maxSlices        = 2500
```

Source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L46-L62

The write path triggers compaction when:

```go
if numSlices%100 == 99 || numSlices > 350 {
    if numSlices < maxSlices {
        go m.compactChunk(...)
    } else {
        m.compactChunk(... once=true ...)
    }
}
```

Source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L2049-L2072

Therefore for `682`:

```text
> 350      yes  -> background compaction path active
< 2500     yes  -> NOT synchronous maxSlices compaction at this moment
< 1000     yes  -> one background compaction can potentially consider most/all of this raw list
```

### Correction to earlier hypothesis

An earlier analysis suggested the current timeout might be caused by the `numSlices >= 2500` synchronous compaction branch holding the inode lock.

That is **not supported by the measured LLEN 682 for chunk 0**.

The 2500-slice synchronous path remains a dangerous future cliff, but it is not presently demonstrated for this chunk.

---

# 12. Background compaction can still be a major amplifier

Even below `maxSlices`, background compaction is very relevant.

## 12.1 Writes schedule background compaction above 350 slices

At 682 raw slices, every subsequent successful metadata write satisfies:

```go
numSlices > 350
```

and attempts:

```go
go m.compactChunk(...)
```

Duplicate compactions for the same chunk are guarded by `m.compacting`, so this does not mean hundreds run simultaneously for the same chunk.

The guard allows a bounded number of concurrent compactions across chunks.

Source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L2659-L2761

---

## 12.2 Reads also trigger background compaction at very low fragmentation

`baseMeta.Read()` contains:

```go
if !ReadOnly && (len(ss) >= 5 || len(builtSlices) >= 5) {
    go m.compactChunk(...)
}
```

Thus a hot VM image doing random reads and writes can continuously encourage compaction.

Source region:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L1974-L2017

---

## 12.3 Compaction explicitly disables writeback

Official v1.4.1:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/compact.go#L50-L104

Key code:

```go
writer := store.NewWriter(id, tierID)
writer.SetWriteback(false)
```

Compaction then:

1. reads old slices;
2. reconstructs a compacted slice;
3. writes it to a new writer;
4. calls `writer.Finish(pos)`;
5. only after the object operation succeeds can metadata be switched to the compacted representation.

This makes sense for correctness, but with:

```text
rclone serve s3 -> Google Drive
```

it can be expensive.

---

## 12.4 Compaction can consume substantial object I/O

The compacted slice can be up to a 64 MiB chunk.

With a 4 MiB block size, a large compaction can issue many block reads and writes.

The captured runtime dump shows many uploader goroutines simultaneously waiting inside:

```text
cachedStore.put
cachedStore.upload
uploadStagingFile
uploader
utils.WithTimeout(... 60s)
```

and just before the metadata timeout, successful object PUTs of only 4 KiB–64 KiB frequently took roughly 10–25 seconds.

Examples:

```text
PUT ... 4096 bytes   ~17.856s  err:nil
PUT ... 8192 bytes   ~23.665s  err:nil
PUT ... 65536 bytes  ~18.020s  err:nil
```

So even successful object operations are very slow.

### Current interpretation

Background compaction is best viewed as an **amplifier** rather than the proven direct metadata-lock cause at LLEN 682:

```text
4K random writes
    |
    +--> many metadata slice records
    |
    +--> inode-wide serialized Meta.Write
    |
    +--> automatic compaction
             |
             +--> many object GETs
             +--> synchronous compacted-object PUTs
             +--> network / rclone / Drive pressure
```

If Redis and object traffic share constrained network resources, compaction traffic may further inflate metadata RTT.

---

# 13. Why WAN Redis is especially suspicious

The user observed:

```text
Redis SCAN over Internet: extremely slow
same SCAN locally on Redis host: fast
```

Do **not** equate SCAN latency directly with one `doWrite()` operation.

`SCAN` may require many network round trips and is naturally bad over WAN.

However the observation is important because `baseMeta.Write()` serializes metadata commit on an inode-wide lock, and `redisMeta.doWrite()` involves multiple Redis operations / transaction stages.

Even modest per-commit network latency can therefore turn into a queue:

```text
many qcow2 4K writes
       |
       v
many completed slice data writes
       |
       v
commitThread per chunk
       |
       v
single inode-wide metadata lock
       |
       v
WAN Redis transaction latency
```

The result can be exactly what the dump shows:

```text
done=true
err=0
committed=false
```

for a large queue of slices.

This hypothesis should be proven with timing instrumentation rather than inferred only from SCAN.

---

# 14. Capture the FULL goroutine dump next time

The previous extraction only included about 500 lines after the timeout, so the dump of ~200 goroutines is incomplete.

That means we still do **not** have the most valuable stacks:

- `chunkWriter.commitThread`
- `baseMeta.Write`
- `redisMeta.doWrite`
- goroutines blocked on `sync.RWMutex`
- compaction goroutines for inode 596152

Next reproduction: capture substantially more.

Example:

```bash
LOG="$HOME/.juicefs/juicefs.log"

N=$(
  grep -n 'flush 596152 timeout after waited' "$LOG" |
  tail -1 |
  cut -d: -f1
)

sed -n "${N},$((N+3000))p" "$LOG" \
  > /tmp/jfs-596152-full-timeout.txt
```

Then search:

```bash
grep -nE \
'commitThread|baseMeta\)\.Write|redisMeta\)\.doWrite|RWMutex|Semacquire|compactChunk|vfs\.Compact|cachedStore\)\.put' \
/tmp/jfs-596152-full-timeout.txt
```

What would strongly prove the main hypothesis:

```text
many commitThread goroutines
    -> baseMeta.Write
    -> blocked in f.Lock / RWMutex
```

or one active writer inside:

```text
baseMeta.Write
 -> redisMeta.doWrite
 -> Redis network operation
```

while many others wait on the inode lock.

---

# 15. Measure Redis latency from the JuiceFS client host

Do **not** benchmark only on the Redis host.

We need the latency as seen by the JuiceFS process.

Use a protected environment variable rather than exposing passwords in shell history if possible:

```bash
export REDIS_URL='redis://.../1'
```

Simple PING latency sampling:

```bash
for i in $(seq 1 30); do
  /usr/bin/time -f '%e' \
    redis-cli -u "$REDIS_URL" PING >/dev/null
done
```

Also:

```bash
redis-cli -u "$REDIS_URL" --latency
```

Run from:

```text
the same host / network namespace where juicefs mount runs
```

If possible, run a comparison from the Redis host itself.

The goal is not absolute Redis benchmark speed; the goal is to understand how much RTT is introduced in the exact JuiceFS path.

---

# 16. Inspect slice counts for all qcow2 chunks

The single `c596152_0 = 682` measurement is useful but not enough.

Run on the Redis host to avoid WAN scan overhead:

```bash
REDIS_URL='redis://...@localhost:6379/1'

redis-cli -u "$REDIS_URL" --scan --pattern 'c596152_*' |
while IFS= read -r k; do
    n=$(redis-cli -u "$REDIS_URL" --raw LLEN "$k")
    printf '%8d %s\n' "$n" "$k"
done |
sort -nr |
head -100
```

Questions:

1. Are any chunks near or above `2500`?
2. How many chunks are above `350`?
3. Is chunk 0 exceptional, or are dozens/hundreds fragmented?
4. Does LLEN drop after the VM goes idle?
5. Does LLEN remain high because compaction cannot finish against the slow object backend?

---

# 17. The `>=2500` path is a dangerous future cliff

Even though chunk 0 currently has 682 slices, Codex should understand what happens later.

`baseMeta.Write()` holds:

```go
f.Lock()
defer f.Unlock()
```

Then:

```go
if numSlices >= maxSlices { // maxSlices=2500
    m.compactChunk(... once=true ...)
}
```

This is synchronous.

So once a chunk reaches 2500 raw slices:

```text
baseMeta.Write
  holds inode f.Lock
       |
       v
compactChunk synchronously
       |
       v
read many old object blocks
       |
       v
vfs.Compact
       |
       v
writer.SetWriteback(false)
       |
       v
Google Drive object writes complete
       |
       v
finally release inode lock
```

With this backend, that could be disastrous.

It is not proven to be the current 682-slice failure, but it should be included in any long-term fix.

---

# 18. `VFS.Read()` ignores flush errors — correctness concern

Official v1.4.1:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/vfs.go#L654-L755

Current code:

```go
_ = v.writer.Flush(ctx, ino)
n, err = h.reader.Read(ctx, off, buf)
```

If pending writes cannot be metadata-committed and `Flush()` returns `EIO`, the read continues.

Potential sequence:

```text
QEMU writes sector
    |
data staging succeeds
    |
metadata commit does not finish
    |
QEMU reads same / related area
    |
VFS.Read calls writer.Flush()
    |
Flush -> EIO
    |
EIO ignored
    |
reader uses committed metadata view
```

The concern is that this can turn a clear writeback error into a read from an older metadata state.

This does **not yet prove** stale data was actually returned during this incident.

But it is a correctness smell serious enough to:

1. instrument;
2. reproduce in a targeted unit/integration test;
3. consider changing `Read()` to propagate the flush error.

Notably, current upstream `main` still contains the same ignored flush return as of 2026-10-01.

Current-main source:

https://github.com/juicedata/juicefs/blob/main/pkg/vfs/vfs.go

---

# 19. Other ignored flush errors

v1.4.1 also ignores flush errors in some other paths.

Examples include:

## `Truncate()`

```go
_ = v.writer.Flush(ctx, ino)
Meta.Truncate(...)
```

## `Release()`

```go
_ = f.writer.Flush(ctx)
```

Relevant source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/vfs.go

By contrast, `Fallocate()` correctly checks the error:

```go
err = v.writer.Flush(ctx, ino)
if err != 0 {
    return
}
```

This is why the observed QEMU `FALLOC_FL_ZERO_RANGE` call returned `EIO` before the metadata fallocate operation ran.

Codex should inspect all ignored flush return values and determine intended POSIX/FUSE error semantics.

---

# 20. Confirmed `Fallocate` behavior in the incident

The log contains:

```text
fallocate (596152,16,73465856,65536) - input/output error
```

Mode decimal `16` = `0x10` = Linux:

```text
FALLOC_FL_ZERO_RANGE
```

In JuiceFS v1.4.1:

```go
err = v.writer.Flush(ctx, ino)
if err != 0 {
    return
}
err = v.Meta.Fallocate(...)
```

Therefore the observed `fallocate EIO` does **not** mean the metadata `ZERO_RANGE` implementation itself failed.

It means:

```text
QEMU requested ZERO_RANGE
        |
        v
JuiceFS first tried to flush pending writes
        |
        v
flush waited 5 min
        |
        v
EIO
        |
ZERO_RANGE never executed
```

This is a useful distinction.

---

# 21. Writeback staging is NOT the direct explanation for this timeout

In writeback mode, normal slice block handling attempts to write the block into local staging.

v1.4.1 source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/chunk/cached_store.go#L380-L447

Simplified:

```go
if writeback {
    stage block locally

    if stage success:
        s.errors <- nil
        upload asynchronously
        return

    if stage fails:
        fall back to direct synchronous object upload
}
```

For the timed-out pending slices we saw:

```text
done:true
err:0
```

That means `writer.Finish()` had already returned successfully for them.

So for those slices the immediate blocker is not:

```text
waiting for Google Drive upload to finish
```

The object upload may still be happening asynchronously, but the slice's data phase is done.

The unresolved step is metadata commit.

---

# 22. `PutTimeout=60s` also applies to local staging

One non-obvious source detail:

```go
utils.WithTimeout(... bcache.stage(...), PutTimeout)
```

So `--put-timeout` is used not only for object PUT but also as a watchdog around local staging.

Source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/chunk/cached_store.go#L403-L446

If local staging exceeds `PutTimeout`, JuiceFS marks staging as failed and falls back to direct upload.

That can be ugly under local disk stalls.

However, again, at the specific metadata-flush timeout being analyzed:

```text
pending slices have done=true err=0
```

so a staging error is not the best explanation for those pending entries.

Keep staging timeout as a secondary diagnostic, not primary root cause.

---

# 23. `fileWriter.err` is sticky, but is probably not the primary event here

`fileWriter` contains:

```go
err syscall.Errno
```

When `commitThread()` encounters a real metadata commit error, it sets:

```go
f.err = err
```

`Write()` eventually returns:

```go
return f.err
```

There is no obvious normal success path that clears it.

This may cause a long-lived VM file descriptor to continue surfacing old asynchronous write errors.

However:

- `fileWriter.flush()` timing out after 5 min sets its local `err=EIO`;
- it does **not obviously assign that timeout to `f.err`**.

Therefore the repeated 5 minute timeout in this trace is better explained by the same pending chunks remaining stuck, rather than a sticky `f.err` alone.

Still worth reviewing for long-running VM semantics.

---

# 24. Durability caveat: staging files are not clearly fdatasync'd

Separate from the live EIO incident:

Writeback guarantees are weaker than a local block device.

A staging file can be written / closed / renamed without an explicit disk `fsync`/`fdatasync` in the obvious staging path.

If the host loses power, cache disk fails, or the process/system dies before staged data reaches durable media/object storage, a guest `fsync` may have returned success even though the object has not reached Google Drive.

This is a **power-loss / crash durability issue**, not the best explanation for the current live 5-minute flush timeout.

Do not mix these two failure modes.

---

# 25. Upstream docs explicitly acknowledge random-write slice fragmentation

JuiceFS documentation says random writes in large files can create many intermittent slices and that frequent random writes require frequent metadata updates; compaction is scheduled to reduce this fragmentation.

Reference:

https://github.com/juicedata/juicefs/blob/main/docs/en/introduction/io_processing.md

Also the sync/compaction documentation warns that small writes combined with compaction can create severe write amplification.

Reference:

https://github.com/juicedata/juicefs/blob/main/docs/en/guide/sync.md

This workload is therefore directly in the known difficult regime for JuiceFS.

---

# 26. Recommended investigation order for Codex

## Phase A — verify repository and reproduce source truth

Run:

```bash
git status
git status --short
git rev-parse HEAD
git describe --tags --always --dirty
git diff
git diff --check
```

Then:

```bash
gofmt -w \
  cmd/flags.go \
  cmd/mount.go \
  pkg/vfs/vfs.go \
  pkg/vfs/writer.go
```

Build/test as appropriate.

Do not rely on line numbers in this handoff if local custom changes moved them. Search by function/symbol name.

---

## Phase B — add diagnostics BEFORE structural behavior changes

Recommended instrumentation:

### B1. Time `commitThread -> Meta.Write`

In:

```text
pkg/vfs/writer.go
chunkWriter.commitThread()
```

Around:

```go
err = f.w.m.Write(...)
```

record:

```text
inode
chunk index
slice id
slice len/off
start time
Meta.Write duration
result errno
```

Log only when slow, e.g.:

```text
> 100ms
```

or use a histogram if convenient.

Example concept:

```go
start := time.Now()
err = f.w.m.Write(...)
dur := time.Since(start)

if dur > 100*time.Millisecond {
    logger.Warnf(
        "slow meta write inode=%d chunk=%d slice=%d len=%d dur=%s err=%v",
        ...
    )
}
```

This establishes whether the commit thread is spending time inside `Meta.Write`.

---

### B2. Split `baseMeta.Write()` timing into lock-wait vs doWrite

Current:

```go
f.Lock()
defer f.Unlock()

st := m.en.doWrite(...)
```

Instrument separately:

```text
wait time to acquire f.Lock
duration inside doWrite
duration in updateParentStat / quota update
total Write duration
```

This is probably the single most important diagnostic patch.

Desired logs:

```text
slow meta lock wait inode=596152 wait=...
slow redis doWrite inode=596152 chunk=... dur=...
```

If lock wait is huge but doWrite is fast:
- inode-wide serialization / queueing is confirmed.

If doWrite itself is huge:
- Redis/network transaction is the direct bottleneck.

---

### B3. Instrument compaction lifecycle

In:

```text
pkg/meta/base.go compactChunk()
pkg/vfs/compact.go Compact()
```

Log at INFO/WARN for slow compactions:

```text
inode
chunk
raw slice count
compacted slice count
bytes
read duration
write/Finish duration
total duration
result
```

Particularly correlate:

```text
inode 596152
chunk 0
```

with the 5-minute fsync event.

---

### B4. Log ignored `Read()` Flush errors

Before changing behavior, at minimum instrument:

```go
if eno := v.writer.Flush(ctx, ino); eno != 0 {
    logger.Errorf("read preflush inode=%d failed: %s", ino, eno)
}
```

Then determine whether QEMU reads are continuing after a failed pre-read flush.

---

# 27. Candidate code changes, ordered from safest to riskiest

## Candidate 1 — explicit writer flush timeout option

Current flush deadline is indirectly derived from metadata retry count and has a hard 5-minute minimum.

For VM storage, returning `EIO` after 5 minutes is catastrophic.

Consider adding a dedicated option such as:

```text
--writer-flush-timeout
```

or similarly explicit name.

Goals:

- preserve current default behavior;
- allow a longer VM-specific timeout;
- decouple metadata retry count from VFS flush deadline;
- emit a clear warning when exceeded.

This is a **mitigation**, not root-cause fix.

A longer timeout trades:

```text
VM freezes longer
```

for:

```text
lower chance of QEMU receiving EIO during temporary metadata backlog
```

Do not make it infinite by default.

---

## Candidate 2 — propagate `Read()` pre-flush errors

Current code:

```go
_ = v.writer.Flush(ctx, ino)
n, err = h.reader.Read(...)
```

Candidate:

```go
if err = v.writer.Flush(ctx, ino); err != 0 {
    return
}
```

But do not blindly apply without tests.

Questions:

1. Is `Flush()` required for read-after-write consistency here?
2. Can returning EIO introduce a behavior regression worse than stale read?
3. Does FUSE/kernel expect some transient error to be mapped differently?
4. Should `EINTR` / cancellation be handled specially?

A targeted regression test should simulate:

```text
writer has uncommitted slice
Meta.Write fails or stalls
Read arrives
```

and assert expected behavior.

---

## Candidate 3 — reduce inode-wide metadata lock scope

This is the structural fix suggested by upstream issue #6398.

However it is risky.

Potential designs to investigate:

1. per-chunk metadata lock for slice list updates;
2. separate inode-attribute lock from chunk-list locks;
3. serialize only operations that may extend file length;
4. allow same-inode different-chunk metadata writes concurrently while preserving:
   - length monotonicity;
   - mtime/ctime correctness;
   - quota stats;
   - cache invalidation;
   - transaction correctness.

Do not simply replace the inode lock with a per-chunk mutex without proving these invariants.

Look for upstream PRs/discussion linked from #6398 before inventing a new design.

---

## Candidate 4 — avoid synchronous compaction while inode lock is held

This is especially important for the future `>=2500` case.

At `numSlices >= maxSlices`, `baseMeta.Write()` calls `compactChunk()` synchronously before the deferred inode unlock.

Given this backend, that can block on Google Drive object I/O while holding the inode metadata lock.

Potential safer designs to evaluate:

- release/openfile lock before synchronous object compaction;
- apply backpressure without holding the inode lock;
- compact asynchronously but prevent slice count from growing without bound;
- use per-chunk state/condition instead of inode-global blocking.

This needs careful race analysis.

---

## Candidate 5 — hot-chunk compaction policy

For a VM actively random-writing the same chunk, immediate repeated background compaction may produce write amplification.

Ideas to evaluate, not blindly implement:

- compact only after a short idle period;
- avoid starting compaction on a currently very hot chunk;
- rate limit compaction more aggressively;
- expose a compaction policy / threshold option;
- prioritize metadata commit latency over read-fragment reduction for VM-like workloads.

Do not simply disable compaction forever: large slice lists hurt read performance and metadata size, and eventually hit `maxSlices`.

---

## Candidate 6 — do NOT casually enable writeback for compaction

`vfs.Compact()` intentionally does:

```go
writer.SetWriteback(false)
```

Changing this to `true` is tempting because Google Drive is slow.

Do **not** do that as a quick fix.

Compaction changes metadata from many old slices to a new compacted slice. If metadata starts referencing a compacted slice that exists only in local staging and the client dies, another client cannot necessarily read it.

A safe writeback-compaction design would need a durability protocol, e.g.:
- keep old slices authoritative until compacted object upload is durable;
- only atomically switch metadata after object-store completion;
- handle retries / crash recovery.

That is much larger than a one-line change.

---

# 28. Operational A/B tests that can isolate the cause

## Test 1 — metadata Redis local to JuiceFS client

This is the strongest A/B test for the inode-lock/WAN-metadata hypothesis.

Keep:

```text
same qcow2 workload
same JuiceFS
same rclone serve s3
same Google Drive
```

but temporarily use a low-latency local Redis metadata copy/test volume.

If the 5-minute pending-commit backlog disappears, that is powerful evidence.

---

## Test 2 — fast/local object backend, same remote Redis

Opposite isolation:

```text
same Redis path
object backend = local/fast
```

If metadata commits still back up, the bottleneck is metadata/locking.

If the problem disappears only with a fast object backend, compaction/object-I/O interference is more important.

---

## Test 3 — stop VM and manually compact its image

Community CLI supports:

```bash
juicefs compact /path/to/proxmox9_01_vol1.qcow2
```

or broader maintenance via:

```bash
juicefs gc META_URL --compact
```

Reference:

https://juicefs.com/docs/community/command_reference/

After VM is stopped and compaction is complete:

```bash
LLEN c596152_0
```

should drop substantially if compaction succeeds.

Then restart the VM and measure how quickly raw slices accumulate and when latency begins.

Do not run aggressive maintenance blindly while the VM is actively mutating the image; prefer controlled tests.

---

# 29. Check all chunk slice counts before changing thresholds

Run locally against Redis:

```bash
REDIS_URL='redis://...@localhost:6379/1'

redis-cli -u "$REDIS_URL" --scan --pattern 'c596152_*' |
while IFS= read -r k; do
    n=$(redis-cli -u "$REDIS_URL" --raw LLEN "$k")
    printf '%8d %s\n' "$n" "$k"
done |
sort -nr
```

Interpretation:

```text
0..4       very low fragmentation
5..350     reads can trigger background compaction
351..2499  writes continuously try to schedule background compaction
>=2500     synchronous maxSlices path can execute in baseMeta.Write
```

The exact “5” trigger applies to `Read()` (`len(ss)>=5 || len(built)>=5`), while the write-trigger is `numSlices%100==99 || numSlices>350`.

---

# 30. Observe compaction metrics

The code exposes:

```text
compact_size_histogram_bytes
```

and JuiceFS documentation refers to:

```text
juicefs_compact_size_histogram_bytes
```

depending on metric registration/prefix.

Check the mount metrics endpoint / stats output and see whether compaction throughput spikes before VM failures.

Source:

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/compact.go

If debug logging is enabled for a controlled reproduction, grep:

```bash
grep -E \
'compact 596152:|compact .* slices|compaction for 596152' \
"$HOME/.juicefs/juicefs.log"
```

The old failure event cannot be reconstructed from debug compaction logs because debug logging was disabled at that time.

---

# 31. Why the custom `--slice-flush-*` knobs may help only partially

The patch adds:

```text
--slice-flush-wait
--slice-flush-idle
```

Increasing waits can reduce fragmentation **when writes can be accumulated into the same writable slice**.

However qcow2 random 4 KiB writes frequently target non-contiguous offsets.

`chunkWriter.findWritableSlice()` only reuses a slice under specific positional conditions; arbitrary random writes create new slices.

Therefore:

```text
5s -> 10s
1s idle -> 3s idle
```

may reduce slice creation for some bursty/locality-heavy patterns, but it cannot solve the fundamental metadata commit bottleneck for fully random writes.

Keep the feature, but do not expect it to be the root fix.

---

# 32. Current hypothesis ranking

## HIGH: A. inode-wide metadata commit serialization + WAN Redis latency

Evidence:

- pending slices are `done=true`, `err=0`, `committed=false`;
- upstream issue #6398 is the same 4 KiB random-write / large-file workload;
- `baseMeta.Write` uses inode-wide `f.Lock`;
- Redis metadata commits are network transactions;
- user observed large difference between remote and local Redis scanning.

Missing proof:

- full stack showing commit threads blocked on lock / Redis;
- direct timing of lock acquisition and `doWrite`.

---

## HIGH: B. JuiceFS 5-minute flush timeout directly causes guest-visible disk `EIO`

This part is confirmed.

The underlying cause of the backlog is not yet proven, but the mechanism delivering errors to QEMU is known.

---

## MEDIUM-HIGH amplifier: C. automatic compaction + very slow object backend

Evidence:

- `LLEN c596152_0 = 682`;
- >350 write-side background compaction trigger active;
- read-side trigger activates from >=5;
- compact writer disables writeback;
- object PUTs take 10–25s even when successful;
- many uploader goroutines are present.

Correction:

- at 682, this is background compaction, not the >=2500 synchronous branch.

---

## HIGH correctness concern, unproven as primary cause: D. `VFS.Read()` ignores flush `EIO`

Could turn a clear metadata-commit failure into a stale/old read.

Needs targeted testing.

---

## MEDIUM / future risk: E. >=2500 synchronous compaction under inode lock

Not currently proven for chunk 0.

Can become catastrophic if fragmentation grows faster than background compaction reduces it.

---

## LOW for this specific timeout: F. local staging failure

The pending slices at timeout already have:

```text
done=true err=0
```

so their data phase completed.

Still monitor `write ... to disk: ... upload it directly` warnings.

---

## LOW for new writes: G. rclone `serve s3` failed-upload corruption bug

User is on rclone v1.75.1, which includes the fix.

Only historic writes under older rclone versions remain a caveat.

---

# 33. Important corrections from earlier analysis

Codex should not inherit earlier speculative conclusions without these corrections.

### Correction 1

Earlier idea:

```text
682 slices => synchronous compaction is blocking Meta.Write
```

Wrong.

Actual threshold:

```text
maxSlices = 2500
```

At 682 it is background compaction.

---

### Correction 2

Earlier idea:

```text
Google Drive async upload not finished => fsync naturally waits on upload
```

Wrong for normal successful writeback staging.

Writeback normally lets `Finish()` succeed after local staging and uploads asynchronously.

The observed `done=true err=0` confirms that the current pending queue is past that phase.

---

### Correction 3

Earlier 30-second timeout logs belong to an older PID/config.

The affected PID shown in the detailed stack has:

```text
PutTimeout=60s
```

---

### Correction 4

Earlier assistant-created patch offsets/fuzz do not by themselves prove the user's v1.4.1 source differs significantly from upstream.

The patch files themselves had bad synthetic hunk line information.

Use the current repo and Git state as source of truth.

---

# 34. Suggested regression / stress tests

Create a reproducible local test before major locking changes.

## Test workload

A single large file:

```text
10 GiB
```

4 KiB random writes:

```bash
fio \
  --name=jfs-randwrite \
  --filename=/mnt/jfs/test.img \
  --size=10G \
  --rw=randwrite \
  --bs=4k \
  --iodepth=32 \
  --numjobs=1 \
  --direct=1
```

Tune fsync behavior separately so it resembles QEMU while remaining reproducible.

Track:

```text
Meta.Write lock-wait histogram
Meta.Write doWrite duration
pending slice count
Redis LLEN per hot chunk
compaction count/duration
fileWriter flush duration
fsync errno
```

Run matrices:

```text
Redis local     vs WAN
object local    vs rclone/Drive
writeback on    vs off
slice timers default vs custom
```

The goal is not raw performance; the goal is to isolate which subsystem makes `committed=false` accumulate.

---

# 35. Tests for `Read()` flush-error handling

A specific test should simulate:

1. write data into a slice;
2. make `Meta.Write` fail / stall;
3. call `VFS.Read`;
4. confirm current behavior;
5. decide desired behavior.

Current likely behavior:

```text
Flush returns EIO
Read ignores it
reader proceeds using committed metadata
```

Candidate desired behavior:

```text
Read returns EIO
```

This may be safer than silently returning stale bytes, especially for a VM block image.

But codify expected behavior before modifying production code.

---

# 36. Questions Codex should answer before implementing a structural fix

1. **Where exactly are `commitThread` goroutines waiting during the 5-minute incident?**
   - openfile lock?
   - Redis transaction?
   - updateParentStat?
   - quota update?
   - something else?

2. **How many `Meta.Write` calls/sec can the remote Redis path sustain?**

3. **How many slice commits/sec does the QEMU workload generate?**

4. **Does background compaction materially increase Redis latency or only object I/O?**

5. **Can the openfile lock safely be split into:**
   - inode attrs lock
   - per-chunk metadata lock?

6. **Can inode length/mtime updates be made atomic in Redis without serializing every chunk write in process?**

7. **What is the intended upstream reason for `VFS.Read` ignoring writer flush errors?**
   - history / commit / issue?
   - deliberate or accidental?

8. **Can `fileWriter.flush` timeout be exposed as an independent config without changing defaults?**

9. **Can hot-chunk compaction be deferred without allowing unbounded raw-slice growth?**

10. **What is the safest behavior once slice count approaches 2500 on a very slow object backend?**

---

# 37. Useful upstream/public references

## JuiceFS v1.4.1 writer

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/writer.go

Key areas:

- `sliceWriter.flushData`
- `chunkWriter.commitThread`
- `fileWriter.Write`
- `fileWriter.flush`
- `dataWriter.flushAll`

---

## JuiceFS v1.4.1 metadata Write / compaction trigger

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L2049-L2072

---

## JuiceFS v1.4.1 compactChunk

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/base.go#L2659-L2761

---

## JuiceFS v1.4.1 VFS compaction data path

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/compact.go#L50-L104

Note:

```go
writer.SetWriteback(false)
```

---

## JuiceFS v1.4.1 Redis doWrite

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/meta/redis.go#L3002-L3045

---

## JuiceFS v1.4.1 VFS read/fsync/fallocate

https://github.com/juicedata/juicefs/blob/v1.4.1/pkg/vfs/vfs.go

Important functions:

```text
Read
Fallocate
Flush
Fsync
Truncate
Release
```

---

## Upstream issue matching workload

https://github.com/juicedata/juicefs/issues/6398

4 KiB random writes to a large file blocked on inode-wide openfile lock.

---

## JuiceFS write/random IO documentation

https://github.com/juicedata/juicefs/blob/main/docs/en/introduction/io_processing.md

---

## JuiceFS compaction / write-amplification docs

https://github.com/juicedata/juicefs/blob/main/docs/en/guide/sync.md

---

## JuiceFS maintenance / compact command

https://juicefs.com/docs/community/administration/status_check_and_maintenance/

---

## rclone changelog v1.75.1

https://rclone.org/changelog/

Relevant entry:

```text
serve s3:
Fix failed uploads deleting or corrupting the object at the key
```

---

# 38. Immediate next action recommendation

Before changing lock architecture:

1. **Instrument `baseMeta.Write()`**
   - inode-lock acquisition wait
   - `doWrite()` duration
   - total duration.

2. **Instrument `commitThread()`**
   - `Meta.Write()` duration.

3. **Instrument compaction**
   - inode/chunk/slices/bytes/duration.

4. Reproduce one VM/fio event.

5. Capture the **complete** goroutine dump.

6. Measure Redis RTT from the JuiceFS client.

With those numbers, the next decision should become straightforward:

```text
lock wait dominates
    -> lock architecture / per-chunk concurrency work

Redis doWrite dominates
    -> metadata placement / Redis txn/network optimization

compaction object I/O dominates
    -> hot-chunk compaction policy / scheduling work

all of the above interact
    -> first stop guest-visible EIO with explicit flush timeout,
       then tackle metadata concurrency
```

---

# 39. Strong caution for production VM data

Until this is fixed, this path is currently capable of:

```text
JuiceFS flush timeout
    -> EIO to QEMU
    -> guest disk I/O failure
```

That is directly observed, not theoretical.

For valuable VM images, do not rely on “it usually retries eventually” as a safety mechanism.

At minimum:

- keep backups;
- avoid abrupt shutdowns while staging is non-empty;
- monitor JuiceFS logs for `flush <inode> timeout`;
- consider a less latency-sensitive storage path for critical VM disks during testing.

---

# 40. One-line state of investigation

> **The data blocks for qcow2 writes are finishing, but slice metadata commits are not draining fast enough; JuiceFS eventually times out the inode flush after 5 minutes and returns EIO to QEMU. The top suspect is inode-wide serialized metadata commit over WAN Redis, with automatic compaction against a very slow rclone/Google Drive backend acting as a significant amplifier.**
