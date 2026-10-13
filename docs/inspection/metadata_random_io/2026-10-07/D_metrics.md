# D: metadata 計測 instrumentation 設計材料（read-only 調査）

- 対象: `/home/kwatanabe/tmp_local/juicefs_inspection/juicefs` HEAD `ea2c3757`（fork）、upstream 比較基準 `0b90c7db`（v1.4.1）
- 調査日: 2026-10-07。ソース・git 状態は未変更（`git status --short` 空を確認）。
- 行番号は HEAD `ea2c3757` 時点。

凡例: **[事実]** = ソース/コマンド出力で確認、**[推論]** = 事実からの解釈、**[未確認]** = 実行・実測していない。

---

## 0. 最終 metric 名の決まり方 [事実]

- mount: `cmd/mount.go:141-170 wrapRegister()` で `prometheus.WrapRegistererWithPrefix("juicefs_", WrapRegistererWith(commonLabels, registry))`。
  - commonLabels = `mp`, `vol_name`, `juicefs_version`, `instance`(hostname) + `--custom-labels "k:v;k2:v2"`（`cmd/flags.go:410`）。
  - Go/Process collector も同 registerer に登録（`cmd/mount.go:167-168`）→ `juicefs_go_*`, `juicefs_process_*`。
- 登録: `cmd/mount.go:471-477 initBackgroundTasks()` → `exposeMetrics()`、`m.InitMetrics(reg)`、`!NoBGJob` のとき `m.InitSharedMetrics(reg)`、`vfs.InitMetrics(reg)`。chunk 系は `chunk.NewCachedStore(..., registerer)` 内 `regMetrics`（`pkg/chunk/cached_store.go:999`）。
- `--metrics` 既定 `127.0.0.1:9567`（`cmd/flags.go:405-407`）。未指定で listen 失敗時は空きポート自動（`cmd/mount.go:118-124`）。`/metrics` は OpenMetrics 有効（exemplar 可、`cmd/mount.go:95-101`）。
- **backend 種別（redis/sql/tkv）ラベルは無い**。`vol_name` のみ。必要なら既存 `--custom-labels "meta_engine:redis"` で付与可能（コード変更不要）[事実: flag 存在 / 推論: 用途]。
- **注意 [事実]**: `bgjobDuration` / `bgjobDels` の Name が `"juicefs_bgjob_duration_seconds"` / `"juicefs_bgjob_deletions_total"`（`pkg/meta/base.go:522,530`）で、prefix と合わせ最終名は **`juicefs_juicefs_bgjob_*`**（upstream 由来の二重 prefix）。

---

## 1. pkg/meta の既存 metrics 全列挙

### 1.1 `InitMetrics`（クライアント毎, `pkg/meta/base.go:610-619`）

| 最終名 | 型 | label | bucket | Observe 箇所 |
|---|---|---|---|---|
| `juicefs_transaction_durations_histogram_seconds` | Histogram | なし | `ExponentialBuckets(0.0001,1.5,30)` = 100µs〜約12.8s（`base.go:405-409`） | redis `redis.go:1157-1158`、tkv `tkv.go:1102-1103`、sql `sql.go:1235,1271,1332`（各 txn/roTxn の defer） |
| `juicefs_transaction_restart` | Counter | `method` | - | redis `redis.go:1199`、tkv `tkv.go:1124`、sql `sql.go:1255,1316,1352`（リトライ時のみ） |
| `juicefs_meta_ops_durations_histogram_seconds` | Histogram | **なし**（op 別でない） | 同上 100µs〜12.8s（`base.go:414-418`） | `timeit()` `base.go:621-626` |
| `juicefs_meta_ops_total` | Counter | `method` | - | `timeit()` |
| `juicefs_meta_ops_duration_seconds` | Counter（合計秒） | `method` | - | `timeit()` |

`timeit()`（`base.go:621-626`）: `opDist.Observe(used)`; `opCount.WithLabelValues(method).Inc()`; `opDuration.WithLabelValues(method).Add(used)`。
→ **op 別は「件数」と「合計時間」（平均のみ）。op 別 p95/p99 は取れない**。histogram は全 op 混在。error 数・errno は記録しない。

### 1.2 `InitSharedMetrics`（`!NoBGJob` 時のみ, `base.go:548-608`）

