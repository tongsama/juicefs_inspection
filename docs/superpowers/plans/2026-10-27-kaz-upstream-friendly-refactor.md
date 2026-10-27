# kaz を本家の新版へ追従しやすくするリファクタリング 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 挙動を一切変えずに、本家（juicedata/juicefs）由来のファイルにある kaz の差分を減らし、次の本家の版（1.4.x と 1.5 系）への merge で起きる衝突を減らす。

**Architecture:** kaz が足した定義を、同じパッケージの `*_kaz.go` へ移す。upstream のファイルには、1 行の hook 呼び出しだけを残す。設定と状態のフィールドは、埋め込み struct にまとめる。診断のログは、文言を変えずに kaz の helper へ出す。

**Tech Stack:** Go 1.25.11、urfave/cli v2（juicedata fork）、encoding/json、go-redis、xorm、TKV。

**Spec:** [調査報告 5 章・7 章](../../inspection/upstream_v1.4.2/2026-10-26/port-investigation-ja.md)、[追従の手順](../../development/upstream-sync/README-ja.md)。棚卸しの要点（file:line）は、各 Task に書いた。

## Global Constraints

- **挙動は変えない。** fsync、書き込みの順序、read-after-write、エラーの伝播、ロックの順序は、移動の前と同じに保つ。
- **ログはそのまま保つ。** 文言、キー名、キーの順序、phase などの値、`payload_bytes=` の位置（`logRequest` の param の先頭）は変えない。調査のスクリプト約 40 本がこれらをパースしている。変わってよいのは、末尾の `[関数@file:line]` だけ。
- **`.config` の JSON のキーの集合と値は変えない。** 順序は変わってよい。
- **フラグの名前、別名、既定値、検証は変えない。** `--help` での並び順だけは変わってよい。
- **作業の場所:** worktree `/home/kwatanabe/tmp_local/juicefs-1.4.2-wt`。`juicefs/` の checkout（本番のビルド元）は触らない。
- **ブランチ:** Task ごとに `1.4.2-improve-kaz` から切り、commit して `--no-ff` で merge する。`1.4.2-improve-kaz` には直接 commit しない。push しない。
- **commit message:** Co-Authored-By などの署名行を付けない。
- **検証の環境:**
  - `GOENV_VERSION=1.25.11`
  - `TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp`（root filesystem 上。使い終わったら消す）
  - Redis が要るテストは、`docker run -d --rm --name jfs142-test-redis -p 127.0.0.1:6379:6379 redis:8.0.5` で立て、終わったら止める。本番の Redis（56379）には触れない。
- **探索の範囲:** `find /` や `$HOME` 全体の走査は禁止（本番の FUSE mount がある）。探すのは worktree の中だけ。
- **コメントの規則:** 調査リポジトリの AGENTS.md に従う。upstream 由来の関数・型の直前には doc コメントを足さず、関数の中で `// kaz:` と書く。kaz が新しく足す関数・型には doc コメントを付ける。
- **計測:** 各 Task の最後に、upstream のファイルの差分量と、本家 main との衝突ブロック数を記録する（Task 0 のスクリプト）。
- **性能:** 毎回通る経路にヒープの確保と、ロック・I/O を増やさない。毎回通る経路は、redis の `txn`、`baseMeta.Write`、`commitSlices`、`ReadAt`、`sliceWriter.write`、staging の書き込み。
  - 計画の前に測った値（2026-10-27、`docs/inspection/upstream_v1.4.2/2026-10-26/port-investigation-ja.md` の 8 章）: 型アサーションは 1 回 2 ns、値型の helper は今の形と同じ（約 120 ns、確保 0）、ポインタで返す helper は確保が 1 回（64 B）増える。
  - helper は値型（スタックに置く）にし、ポインタを返す構築関数は作らない。
  - 変えた関数について、`go build -gcflags=-m ./pkg/<パッケージ>/ 2>&1 | grep <関数名>` で、新しく `escapes to heap` / `moved to heap` が出ていないことを確かめる。
