# kaz staging pack と DELETE の省略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** writeback の staging の待ち行列で、同じ chunk の小さい slice を 1 回の PUT にまとめる（`--kaz-staging-pack`）。あわせて、この process が一度も PUT していない object の DELETE を省く（`--kaz-skip-unuploaded-delete`）。

**Architecture:**
- chunk store（`pkg/chunk`）: pending の item に kaz の状態（待ち／押さえ中／まとめ済み、PUT の印、この process の印）を持たせる。uploader が取り出した直後に、vfs が登録した packer を呼ぶ。
- vfs（`pkg/vfs`）: commit した slice の `id → (inode, chunk)` を覚える。同じ chunk の待っている slice を押さえ、データ（staging のファイルから読んだもの）を meta に渡す。
- meta（`pkg/meta`）: 見えている範囲を 1 slice に連結して PUT する。差分方式の置き換え（`doKazReplaceChunk`）で record を置き換え、参照が 0 になった slice を返す。
- DELETE の省略: `Retire` の時点で判断した key をメモリの集合に入れ、`rSlice.Remove` の DELETE で消費する。

**Tech Stack:** Go 1.25.11（`GOENV_VERSION=1.25.11`）、testify、prometheus、urfave/cli v2。metadata のテストは sqlite・redis・memkv。

**Spec:** `docs/superpowers/specs/2026-10-27-kaz-staging-pack-design.md`（2026-10-27 ユーザー承認）。調査は `docs/inspection/staging_pack/2026-10-27/feasibility-ja.md`。

## Global Constraints

- 本体の作業ブランチは `feat/kaz-staging-pack`（起点 `1.4.2-improve-kaz`、HEAD `d246b0f7`）。`1.4.2-improve-kaz` には直接 commit しない。merge は実機の試験の後に `--no-ff` で行う（ユーザーの確認後）。
- commit の手順は、実行を始めるときにユーザーが feat ブランチへの commit を許可した場合だけ行う。push はしない。調査リポジトリの文書は commit しない（ユーザーが行う）。
- テストのコマンドには必ず `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp` を付ける（Go 1.26 では mockey のリンクエラー、`/tmp` は tmpfs で小さい）。
- 新しい `.go` ファイルは Apache 2.0 のヘッダ（`Copyright 2026 Juicedata, Inc.`）で始める。
- upstream 由来の関数・型を変えるときは、直前に doc コメントを足さない。関数の中の変えた行の上に `// kaz:` の行コメントを書く。kaz が新しく足す関数・型には doc コメントを付ける。
- 判断と処理は `*_kaz.go` に置く。upstream 由来のファイルの変更は、設計書 §7 の表の hook の行だけにする。
- フラグは `--kaz-staging-pack`（bool、既定 false）と `--kaz-skip-unuploaded-delete`（bool、既定 false）。
- まとめる上限は block の大きさ（`conf.BlockSize`、本番 4 MiB）。相手は slice の長さが block 未満のもの（block が 1 つだけ）。
- まとめは `--kaz-compaction-mode delta` のとき、`kazDeltaUsable` が真のエンジン（Redis／SQL）、`TrashDays == 0` のときだけ行う。
- fsync、書き込み順序、read-after-write を弱めない。保存の失敗、容量不足、quota のエラーを隠さない。
- 「DELETE を省いてよい」と判断できないときは、必ず DELETE を送る（安全側）。

## Review Focus

1. **Retire と uploader の競合で、PUT 済みの object の DELETE を省いてしまう**（object storage に漏れる）。PUT の印は `pendingMutex` の中で、PUT を始める前に付ける。`Retire` は同じ mutex の中で印を読む。Task 2 の `TestKazSkipDeleteAfterPutStarted` と `TestKazSkipDeleteRaceWithUploader` で確かめる。
2. **参照が残る slice をまとめ済みにして、永遠にアップロードしない**（clone／copy_file_range の後、データが消える）。Task 4 の `TestKazReplaceOnDeadSkipsShared` と、Task 7 の `TestKazStagingPackSharedSliceUploads` で確かめる。
3. **まとめに失敗したとき、block が列に戻らない、または二重に入る**（アップロードが遅れる、または PUT が 2 回出る）。Task 3 の `TestKazReleaseRequeuesOnce` で確かめる。
4. **まとめた後、upstream の client と別の client から見え方が変わる**（古いデータや 0 が見える）。Task 6 の `TestKazPackChunkView` と、Task 7 の `TestKazStagingPackReadBack` で確かめる。
5. **起動時の scan が作った item や、すぐ PUT した item の DELETE を省いてしまう**。Task 2 の `TestKazSkipDeleteScanItem` と `TestKazSkipDeleteImmediate` で確かめる。