`juicefs_used_space` / `used_inodes` / `total_space` / `total_inodes`（Gauge, 10s 毎 StatFS）、`juicefs_subdir_info{subdir}`、quota 系 12 本（`dir|user|group_quota_{max,used}_{space_bytes,inodes}` label `inode|uid|gid`）、`juicefs_juicefs_bgjob_duration_seconds{job,status}`（`ExponentialBuckets(1,2,13)` 1s〜4096s）、`juicefs_juicefs_bgjob_deletions_total{job}`、**fork 追加** `juicefs_compaction_gc_events_total{event}`（`base.go:536-539`、event = `local_error|local_retire_success|remote_enqueued|remote_deferred|hint_overflow`, `compaction_gc.go:92,98,109,111,131`）。

### 1.3 Meta API 別の計測有無 [事実]

| API | timeit | 位置 / 備考 |
|---|---|---|
| Lookup | あり | `base.go:1264`。ただし `..`/`.` で内部 GetAttr も別途計測（二重計上） |
| GetAttr | あり（cache miss のみ） | `base.go:1450-1453`: open-file cache hit (`m.of.Check`) は計測前に return |
| Read | あり（cache miss のみ） | `base.go:2108-2119`: `f.RLock()` → `of.ReadChunk` hit なら return → その後 `timeit`。**RLock 待ちは計測外** |
| Write | あり | `base.go:2184`（`start` は f.Lock 前 → **lock 待ちを含む**） |
| Truncate | あり | `base.go:2230`（f.Lock 待ち含む, `2233`） |
| Fallocate | あり | `base.go:2265`（f.Lock `2268` 待ち含む） |
| CopyFileRange | あり | redis `redis.go:3189`/sql `3405`/tkv `2697`（f.Lock 待ち含む） |
| **NewSlice** | **なし** | `base.go:2150-2163`。`freeMu` 下で 4096 個（`sliceIdBatch = 4<<10`, `base.go:51`）毎に `en.incrCounter("nextChunk")`。redis は `rdb.IncrBy` 直（ChangeLog 無効時, `redis.go:484-494`）で txn を通らない → txDist にも出ない |
| **Open** | **なし** | `base.go:2043-2092`（OpenCheck cache 経路あり、内部 GetAttr は計測される） |
| **Close** | **なし** | `base.go:2166-2178`（通常 RTT 無し、removed 時のみ doDeleteSustainedInode） |
| Mknod/Create/Unlink/Rename/Readdir/SetAttr/StatFS 等 | あり | `base.go:1135-3862` に 26 箇所、redis/sql/tkv に Resolve/GetXattr/ListXattr |

### 1.4 トランザクション・lock 待ち [事実]

- client 内 pessimistic lock: `txlocks [1024]sync.Mutex`（`base.go:279`, `nlocks=1024` `base.go:52`）。
  - redis: `txn()` が `fnv32(keys[0]) % 1024` で `m.txLock(h)`（`redis.go:1160-1162`）。
  - tkv/sql: `txBatchLock(inodes...)`（`base.go:673-702`、`tkv.go:1104`, `sql.go:1242`）。
- **txDist は lock 待ちを含む**（redis: `start` が `txLock` 前, `redis.go:1157-1162`; tkv/sql も `defer txBatchLock` が start 後）。
- **lock 待ち単体・attempt 数・method 別 tx 時間の metric は無い**。fork の redis `txn` は `lockWait`/`activeStart`/`attempts` を変数として計算しているが（`redis.go:1160-1189`）、出力は DEBUG ログと 1s 以上の WARN ログのみ（`redis.go:1167-1172`）。
- `txRestart` の method は `txMethod.name(ctx)` → ctx の `txMethodKey` か `callerName()`（`runtime.Callers` スタック走査, `utils.go:624-640`）で遅延評価。**リトライ時のみ評価**なので通常パスにコスト無し。
- open-file lock（`openFile` の `sync.RWMutex`, `openfile.go:19-20`）待ちの metric 無し。Write の待ちは fork の WARN ログ `lock_wait=` のみ。
- Redis 個別コマンド RTT の metric は無い。go-redis hook は client-side cache 用に `redis_csc.go:96 c.cli.AddHook(c)` / `ProcessHook` (`:260`) の実例があり、同方式で RTT 計測 hook を差し込める [事実: 機構あり / 推論: 流用可能]。

---