- **テストが作るファイル:** vfs のテストは `pkg/vfs/?_journal=WAL&_timeout=5000&cache=shared` を作る。commit の前に消す。

## Review Focus

1. **埋め込みでフィールド名が隠れる場合:** upstream が同じ名前のフィールド（例 `StagingNoSync`、`WriterFlushTimeout`）を外側の struct に足すと、Go の selector でも JSON でも外側が黙って勝ち、コンパイルエラーにならない。→ Task 6 の `TestKazEmbeddedFieldsNotShadowed` で検出する。
2. **旧名のフラグを使った mount:** 本番のスクリプトは旧名（`--writer-flush-timeout` など）を使っている。フラグの定義を kaz のファイルへ移した後も、旧名の別名と警告が残っていること。→ Task 4 の `TestKazFlagSetUnchanged` で確かめる。
3. **`MetaWriteBatch>1` で、`WriteSlices` を持たない Meta を使う場合:** 任意 interface にした後は、型アサーションが失敗すると 1 個ずつ Write する。テスト用の wrapper もこの経路を通る。→ Task 3 の `TestWriterFallsBackWithoutSliceBatchWriter` で確かめる。
4. **DEBUG ログの書式:** helper へ移した後も、解析スクリプトの正規表現に一致すること。→ Task 7 の `TestKazTraceLogFormats` で確かめる。
5. **`.config` を古い版の client が読み戻す場合（umount・upgrade のとき）:** キーの集合が同じであること。→ Task 0 の `TestKazConfigJSONKeys` で、Task 6 の前後の変化を検出する。

---

### Task 0: 安全網と計測

**ブランチ:** `test/kaz-refactor-guards`

**Files:**
- Create: `cmd/kaz_refactor_guard_test.go`
- Create: `cmd/testdata/kaz_config_keys.golden`、`cmd/testdata/kaz_mount_flags.golden`
- Create（調査リポジトリ）: `docs/development/upstream-sync/upstream_diff_size.sh`

**Interfaces:**
- Produces: golden ファイル 2 つ（後の Task はこれと一致させる）、`upstream_diff_size.sh <repo> <base> <head>`（upstream のファイルの `files +add -del` を出す）。

- [ ] **Step 1:** `TestKazConfigJSONKeys` を書く。`getVfsConf` で作った `vfs.Config`（`Meta` と `Chunk` を含む。kaz の値はすべて既定以外にする）を `json.Marshal` し、ネストしたキーのパス（`Meta.KazCompactionMode`、`Chunk.StagingNoSync` など）を辞書順に並べる。それが golden と一致することを確かめる。値も golden に含める。`-update` フラグで golden を書き直せるようにする。
- [ ] **Step 2:** `TestKazFlagSetUnchanged` を書く。`cmdMount().Flags` の各フラグについて、「全ての名前（別名を含む）、既定値の文字列」の集合を辞書順で golden と比べる。
- [ ] **Step 3:** golden を生成して PASS を確かめる。
  - `go test ./cmd -run 'KazConfigJSONKeys|KazFlagSetUnchanged' -update`
  - その後 `-update` なしで PASS。
- [ ] **Step 4:** `upstream_diff_size.sh` を書く。`git diff --numstat <base> <head>` から、`*_kaz*`、`kaz_*`、`*_test.go`、kaz が新しく作ったファイル（`git diff --diff-filter=A --name-only` で base にないもの）を除いて合計する。現在値（29 files +2,658 −239 前後）を報告書に記録する。
- [ ] **Step 5:** commit して merge する。
  - message: `test: pin the .config keys and the mount flag set before the kaz refactoring`

### Task 1: pkg/chunk の kaz の定義を `*_kaz.go` へ移す

**ブランチ:** `refactor/kaz-move-chunk`

