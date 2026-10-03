# Diagnosing stalled VM image writes

> この文書は調査プロジェクト側へ移設した記録です。`pkg/`・`cmd/` などのソースパスと Go コマンドは、別管理の `juicefs/` リポジトリを基準にします。記載の作業状況は当時の履歴で、最新の知見は調査ルートの README と `docs/findings.md` を参照してください。

This local change targets the v1.4.1 client. It propagates the writer preflush error from `VFS.Read`: a read that cannot flush pending writes returns zero bytes and the flush errno, rather than fetching previously committed data. Reader errors retain their existing handling. Normal reads still flush before fetching data. Failed write errors remain sticky for the existing file writer, as before.

The added logs diagnose slow slice commits without changing metadata locks, compaction thresholds, or writeback durability. The subsequent wait-policy change below makes the whole-file flush deadline opt-in. The existing `--slice-flush-wait` and `--slice-flush-idle` options retain their 5s and 1s defaults. These slice timers are different from the whole-file flush deadline.

## Logs

Operations taking at least one second emit WARN summaries at the normal log level:

| Message | Fields and interpretation |
| --- | --- |
| `slow slice commit` | `inode`, `chunk`, `slice`, `metadata`, `errno`: total time spent inside `Meta.Write`, including synchronous compaction. |
| `slow metadata write` | `total`, `lock_wait`, `doWrite`, `stat`, `compact`, `slices`, `errno`: open-file lock acquisition, backend update, parent/user/group statistics, and synchronous compaction durations. |
| `slow metadata transaction` | `key`, `total`, `lock_wait`, `active`, `attempts`, `err`: Redis local transaction lock wait versus transaction execution. `active` includes WATCH/GET/EXEC, pool waits, retry backoff and quota work; it is not a pure Redis network RTT. The key hash selects one of 1,024 locks, so other keys can contend too. |
| `slow compaction` | `inode`, `chunk`, `once`, `force`, `slices`, `bytes`, `total`, `queue_wait`, `object`, `metadata`: compaction admission/existing-job wait, object callback, and backend metadata replacement plus old-slice cleanup (which can include object DELETE). `object` includes VFS memory pressure waits, object reads and uploads. |

Durations do not necessarily add up to `total`: setup, cache invalidation, deferred cleanup and diagnostic logging also contribute. The compaction summary can describe an early exit or failed attempt; it is not proof of successful object upload or metadata replacement. Existing error logs carry the failure details. Forced recursive compaction can include subsequent passes in the outer `total`.

DEBUG logs report entry into processing phases. Enable the existing debug logging option for a bounded reproduction when a call is stuck and has no completion summary. Use a local, responsive log destination: debug output increases volume and some phase messages occur while an inode/transaction lock is held.

Correlate `inode`, `chunk`, and `slice` for VFS and base metadata writes. Redis transaction logs identify `key`; multiple operations on the same key can interleave, so use a complete goroutine dump to resolve ambiguity. A transaction's DEBUG `phase=active` means its local lock has been acquired; absence of a WARN does not prove completion.

To extract the relevant messages from a captured log:

```bash
rg 'slow (slice commit|metadata write|metadata transaction|compaction)|phase=' /path/to/juicefs.log
```

## What the source establishes

`baseMeta.Write` holds the open-file inode lock across backend `doWrite`, statistics updates, and synchronous compaction. Redis also serializes transactions using a hash of the first watched key; `doWrite` watches the inode key. Replacing only the outer lock with a per-chunk lock therefore cannot establish concurrent Redis writes to the same inode.

Background `compactChunk` does not itself acquire the open-file inode lock. However, a write with at least 2,500 raw slices invokes compaction synchronously while retaining that lock, and can wait for an already running background compaction. A later raw list length of 682 for one chunk does not exclude a threshold crossing during the earlier failure or in another chunk.

Pending slices with `done=true`, `err=0`, and `committed=false` show that VFS commit processing has not finished. They do not prove that the backend RPUSH has not happened: `Meta.Write` can remain in statistics or compaction after the backend update succeeds, and later slices can simply be queued behind the first one.

## Next reproduction

Use the diagnostic build on an isolated test image with the same metadata/object topology. Capture the complete log and goroutine dump around a flush timeout, all hot-chunk raw slice counts, and Redis RTT from the client. Compare low-latency Redis with the WAN path, and a local object backend with rclone/Drive, one variable at a time.

The Read regression fix prevents a successful stale read after preflush failure. It does not remove the metadata backlog. Before the wait-policy change, the five-minute-minimum flush deadline could still return EIO. Those failure mechanisms still require measurements on the affected deployment before changing concurrency or compaction policy.

## Local verification (2026-10-01)

The four metadata-failure cases (EIO, ENOSPC, EDQUOT, ENOENT) returned `"old"` with no error before the Read fix. The fixed regression test checks zero bytes, the original flush errno, an untouched buffer, and handle cleanup. The targeted race check passed. The VFS and high-level FS suites passed with a temporary local Redis.