---

## File Structure

| ファイル | 種類 | 役割 |
|---|---|---|
| `pkg/chunk/staging_pack_kaz.go` | 新規 | item の kaz の状態、押さえ／戻す、packer の登録と呼び出し、DELETE を省く集合、metrics |
| `pkg/chunk/staging_pack_kaz_test.go` | 新規 | Task 1〜3 のテスト |
| `pkg/chunk/cached_store.go` | 変更（hook） | `pendingItem` に 1 項目、`addDelayedStaging` の初期化の行、`wSlice.upload` の登録、`scanDelayedStaging`、`uploadStagingFile`、`rSlice.Remove` |
| `pkg/chunk/cached_store_staging_kaz.go` | 変更 | `beginStagingUpload`／`beginImmediateStaging` で PUT の印、`Retire` で集合へ |
| `pkg/chunk/config_kaz.go` | 変更 | `KazSkipUnuploadedDelete` |
| `pkg/meta/compaction_block_kaz.go`、`redis_compact_kaz.go`、`sql_compact_kaz.go`、`tkv_compact_kaz.go` | 変更 | `doKazReplaceChunk` に `onDead` を足す |
| `pkg/meta/compaction_pack_kaz.go` | 新規 | `planKazPack`、`KazPackChunk`、`KazStagingPacker` |
| `pkg/meta/compaction_pack_kaz_test.go` | 新規 | Task 5〜6 のテスト |
| `pkg/vfs/staging_pack_kaz.go` | 新規 | slice の対応表、`VFS.kazPackStaged`、登録 |
| `pkg/vfs/staging_pack_kaz_test.go` | 新規 | Task 7 のテスト |
| `pkg/vfs/writer_kaz.go` | 変更 | `commitSlices` の後で対応表に入れる |
| `pkg/vfs/vfs.go` | 変更（hook 1 行） | `NewVFS` で `v.kazInitStagingPack()` |
| `pkg/vfs/config_kaz.go` | 変更 | `KazStagingPack bool` |
| `cmd/kaz_flags.go`、`cmd/kaz_vfs_flags.go`、`cmd/kaz_cache_flags.go` | 変更 | フラグと設定、delta でないときの WARN |
| `docs/en|ja|zh_cn/reference/kaz_options.md` | 変更 | 2 行ずつ |
| 調査リポジトリ `tests/juicefs/settings.toml` | 変更 | suite の設定 3 つ |

---

### Task 1: pending の item の kaz の状態と PUT の印

**Files:**
- Create: `pkg/chunk/staging_pack_kaz.go`、`pkg/chunk/staging_pack_kaz_test.go`
- Modify: `pkg/chunk/cached_store.go`（`pendingItem` の型 :48-53、`addDelayedStaging` の初期化の行 :1152、`wSlice.upload` の `addDelayedStaging` の呼び出し :480、`scanDelayedStaging` :1196）、`pkg/chunk/cached_store_staging_kaz.go`（`beginStagingUpload`、`beginImmediateStaging`）

**Interfaces:**
- Produces:
  - `type kazStagingState uint8`、定数 `kazStagingWait`（0）、`kazStagingClaimed`、`kazStagingPacked`
  - `type kazPendingInfo struct { state kazStagingState; putStarted bool; local bool }`（`pendingMutex` の中だけで読み書きする）
  - `pendingItem` に項目 `kaz kazPendingInfo` を 1 行で足す。`addDelayedStaging` の位置で並べた初期化は、名前付きの形 `&pendingItem{key: key, fpath: stagingPath, ts: added}` に直す（`// kaz:`）。
  - `func (store *cachedStore) kazAddLocalStaging(key, path string, added time.Time)`: 呼ぶ前に item が無かったときだけ、呼んだ後の item に `local = true` を付ける。`wSlice.upload` の `addDelayedStaging(key, stagingPath, time.Now(), false)` をこれに置き換える。
  - `beginStagingUpload`: item があり `state != kazStagingWait` なら `item.uploading.Store(false)` として nil を返す（PUT しない）。PUT するときは `putStarted = true`。`beginImmediateStaging` も `putStarted = true` を付ける。
  - `scanDelayedStaging`: `state != kazStagingWait` の item は列に入れない（`// kaz:` 1 行の条件）。