## 2. fork が追加した計測（`git diff 0b90c7db HEAD`）

| 箇所 | 内容 | 形態 |
|---|---|---|
| `pkg/meta/base.go:2182-2227` `baseMeta.Write` | `lock_wait`（f.Lock）、`doWrite`（backend）、`stat`（updateParentStat/UserGroupStat）、`compact`（maxSlices 超の同期 compaction）、`slices` を分解。phase 毎 DEBUG、total ≥1s で WARN `slow metadata write ...`（`:2189`） | **ログのみ** |
| `pkg/meta/redis.go:1143-1210` `redisMeta.txn` | `lock_wait`（txLock）/`active`/`attempts`/`err`。DEBUG `phase=lock_wait|active`、total ≥1s WARN `slow metadata transaction` | **ログのみ**（txDist は従来通り合算） |
| `pkg/meta/base.go:2843-2960` `compactChunk` | `queue_wait`（compacting map 待ち）、`object`（newMsg CompactChunk）、`metadata`（doCompactChunk）、slices/bytes。phase DEBUG、≥1s WARN `slow compaction`（`:2875`） | **ログのみ** |
| `pkg/meta/compaction_gc.go` | `compaction_gc_events_total{event}` | **metrics** |
| `pkg/meta/compaction_scheduler*.go` | priority scheduler（11 workers, cap 1024, `compaction_scheduler_lifecycle.go:20-27`）。capacity 超で `schedule()` が false を返し hint 破棄（`compaction_scheduler.go:99-101`） | **metric 無し**（drop も無計測） |
| `pkg/vfs/writer_trace.go` | flush barrier の origin（`vfs.Read/Fsync/Flush/Release/Truncate/...`）・id・elapsed、slice freeze reason（`full_slice|writable_window|commit_age|explicit_flush`）・age/idle、slice finish。**DEBUG 有効時のみ**（`writer_trace.go:40-45,64-70`） | **ログのみ** |
| `pkg/vfs/writer.go:225-230` commitThread | `m.Write` 所要 ≥1s で WARN `slow slice commit`、DEBUG phase=metadata | **ログのみ** |
| `pkg/vfs/writer.go:409-475` flush | 無期限待ち時 5 分毎 WARN `still waiting`（pending_chunks/slices） | **ログのみ** |

**分解可能範囲 [推論]**: Write 1 回の `lock_wait / doWrite / stat / compact`、redis txn の `lock_wait / active / attempts`、compaction の `queue_wait / object / metadata` はログから再構成可能。ただし WARN は 1s 以上のみ、DEBUG は全件出るため本番量では重い。**分布（p95/p99）や件数の時系列は metrics では取れない**。sql/tkv の txn には fork の分解ログが無い（`sql.go`/`tkv.go` の差分はコメント 1 行程度）。

---

## 3. pkg/vfs, pkg/chunk の metrics [事実]

### vfs（`pkg/vfs/vfs.go:1403-1414 InitMetrics`, `initVFSMetrics` `:1354`）
- `juicefs_fuse_ops_durations_histogram_seconds`（label なし, `ExponentialBuckets(1e-5,1.8,29)`）、`juicefs_fuse_ops_total{method}`、`juicefs_fuse_ops_durations_seconds{method}`、`juicefs_fuse_ops_io_errors{errno}` — すべて `logit()`（`accesslog.go:66-73`）。meta と同じく op 別 histogram 無し。
- `juicefs_fuse_read_size_bytes` / `juicefs_fuse_written_size_bytes`（`LinearBuckets(4096,4096,32)`）。
- `juicefs_compact_size_histogram_bytes`（`ExponentialBuckets(1024,2,16)`, `compact.go:30-34`, Observe `:62`）→ **`_count` が「object 側 compaction 実行回数」の代理**（vfs.Compact 成功経路の 1 回=1 観測）[推論: 失敗/wasted は別]。
- Gauge: `juicefs_fuse_open_handlers`、`juicefs_used_buffer_size_bytes`、`juicefs_store_cache_size_bytes`、`juicefs_used_read_buffer_size_bytes`。
- writer の commit/flush latency、freeze 件数、slice 長の metric は **無い**。