The requested broad Makefile targets were attempted. `test.pkg` requires absent GlusterFS headers/libraries. `test.meta.core` reaches an unconfigured TiKV service in `TestLoadDump` even with `SKIP_NON_CORE=true`; it does not fully complete in this environment. Targeted Redis, SQLite and MemKV suites, quota checks and canceled-KV-transaction checks cover the modified common metadata path.

Additional broad checks found `pkg/chunk` test linking failures in the existing mockey library (`runtime.duffcopy`/`runtime.duffzero`) with Go 1.26.4 and a FUSE `FstatDeleted` time-attribute mismatch. Both reproduce from an unchanged archive of HEAD `febf149a`; they are not introduced by this patch. Full broad test success is therefore not claimed.


## Incident confirmed on 2026-10-01

The user's log `/home/kwatanabe/.juicefs/diagnostics/vm-io-20261001-183557.log` identifies the immediate cause of the observed fsync EIO. The running client is PID 989530. The log continues to grow, so the sequence below describes observed events rather than the final state of the mount.

| Local time (JST) | Observed event |
| --- | --- |
| 18:46:13.311124 | Background compaction for inode 596153, chunk 0, output slice 3721413 enters the backend/cleanup phase after object generation. |
| 18:55:31.001431 | Write of slice 3727383 succeeds in `doWrite` in 42.074811ms, producing exactly 2,500 raw slices. |
| 18:55:31.001475–001486 | This Write enters synchronous compaction and waits for the existing background compaction. |
| 19:00:31.024267 | Whole-file flush reaches its five-minute deadline; the same slice is pending with done=true, err=0, committed=false. |
| 19:00:31.033106 | `fsync(596153,1)` returns EIO after 300.074009 seconds. |
| 19:04:28.187113 | Existing background compaction finally finishes: total 18m26.307667724s, backend/cleanup 18m14.875939104s. |
| 19:04:28.187220 | Synchronous compaction leaves its queue wait after 8m57.185735463s, then begins another pass. |

The timeout goroutine dump confirms that the foreground commit thread is sleeping in `compactChunk(once=true)`, invoked by `baseMeta.Write`. The background compaction for the same inode/chunk is inside `redisMeta.doCompactChunk -> deleteSlice -> deleteSlice_ -> cachedStore.Remove -> rSlice.Remove -> cachedStore.delete`. It is waiting on object DELETE, not on an inode lock acquisition or a Redis WATCH operation.

The supplied `--max-deletes=-1` is decisive. `startDeleteSliceTasks` starts workers only when MaxDeletes > 0. `deleteSlice` skips deletion only for MaxDeletes == 0, and falls back to synchronous deletion when the queue is nil. A negative value therefore performs deletion in the caller, rather than providing unlimited deletion concurrency. Redis compaction updates the chunk list and reference counts before deleting obsolete slices, but keeps the compaction marked active until the deletion loop completes. With hundreds or 1,000 old slices and object DELETE calls often around 0.7–1 seconds each, this loop holds up subsequent synchronous compaction long enough to exceed the flush deadline.

For the next controlled test, replace `--max-deletes=-1` with the CLI default `--max-deletes 10`. Keep the other settings initially for comparison. Positive values use background deletion workers with a bounded queue; this removes the demonstrated in-line deletion path, but sustained queue saturation and slow object processing remain possible. Do not interpret this configuration correction as proof that all causes of VM corruption have been resolved.

The preceding advice to retain every mount option overlooked the negative deletion setting and is superseded by this incident analysis. The Read preflush-error fix remains relevant. On the incident build it did not prevent the synchronous compaction deadline failure shown here; the subsequent wait-policy change addresses the deadline conversion. No production mount, Redis metadata, object storage, or VM disk has been modified by the investigation.


## Wait-first policy added after the incident

The user explicitly selected completion-based waiting as the default for this investigation branch. `--writer-flush-timeout` defaults to `0s`. A default flush waits for its pending slices/metadata commits regardless of elapsed time; this includes synchronous compaction waiting on an existing job whose old-slice cleanup is blocked by a full deletion queue. A waiting flush emits a summary warning every five minutes without completing the request with EIO. Queue capacity, compaction ordering, and object cleanup durability have not been weakened.

A real writer error (including EIO, ENOSPC, or EDQUOT) is still returned, even if another chunk has pending commits. A request is never reported successful before its pending commits finish. Timeout and cancellation do not discard or roll back in-flight writes. Explicit request cancellation retains the existing `PutTimeout × 2` grace and returns EINTR if the operation is still pending. Completing commits take precedence over an elapsed opt-in deadline when the waiter reacquires its lock.

`--writer-flush-timeout auto` explicitly selects the retry-derived legacy deadline, with a five-minute minimum. Very large retry counts saturate at the maximum duration instead of overflowing into a short deadline. Positive durations opt into a finite deadline; reaching it with pending commits returns EIO as before. Invalid or negative CLI durations are rejected before command execution. Go callers use `WriterFlushTimeout=0` for unlimited waiting and `AutoWriterFlushTimeout` for legacy behavior; other negative values emit a warning and normalize to zero at writer initialization.