- [ ] **Step 1: 失敗するテストを書く**（`staging_pack_kaz_test.go`、retire_test.go と同じ組み立て方の store で）
  - `TestKazPendingPutMark`: `beginStagingUpload(key)` の後、`item.kaz.putStarted == true`。`beginImmediateStaging` も同じ。
  - `TestKazPendingLocalMark`: `kazAddLocalStaging` で作った item は `local == true`。先に `addDelayedStaging`（scan と同じ呼び方）で作った item は、後で `kazAddLocalStaging` を呼んでも `local == false`。
  - `TestKazClaimedNotUploaded`: `state = kazStagingClaimed` の item で、`beginStagingUpload` が nil を返し、`uploading` が false。`scanDelayedStaging` の後も `pendingCh` は空。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp go test ./pkg/chunk/ -run 'TestKazPending|TestKazClaimedNotUploaded' -count=1`
  Expected: FAIL（`kazAddLocalStaging` などが未定義）
- [ ] **Step 3: 上の Interfaces のとおりに実装する**
- [ ] **Step 4: 走らせて通ることと、既存のテストが壊れていないことを確かめる**
  Run: 同じコマンド。続けて `... go test ./pkg/chunk/ -run 'TestRetire|TestUploadStaging' -count=1`
  Expected: PASS
- [ ] **Step 5: commit**（許可がある場合）`feat(chunk): track kaz staging state and put marks on pending items`

### Task 2: 一度も PUT していない object の DELETE を省く

**Files:**
- Modify: `pkg/chunk/staging_pack_kaz.go`、`pkg/chunk/cached_store_staging_kaz.go`（`Retire`）、`pkg/chunk/cached_store.go`（`rSlice.Remove` の DELETE のループ :199-201）、`pkg/chunk/config_kaz.go`
- Test: `pkg/chunk/staging_pack_kaz_test.go`

**Interfaces:**
- Consumes: Task 1 の `kazPendingInfo`
- Produces:
  - `KazChunkConfig.KazSkipUnuploadedDelete bool`
  - `const kazUnuploadedMax = 1 << 20`
  - `func (store *cachedStore) kazTakeUnuploaded(key string) bool`: 集合にあれば消して true を返す。
  - `Retire`: `cancel()` の中で、消す item が `local && !putStarted` なら候補にする。unlink がすべて成功して `busy == false` のときだけ、候補を集合に入れる（上限を超えたら入れない）。設定が false なら何もしない。
  - `rSlice.Remove`: `if s.store.kazTakeUnuploaded(keys[i]) { continue }`（`// kaz:`）。
  - metric `kaz_delete_skipped_total`（Counter）