### chunk（`cached_store.go:954-1040`, `metrics.go:40-95`）
- `juicefs_object_request_durations_histogram_seconds{method,storage_class}`（`ExponentialBuckets(0.01,1.5,25)` 10ms〜約170s。PUT `:323`, GET `:766,829`, DELETE `:344`）、`juicefs_object_request_data_bytes{method,storage_class}`、`juicefs_object_request_errors`、`juicefs_object_request_uploading`（Gauge）。
- blockcache: `hits/miss/hit_bytes/miss_bytes/read_hist_seconds/blocks/bytes/drops/writes/evicts/write_bytes/write_hist_seconds`。
- staging: `juicefs_staging_blocks`, `staging_block_bytes`, `staging_write_bytes`, `staging_writing_blocks`, `staging_block_delay_seconds`（Counter 合計, `:1102`）, `staging_block_errors`。
- **slice 数 / chunk 毎 slice 分布 / compaction 回数（meta 側）の metric は存在しない**。

---

## 4. 取得経路 [事実]

| 経路 | 内容 | 制約 |
|---|---|---|
| `/metrics`（`--metrics`, 既定 127.0.0.1:9567） | 全 registry、bucket 付き | p95/p99 は bucket から算出可 |
| `<mp>/.stats`（`vfs/internal.go:82`, 生成 `CollectMetrics` `:152-186`） | テキスト。label `method`/`errno` のみ名前に連結（`name_<value>`）。**Histogram は `_total`(count) と `_sum` のみ、bucket 無し** | `method`/`errno` 以外の label（`storage_class`, `event`, `job`, `phase` 等）は同名行が重複出力 → 新 metric で区別したい次元は `method` label にするのが `.stats` 互換上有利 [推論] |
| `juicefs stats` (`cmd/stats.go:148-187`) | `.stats` を周期読みし差分表示。meta 欄 = `meta_ops_durations_histogram_seconds`（ops/平均）、`-l 1` で `transaction_durations...` と `transaction_restart` | 平均のみ。op 別不可 |
| `juicefs profile` (`cmd/profile.go`) | `.accesslog`（FUSE op 単位）を集計 | meta API 単位ではない |
| pprof | `cmd/main.go:22` で `net/http/pprof` import、`--no-agent` 無しなら `127.0.0.1:6060〜6099` で listen（`main.go:331-339`）。`juicefs debug` が `/debug/pprof/` を収集（`cmd/debug.go:252,364`） | **`runtime.SetMutexProfileFraction` / `SetBlockProfileRate` はリポジトリ内に一切無い**（grep 0 件）→ mutex/block profile は既定で空 |
| pyroscope (`--pyroscope`, `main.go:341-366`) | ProfileMutex*/Block* を要求するが pyroscope-go v1.2.1 本体は fraction を設定しない（設定は example のみ, GOMODCACHE で確認） | 同上で mutex/block は実質空 [推論: ライブラリ挙動は grep ベース] |

→ mutex/block profile を使うには起動時に fraction 設定を足す必要あり（例: 環境変数 `JFS_MUTEX_PROFILE_FRACTION` を `cmd/main.go setup()` で読む）[推論・提案]。なお `txlocks`/`openFile` の lock 待ちは sync.Mutex なので mutex profile に出るが、呼び出しスタック単位でありレイテンシ分布ではない。

---

## 5. 新規 metric 設計案

方針: 既存名・型は**変更しない**（`juicefs stats` やダッシュボード互換）。追加は `baseMeta` フィールド + `newBaseMeta`（`base.go:388-543`）で生成、`InitMetrics`（`base.go:610`）で登録（クライアント毎の値なので Shared でなく InitMetrics 側）。vfs は package 変数 + `vfs.InitMetrics`（`vfs.go:1403`）。