This changes the default for all users of this VFS configuration, including Go callers using zero-valued configuration, not just the FUSE mount command. Existing deployments requiring a finite wait must opt in. QEMU/device/guest timeouts, genuine object or metadata failures after retries, and local staging capacity failures can still surface separately. Waiting indefinitely can make a VM, read, fsync, close, or normal unmount wait indefinitely during a persistent outage. Successful JuiceFS writeback staging retains its existing semantics; this patch does not make fsync wait for every asynchronous cloud upload.

Negative `--max-deletes` values now warn at startup that they perform synchronous deletion in the caller rather than unlimited concurrent deletion. CLI help and the shared option reference explain positive/zero/negative modes and queue capacity.

Regression coverage includes: pending flush completion and real errors in both writeback modes; opt-in deadlines; successful completion at a deadline; cancellation and its grace; a saturated production deletion queue with resume/cancel; and real MemKV metadata replacement followed by a 2,500-slice synchronous compaction blocked on deletion-queue cleanup. The metadata compaction test simulates object creation via the existing message callback convention and checks final visible slice metadata. A separately enabled `JFS_TEST_LONG_FLUSH_WAIT=1` test waits beyond the real former five-minute deadline and then releases pending commits in both writeback modes.


The wait-policy validation passed the VFS and FS suites; selected Redis/SQLite/MemKV, quota and cancellation suites; three runs of the new VFS/metadata/CLI tests under the race detector; and the explicitly enabled 304-second default-wait regression in both writeback modes. Normal builds succeed. The full `make test.cmd` target could not run because sudo requires authentication; the modified CLI paths passed their targeted normal/race tests. Earlier broad-suite dependency/baseline limitations above still apply.

## PostgreSQL source review (2026-10-01; no runtime tests)

At the user's request, the PostgreSQL path was reviewed without starting a database or executing tests. PostgreSQL registers `newSQLMeta` and uses the same `dbMeta.doWrite` and `dbMeta.doCompactChunk` implementation as SQLite. The constructor selects pgx, and `Name()` maps it back to `postgres` so PostgreSQL-specific branches remain active.

The default no-deadline flush, real-writer-error propagation, Read preflush check, compaction admission and deletion workers/queue all live in common VFS/baseMeta code. They do not depend on Redis-specific methods and apply to PostgreSQL as well. SQL slice appending uses the SQLite/PostgreSQL `ON CONFLICT` path, and the slice count feeds the same common 2,500-slice compaction threshold.

The lock mechanisms differ: SQLite forces transaction serialization through one client-side lock slot; PostgreSQL uses the supplied inode lock slot(s) plus `SELECT ... FOR UPDATE` on inode/chunk rows. `dbMeta.doWrite` updates slice data, slice references and inode attributes inside the transaction. The pinned Xorm `Transaction()` returns success only after `session.Commit()` succeeds. SQL compaction also updates the chunk and references transactionally, then looks up obsolete references and calls `deleteSlice` after that transaction has returned. Thus a full deletion queue does not keep that SQL transaction open, although the common compaction flag and an outer synchronous Write's open-file lock can still remain held while cleanup waits.

With `--max-deletes 10`, the same 102,400-slice queue and ten background deletion workers are used. If saturated, PostgreSQL cleanup waits for capacity, and a default VFS flush keeps waiting for pending commits without converting elapsed time into EIO. The negative-deletion warning applies through common Config.SelfCheck too.

This review does not mean PostgreSQL can never return EIO. The pre-existing SQL transaction retry loop is bounded at 50 attempts; `shouldRetry` has PostgreSQL-specific handling, and a final database/driver/commit failure passes through `errno` and VFS writer error handling. Those retry limits, SQL statements and lock mechanisms were not changed. Database/driver-side timeouts or connection failures can still end an operation separately from the removed default VFS flush deadline. Redis-specific transaction phase logs are not emitted for SQL, but common metadata Write, compaction, slice commit logs and transaction-duration metrics remain available.

No PostgreSQL-specific incompatibility in the wait-policy change was found by this source review. PostgreSQL runtime behavior, deployed database settings and performance remain unverified; no tests, build, database connections or migrations were run for this review.

## Small PUT investigation (2026-10-01)

This follow-up separates three different sizes: a VFS slice's uncompressed span, an object block's uncompressed length, and the compressed bytes passed to object storage. The diagnostic changes add freeze-reason and payload-length logs; they do not change write aggregation, read-before-flush behavior, fsync/writeback durability, or metadata commit waiting. The diagnostics are covered by targeted tests; broad-suite limitations and the pre-existing disk-cache race are recorded below.

The writer reuses an unfrozen slice only when the next write starts between its completed-block boundary and its current end: `s.off + floor(s.slen / blockSize) * blockSize <= pos <= s.off + s.slen`. This follows `findWritableSlice` in `pkg/vfs/writer.go`. Writes to an earlier complete block, writes separated by a gap, and writes in another 64 MiB chunk can therefore create another slice before either configured timer expires. An overlap that cannot reuse the current slice ends the search and creates a new slice; it does not merge arbitrary pending ranges. Once a slice has an ID, `sliceWriter.write` calls `FlushTo` for complete blocks as data grows. Increasing slice timers does not delay that complete-block upload path.