**Files:**
- Modify: `pkg/chunk/cached_store.go`、`pkg/chunk/disk_cache.go`、`pkg/chunk/utils_unix.go`、`utils_linux.go`、`utils_darwin.go`、`utils_windows.go`
- Modify: `pkg/chunk/cached_store_kaz.go`、`pkg/chunk/disk_cache_kaz.go`
- Create: `pkg/chunk/cached_store_staging_kaz.go`、`pkg/chunk/utils_kaz_unix.go`、`utils_kaz_linux.go`、`utils_kaz_darwin.go`、`utils_kaz_windows.go`（それぞれ元のファイルと同じ `//go:build`）

**Interfaces:**
- Produces（新しい hook）:
  - `func (store *cachedStore) kazInitMetrics()`
  - `func (store *cachedStore) kazRegMetrics(reg prometheus.Registerer)`
  - `func (store *cachedStore) kazPutParam(payload int, used time.Duration) string`（今の param の生成をそのまま移す）

移すもの（そのまま移す。中身は変えない）:

| 移動元 | 定義 | 移動先 |
|---|---|---|
| `cached_store.go:779-827` | header-timeout の補助（`headerTimeoutMemory` から `errGetHeaderTimeout`） | `cached_store_kaz.go` |
| `cached_store.go:895-1030` | `readBlockDetached`、`loadDetached`、`cancelOnClose`、`getWithHeaderTimeout` | `cached_store_kaz.go` |
| `cached_store.go:1393-1480` | staging 管理の 7 関数 | `cached_store_staging_kaz.go` |
| `cached_store.go:1569-1600` | `ErrSliceUploadInFlight`、`retireCache`、`Retire` | `cached_store_staging_kaz.go` |
| `cached_store.go:1266-1277, 1292-1294` | kaz の metrics の生成と登録 | `kazInitMetrics`、`kazRegMetrics` の hook にする |
| `cached_store.go:319-327` | PUT の `payload_bytes` の param と logrus の import | `kazPutParam`。`logRequest` の呼び出しはその場に残す |
| `disk_cache.go:609, 638, 713, 1397, 1667` | `syncDirs`、`forgetDurableDir`、`removeLocal`、`(*cacheManager).retire`、`syncStagingFile`／`syncStagingDir` | `disk_cache_kaz.go` |
| `utils_*.go` | `fsyncDir`、`fdatasyncFile` | `utils_kaz_*.go` |

- [ ] **Step 1:** 移す前の状態で、`go test ./pkg/chunk/` が PASS することを確かめる（基準）。
- [ ] **Step 2:** 上の表のとおり移す。移した定義の中身は、`git diff --color-moved=zebra` で「移動だけ」に見えることを確かめる。
- [ ] **Step 3:** 確かめる。
  - `go build ./... && GOOS=darwin go vet ./pkg/chunk/`（darwin は vet だけ。cgo が要るときは build tag の確認だけでよい）
  - `go test ./pkg/chunk/`：PASS
  - Task 0 のテスト：PASS
- [ ] **Step 4:** upstream のファイルの差分量と、本家 main との衝突ブロック数を記録する。
- [ ] **Step 5:** commit して merge する。
  - message: `refactor(chunk): move kaz definitions out of upstream files into *_kaz.go`

### Task 2: pkg/vfs の kaz の定義を移す

**ブランチ:** `refactor/kaz-move-vfs`

**Files:**
- Modify: `pkg/vfs/writer.go`、`pkg/vfs/vfs.go`
- Create: `pkg/vfs/writer_kaz.go`、`pkg/vfs/vfs_kaz.go`

**Interfaces:**
- Produces:
  - `func kazNormalizeWriterConf(conf *Config)`（`NewDataWriter` の冒頭 `writer.go:823-849` の正規化をそのまま移す）
  - `func (v *VFS) kazFallocateLength(ino Ino, mode uint8, off, size int64) …`。`vfs.go:925-937` の `UpdateMeta`／`GrowTo` の分岐を出す。戻り値は、今のコードが後で使う値に合わせる。

移すもの:

| 移動元 | 定義 | 移動先 |
|---|---|---|
| `writer.go:122, 127` | `freeze`、`freezeWithTrace` | `writer_kaz.go` |
| `writer.go:305-370` | `commitBatch`、`commitSlices`、`metaErrorUnapplied`、`markCommitted` | `writer_kaz.go` |
| `writer.go:523-806` | `raiseMtimeFloor`、`flushWait`、`waitFlushed`、`rangeFlushSet`、`flushRange`、`(*fileWriter).GrowTo` | `writer_kaz.go` |
| `writer.go:807-990` | `flushTimeout`、`(*dataWriter).FlushRange`、`GrowTo`、`UpdateMeta` | `writer_kaz.go` |
| `writer.go:32-41` | 定数 | upstream の `flushDuration` を元の名前に戻す。kaz 側で `defaultSliceFlushWait = flushDuration` と書く |
| `vfs.go:954, 964` | `flushForRange`、`prelockFlush` | `vfs_kaz.go` |

`commitThread`、`flush` の本体の書き換えは、この Task では動かさない（スコープ外の節を参照）。

- [ ] **Step 1:** 基準を取る。`go test ./pkg/vfs/`（一時 Redis を使う）が PASS すること。
- [ ] **Step 2:** 移動と hook 化を行う。
- [ ] **Step 3:** 確かめる。
  - `go test ./pkg/vfs/`（一時 Redis）と `go test ./pkg/fuse/ -run 'TestGenFuseOptHasNoRequestDeadline|TestFUSEWaitsForPendingFsync'`：PASS
  - Task 0 のテスト：PASS
- [ ] **Step 4:** 差分量と衝突ブロック数を記録する。
- [ ] **Step 5:** commit して merge する。
  - message: `refactor(vfs): move kaz writer and flush helpers into *_kaz.go`

### Task 3: pkg/meta の kaz の定義を移し、`WriteSlices` と `RetireSlice` を upstream の interface・番号から外す

**ブランチ:** `refactor/kaz-move-meta`

**Files:**
- Modify: `pkg/meta/base.go`、`redis.go`、`sql.go`、`tkv.go`、`interface.go`、`config.go`、`compaction_gc.go`、`pkg/vfs/writer_kaz.go`（Task 2 で作ったもの）
- Create: `pkg/meta/write_slices_kaz.go`
- Modify: `redis_compact_kaz.go`、`sql_compact_kaz.go`、`tkv_compact_kaz.go`、`compaction_scheduler.go`
- Test:
  - Modify: `pkg/vfs/writer_range_test.go:81,92`、`writer_batch_test.go:74,89`、`vfs_test.go:217`。`m.Meta.WriteSlices` を `m.Meta.(meta.KazSliceWriter).WriteSlices` にする。
  - Create: `pkg/vfs/writer_batch_fallback_kaz_test.go`

**Interfaces:**
- Produces:
  - `type KazSliceWriter interface { WriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time) (n int, st syscall.Errno, uncertain bool) }`（`pkg/meta/write_slices_kaz.go`。署名は今の `Meta.WriteSlices` と同じにする）
  - `RetireSlice` は `compaction_gc.go` へ移し、値を `1<<16 + 1` にする。
- Consumes: Task 2 の `commitSlices`（`writer_kaz.go`）

移すもの:

| 移動元 | 定義 | 移動先 |
|---|---|---|
| `base.go:2224-2360` | `compactionWanted`、`sliceBatchWriter`、`writePhases`、`errWriteSlicesFallback`、`batchErrorUnapplied`、`WriteSlices`、`writeLocked`、`afterWrite` | `write_slices_kaz.go` |
| `redis.go:3192`、`sql.go:3489`、`tkv.go:2720` | `doWriteSlices` | 各 `*_compact_kaz.go` |
| `interface.go:340`（`SliceWrite`）、`interface.go:485-490`（`Meta.WriteSlices`）、`interface.go:60-61`（`RetireSlice`） | 型、interface のメソッド、定数 | `write_slices_kaz.go`、`compaction_gc.go`。`Meta.WriteSlices` は消す |
| `config.go:129, 372` | `ValidateCompactionScheduler`、`ValidateCompactionGC` | `compaction_scheduler.go`、`compaction_gc.go` |
| `config.go:93-114` | SelfCheck の冒頭の kaz の検証 | `func (c *Config) kazSelfCheck()` の 1 行の hook にする |