| # | 提案名（最終名） | 型 / label | 追加場所 | 目的 |
|---|---|---|---|---|
| 1 | `juicefs_meta_op_durations_seconds` | HistogramVec `{method}`、bucket 既存と同じ `ExponentialBuckets(0.0001,1.5,30)` | `timeit()` `base.go:621` で追加 Observe | op 別 p95/p99（既存 opDist は残す） |
| 2 | `juicefs_meta_op_errors_total` | CounterVec `{method,errno}`（`utils.ErrnoName`） | `timeit` に errno を渡す `timeitErr(method, start, *syscall.Errno)` を追加し、named return を持つ API（Write/Read/Open 等）から段階適用 | エラー率 |
| 3 | NewSlice/Open/Close の計測 | 既存 `timeit("NewSlice")` 等（1 と同系列） | `base.go:2150`（**incrCounter 呼出時のみ**計測が実用的: 4096 回に 1 回の RTT）、`base.go:2043`（Open, cache hit 時は `method="Open"` でも可。区別したいなら `OpenCached`）、`base.go:2166` | 欠けている API の補完 |
| 4 | `juicefs_meta_openfile_lock_wait_seconds` | HistogramVec `{method}`（Write/Truncate/Fallocate/CopyFileRange/Read） | `base.go:2197`（Write, fork の `lockWait` をそのまま Observe）、`:2110`（Read RLock）、`:2233`,`:2268`、`redis.go:3192`/`sql.go:3408`/`tkv.go:2701` | client 側 inode lock 待ち |
| 5 | `juicefs_transaction_lock_wait_seconds` | Histogram（label なし、or `{engine}` 不要） | redis `redis.go:1162` 直後（`lockWait` 既存）、tkv/sql は `txBatchLock` を計測する wrapper（`base.go:673`）で `tkv.go:1104`/`sql.go:1242` | txLock（1024 stripes）待ち |
| 6 | `juicefs_transaction_attempts` | Histogram bucket `{1,2,3,5,10,20,50}` | redis `redis.go:1206` 付近 return 前、tkv/sql 同等位置 | 再試行分布（txRestart は合計のみ） |
| 7 | `juicefs_meta_redis_cmd_duration_seconds` | HistogramVec `{cmd}`（`cmd.Name()` 小文字、pipeline は `"pipeline"`/`"multi"`）| go-redis `Hook` を `newRedisMeta` で `rdb.AddHook`（先例 `redis_csc.go:96,260`） | 純粋な Redis RTT。RTT 注入実験の検証軸 |
| 8 | `juicefs_meta_chunk_slices` | HistogramVec `{method="Write|Read"}`、bucket `{1,2,5,10,20,50,100,200,350,500,1000,2500}` | Write: `base.go:2207` 以降 `numSlices`、Read: `base.go:2131` 付近 `len(ss)` | chunk 断片化分布 |
| 9 | `juicefs_meta_compaction_total` | CounterVec `{trigger,result}`（trigger=`write_sync|write_bg|read_bg|manual`, result=`ok|skipped|wasted|error`）| `compactChunk` `base.go:2843-2960`（引数 once/force と return 経路で分類） | meta 側 compaction 件数 |
| 10 | `juicefs_meta_compaction_phase_seconds` | HistogramVec `{phase=queue_wait|object|metadata}` bucket `ExponentialBuckets(0.001,2,16)` | 同上、fork 既存変数 `queueWait/objectTime/metadataTime` | WARN ログの metrics 化 |
| 11 | `juicefs_meta_compaction_hints` | Counter `{result=scheduled|coalesced|dropped}` + GaugeFunc pending | `compaction_scheduler.go:91-133 schedule()` | scheduler の drop 可視化 |
| 12 | `juicefs_writer_commit_duration_seconds` / `juicefs_writer_flush_duration_seconds{origin}` / `juicefs_writer_slice_freeze_total{reason}` | Histogram / HistogramVec / CounterVec | `writer.go:225-230`、`writer.go:409`（origin は現状 DEBUG 時のみ付与 → 常時付与に変更要, `writer_trace.go:40-45`）、`freezeWithTrace`（`writer.go` freeze 定義） | vfs 側 commit/flush 待ち |

### overhead 評価 [推論]
- `time.Now()` は Linux vDSO で数十 ns。既存 `timeit` が既に呼んでおり、追加は lock 前後 1〜2 回。Redis RTT（loopback でも数十 µs、実 VM で数百 µs〜ms）に対し 0.1% 未満。
- `WithLabelValues` は毎回 label hash + map lookup（~50-100ns）。ホットパスでは `newBaseMeta` 時点で method 毎の `Observer` を事前取得（`CurryWith`/固定変数）するか、固定 method 文字列で十分。
- Histogram Observe は atomic 加算。多コア高頻度では同一 histogram の cache line 競合がありうるが、メタ RTT 支配下では無視可能。
- **カーディナリティ**: method ~45 種 × 30 bucket ≈ 1,400 series（#1）。`{method,errno}` は実際に発生した組のみ生成。**inode・key・slice id・origin の自由文字列は label にしない**（origin は固定 8 種程度なので可）。redis `cmd` は使用コマンド数十種。
- `callerName()`（runtime.Callers）を毎 txn で呼ぶと高コスト → method 別 txDist は作らない、または ctx の `txMethodKey` が明示されている場合のみ。
- `.stats` 互換: `method`/`errno` 以外の label は `.stats` で重複行になる（§4）。`juicefs stats` で見たいものは `method` label に寄せる。