The new DEBUG `slice freeze` log records `inode`, `chunk`, `slice`, `off`, `raw_length`, `reason`, `age`, `idle`, and `started_unix_ns`. The caller holds the file lock when freezing the slice. A freeze can happen before its slice ID is allocated, so `slice=0` is possible. The later `slice finish` entry repeats the inode/chunk/offset/start identity with the allocated slice ID. That entry marks entry into slice completion, not proof of a successful upload or metadata commit.

| Freeze reason | Source condition |
| --- | --- |
| `age` | The background scan sees time since creation greater than `SliceFlushWait`. |
| `idle` | Both time since the last write and time since creation exceed `SliceFlushIdle`. |
| `slice_pressure` | More than 800 pending slices exist for the file; the scan selects one parity of chunks and the earlier half of their slices. Age and idle take precedence in the reason label when multiple conditions hold. |
| `writable_window` | A slice encountered fifth or later in the newest-to-oldest search cannot accept this write. An earlier overlap can stop the search before this condition is reached. |
| `commit_age` | The commit thread's first unfinished slice exceeds twice `SliceFlushWait`. |
| `full_slice` | The slice's length reaches the 64 MiB chunk size. |
| `explicit_flush` | A file flush freezes all pending writable slices. Read, fsync, close, fallocate, truncate, copy operations, and other explicit barriers can reach this path. |

The background scan runs approximately every 100 ms. Zero or negative slice timer settings normalize to the existing 5s age and 1s idle defaults; they do not disable these conditions. At 1,000 pending slices a new write waits. Memory pressure also delays writes, but that wait does not itself freeze slices: buffer usage above the configured limit causes a short delay, and usage above twice the limit continues waiting for memory to be released. Long timers can therefore interact with memory pressure and other pending work.

`explicit_flush` identifies the writer barrier but does not identify its caller. To distinguish a Read-triggered barrier from fsync or close, correlate the access log, caller tracing, and time window. Normal VFS Read flushes the inode before reading; high-level `fs.File` Read flushes its associated writer when present. A frozen slice finishes its data before metadata commit, and slices within a chunk commit in creation order. A new growing chunk can also depend on a growing slice from a preceding chunk. Consequently, data upload completion alone does not establish that file flush or fsync has completed.

| Size or storage representation | Interpretation |
| --- | --- |
| Freeze/finish `raw_length` | The entire VFS slice's uncompressed length, potentially spanning several object blocks. |
| Object key suffix, for example `_0_4096` | The individual block's original uncompressed length. Compression does not replace this suffix with the compressed size. |
| PUT `payload_bytes` | The length of the page handed by `cachedStore.put` to its object-storage interface, after compression. If that interface has an encryption wrapper, this is before the wrapper encrypts the data, rather than the final provider-side byte count. |
| Local writeback stage file | Raw block data, with an optional checksum trailer and a stage footer. Its file size is not the compressed cloud payload size. |

A key suffix below 1 KiB therefore means the original block itself is below 1 KiB. A provider-visible payload below 1 KiB with a larger suffix can instead be explained by compression. The old PUT logs do not include `payload_bytes`, so they cannot establish the actual compressed request sizes.

The historical log analysis used a fixed 144,316,877-byte snapshot of `/home/kwatanabe/.juicefs/diagnostics/vm-io-20261001-183557.log`, SHA-256 `9f92d051a6e36551eaeb4b90aaedaf848f667e2767d0ab55d163aee1f2a6264c`, covering local timestamps 18:36:03.847499 through 20:24:40.937939. The source log was retained without modification. These counts classify successful PUT entries by their raw object-key suffix, and separate ordinary writes from compaction outputs; they are not counts of compressed payload sizes or unique retained objects.

| Snapshot category | Observed successful PUT entries |
| --- | --- |
| Ordinary writes, total | 15,719 |
| Ordinary writes with raw length 4 KiB | 9,627 (61.24%) |
| Ordinary writes with raw length at most 64 KiB | 94.31% |
| Ordinary writes with raw length below 1 KiB | 11, all with raw length 8 bytes |
| Ordinary writes with raw length 4 MiB | 52 |
| Compaction outputs, total | 737 |
| Compaction outputs with raw length 4 MiB | 700 |

Thus the historical ordinary-write stream contains many genuinely small raw blocks, especially 4 KiB blocks, but very few raw blocks below 1 KiB. The much larger compaction outputs are a separate population. The snapshot alone cannot tell which freeze condition generated the small ordinary blocks, nor how small their compressed payloads were.

A read-only inspection of the current FUSE mount's root `.config` reported `Version=1.4.1+2026-10-01.febf149a-vmio-wait`, zstd compression, 4 MiB block size, `SliceFlushWait=30s`, `SliceFlushIdle=10s`, `WriterFlushTimeout=0`, `MaxDeletes=10`, writeback threshold 4,194,305 bytes, and upload delay zero. These values describe the exposed configuration at inspection time. The process executable and active metadata database driver were not independently verified, and this configuration should not be assumed to describe every earlier snapshot entry. No encryption key was present in the inspected configuration; an `EncryptAlgo` value alone is insufficient evidence that the encryption wrapper is enabled.