- [ ] **Step 1:** 失敗するテストを書く。`TestWriterFallsBackWithoutSliceBatchWriter`
  - `meta.Meta` を埋め込むだけで `WriteSlices` を持たない wrapper を使い、`MetaWriteBatch: 8` で 3 個の slice を書いて fsync する。
  - wrapper の `Write` が 3 回呼ばれ、読み戻した内容が書いた内容と一致することを確かめる。
  - 今は `Meta.WriteSlices` が interface にあるので、埋め込んだ側の `WriteSlices`（batch）が呼ばれ、`Write` の回数が 3 にならずに失敗する。
- [ ] **Step 2:** `KazSliceWriter` を足して `Meta.WriteSlices` を消し、`commitSlices` を `if bw, ok := f.w.m.(meta.KazSliceWriter); ok { … } else { 1 個ずつ Write }` にする。else の側は、今の `baseMeta.WriteSlices` の逐次の経路と同じ結果（`(i, st, false)`）にする。
- [ ] **Step 3:** 残りの定義を移し、`RetireSlice` を移して値を変える。
- [ ] **Step 4:** 確かめる。
  - `go test ./pkg/meta/ -run 'Kaz|WriteSlices|Compaction|Delta|Block|Retire|DeleteQueue|TestRedisClient|TestSQLiteClient|TestMemKVClient|TestBadgerClient'`（一時 Redis）：PASS
  - `go test ./pkg/vfs/`（一時 Redis）と `go test ./cmd/ -run 'Kaz|Retire|CombinedVM|WriterFlush|MetaWriteBatch'`：PASS
  - `cmd/retirement_callback_test.go` が PASS すること（`RetireSlice` の番号の変更）。
- [ ] **Step 5:** 差分量と衝突ブロック数を記録する。
- [ ] **Step 6:** commit して merge する。
  - message: `refactor(meta): move kaz slice batching out of upstream files and the Meta interface`

### Task 4: cmd の kaz のフラグと設定の取り込みを移す

**ブランチ:** `refactor/kaz-move-cmd`

**Files:**
- Modify: `cmd/flags.go`、`cmd/mount.go`、`cmd/kaz_cache_flags.go`、`cmd/kaz_compaction_policy.go`
- Create: `cmd/kaz_flags.go`、`cmd/kaz_vfs_flags.go`

**Interfaces:**
- Produces:
  - `func kazStorageFlags() []cli.Flag`、`func kazDataCacheFlags() []cli.Flag`。中身は今の定義の並び順のまま移す。
  - `func applyKazVfsFlags(c *cli.Context, cfg *vfs.Config)`
  - `func kazRegisterMetaMsg(m meta.Meta, store chunk.ChunkStore)`
  - `parseWriterFlushTimeout` は `kaz_vfs_flags.go` へ移す。

やること:
- `storageFlags()` と `dataCacheFlags()` の中の kaz のフラグ（`flags.go:125, 160-221, 285-349, 413-447`）を移す。upstream のリテラルは `append([]cli.Flag{…upstream…}, kazStorageFlags()...)` の形にする。
- `max-deletes` と `cache-eviction` の Usage の書き換え（`flags.go:157, 410`）は、`kazDataCacheFlags` などの中で、名前でフラグを探して Usage を差し替える形にする。upstream の行は元の文言に戻す。
- `flags.go:26-27` の meta と vfs の import は、使わなくなるなら消す。
- 設定の取り込みを移す。
  - `getVfsConf`（`mount.go:300-320`）の kaz の 6 項目と `parseWriterFlushTimeout` → `applyKazVfsFlags`
  - `getMetaConf`（`mount.go:378-386`）→ 既存の `applyKazCompactionPolicyFlags` に入れる。`MaxDeletes`、`NoBGJob`、`ReadOnly` を代入した後に呼ぶ順序は保つ。
  - `getChunkConf`（`mount.go:429, 437, 439`）→ `applyKazCacheFlags` に入れる。
  - `registerMetaMsg`（`mount.go:346-354`）→ `kazRegisterMetaMsg`