---

## 6. 環境・benchmark 手段

### 手元ツール [事実: `which`/`--version`]
| ツール | 状態 |
|---|---|
| fio | **無し** |
| tc | `/usr/sbin/tc`, iproute2-6.19.0 |
| ip | `/usr/sbin/ip` |
| redis-server / valkey-server | **無し** |
| redis-cli | 8.0.5（127.0.0.1:6379 は接続拒否 = 未起動） |
| go | go1.25.11 linux/amd64 |
| docker | 29.8.1（daemon 応答あり、redis/valkey/toxiproxy image は local に無し） |
| kernel | 6.18.40.1-microsoft-standard-WSL2、`CONFIG_NET_SCH_NETEM=m`, `CONFIG_IFB=m`、`sch_netem.ko` 存在（未ロード） |
| 権限 | uid 1000、`sudo -n` 不可（パスワード要） |

### RTT 注入手段 [推論 / 未確認]
1. **host の `tc qdisc ... dev lo netem delay`**: sudo が対話要求なのでユーザー操作が必要。lo 全体に効く副作用あり。
2. **docker で redis を別 netns に立て、container 内で netem**（`--cap-add NET_ADMIN`、container 内 `tc qdisc add dev eth0 root netem delay Xms`）。host sudo 不要。ただし image pull（ダウンロード）にはユーザー承認が必要、WSL2 カーネルで container から `sch_netem` が autoload されるかは **未確認**。
3. **toxiproxy container**（L4 proxy で latency 注入）: カーネル依存なし。pull 要承認。
4. **Go 内注入**: go-redis Hook（§5 #7 と同じ位置）でテスト時のみ `time.Sleep` を挟む。プロセス外の netem と違い TCP 挙動は再現しないが、権限・ダウンロード不要で決定的。

### repo 内 benchmark [事実]
- `pkg/meta/benchmarks_test.go`（648 行）: `BenchmarkRedis`（`redis://127.0.0.1/1` 固定, `:31,633`）、`BenchmarkSQL`（sqlite3 tempdir, `:638`）、`BenchmarkTKV`（badger tempdir, `:644`）。`benchmarkData` に `newchunk`（NewSlice, `:509`）、`write`（`:519`, 1KiB step で同一 chunk に追記）、`read_1/read_10`。`BenchmarkReadSlices`/`ReadSliceBuf`（`:69,93`, 純 CPU）。Redis 版は redis-server 起動が前提（現状不可）。SQL/TKV 版はローカル完結で今すぐ実行可能 [未確認: 実行していない]。
- `pkg/meta/compaction_scheduler_bench_test.go:26 BenchmarkPriorityHintUpdate`。
- `cmd/mdtest.go`（hidden `juicefs mdtest META-URL PATH`, `--threads/--dirs/--depth/--files/--write/--access-log`）: mount 不要で meta+vfs+chunk を直結し、`exposeMetrics` + `m.InitMetrics` + `vfs.InitMetrics` を登録（`:233-245`）→ **新 metric をそのまま `/metrics` で観測できる最小ハーネス**。ただし `InitSharedMetrics` は呼ばない（compaction_gc_events 等は出ない）。
- `cmd/bench.go`（`juicefs bench PATH`, big/small file, threads）: mount 済み PATH 必要。
- `cmd/objbench.go`: object storage 単体。meta RTT とは無関係。

---

## 7. 未確認事項
- 上記 benchmark・mdtest は一切実行していない。
- WSL2 で netem が container/host でロード・動作するか。
- `vfs.Compact` 失敗時に `compact_size_histogram_bytes` が観測されないこと（`compact.go:62` の位置のみ確認、経路全体は未精査）。
- sql/tkv 経路の `txn` 内で fork の分解ログが無いことは差分の行数で判断（詳細未読）。
- pyroscope-go の mutex/block fraction 非設定は grep（example 以外ヒット無し）による。