The local measurement fixture uses a separate MemKV client and a memory object backend, with an optional local disk writeback cache. MemKV initialization writes the shared `/tmp/juicefs.memkv.setting.json`, so the fixture backs up and restores that file. It also isolates `_FUSE_STATE_PATH`. The fixture calls chunk configuration `SelfCheck`, enables creation of its temporary cache directory, checks the normalized writeback threshold, and verifies positive `staging_write_bytes`. This corrected fixture exercised real disk staging and passed three runs with both writeback modes. An earlier incomplete fixture did not exercise staging and is excluded from the final writeback evidence. The following measurements use 30s/10s timers and verify the latest written data through VFS reads:

| Write pattern | Observed raw PUT lengths |
| --- | --- |
| Sequential writes at offsets 0 and 512, then fsync | One 1,024-byte PUT |
| Repeated overwrite at offset 0, then fsync | One 512-byte PUT |
| Writes separated by a gap | Two 512-byte PUTs |
| Writes in separate 64 MiB chunks | Two 512-byte PUTs |
| Read or fsync between writes | Two 512-byte PUTs |
| Complete 4 MiB block followed by a 512-byte overwrite of its beginning | One 4 MiB PUT and one 512-byte PUT |

For a 128 KiB zero-filled write, the measured payloads were 131,072 bytes with no compression, 524 bytes with lz4, and 22 bytes with zstd. A 4 KiB zero-filled write with zstd also produced a payload below 1 KiB. These controlled measurements demonstrate that raw block length and compressed PUT length can differ greatly; they do not establish the data content or payload distribution of the VM's historical writes. The additional blocked-cloud test stages 512 raw zero bytes to a 516-byte local file (four-byte checksum, no tier footer), completes fsync and reads the committed data while PUT is blocked, then measures a 19-byte zstd payload after release. This preserves the existing writeback staging semantics; it does not make fsync wait for cloud upload.

The diagnostic test failed before the additions because freeze-reason/payload fields were absent, then passed after the change. The VFS suite passed with a temporary local test Redis (12.236s). Disk-writeback race testing detected a `stageFull` read versus `checkFreeSpace` write; the same test reproduces that race from an unchanged archive of HEAD `3bed0d82`, with only the measurement test copied in. An earlier direct-upload fixture also exposed disk-cache `rawFull` and cache/`noOp.Compress` races; those two observations have not been independently compared to baseline. No claim of a race-clean disk-writeback suite is made. `make test.pkg` was blocked in this run by missing GlusterFS development files and its absent coverage output directory. The Go 1.26 mockey linking incompatibility recorded in the earlier investigation remains an additional known limitation, but this run stopped before reaching that link stage. These are distinct from the targeted validation.

The user clarified that the small size was inferred from `juicefs stats --verbosity` object columns `put` and `put_c`. In `cmd/stats.go`, `put` is the interval delta of `juicefs_object_request_data_bytes_PUT`, divided by the interval seconds. `put_c` is the interval delta of `juicefs_object_request_durations_histogram_seconds_PUT_total`, also divided by seconds. Thus ordinary update rows show bytes/second and completed object-storage calls/second, and their ratio estimates the mean compressed payload per call in that interval. The adjacent `lat` value is milliseconds per call. Displayed values are rounded/truncated and use binary scaling; a ratio below 1 KiB does not prove every request is below 1 KiB. A longer window of exact counter deltas is preferable to ratios from rounded columns.

Both counters are updated after `storage.Put` returns, for success and failure alike, at the chunk-store boundary. JuiceFS upload retries count again; retries hidden inside a storage SDK need not appear as separate metric calls. The byte counter counts the buffer offered to the storage API, including unsuccessful attempts, rather than measuring actual wire bytes sent. Normal writes, compaction and staging reuploads share these counters. With writeback, these upload-completion counters can lag the application's writes/fsyncs. Therefore a small average in stats is compatible with many small raw ordinary-write blocks plus zstd compression, but does not establish the freeze reason or a size histogram.

The next bounded reproduction should correlate freeze reasons, allocated slice IDs, PUT key suffixes and `payload_bytes`, access-log read/fsync/close calls, and commit/compaction progress. This can separate timer-driven short slices from application barriers, noncontiguous writes, completed-block rewrites, compression, and metadata backlog. The diagnostic additions preserve the existing barriers while that evidence is collected.

Broader aggregation across these limits would require a separate design. Reading pending data directly would need a correct pending-range overlay, including overlap ordering and read-range semantics. Combining noncontiguous writes would need sparse-range or coalescing support rather than simply extending a slice. Aggregation across fsync would require durable staging indirection that preserves recovery and visibility guarantees. These are substantial behavior changes: an operation must not return success before its required durability and metadata visibility conditions hold. Increasing timers or adding logs alone does not provide those semantics.


## Restarted diagnostic run (2026-10-02)