- [ ] **Step 1: 失敗するテストを書く**（object storage は DELETE を数える mem のラッパー）
  - `TestKazSkipDeleteUnuploaded`: 設定 true。`kazAddLocalStaging` → `Remove(id, len)` → DELETE 0 回、staging のファイルは無い、`kaz_delete_skipped_total == 1`。
  - `TestKazSkipDeleteDisabled`: 設定 false → DELETE 1 回。
  - `TestKazSkipDeleteAfterPutStarted`: `beginStagingUpload` → `endStagingUpload`（PUT が失敗した形）→ `Remove` → DELETE 1 回。
  - `TestKazSkipDeleteRaceWithUploader`: uploader の `uploadStagingFile` と `Remove` を 200 回並行に走らせ、object storage に残る key が 0 であること（`-race` で）。
  - `TestKazSkipDeleteScanItem`: `addDelayedStaging`（scan と同じ）で作った item → DELETE 1 回。
  - `TestKazSkipDeleteImmediate`: `beginImmediateStaging` の item → DELETE 1 回。
  - `TestKazSkipDeleteDeferredTwoRetires`: `Retire` を先に 1 回呼び（GC の dispatcher と同じ）、後で `Remove` → DELETE 0 回。
  - `TestKazSkipDeleteBusyNotRecorded`: `activeStaging > 0` で `Retire` が `ErrSliceUploadInFlight` → 集合に無い。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp go test ./pkg/chunk/ -run 'TestKazSkipDelete' -count=1 -race`
  Expected: FAIL
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 走らせて通ることを確かめる**
  Run: 同じコマンド、続けて `... go test ./pkg/chunk/ -run 'TestRetire' -count=1 -race`
  Expected: PASS
- [ ] **Step 5: commit**（許可がある場合）`feat(chunk): skip DELETE of staged blocks never put (--kaz-skip-unuploaded-delete)`

### Task 3: 押さえ・戻すと packer の hook

**Files:**
- Modify: `pkg/chunk/staging_pack_kaz.go`、`pkg/chunk/cached_store.go`（`uploadStagingFile` の `beginStagingUpload` の直前に hook 1 行）
- Test: `pkg/chunk/staging_pack_kaz_test.go`

**Interfaces:**
- Consumes: Task 1
- Produces（vfs から型アサーションで使う。`ChunkStore` interface には足さない）:
  - `type KazStagedRef struct { ID uint64; Length int }`
  - `type KazStagedData struct { ID uint64; Data []byte; Tier uint8 }`
  - `func (store *cachedStore) KazSetStagingPacker(p func(id uint64, length int) bool)`
  - `func (store *cachedStore) KazClaimStaged(refs []KazStagedRef) (claimed []KazStagedData, absent []uint64)`: `pendingMutex` の中で、key（`<id>_0_<length>`）の item が `state == wait && !putStarted && activeStaging == 0` なら `claimed` にする（`uploading` は見ない。列の中の item も押さえる）。item が無い id は `absent` に入れる。条件を満たさない id はどちらにも入れない。押さえた後、mutex の外で staging のファイルを読む（`openCacheFile` と checksum、footer の tier）。読めなければ wait に戻して外す。
  - `func (store *cachedStore) KazReleaseStaged(ids []uint64, packed map[uint64]bool)`: `packed[id]` なら `state = packed`。それ以外は `state = wait` とし、`uploading.CompareAndSwap(false, true)` が成功したら `pendingCh` へ非ブロッキングで送る（入らなければ `uploading` を false に戻す）。item が既に無い id は何もしない。
  - `func (store *cachedStore) KazStagingPackEvent(event string, n int)`、metric `kaz_staging_pack_total{event}`。event は `packed`、`packed_slices`、`packed_bytes`、`skip_no_peer`、`skip_unknown`、`skip_busy`、`skip_trash`、`skip_engine`、`skip_not_in_chunk`、`fail_put`、`fail_replace`。
  - hook: `uploadStagingFile` で、`beginStagingUpload` の前に `if store.kazTryPack(key) { return }`（`// kaz:`）。`kazTryPack` は、packer が登録され、key の block 番号が 0 で、長さが `BlockSize` 未満のときだけ `packer(id, length)` を呼び、その戻り値を返す。それ以外は false。

- [ ] **Step 1: 失敗するテストを書く**
  - `TestKazClaimQueuedItem`: `addDelayedStaging` で列に入った（`uploading == true`）item を `KazClaimStaged` で押さえられ、Data が staging の中身と同じ。
  - `TestKazClaimSkipsPutStarted`: `putStarted` の item は `claimed` にも `absent` にも入らない。
  - `TestKazReleaseRequeuesOnce`: 押さえた item のうち、列の外のもの（`uploading == false`）は戻すと列に 1 回だけ入る。列の中のもの（`uploading == true`）は入らない。
  - `TestKazReleasePacked`: `packed` の item は uploader が取り出しても PUT されない（object storage の PUT 0）。
  - `TestKazTryPackHook`: packer が true を返すと PUT 0、false なら PUT 1。block 番号が 1 の key と、長さが `BlockSize` の key では packer を呼ばない。
  - `TestKazClaimedRetireSucceeds`: 押さえ中の item の `Retire` は `ErrSliceUploadInFlight` を返さない。
  - `TestKazClaimIgnoresDelay`: `UploadDelay = 60s` で、登録したばかり（delay 前）の item も押さえられる（設計書 §5.6、ユーザー確認済み）。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp go test ./pkg/chunk/ -run 'TestKazClaim|TestKazRelease|TestKazTryPack' -count=1 -race`
  Expected: FAIL
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 走らせて通ることを確かめ、`make test.pkg` の chunk 分を流す**
  Run: 同じコマンド、続けて `... go test ./pkg/chunk/ -count=1`
  Expected: PASS（既存の DATA RACE の 11 件は findings の既知の問題。新しい失敗が無いこと）
- [ ] **Step 5: commit**（許可がある場合）`feat(chunk): claim and release staged blocks for kaz staging pack`

### Task 4: `doKazReplaceChunk` が参照 0 の slice を知らせる

**Files:**
- Modify: `pkg/meta/compaction_block_kaz.go:31-37, 332`、`pkg/meta/redis_compact_kaz.go`、`pkg/meta/sql_compact_kaz.go`、`pkg/meta/tkv_compact_kaz.go`、既存のテストの呼び出し（`compaction_block_replace_kaz_test.go`、`compaction_delta_kaz_test.go`。末尾に `nil` を足すだけ）
- Test: `pkg/meta/compaction_pack_kaz_test.go`

**Interfaces:**
- Produces: `doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []kazCreated, delayed []byte, delayedKey uint64, onDead func(id uint64, size uint32)) syscall.Errno`。成功したとき、各エンジンが `enqueueCompactionDelete(id, size)` を呼ぶのと同じ slice について、その直後に `onDead` を呼ぶ（nil なら呼ばない）。trash（`delayed != nil`）のときは呼ばない。

- [ ] **Step 1: 失敗するテストを書く**（`kazBlockBackends` の各エンジンで）
  - `TestKazReplaceOnDeadReports`: record 3 つ（a、b1、b2）のうち b1、b2 を外すと、`onDead` は b1、b2 をちょうど 1 回ずつ受け取る。
  - `TestKazReplaceOnDeadSkipsShared`: b1 を指す record が 2 つある（同じ id の record を 2 つ書く、clone と同じ形）とき、1 つだけ外すと `onDead` は呼ばれない。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp go test ./pkg/meta/ -run 'TestKazReplace' -count=1`
  Expected: FAIL（引数の数が合わない）