- [ ] **Step 1:** 移動と hook 化を行う。
- [ ] **Step 2:** 確かめる。
  - `go test ./cmd/ -run 'Kaz|CombinedVM|WritebackFsync|WriterFlush|MetaWriteBatch|FuseWait|MaxDeletes|Retire'`：PASS
  - Task 0 の `TestKazFlagSetUnchanged` と `TestKazConfigJSONKeys`：PASS（golden を書き直さないこと）
- [ ] **Step 3:** 差分量と衝突ブロック数を記録する。
- [ ] **Step 4:** commit して merge する。
  - message: `refactor(cmd): define kaz flags and apply kaz settings outside upstream literals`

### Task 5: 1.5 系の改名と分割への準備

**ブランチ:** `refactor/kaz-1.5-prep`

**Files:**
- Modify: `pkg/chunk/disk_cache.go`、`disk_cache_kaz.go`、`cache_state_kaz.go`、`cached_store_kaz.go`、`cached_store_staging_kaz.go`
- Modify（テスト）: `cache_state_kaz_test.go`、`disk_cache_kaz_test.go`、`cached_store_kaz_test.go`、`cache_compat_kaz_test.go`、`staging_sync_test.go`、`retire_test.go`

**Interfaces:**
- Produces:
  - `type kazDiskCache = cacheStore`
  - `var kazNewDiskCache = newCacheStore`
  - `func (cache *cacheStore) flushPageOpt(path string, data []byte, …, durable bool) error`（今の `flushPage` の 5 引数版の本体）。`flushPage` は upstream の 4 引数の署名に戻し、`flushPageOpt(…, false)` を呼ぶ薄い wrapper にする。今 `durable=true` で呼んでいる箇所は `flushPageOpt` を直接呼ぶ。
  - `func kazSliceKeys(id uint64, length int, store *cachedStore) []string`（`Retire` と `rSlice.Remove` で使う。今の `keys()` と同じ並びを返す）

- [ ] **Step 1:** 失敗するテストを書く。`TestKazSliceKeysMatchesKeys`
  - 0、1 block、ちょうど 1 block、複数 block と端数、の 4 通りの長さで、`kazSliceKeys` が `sliceForRead(...).keys()` と同じになることを確かめる。
- [ ] **Step 2:** `kazSliceKeys` を実装し、kaz の `keys()` の呼び出し（`cached_store_staging_kaz.go` の `Retire`、`rSlice.Remove` の kaz の部分）を置き換える。
- [ ] **Step 3:** kaz のファイルとテストの `cacheStore`／`newCacheStore` を、エイリアスと変数に置き換える。upstream のファイルは触らない。
- [ ] **Step 4:** `flushPage` の署名を upstream に戻す。
- [ ] **Step 5:** 確かめる。
  - `go test ./pkg/chunk/`：PASS
  - `grep -n 'cacheStore\b\|newCacheStore' pkg/chunk/*kaz*.go`：エイリアスの定義行だけが出ること
- [ ] **Step 6:** 差分量と衝突ブロック数を記録する。
- [ ] **Step 7:** commit して merge する。
  - message: `refactor(chunk): prepare kaz code for the upstream diskCache rename and split`

### Task 6: 設定と状態のフィールドを埋め込み struct にまとめる

**ブランチ:** `refactor/kaz-embed-config`