The latest supplied run is `vm-io-20261002-014922.log`, PID 3153, version `1.4.1+2026-10-01.3bed0d82-smallput-diag-local`, using Redis. Its immutable analysis prefix is 179,156,595 bytes / 1,111,514 lines, timestamps 01:49:30.457741–02:24:09.308508 JST, SHA-256 `2f70019e76d958259462fff848083b8726fb7bcb3dd5701dfd9d1fbea2604788`. The original log remains unmodified and continues growing. Current exposed configuration retains 30s/10s slice timers, zero writer deadline, max-deletes 10, zstd/4 MiB blocks, and writeback.

Of 17,696 freezes, 9,900 (55.94%) were `explicit_flush`, 7,781 (43.97%) were `writable_window`, and 15 (0.085%) were `idle`. Other reason counts were zero. Explicit-flush age median was 1.716 ms, p95 3.632 s, maximum 7.516 s. Writable-window age median was 1.192 ms and p95 7.949 ms. All explicit/window freezes happened before both configured timers. Raw slice length was 4 KiB for 11,656 freezes (65.87%). All slices reached metadata `phase=done` with errno zero. These observations identify timer-independent limits; they do not distinguish Read, fsync, close or other explicit-flush callers.

The restart scan found 38,809 staging keys. Successful PUTs comprised 8,337 recovered-staging objects and 3,381 compaction blocks, with no PUT matching a newly committed ordinary slice in this prefix. Recovered staging continued after the first five minutes, so discarding five startup minutes does not isolate ordinary live uploads. Recovered staging payload median was 844 bytes and 54.89% were below 1 KiB. Compaction payload median was 226,094 bytes; 178 blocks were below 1 KiB, despite raw sizes of 4 MiB for 176 and 3.5 MiB for two. Overall, 4,754 of 11,718 successful PUTs (40.57%) had payload below 1 KiB.

The new ordinary slice IDs had 15,495 successful DELETE entries but no matching PUT in the prefix. Pending staging can be removed when a slice becomes obsolete; upload code checks pending validity and skips unnecessary objects. Compaction and overwrite are possible explanations, but DELETE alone does not prove either cause or establish the number still awaiting cloud upload. Freeze-reason proportions must not be presented as the proportions of the observed cloud PUTs.

There were no ERROR/FATAL entries or explicit flush-deadline/EIO events in the prefix. Three compaction PUT attempts failed (two HTTP 500s, one response-header timeout), and each key subsequently uploaded successfully. Three slow Read operations returned OK after 12–21 seconds. Redis retry warnings reported eventual success. This supports the user's bounded observation of more stable operation; it does not establish guest filesystem integrity or long-term fault resolution. The next diagnostic step is explicit-flush caller attribution and staging lifecycle correlation, followed by a controlled writable-window aggregation experiment without weakening fsync or read-after-write.


## Filesystem-error recurrence and the outer FUSE watchdog (2026-10-02)

The user reported the same guest WRITE I/O error / journal abort / delayed-allocation failure / read-only symptoms in at least two of three running VMs, and subsequently stopped all three VMs. The earlier stability observation covered only 01:49–02:24; it does not describe the later incident. Small-PUT optimization is on hold while this failure is investigated.

The extended analysis prefix of `vm-io-20261002-014922.log` is 813,182,468 bytes / 5,125,719 lines, ending 07:31:10.786546 JST, SHA-256 `bbc9556b9adbd20e06c63d3e85419ae52cd4f48c0498f41c8afd23033ccfa5b4`. Mount configuration still showed writer timeout zero but **FuseOpts.Timeout=15 minutes**. No production mount, VM image, metadata, cache or staging was modified.

| Time (JST) | Observed event |
| --- | --- |
| 05:42:55.306840 | Write of inode 596153 slice 4110001 reaches 2,500 raw slices and waits in synchronous compaction. Backend doWrite succeeded in 39.745 ms. |
| 05:57:55.765007 | go-fuse watchdog interrupts request 1316250, Opcode 20 (fsync), NodeId 596153, after 15m0.765s, configured timeout 15m. |
| 05:57:56.768451 | go-fuse logs its forced EINTR reply to the kernel for request 1316250. |
| 05:57:58.182179 | VFS fsync reports EINTR after 903.184 seconds. |
| 06:12:58 / 06:28:01 | Two fallocate calls on inode 596153 also return EINTR after approximately 15 minutes each. |
| 06:19:39.456960 | Write of inode 596154 slice 4123567 reaches 2,500 raw slices and waits in synchronous compaction. Backend doWrite succeeded in 37.721 ms. |
| 06:28:41.058980 | Inode 596153's Write finally completes with errno zero, after 45m45.792s. |
| 06:34:40.088913 | go-fuse watchdog interrupts request 1510696, Opcode 20, NodeId 596154, after 15m0.670s. |
| 06:34:42.593855 | VFS fsync reports EINTR after 903.175 seconds. |
| 06:49:56.243852 | Read of inode 596154 returns zero bytes and EINTR after 903.185 seconds. |
| 07:03:16.177091 | Inode 596154's Write finally completes with errno zero, after 43m36.758s. |