- [ ] **Step 3: 実装し、既存の呼び出しに `nil` を足す**
- [ ] **Step 4: 走らせて通ることを確かめる**
  Run: `... go test ./pkg/meta/ -run 'TestKazReplace|TestKazBlock|TestKazDelta' -count=1`
  Expected: PASS
- [ ] **Step 5: commit**（許可がある場合）`refactor(meta): report slices whose last reference was dropped by a kaz replace`

### Task 5: まとめの計画 `planKazPack`

**Files:**
- Create: `pkg/meta/compaction_pack_kaz.go`
- Test: `pkg/meta/compaction_pack_kaz_test.go`

**Interfaces:**
- Produces:
  - `type kazPackOut struct { pos uint32; srcID uint64; srcOff uint32; length uint32 }`（chunk の位置、元の slice の id、その slice の中の位置、長さ）
  - `type kazPackPlan struct { removed []*slice; kept []*slice; outs []kazPackOut; ids []uint64 }`（`ids` は実際にまとめる slice の id）
  - `func planKazPack(ss []*slice, cands []uint64, maxBytes uint32) *kazPackPlan`
    - `cands` の順（先頭が取り出した block）に、その id の record をすべて外す候補にする。見えている範囲は `buildSlice(ss)` の見え方から、その id の部分を取る。
    - 見えている範囲の合計が `maxBytes` を超える id は、その id ごと外さない（残す）。完全に隠れた record は、データなしで外す。
    - `ids` に残った id が 2 つ未満なら nil を返す。
    - `outs` は位置の順。隣り合っていてもつながない（元のデータを読まない）。

- [ ] **Step 1: 失敗するテストを書く**
  - `TestKazPackPlanView`: 乱数で作った record の列（100 通り、固定の seed）と候補について、置き換えた後の `buildSlice` の見え方（`kept` の後に `outs` を、まとめた slice への record として並べたもの）のバイト列が、置き換える前と同じ（I1）。外した record の見えていた範囲が、全部 `outs` に覆われる（I2）。
  - `TestKazPackPlanCap`: `maxBytes = 8192`、候補が 3 つ（4096 バイトずつ見えている）→ `ids` は先頭の 2 つ。
  - `TestKazPackPlanSingle`: 見えている候補が 1 つだけ → nil。
  - `TestKazPackPlanHidden`: 後の record に完全に隠れた候補は、`removed` に入り、`outs` に出ない。
  - `TestKazPackPlanLaterWrite`: 候補より後ろにある候補でない record が、候補の一部を隠しているとき、隠れた部分は `outs` に入らない。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `... go test ./pkg/meta/ -run 'TestKazPackPlan' -count=1`
  Expected: FAIL
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 走らせて通ることを確かめる**（同じコマンド、Expected: PASS）
- [ ] **Step 5: commit**（許可がある場合）`feat(meta): plan a kaz staging pack from a chunk's records`