**Files:**
- Modify: `pkg/chunk/cached_store.go`（`Config`、`cachedStore`、`NewCachedStore`）、`pkg/chunk/disk_cache.go`（`cacheStore`、`newCacheStore`）
- Modify: `pkg/meta/config.go`（`Config`）、`pkg/meta/base.go`（`baseMeta`、`newBaseMeta`）
- Modify: `pkg/vfs/vfs.go`（`Config`）、`pkg/vfs/writer.go`（`fileWriter`）
- Create: `pkg/chunk/config_kaz.go`、`pkg/meta/config_kaz.go`、`pkg/vfs/config_kaz.go`
- Test:
  - Create: `pkg/chunk/config_kaz_test.go`、`pkg/meta/config_kaz_test.go`、`pkg/vfs/config_kaz_test.go`（シャドーイングの検出）
  - Modify: 複合リテラルで kaz のフィールドを書いている約 80 か所。chunk 14、meta 約 55、vfs 11。upstream のテストファイル `pkg/vfs/vfs_test.go:84,86`、`pkg/fuse/fuse_test.go:105-106` を含む。

**Interfaces:**
- Produces（どれも埋め込み。ポインタにしない。json タグは付けない）:

| 型 | 中身 | 埋め込み先 |
|---|---|---|
| `type KazChunkConfig struct` | `StagingNoSync`、`FinishCanceledGet`、`GetHeaderTimeout`、`KazCache*`（8 個）、`KazWholeReadMax`。名前と型は今のまま | `chunk.Config` |
| `type KazMetaConfig struct` | `CompactionGCMode`、`CompactionScheduler`、`KazCompaction*`（10 個） | `meta.Config` の末尾 |
| `type KazVfsConfig struct` | `SliceFlushWait`、`SliceFlushIdle`、`WriterReuseWindow`、`WriterFlushTimeout`、`WriterFlushScope`、`MetaWriteBatch` | `vfs.Config` |
| `type kazStoreState struct` | `cachedStore` の kaz の 9 フィールド | `cachedStore`（初期化は `store.kazInitState(config)` の 1 行） |
| `type kazMetaState struct` | `baseMeta` の kaz の 6 フィールド | `baseMeta`（atomic.Pointer を含むので、代入で初期化する） |
| `type kazDiskState struct` | `stagingSync`、`durableMu`、`durableDirs` | `cacheStore`（初期化は `kazAttach` へ移す） |
| `type kazFileWriterState struct` | `rangewaiting`、`mtimeFloor`、`mtimeGen`、`commitMu` | `fileWriter` |

- [ ] **Step 1:** 失敗するテストを書く。`TestKazEmbeddedFieldsNotShadowed`（3 パッケージそれぞれ）
  - reflect で、外側の struct の直下のフィールド名と、埋め込んだ Kaz* struct のフィールド名が重ならないことを確かめる。
  - 埋め込み struct がまだないので、コンパイルエラーで失敗する。
- [ ] **Step 2:** 埋め込み struct を作り、フィールドを移す。upstream の struct の桁揃えは、元の行に戻す。
- [ ] **Step 3:** 複合リテラルを書き換える。代入（`conf.KazX = …`）はそのまま通る。リテラルは `Config{KazChunkConfig: KazChunkConfig{…}}` の形にするか、テストでは代入に変える。
- [ ] **Step 4:** 確かめる。
  - Task 0 の `TestKazConfigJSONKeys`：PASS（golden は変えない。キーの集合と値が同じであること）
  - `go vet ./...`：copylocks の警告がないこと
  - `go test ./pkg/chunk/ ./pkg/meta/(kaz 系) ./pkg/vfs/ ./pkg/fuse/ ./cmd/`：PASS
- [ ] **Step 5:** 差分量と衝突ブロック数を記録する。
- [ ] **Step 6:** commit して merge する。
  - message: `refactor: group kaz config and state fields into embedded structs`

### Task 7: 診断のログを kaz の helper へ出す

**ブランチ:** `refactor/kaz-trace-hooks`

**Files:**
- Modify: `pkg/meta/base.go`（`compactChunk`、`Write`）、`pkg/meta/redis.go`（`txn`）、`pkg/vfs/writer.go`（commit のログ）
- Create: `pkg/meta/trace_kaz.go`
- Test: Create `pkg/meta/trace_kaz_test.go`、`pkg/vfs/trace_format_kaz_test.go`