`GenFuseOpt` previously hard-coded 15 minutes independently of `WriterFlushTimeout`; mount setup retained that value and `Serve` passed it to the JuiceData go-fuse fork. That fork starts `checkRequestTimeout` only when `Timeout > 0`. Its checker closes the request's cancel channel on elapsed-time or request-number-gap conditions and can send EINTR directly to the kernel before the operation returns. The VFS-only no-deadline change therefore did not eliminate this outer automatic failure mechanism. This is a confirmed host-side fsync failure path; matching the guest failure's exact timestamps and QEMU error handling remains separate from proving saved-data corruption.

The correction sets the generated FUSE server timeout to zero. It affects all mount FUSE requests, not only fsync. Explicit writer deadlines, request cancellation observed by operations, real storage/metadata errors, commit ordering, Read preflush errors and writeback semantics remain in place. Disabling the checker also removes its request-number-gap interrupt and forced reply fallback for already-interrupted requests. Actual kernel interrupt notifications still arrive through the normal go-fuse interrupt handler, but operations that ignore cancellation can now take longer to reply. Persistent backend stalls can keep requests pending indefinitely. macOS's separate `daemon_timeout=60` is unchanged; no all-platform unlimited-wait claim is made.

The long wait is still present: inode 596153's synchronous compaction spends 11m10.411s waiting for an earlier job, then 34m24.546s inside the metadata/backend-cleanup phase. Inode 596154 spends 12m40.422s waiting and 30m44.603s in that phase. These phases include obsolete-slice cleanup, not only Redis transactions. The observed slow Redis transactions were seconds rather than tens of minutes.

A read-only HTTP goroutine profile of the live mount at **07:43:48 JST**, after the log cutoff, provides separate direct evidence of deletion backpressure: ten deletion workers are inside object DELETE; one background compaction is parked in `deleteSlice`'s channel send; two other compactions and `doCleanupSlices` wait on the same `dSliceMu`. Thus positive MaxDeletes=10 does not prevent the bounded 102,400-item deletion queue from blocking compaction when it fills. This later profile supports the same mechanism but is not a stack dump from the earlier failed fsync timestamps. Queue capacity, locking and cleanup behavior were not changed by this correction.

In the fixed log prefix, all 42 failed PUT keys subsequently uploaded successfully. Cache checksum comparisons showed no mismatches in 2,858,824 logged checks. There were no metadata-commit error results; the long commits eventually returned success. These facts do not negate the actual failed fsyncs or the user's guest filesystem-error report. Writeback staging has a separate pre-existing crash-durability limitation: stage writes close/rename without file or directory fsync, so this correction does not establish host power-loss durability or make fsync wait for asynchronous cloud upload.

Regression validation includes a failing-before/fixed-after generated-option test, actual CLI-to-FUSE configuration propagation for default and explicit-hour writer deadlines, and isolated real-kernel FUSE fsync tests. The kernel fixture holds a pending operation and rejects early completion, releases it to success, returns ENOSPC unchanged, reproduces EINTR with a short enabled watchdog, and verifies that killing only its own helper process delivers a real kernel interrupt with the watchdog disabled. The accelerated watchdog test does not wait the real fifteen minutes. Existing VFS and metadata deletion-queue regressions cover their separate completion/error boundaries. Production VM recovery and deployment remain unperformed.