### Task 6: `KazPackChunk`（読む・PUT・置き換え）

**Files:**
- Modify: `pkg/meta/compaction_pack_kaz.go`
- Test: `pkg/meta/compaction_pack_kaz_test.go`

**Interfaces:**
- Consumes: Task 4 の `onDead`、Task 5 の `planKazPack`
- Produces:
  - `type KazPackResult struct { Packed []uint64; Dead []uint64; Bytes uint32; Reason string }`（`Packed` はまとめた id、`Dead` はそのうち参照が 0 になった id、`Reason` はまとめなかった理由。Task 3 の event 名で `skip_busy`、`skip_trash`、`skip_engine`、`skip_not_in_chunk`、`fail_put`、`fail_replace`）
  - `type KazStagingPacker interface { KazPackChunk(inode Ino, indx uint32, cands []uint64, data map[uint64][]byte, write func(id uint64, data []byte) error) KazPackResult }`
  - `func (m *baseMeta) KazPackChunk(...) KazPackResult`
    1. `kazDeltaActive()` と `kazDeltaUsable()` が偽 → `skip_engine`。`m.toTrash(0)` が真 → `skip_trash`。
    2. `m.compacting[k]` が真 → `skip_busy`（待たない。`len(m.compacting) > 10` の制限は使わない）。取れたら `defer` で外す。
    3. `doRead` → `planKazPack(ss, cands, blockSize)`。nil → `skip_not_in_chunk`。
    4. `NewSlice` で id を取り、`outs` の順に `data[srcID][srcOff : srcOff+length]` を連結して `write(id, buf)`。失敗 → `m.deleteSlice(id, size)`、`fail_put`。
    5. `origin` = `ss` 全体、`replaced` = `kept` の後に `outs`（`marshalSlice(pos, id, size, 累積の off, length)`）、`created` = `[]kazCreated{{Slice{Id: id, Size: size, Len: size}, len(outs)}}`、`onDead` で `Dead` を集める。0 以外 → `fail_replace`（EINVAL のときの参照の扱いはエンジンの中で済む）。
    6. Debug ログ `staging pack inode=%d chunk=%d slice=%d slices=%d bytes=%d dead=%d`。

- [ ] **Step 1: 失敗するテストを書く**（`kazBlockBackends` で、delta モード、trash 0）
  - `TestKazPackChunkView`: 4 KiB の書き込みを 5 つ（うち 1 つは別の書き込みに一部を隠される）→ 5 つの id を cands に `KazPackChunk`。成功し、`buildSlice` の見え方が前と同じ。新しい slice の参照の数は `outs` の数。`doCleanupSlices` で消える id は `Dead` と同じ。
  - `TestKazPackChunkConcurrentWrite`: `write` の中で同じ chunk に書き込みを足す → 置き換えは成功し、足した書き込みが一番上に見える（I3）。
  - `TestKazPackChunkChanged`: `write` の中で、候補の record を外す（別の置き換え）→ `fail_replace`、新しい slice は削除の対象になる。
  - `TestKazPackChunkTrash`: `TrashDays = 1` → `skip_trash`、`write` は呼ばれない。
  - `TestKazPackChunkBusy`: `compacting[k] = true` → `skip_busy`。
  - memkv では `skip_engine`。
  - `TestKazPackChunkUpstreamRead`: まとめた後の chunk を upstream の読み方（`Read` → `buildSlice`）で読み、書いたバイト列と同じ。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `... go test ./pkg/meta/ -run 'TestKazPackChunk' -count=1`（redis は `REDIS_ADDR` がある場合。既存の kaz のテストと同じ）
  Expected: FAIL
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 走らせて通ることを確かめ、`make test.meta.core` を流す**
  Expected: PASS
- [ ] **Step 5: commit**（許可がある場合）`feat(meta): pack staged slices of one chunk into one slice (KazPackChunk)`

### Task 7: vfs のつなぎ（対応表、packer、登録）

**Files:**
- Create: `pkg/vfs/staging_pack_kaz.go`、`pkg/vfs/staging_pack_kaz_test.go`
- Modify: `pkg/vfs/writer_kaz.go`（`commitSlices` の最後）、`pkg/vfs/vfs.go`（`NewVFS` の return の前に `v.kazInitStagingPack()`、`// kaz:`）、`pkg/vfs/config_kaz.go`（`KazStagingPack bool`）