**Interfaces:**
- Produces:
  - `type kazCompactTrace struct`（値型）。`func (m *baseMeta) kazCompactTrace(inode Ino, indx uint32, once, force bool) kazCompactTrace` で作り、呼び出し側のローカル変数に置く。メソッドはポインタ receiver で、`queueWait()`、`phase(name string)`、`setSlices(n int, bytes uint64)`、`done(err error)`。
  - `type kazTxnTrace struct`（値型）。`func newKazTxnTrace(key string) kazTxnTrace` で作る。メソッドは `lockAcquired()`、`attempt()`、`done(err error)`。
  - slow compaction の書式は、`compaction_block_kaz.go:178` と共通の関数にする。` mode=` は今どおり block の側だけに付ける。

- [ ] **Step 1:** 失敗するテストを書く。`TestKazTraceLogFormats`
  - logger を DEBUG にして出力を取り込み、compaction（`phase=queue_wait`、`slow compaction … once= force= slices= bytes= total= queue_wait= object= metadata=`）、`metadata transaction key= phase=lock_wait`、`slow metadata transaction key= total= lock_wait= active= attempts= err=` を出させる。
  - 各行が、調査スクリプトの正規表現に一致することを確かめる。正規表現は `docs/inspection/incident_recurrence/2026-10-02/compaction_queue_analysis.py:16-19` と `cache_churn/2026-10-20/compaction_overnight.py:25-30` から、そのまま写す。
  - helper がまだないので失敗する。
- [ ] **Step 2:** helper を実装し、upstream の関数の中の計測とログを 1 行の呼び出しに置き換える。計測の位置（いつ時刻を取るか）は変えない。
- [ ] **Step 3:** 確かめる。
  - `TestKazTraceLogFormats`：PASS
  - `BenchmarkKazTxnTrace`（`pkg/meta/trace_kaz_test.go` に置く。`-benchmem` で `0 allocs/op` であること）
  - `go build -gcflags=-m ./pkg/meta/ 2>&1 | grep -E 'kazTxnTrace|kazCompactTrace'` に `escapes to heap` が出ないこと
  - 既存の `writer_trace_test.go`：PASS
  - `go test ./pkg/meta/ -run 'Compaction|Kaz|TestRedisClient'`（一時 Redis）：PASS
- [ ] **Step 4:** 差分量と衝突ブロック数を記録する。
- [ ] **Step 5:** commit して merge する。
  - message: `refactor(meta): move kaz compaction and transaction tracing into helpers without changing log formats`

### Task 8: 結果の記録

**Files（調査リポジトリ、commit はリリースの前にユーザーが行う）:**
- Modify: 報告書（`docs/inspection/upstream_v1.4.2/2026-10-26/port-investigation-ja.md`）、`docs/development/upstream-sync/README-ja.md`、`TODO.md`、`agent_memo.md`

- [ ] **Step 1:** Task ごとの差分量と、本家 main との衝突ブロック数を、表で報告書に書く。
- [ ] **Step 2:** 手順書の「upstream のファイルへ kaz の差分を入れるときの規則」を、実際にできた形に合わせて更新する。対象は `*_kaz.go`、埋め込み struct、`kazStorageFlags`、エイリアス。
- [ ] **Step 3:** TODO と memo を更新する。

## スコープ外（1.5 系への追従のときに行う）

- `commitThread`、`flush`／`waitFlushed`、`wSlice.upload` の staging の分岐の、関数ごとの移動・抽出。
  - 本家 main がこの 3 か所をすでに書き換えている（#7693、#7500、#7517）。今動かしても、1.5 系への追従のときに本家の修正を kaz の関数へ手で移すことになる。その作業と一緒に行う方が、1 回で済む。
  - 並行性と耐久性の順序を写し間違えるリスクも高い。
- `WithWriterFlushOrigin` の明示文字列の置き換え。値（`vfs.Read` など）を調査スクリプトが使っているので、残す。
- `redis.go` の WATCH の追加（CopyFileRange、clone）。正しさの修正なので、その場に残す。