QEMU source review adds a reason not to assume EINTR is harmless. The local installed QEMU reports 10.2.1, but the stopped VMs' executable and AIO mode were not independently matched. In upstream [v10.2.1 qemu_fdatasync](https://github.com/qemu/qemu/blob/v10.2.1/util/osdep.c), the wrapper returns fdatasync/fsync's result directly. The [thread-pool flush backend](https://github.com/qemu/qemu/blob/v10.2.1/block/file-posix.c) returns the failure errno and, for buffered I/O, records page-cache inconsistency so later flushes continue failing. This supports a possible propagation path; it does not establish which backend/error policy those VMs used.


## Combined opt-in improvements (2026-10-02)

The combined local revision includes the FUSE watchdog correction plus independently selectable modes. Their defaults are `--compaction-gc-mode legacy`, `--compaction-scheduler legacy`, and `--writer-reuse-window 4`. The legacy compaction/deletion queue policy is retained. Shared chunk safety corrections additionally guard active staging work and propagate local retirement errors; watchdog and Redis copy/clone snapshot corrections remain in place. The initial local validation did not deploy these modes. The user subsequently started a three-VM trial; that trial exposed staging backlog and a guest reboot whose cause remains unconfirmed.

Deferred compaction GC hands only durably retired obsolete references to a separate 1,024-hint channel and one dispatcher. Submission neither locks the legacy deletion mutex nor waits for its queue. The dispatcher now invokes optional local-only retirement before remote enqueue, so remote DELETE backlog does not hold accepted local retirement hints behind its FIFO. Existing physical deletion workers remain. A full remote queue defers only remote intent rather than blocking subsequent local retirement. A full or stopped hint path leaves the durable marker for the existing recovery scan; the scan interval and metadata format do not change. Deferred mode requires `max-deletes > 0`, writable metadata, and session background recovery. Actual DELETE failures retain markers. This improves commit latency but does not guarantee prompt garbage collection or bounded obsolete storage during overload. Other deletion paths, including cleanup of unsuccessful compaction outputs, retain their existing behavior.

Reference retirement must be consistent with copying shared slices. Deterministic isolated-Redis tests reproduced a stale snapshot transaction in CopyFileRange, single Clone and BatchClone while another client compacted and deleted the old source slice. The combined revision adds WATCH of the actual source chunk keys before their reads and retains the normal transaction retry. Scope is proportional to the chunks being copied, not the volume's files/chunks. Metadata format and inode lock architecture do not change. Every writable client needs the new guards: an old client can still perform an unguarded copy. Existing SQL/MemKV transaction mechanisms are retained; PostgreSQL was source-reviewed only, and other KV engine runtime atomicity is not claimed.

Priority scheduling consumes already available raw counts/tier. A bounded dedup map and fixed 3 buckets coalesce hot notifications; accepted ready inodes are selected round-robin with weighted bucket service and one active job per inode. One client has 11 workers and 1,024 total active/pending hints. Storage callback execution is outside bookkeeping locks. Notifications during a callback cause another evaluation. Overflow can reject a new advisory key, so eventual background compaction of untouched cold chunks is not promised. The 2,500-slice synchronous guard and manual force path remain. Session shutdown stops hints and joins active callbacks before metadata connection shutdown. This does not split inode locks.

DEBUG flush traces now record caller `origin`, a process-local `barrier`, begin/freeze/end, final numeric errno and elapsed time. VFS and high-level FS Read, fsync, close, truncate, fallocate and copy paths are annotated. Origin wrapping and barrier counters, as well as slice diagnostic argument formatting, are gated when DEBUG is disabled. The reuse option changes only the unmatched-candidate freeze distance: it does not merge gaps or permit overwriting uploaded complete blocks. Local window-4 versus window-16 tests reduced metadata commits from 10 to 9 in one controlled revisit workload and verified all final bytes. This is not a measured cloud PUT reduction. Compare normal PUT, compaction PUT, recovered staging, bytes, memory and latency separately in future controlled tests.

The implementation spec and plan are maintained in Japanese in this investigation project under `docs/superpowers/specs/2026-10-02-vm-io-combined.md` and `docs/superpowers/plans/2026-10-02-vm-io-combined.md`. Production mount changes, VM startup and damaged guest data repair remain outside this local revision.


## Local staging retirement correction (2026-10-02)

The first deferred revision separated compaction completion from deletion enqueue, but pending cancellation and staging unlink still occurred only when a physical worker reached the slice. This retained a dependency on preceding slow remote DELETEs. The correction invokes local-only `RetireSlice` from the bounded compaction GC dispatcher after committed reference retirement, then tries the existing physical queue without waiting. If that queue is full, remote intent remains in the durable dead marker and later local hints continue. The physical worker count, metadata format and periodic recovery scan interval remain unchanged. This does not promise bounded remote garbage under overload.

The cached-store adapter removes obsolete pending entries and local cache/stage files without object requests. It reports local unlink failures for retry, including files surviving a previous index eviction. A custom `ChunkStore` lacking optional `Retire` keeps cleanup in its original `Remove` callback. Local callback success counts slice callbacks, not files or reclaimed bytes.

Writeback pending state is published before its staging acknowledgement. Actual staging uploads are tracked within the existing upload-slot bound. A local retirement during an active same-client staging PUT returns a retryable error after local cancellation, so the remote worker cannot acknowledge deletion and erase the durable marker before that PUT and abandoned cleanup finish. Immediate writeback and queued staging uploads use the same completion rules. A canceled failed upload does not recreate its pending entry. The read/fsync contract remains the existing writeback contract: completed local staging/metadata commit, asynchronous cloud upload. This correction does not add host power-loss durability.

Upload coordination is process-local. It does not establish a distributed upload lease between clients; the pre-existing other-client PUT/DELETE race remains outside this correction. Copy/clone snapshot guards are still required on every writable client. Hint overflow can still postpone local retirement until the existing periodic recovery; startup does not synchronously drain every old marker. No all-file/chunk scan or per-write metadata lookup is added.

The new counter `juicefs_compaction_gc_events_total{event=...}` reports fixed outcomes:

- `local_retire_success`: accepted local callbacks completed successfully, including idempotent cleanup.
- `local_error`: callback failed or requested retry while local staging/upload work was active; the marker remains. This is GC status, not a failed guest fsync.
- `remote_enqueued`: handed to existing physical workers, not necessarily deleted yet.
- `remote_deferred`: physical queue was full; durable intent remains for recovery.
- `hint_overflow`: the bounded hint channel could not accept a notification; durable intent remains.

With DEBUG enabled, monitor local retirement and remote deferral alongside existing commit/error traces:

```sh
tail -F "${JUICEFS_DIAG_LOG}" | \
  rg --line-buffered 'compaction GC|slow compaction|slow operation|writer flush .*errno=[1-9]|<ERROR>|<FATAL>'
```

`juicefs stats` object `del_c` continues to count physical DELETE request completions, including failures. Local retirement does not increment it, and upload success also removes staging independently. Consequently, after this correction, staging can fall without a matching `del_c` burst. This is intentional separation, not missing physical DELETE accounting.