**Interfaces:**
- Consumes: Task 3 の `KazSetStagingPacker`／`KazClaimStaged`／`KazReleaseStaged`／`KazStagingPackEvent`、Task 6 の `meta.KazStagingPacker`
- Produces:
  - `type kazPackIndex struct`（mutex、`byID map[uint64]kazPackLoc`、`byChunk map[uint64]map[uint64]int`（chunk の key → id → 長さ）、上限 `kazPackIndexMax = 1 << 18`、あふれたら一番古いものを捨てる）
  - `type kazPackLoc struct { inode Ino; indx uint32; length int }`
  - `func (w *dataWriter) kazRememberStaged(inode Ino, indx uint32, s *sliceWriter)`: `commitSlices` の後、`end` までの slice で、writeback が有効、`s.length < BlockSize` のものを表に入れる。
  - `func (v *VFS) kazInitStagingPack()`: `conf.KazStagingPack` が真で、store が `KazSetStagingPacker` を持ち、meta が `meta.KazStagingPacker` を持つときだけ登録する。
  - `func (v *VFS) kazPackStaged(id uint64, length int) bool`
    1. 表に無い → `skip_unknown`、false。
    2. 同じ chunk の id を、`id` を先頭に、残りを id の昇順で、長さの合計が BlockSize 以下になるまで集める。
    3. `KazClaimStaged`。`absent` は表から消す。押さえた中に `id` が無いか、2 つ未満 → 全部 `KazReleaseStaged(ids, nil)`、`skip_no_peer`、false。
    4. `KazPackChunk(inode, indx, ids, data, write)`。`write` は `store.NewWriter(newID, tier)` → `SetWriteback(false)` → `WriteAt(data, 0)` → `Finish(len(data))`（失敗なら `Abort`）。
    5. `KazReleaseStaged(押さえた全部, Dead の集合)`。`Packed` の id を表から消す。event を数える。
    6. `id` が `Dead` にあれば true（取り出した block は PUT しない）、無ければ false（そのまま PUT する）。

- [ ] **Step 1: 失敗するテストを書く**（`newKazDeltaTestVFS(t, true)` を土台に、`MaxUpload: 1`、PUT を止められる object storage のラッパー）
  - `TestKazStagingPackReadBack`: PUT を止めた状態で、同じ chunk のばらばらな位置に 4 KiB を 6 回書いて fsync する → PUT を流す → PUT の回数が 6 より少ない（`packed` の event が 1 以上）、全部の範囲を読み直すと書いた内容と同じ。
  - `TestKazStagingPackUnknownUploads`: 表に無い slice（再起動の後と同じ形。表を空にする）→ そのまま PUT される。
  - `TestKazStagingPackSharedSliceUploads`: まとめる前に、1 つの slice を copy_file_range で別のファイルにも指させる → まとめの後、その slice の object が PUT される（object storage にある）。読み直しが両方のファイルで正しい。
  - `TestKazStagingPackWithSkipDelete`: 両方のフラグを有効にした上で、まとめの後に GC を流す → まとめた古い slice への DELETE は 0 回、object storage に参照の無い object が残らない（`juicefs gc` と同じ照合を、テストの中で key の一覧と record の比較で行う）。
  - `TestKazStagingPackDisabled`: `KazStagingPack = false` → PUT の回数は書いた slice の数と同じ。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp go test ./pkg/vfs/ -run 'TestKazStagingPack' -count=1 -race`
  Expected: FAIL
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 走らせて通ることを確かめ、vfs の全体を流す**
  Run: 同じコマンド、続けて `... go test ./pkg/vfs/ -count=1`
  Expected: PASS
- [ ] **Step 5: commit**（許可がある場合）`feat(vfs): pack staged slices of one chunk at upload time (--kaz-staging-pack)`

### Task 8: フラグ、設定、文書

**Files:**
- Modify: `cmd/kaz_flags.go`（mount の kaz のフラグの並びに 2 つ）、`cmd/kaz_vfs_flags.go`（`applyKazVfsFlags`）、`cmd/kaz_cache_flags.go`（`KazSkipUnuploadedDelete`）、`docs/en/reference/kaz_options.md`、`docs/ja/reference/kaz_options.md`、`docs/zh_cn/reference/kaz_options.md`
- Test: `cmd/kaz_flags_test.go`

**Interfaces:**
- Consumes: Task 2 の `KazChunkConfig.KazSkipUnuploadedDelete`、Task 7 の `KazVfsConfig.KazStagingPack`
- Produces:
  - `--kaz-staging-pack` の Usage（英語）: `[kaz] when writeback uploads are queued, upload the small staged slices of one chunk waiting in the queue as one object and point their records into it (needs --kaz-compaction-mode delta and a Redis or SQL metadata engine; skipped while the trash is enabled; uploads are never delayed for it; do not run reflink copies from unmodified clients while it runs)`
  - `--kaz-skip-unuploaded-delete` の Usage: `[kaz] do not send DELETE for a staged block that this process never started to upload when its slice is removed; blocks staged before a restart, blocks with any upload attempt and direct uploads are still deleted`
  - `applyKazVfsFlags`: `--kaz-staging-pack` が真で `--kaz-compaction-mode` が delta でなければ、`logger.Warnf` して false にする。
- [ ] **Step 1: 失敗するテストを書く**
  - `TestKazStagingPackFlag`: `--kaz-staging-pack --kaz-compaction-mode delta` → `cfg.KazStagingPack == true`、mode が upstream → false。
  - `TestKazSkipUnuploadedDeleteFlag`: chunk の Config に写る。既定は false。
  - 既存の `kaz_flag_names_test.go` の「kaz のフラグは `kaz-` で始まる」のテストが、新しいフラグでも通ること。
- [ ] **Step 2: 走らせて失敗を確かめる**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp HOME=$(mktemp -d -p /home/kwatanabe/tmp_local/jfs142-gotmp) go test ./cmd/ -run 'TestKazStagingPackFlag|TestKazSkipUnuploadedDeleteFlag|TestKazFlag' -count=1`（HOME を一時ディレクトリにして、テストのキャッシュを本番の場所に残さない）
  Expected: FAIL
- [ ] **Step 3: 実装し、`kaz_options.md` の 3 つの言語に 1 行ずつ足す**（ja は日本語、zh_cn は既存の行と同じく英語のままでよい）
- [ ] **Step 4: 走らせて通ることを確かめ、`go build ./...` と `gofmt -l pkg cmd` が空であることを確かめる**
- [ ] **Step 5: commit**（許可がある場合）`feat(cmd): add --kaz-staging-pack and --kaz-skip-unuploaded-delete`

### Task 9: 全体の確かめと suite の設定

**Files:**
- Modify（調査リポジトリ、commit しない）: `tests/juicefs/settings.toml`

- [ ] **Step 1: 本体の全体のテストを流す**
  Run: `GOENV_VERSION=1.25.11 TMPDIR=/home/kwatanabe/tmp_local/jfs142-gotmp make test.meta.core` と `... go test ./pkg/chunk/ ./pkg/vfs/ -count=1`
  Expected: 新しい失敗が無い（既知の DATA RACE を除く）
- [ ] **Step 2: settings.toml に 3 つの設定を足す**（どれも `mode = "delta"`、`kaz-compaction-delta-trigger = 30`、`kaz-compaction-delta-block-ratio = 0.5`）
  - `pack`: `"kaz-staging-pack" = true`
  - `skipdel`: `"kaz-skip-unuploaded-delete" = true`
  - `pack-skipdel`: 両方 true
  - suite の flags の置き換えが `[compaction]` の印の行だけを対象にしているなら、印の無い新しいフラグを足せるかを `tests/juicefs/lib` で確かめる。足せなければ、`mount_flags.txt` に 2 行（`[compaction]` の印つき、既定 false）を足す。
- [ ] **Step 3: ビルドしてユーザーに渡す**: `make juicefs` の出力を `~/.local/bin/kaz-test/juicefs-pack-<commit>` に置き、sha256 を記録する。suite の実行（本物の Drive に PUT する、約 1 時間×設定）は、ユーザーの確認の後で `jfsreplay suite --only trigger30,pack,skipdel,pack-skipdel --repeat 2` を流す。
- [ ] **Step 4: 結果を調査リポジトリに書く**: `docs/inspection/staging_pack/<日付>/` に比較表（PUT、DELETE、`kaz_staging_pack_total`、`kaz_delete_skipped_total`、compaction の出力、meta ops、`juicefs gc` の leaked）、`docs/inspection/README.md`、`findings.md`、`TODO.md`、`agent_memo.md` を更新する。
