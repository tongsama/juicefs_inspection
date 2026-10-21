# 差分 slice 方式の compaction（kaz delta compaction）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--kaz-compaction-mode=delta` を足し、VM のディスクのように小さい書き込みが chunk に散らばるファイルでも、compaction の PUT を「新しく書いた分」程度に抑える。

**Architecture:** 今の block compaction の流れ（`runKazBlockCompaction` → 計画 → `writeKazBlocks` → `doKazReplaceChunk`）はそのまま使い、計画の関数を差分用に足す。差分の段は 1 つの slice に詰め、複数の record（同じ id・別々の `off/len`）で指す。置き換えでは、その slice の参照の数を record の数に合わせる。起動の条件は、`afterWrite` の hook で「前の計画の後に来た record の数」に替える。判断はすべて kaz のファイルに置く。upstream 由来のファイルは `base.go`（`afterWrite` の数行と baseMeta の項目 1 つ）、`config.go`（項目 4 つ）、`cmd/flags.go`（フラグ 4 つ＋help）、`pkg/chunk/cached_store.go`（条件 1 か所と Config の項目 1 つ）だけを変える。

**Tech Stack:** Go（module `github.com/juicedata/juicefs`）、testify、Redis 8（テストは一時的な Redis）、SQLite、memkv。

**Spec:** `docs/superpowers/specs/2026-10-20-kaz-delta-compaction-design.md`（このリポジトリ）。実装する人は設計書と、この計画の両方を読むこと。

## Global Constraints

- 本体のリポジトリは `/home/kwatanabe/tmp_local/juicefs_inspection/juicefs`。ブランチ `feat/kaz-delta-compaction` を `1.4.1-improve-kaz`（c5c5658b）から作る。push と merge はしない。
- **commit はユーザーの許可を得てから。** 許可が無ければ、各 Task の「Commit」の手順は飛ばし、変更を残したまま次に進む。Co-Authored-By の行は付けない。
- 新しい `.go` ファイルは Apache 2.0 のヘッダ（`JuiceFS, Copyright 2026 Juicedata, Inc.`）で始める。新しい・変更する関数と型には、目的が分かる英語のコメントを付ける（今の kaz のファイルと同じ書き方）。
- 判断は `*_kaz.go` に置く。upstream 由来のファイルの変更は、Architecture に挙げた場所だけにする。
- メタデータの形式（record の 24 バイト、参照数のキー）と、新しい種類の key は増やさない。
- 既定値（設計書 §5.10 の表と同じ）: mode の既定は upstream。`delta-tiers` は 8（1〜16）、`delta-max-records` は 500（50〜800）、`delta-gap` は 16 KiB（0〜1 MiB）、`delta-min-file-size` は 0（0 以上）。
- 固定値: `kazDeltaTrigger = 99`、record の上限を下回らせる余裕 `kazDeltaFoldMargin = 50`、基準の数の表の上限 `kazDeltaBaseCap = 65536`、丸ごと取る object の大きさ `kazWholeReadMax = 1 << 20`。
- 正しさの不変条件（設計書 §5.4）: (I1) 置き換えの前後で `buildSlice` の見え方が同じ。(I2) 外す record の範囲は outs で覆う。(I3) outs は kept の上。(I4) 段の slice の参照の数は、それを指す record の数。
- テストの実行: `go test ./pkg/meta/ -run '<名前>' -race -count=1`。Redis は一時的な Redis を立て（`redis-server --port 6379 --save '' --appendonly no` を scratch のディレクトリで起動し、終わったら止める）、`JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/<db>` を付ける。本番の Redis には繋がない。
- 全体の確認（最後の Task）: `make test.meta.core`、kaz と GC のテスト（Redis あり、`-race`）、`go test ./pkg/chunk/ ./pkg/vfs/ -run Kaz -race`、`go test ./cmd/ -run Kaz`、`go vet ./pkg/meta/ ./pkg/chunk/ ./pkg/vfs/ ./cmd/`、`gofmt -l`。

## Review Focus

1. **ブロックの境目をまたぐ小さい書き込み**（例: pos = 4 MiB − 2 KiB、len 4 KiB）で、片方のブロックだけが書き直しになる場合。見え方が変わらず、record が二重に外れないこと。→ Task 3 の `TestPlanKazDeltaSpansFoldedBlock`。
2. **compaction の最中に同じ chunk へ書き込みが来る場合。** EINVAL にならず末尾に残るか、EINVAL なら段の slice が消え、基準の数が更新されないこと。→ Task 4 の `TestKazDeltaCompactionConcurrentCommit`。
3. **段の record の 1 つだけが後の書き込みで完全に隠れた場合。** 次の compaction でその record だけが外れ、slice は残りの record で生き続けること（参照の数が N−1）。全部外れたら消えること。→ Task 4 の `TestKazDeltaPartialRelease`。
4. **ファイルを truncate で縮めた後に、差分のある chunk を compaction する場合**（id 0 の record が段より上に来る）。見え方が 0 のままであること。→ Task 3 の property test に truncate を混ぜる。Task 7 の `TestKazDeltaChunkOps`。
5. **mode=delta で mount したまま、他の client（block モード）が同じ chunk を書き直した場合**（基準の数より record が減る）。基準の数を捨てて今の条件に戻ること。→ Task 5 の `TestKazDeltaWantedForgetsShrunk`。

---

### Task 1: ノブ（Config・検証・フラグ）

**Files:**
- Modify: `pkg/meta/config.go`（`KazCompactionBlockMaxSlices` の下に 4 項目を足す。`SelfCheck` の block のリセットに 4 項目も加える）
- Create: `pkg/meta/compaction_delta_config_kaz.go`
- Modify: `pkg/meta/compaction_block_config_kaz.go`（`ValidateKazBlockCompaction` の mode に `delta` を足し、delta の検証を呼ぶ）
- Modify: `cmd/flags.go`（4 フラグ。`kaz-compaction-mode` の help に delta を足す）
- Modify: `cmd/kaz_compaction_policy.go`（`kazBlockCompactionFromFlags` の引数に delta の 4 値）
- Test: `pkg/meta/compaction_delta_config_kaz_test.go`、`cmd/kaz_compaction_policy_flag_test.go`

**Interfaces:**
- Produces:
  - `Config` の項目 `KazCompactionDeltaTiers int`、`KazCompactionDeltaMaxRecords int`、`KazCompactionDeltaGap int`（バイト）、`KazCompactionDeltaMinFileSize uint64`（バイト）。どれも 0 は既定値を意味する。
  - `const KazCompactionModeDelta = "delta"`
  - `func (c *Config) kazDeltaEnabled() bool`（mode が delta）
  - `type kazDeltaConf struct { tiers, maxRecords, trigger int; gap uint32; hiddenRatio float64; minFileSize uint64 }`
  - `func (c *Config) kazDeltaConf() kazDeltaConf`（0 を既定値にし、trigger は `kazDeltaTrigger`、hiddenRatio は `c.kazBlockHiddenRatio()`）
  - `func (c *Config) validateKazDelta() error`
  - 定数 `kazDeltaTrigger = 99`、`kazDeltaFoldMargin = 50`、`kazDeltaBaseCap = 65536`
  - cmd のフラグ `kaz-compaction-delta-tiers`（Int、既定 8）、`kaz-compaction-delta-max-records`（Int、既定 500）、`kaz-compaction-delta-gap`（String、既定 `16K`、`utils.ParseBytes` と同じ書き方）、`kaz-compaction-delta-min-file-size`（String、既定 `0`）

- [ ] **Step 1: テストを書く**

`TestKazDeltaConfigDefaults`: `Config{KazCompactionMode: "delta"}.kazDeltaConf()` が `{tiers: 8, maxRecords: 500, trigger: 99, gap: 16384, hiddenRatio: 0.5, minFileSize: 0}`。

`TestKazDeltaConfigValidate`（表のテスト）: tiers 0・1・16 は ok、17 は err。max-records 0・50・800 は ok、49・801 は err。gap 0・1<<20 は ok、1<<20+1 と −1 は err。mode `"Delta"` は err。mode が block のときは delta の値を検証しない（tiers 99 でも ok）。

`TestKazDeltaConfigSelfCheckResets`: tiers 99 の delta の Config に `SelfCheck()` をかけると、mode が `""`、4 項目が 0 に戻る。

cmd の `TestKazDeltaFlags`: `--kaz-compaction-mode delta --kaz-compaction-delta-tiers 4 --kaz-compaction-delta-max-records 300 --kaz-compaction-delta-gap 4K --kaz-compaction-delta-min-file-size 1G` が `4, 300, 4096, 1<<30` になる。tiers 0 は `kazBlockCompactionFromFlags` が err を返す（コマンドラインでは 0 を範囲外とする。read-min と同じ扱い）。

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./pkg/meta/ -run 'TestKazDeltaConfig' -count=1` と `go test ./cmd/ -run TestKazDeltaFlags -count=1`
Expected: FAIL（未定義）

- [ ] **Step 3: 実装する**

`compaction_delta_config_kaz.go` に、上の定数・型・関数を置く。help の文言は設計書 §5.10 の「意味」を英語にし、先頭を `[kaz] with --kaz-compaction-mode delta, …` にする。`kaz-compaction-mode` の help には、`delta (like block, but scattered small changes over existing blocks are packed into one small slice per compaction; see --kaz-compaction-delta-*)` を足す。`kaz-compaction-delta-tiers` の help の末尾に、`clients without delta mode (upstream or block) still read and write these chunks correctly but rewrite the touched blocks or the whole chunk` を足す（設計書 §5.12）。

- [ ] **Step 4: 通ることを確かめる**

Run: Step 2 と同じ。Expected: PASS。`go test ./pkg/meta/ -run 'TestKazBlock.*Config|TestKazCompactionPolicyConfig' -count=1` も PASS（既存）。

- [ ] **Step 5: Commit**（許可がある場合）

```bash
git add pkg/meta/config.go pkg/meta/compaction_delta_config_kaz.go pkg/meta/compaction_delta_config_kaz_test.go pkg/meta/compaction_block_config_kaz.go cmd/flags.go cmd/kaz_compaction_policy.go cmd/kaz_compaction_policy_flag_test.go
git commit -m "feat(kaz): add delta compaction mode options"
```

---

### Task 2: 置き換えで作った slice の参照の数を N にする（3 エンジン）

**Files:**
- Modify: `pkg/meta/compaction_block_kaz.go`（interface と、`runKazBlockCompaction`・`writeKazBlocks` の呼び出し側）
- Modify: `pkg/meta/redis_compact_kaz.go`、`pkg/meta/sql_compact_kaz.go`、`pkg/meta/tkv_compact_kaz.go`
- Test: `pkg/meta/compaction_block_replace_kaz_test.go`（既存のテストは新しい型に合わせて直す）

**Interfaces:**
- Produces:
  - `type kazCreated struct { Slice; Refs int }`（Refs は 1 以上）
  - `doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []kazCreated, delayed []byte, delayedKey uint64) syscall.Errno`
  - `type kazDeltaCapable interface{ kazDeltaCapable() bool }`。redisMeta と dbMeta が true を返して実装する。kvMeta は実装しない。
  - `func kazDedupDeletes(removed []*slice, dead func(*slice) bool) []*slice`（死んだ record を id ごとに 1 つにまとめる。SQL・TKV の通知で使う）

- [ ] **Step 1: テストを書く**

`TestKazReplaceChunkSharedRefs`（`kazBlockBackends` の 3 つ）:
1. base（0, 8192）の上に、小さい record を 3 つ（100/50、3000/50、6000/50）書く。
2. 新しい slice c（size 150）を 1 つ作り、3 つの record `marshalSlice(100, c, 150, 0, 50)`、`(3000, c, 150, 50, 50)`、`(6000, c, 150, 100, 50)` で置き換える（`created = []kazCreated{{Slice{Id: c, Size: 150, Len: 150}, 3}}`）。
3. 置き換えの後、c の record を 1 つ外す置き換えをする（`removed` にその record、`created` は nil）。`doCleanupSlices` の後も `deletedIDs()` に c が無い。
4. もう 1 つ外しても c は無い。最後の 1 つを外すと `deletedIDs()[c] == 1`（通知も DELETE も 1 回）。

`TestKazReplaceChunkSharedRefsChanged`: 古い origin で Refs 3 の c を置き換えると EINVAL。`doCleanupSlices` の後に `deletedIDs()[c] == 1`。

`TestKazReplaceChunkSharedRefsDelayed`: trash（delayed）のとき、3 record を外しても期限まで消えない。期限の後に c が 1 回だけ消える。

`TestKazDeltaCapable`: redis と sqlite は `kazDeltaCapable()` が true、memkv は interface を実装しない。

既存の 3 つの replace のテストは、`[]kazCreated{{Slice{…}, 1}}` に書き換え、期待値は変えない。

- [ ] **Step 2: 失敗を確かめる**

Run: `JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/1 go test ./pkg/meta/ -run 'TestKazReplaceChunk|TestKazDeltaCapable' -race -count=1`
Expected: FAIL（コンパイルできない）

- [ ] **Step 3: 実装する**

- 参照の key を作る値は次のとおり（設計書 §5.6）。
  - Redis: `HSet(..., strconv.Itoa(c.Refs-1))`
  - SQL: `mustInsert(s, sliceRef{c.Id, c.Size, c.Refs})`
  - TKV: `packCounter(int64(c.Refs-1))`
- 成功の後の `cleanupZeroRef` は今のまま呼んでよい（値 0 のときだけ消すことを Redis と TKV で確認済み）。EINVAL のときの −1 と、「分からない」ときの `created[0]` の確かめ方は変えない。
- 外す record の減算は record ごとのまま。
- SQL と TKV の通知は、`kazDedupDeletes` で id ごとに 1 回にする。Redis は MULTI の結果が `< 0` の record だけなので、今のままでよい。
- 呼び出し側: `writeKazBlocks` の戻り値を `[]kazCreated` にする。block の出力は Refs 1。失敗のときの `m.deleteSlice` と、`delayedKey = created[0].Id` はそのまま使う。

- [ ] **Step 4: 通ることを確かめる**

Run: Step 2 と同じ＋`-run 'TestKazBlock|TestKazForced|TestKazReplace'`
Expected: PASS（memkv・sqlite・redis）

- [ ] **Step 5: Commit**（許可がある場合）

```bash
git add pkg/meta/compaction_block_kaz.go pkg/meta/redis_compact_kaz.go pkg/meta/sql_compact_kaz.go pkg/meta/tkv_compact_kaz.go pkg/meta/compaction_block_replace_kaz_test.go
git commit -m "feat(kaz): let a replacement give a new slice several references"
```

---

### Task 3: 差分の計画の関数（エンジン不要）

**Files:**
- Modify: `pkg/meta/compaction_block_plan_kaz.go`（`kazBlockOut` に `delta bool` を足す。`kazBlockPlan` に `event string` を足す）
- Create: `pkg/meta/compaction_delta_plan_kaz.go`
- Test: `pkg/meta/compaction_delta_plan_kaz_test.go`（`kazFakeStore` と、delta に対応させた `applyKazPlan` を使う）
- Modify: `pkg/meta/compaction_block_kaz_helpers_test.go`（`applyKazPlan` が `delta` の outs を 1 つの slice に詰め、`off` を累積で振る）

**Interfaces:**
- Consumes: Task 1 の `kazDeltaConf`
- Produces:
  - `kazBlockOut.delta bool`: true の outs は、計画の中で 1 つの段の slice を共有する。位置順に連結し、record は `marshalSlice(o.pos, id, size, offInSlice, o.length)` になる。`size` は delta の outs の長さの合計。
  - `kazBlockPlan.event string`: `"delta_tier"`、`"delta_merge"`、`"delta_fold"`、`"delta_prune"`（隠れた record を外すだけ）、`"delta_block_fallback"`。block の計画では空。
  - `type kazRecKind uint8`。定数は `kazRecBase`、`kazRecZero`、`kazRecDelta`、`kazRecNew`。
  - `func kazClassify(ss []*slice, g kazBlockGeom) []kazRecKind`（設計書 §5.2）
  - `func planKazDelta(ss []*slice, blockSize uint32, c kazDeltaConf) (plan *kazBlockPlan, base int)`。plan は nil なら noop。base は、noop のとき「len(ss) − 新しい record の数」、計画があるとき `len(plan.kept) + len(plan.outs)`（commit が成功したときの基準の数。Task 4 が使う）。

- [ ] **Step 1: 分類と小さい場面のテストを書く**

（ブロックは 4 KiB。`kazEB` を使う）

- `TestKazClassify`: id 0 は Zero。同じ id が 2 record あれば両方 Delta。`off=10` の record 1 つも Delta。4 KiB ちょうど（ブロックを埋める）は Base。100 バイトは New。
- `TestPlanKazDeltaNoop`: base 16 ブロックの上に New が 98 個なら nil で、base == 16。New が 99 個なら nil ではない。
- `TestPlanKazDeltaTier`: base 16 ブロックの上に、ブロック 2・5・11 に 100 バイトずつ New を 99 個。plan.event == `"delta_tier"`。outs は全部 delta で、書き直すブロックは無い。kept は base 16 個。`applyKazPlan` の後に見え方が同じ（I1）。
- `TestPlanKazDeltaGap`: 隙間 16 KiB 以下の 2 つの New は 1 つの out にまとまる。gap 0 なら 2 つ。
- `TestPlanKazDeltaHalfBlock`: 1 ブロックの中の New の和集合が 2 KiB 以上（4 KiB の半分）なら、そのブロックは書き直し（`delta == false`、長さ 4 KiB）。他のブロックは delta。
- `TestPlanKazDeltaNoBase`: 下に Base の無いブロック（追記の範囲）の New は書き直しになる。
- `TestPlanKazDeltaMerge`: tiers 2 のとき、段が 2 つある chunk に New が 99 個来たら、event は `"delta_merge"`。前の段の record も全部外れ、outs の delta の slice は 1 つ。
- `TestPlanKazDeltaFold`: max-records 60 のとき、置き換えの後の record が 60 を超える計画では、event が `"delta_fold"`。record が 10 以下（60 − 50）になるまで、差分の record の多いブロックから書き直しに加わる。
- `TestPlanKazDeltaPrune`: New が 0 で、完全に隠れた Base が 1 つあるとき、event は `"delta_prune"`、outs は空、removed はその record だけ。
- `TestPlanKazDeltaZeroKept`: Zero の record（100 バイト）は removed にも outs にも入らない（書き直すブロックの中だけにある場合を除く）。
- `TestPlanKazDeltaSpansFoldedBlock`（Review Focus 1）: ブロック 2 の末尾〜ブロック 3 の先頭をまたぐ New があり、ブロック 2 だけが半分以上の規則で書き直しになる場合。その New は removed に 1 回だけ入る。ブロック 3 の側は delta の out に入り、見え方が同じ。

- [ ] **Step 2: 性質のテストを書く**

`TestPlanKazDeltaProperty`:
- 種を固定した乱数で 200 回まわす。各回は、base 16 ブロックの上に、次を混ぜた 300 個の書き込みの列を作る。
  - 4〜64 KiB 相当の小さい書き込み（env ではバイトを 1/1024 に縮め、4〜64 バイト）
  - 4 KiB ちょうどの書き込み
  - ブロックをまたぐ書き込み
  - Zero の record
  - 末尾を超える追記
  - truncate（末尾を id 0 の record で覆う）
- 50 個ごとに `planKazDelta` → `applyKazPlan` を繰り返す。ノブ（tiers 1〜16、max-records 50〜200、gap 0〜64）も乱数にする。各回で次を確かめる。
  - (I1) `kazFakeStore.view` の前後のバイト列が同じ。
  - removed に同じ `*slice` が 2 回無い。
  - (I4) delta の out の数が、`applyKazPlan` で作った段の slice を指す record の数と同じ。
  - 置き換えの後の record の数 ≤ max-records（full に倒れたときを除く）。
  - 1 回の PUT のバイト（delta の outs の長さの合計＋書き直すブロックの長さ）≤「small の record（Delta と New）が触れたブロックの span の合計」（設計書 §5.4 の 6）。

- [ ] **Step 3: I/O パターンの比較のテストを書く**

`TestPlanKazDeltaPatternsVsBlock`。各パターンで、同じ書き込みの列に、次の 2 つを 99 個ごとに当てた場合の PUT のバイトの合計を比べる。
- delta: `planKazDelta`（既定のノブ）
- block: `planKazBlockCompaction(ss, bs, 0.5, 17)`

| パターン | 期待 |
|---|---|
| まばら（16 ブロックに 4〜8 バイトを乱数で 2,000 個） | delta ≤ block の 1/3 |
| 密（1 ブロックの全体を 64 バイトずつ順に書き直す） | delta ≤ block |
| 追記（末尾に 64 バイトずつ 2,000 個） | delta ≤ block |
| 大きい（4 KiB ちょうどを 200 個） | delta ≤ block |

block の側が full に倒れる回も、そのまま数える。

- [ ] **Step 4: 失敗を確かめる**

Run: `go test ./pkg/meta/ -run 'TestKazClassify|TestPlanKazDelta' -count=1`
Expected: FAIL（未定義）

- [ ] **Step 5: `kazClassify` と `planKazDelta` を実装する**

設計書 §5.4 の順に、次の手順で作る（ここに書いていない細部は実装する人が決める）。

1. g は `kazNewBlockGeom`。kinds は `kazClassify`。hidden は `kazFullyHidden`。
   - New の数は、kind が New で hidden でないもの。
   - 段の数は、hidden でない Delta の record の異なる id の数。
   - prunable は、hidden で id > 0 の record の数。
2. noop: New < trigger、かつ prunable == 0、かつ len(ss) ≤ maxRecords、かつ 段の数 ≤ tiers なら、`nil, len(ss) − New` を返す。
3. R（外す record）: hidden の全部と、New の全部。段の数 + 1 > tiers なら、Delta の全部も入れる（merge）。
4. U: R のうち hidden でない id > 0 の record の範囲 `[pos, pos+len)` の和集合。中身は見え方からコピーするので、見えているかどうかを数える必要はない。
5. ブロックの選択 F: U が触れるブロック b のうち、次のどちらかを満たすもの。
   - `|gapjoin(U ∪ 残す Delta の範囲, gap) ∩ span(b)| ≥ g.size/2`
   - b に kind Base で id > 0 の record が重ならない
6. F に入ったブロックについて:
   - その中だけにある record（kind を問わない）は R に加える。
   - ブロックをまたぐ record は、そのブロックが全部 F なら R に加え、それ以外は残す。
7. T: `gapjoin(U, gap)` から F のブロックの span を引き、ブロックの境目で切った範囲の列。
8. 上限: 残る record（len(ss) − |R|）＋ |F| ＋ |T| が maxRecords を超える場合は、F に入っていないブロックのうち「その中だけにある、残す Delta の record と T の範囲」が一番多いものを F に加え、6〜7 をやり直す。maxRecords − `kazDeltaFoldMargin` 以下になるまで繰り返す（event は delta_fold）。加えられるブロックが無ければ、`planKazBlocks(ss, bs, 0, maxRecords, true)` を返す。
9. 保証: delta のバイト（T の長さの合計＋F の span の合計）が、block のバイト（Delta と New の record（hidden を除く）が触れたブロックの span の合計）を超える場合は、`planKazBlocks(ss, bs, c.hiddenRatio, c.maxRecords, false)` を返す（event は delta_block_fallback）。
10. base の隠れの割合: 置き換えの後の record の見え方で、kind Base の id のデータのうち見えないものの割合が c.hiddenRatio を超えたら、`planKazBlocks(..., true)` を返す。
11. outs: F の各ブロックは `kazClipPieces(view, span)`（全部 hole なら、残す record が重なるときだけ mask）。その後に T の各範囲を `delta: true` で並べ、位置順にする。kept は R に入っていない record を元の順に並べる。
12. 返す base は `len(kept) + len(outs)`（commit が成功したときの基準の数）。

- [ ] **Step 6: 通ることを確かめる**

Run: `go test ./pkg/meta/ -run 'TestKazClassify|TestPlanKazDelta|TestPlanKazBlock' -race -count=1`
Expected: PASS（既存の block の計画のテストも PASS）

- [ ] **Step 7: Commit**（許可がある場合）

```bash
git add pkg/meta/compaction_block_plan_kaz.go pkg/meta/compaction_delta_plan_kaz.go pkg/meta/compaction_delta_plan_kaz_test.go pkg/meta/compaction_block_kaz_helpers_test.go
git commit -m "feat(kaz): plan delta compaction of scattered small writes"
```

---

### Task 4: 差分の compaction を動かす（書き込み・commit・振り分け・基準の数・metrics）

**Files:**
- Create: `pkg/meta/compaction_delta_kaz.go`
- Modify: `pkg/meta/compaction_block_kaz.go`（`runKazBlockCompaction` の計画の選び方、`writeKazBlocks` の delta の outs、ログの mode）
- Modify: `pkg/meta/base.go`（baseMeta に `kazDeltaBases kazDeltaBaseTable` を 1 つ足す。ゼロ値で使える型にする）
- Modify: `pkg/meta/compaction_policy_kaz.go`（metrics の help の event と kind の一覧に delta の値を足す）
- Test: `pkg/meta/compaction_delta_kaz_test.go`

**Interfaces:**
- Consumes: Task 2 の `kazCreated`・`kazDeltaCapable`、Task 3 の `planKazDelta`・`kazBlockOut.delta`・`kazBlockPlan.event`
- Produces:
  - `type kazDeltaBaseTable struct`（mutex、`map[uint64]int`、上限 `kazDeltaBaseCap`。あふれたら一番古いものを捨てる）
    - `get(inode Ino, indx uint32) (int, bool)`
    - `set(inode Ino, indx uint32, n int)`
    - `forget(inode Ino, indx uint32)`
  - `func (m *baseMeta) kazDeltaActive() bool`: mode が delta、`m.en` が `kazChunkReplacer` かつ `kazDeltaCapable` で true、block の大きさが使える、`kazBlockCompactLimit(bs) ≥ maxRecords + kazDeltaTrigger + kazDeltaFoldMargin`。
  - `logKazCompactionMode` は、delta のとき `kaz compaction mode: delta (tiers %d, max-records %d, gap %d, min-file-size %d)` を INFO で出す。使えないときは `kaz compaction mode: delta is not usable here (...); using block compaction` を WARN で出す。
  - metrics の event: `fallback_block` と、Task 3 の event の名前。amount: `delta_bytes`、`delta_records`、`delta_fold_blocks`。

- [ ] **Step 1: テストを書く**

`kazDeltaBackends = []string{"sqlite", "redis"}` を使い、env は `newKazBlockEnv` に mode delta を設定したもの。

- `TestKazDeltaCompactionTier`:
  1. base 16 ブロックの上に、ブロック 2・5・11 に小さい書き込みを 99 個。
  2. `kazRun` の後、PUT（`store.callCount()`）が 1 回で、見え方が同じ。
  3. 段の slice の record の数を N とする。そのうち 1 つを外す置き換えをしても、`doCleanupSlices` の後に段の slice が消えない。
  4. amount `delta_records` == N、event `delta_tier` == 1。
- `TestKazDeltaPartialRelease`（Review Focus 3）: 段の record の 1 つの範囲を新しい書き込みで完全に覆い、もう一度 compaction する。覆われた record だけが外れ、段の slice は残る。その後、段の全範囲を覆って compaction すると、段の slice が 1 回だけ消える。
- `TestKazDeltaCompactionConcurrentCommit`（Review Focus 2）: `store.during` で、PUT の最中に同じ chunk の先頭を変える書き込み（`doWrite` ではなく、先頭が変わる置き換え）を入れる。結果は EINVAL（wasted 1）、段の slice が消える、`kazDeltaBases.get` が前の値のまま。末尾に足すだけの書き込みなら成功し、その record が最後に残る。
- `TestKazDeltaBaseUpdated`: noop の後の get が「len(ss) − New」、成功の後の get が `len(kept)+len(outs)`。
- `TestKazDeltaFallbackLimit`: `maxCompactSlices` を一時的に 100（etcd と同じ）にした sqlite の env では、`kazDeltaActive()` が false（テストの終わりに値を戻す）。
- `TestKazDeltaFallbackTKV`: memkv の env で mode delta のとき、`kazDeltaActive()` が false。compaction は block の形の出力になり、metrics の `fallback_block` が増える。
- `TestKazDeltaMinFileSize`: min-file-size 1 MiB で、ファイルの長さが 64 KiB なら block の計画（outs が全部 delta ではない）。2 MiB に伸ばすと delta の計画。
- `TestKazDeltaForcedIsBlock`: delta の段がある chunk に `forcedCompactChunk` をかけると、全部ブロックの形・隠れ 0 になり、段の slice が消える。
- `TestKazDeltaLogs`: DEBUG の行に `mode=delta-tier` と、段の数・差分の record の数・段のバイト・書き直したブロックの数が出る。slow compaction の行は `mode=delta`（今の `TestKazBlockCompactionLogs` の形）。

- [ ] **Step 2: 失敗を確かめる**

Run: `JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/2 go test ./pkg/meta/ -run 'TestKazDelta' -race -count=1`
Expected: FAIL

- [ ] **Step 3: 実装する**

- `runKazBlockCompaction` で、`force == false` かつ `m.kazDeltaActive()` のときだけ delta にする。
  - min-file-size > 0 なら、計画の前に `GetAttr` で長さを見る。長さが足りなければ、今の block の計画を使う。
  - 計画が nil のとき、または commit が成功したときは、`kazDeltaBases.set` で基準の数を更新する。EINVAL などのときは更新しない。
- `writeKazBlocks`: delta の outs は、piece を位置順に連結して 1 つの id で書く（`CompactChunk` 1 回）。戻り値の `kazCreated` は、その slice（Size は長さの合計、Refs は delta の outs の数）を最後に置く。
- replaced を作るところで、delta の outs は `marshalSlice(o.pos, c.Id, c.Size, off, o.length)` にし、off を累積で進める。
- `backgroundCompactChunk` は今のまま `kazBlockReplacer` 経由で入る（delta の判定は `runKazBlockCompaction` の中）。mode delta は block と同じく `kazBlockReplacer` が replacer を返すように、`kazBlockCompactionEnabled` を「block か delta」にする。
- 使えないときの `fallback_block` は、`kazBlockReplacer` で mode が delta かつ `!kazDeltaActive()` のときに数える。

- [ ] **Step 4: 通ることを確かめる**

Run: Step 2 と同じ＋`-run 'TestKazBlock|TestKazForced|TestKazReplace|TestKazBackground'`
Expected: PASS

- [ ] **Step 5: Commit**（許可がある場合）

```bash
git add pkg/meta/compaction_delta_kaz.go pkg/meta/compaction_delta_kaz_test.go pkg/meta/compaction_block_kaz.go pkg/meta/base.go pkg/meta/compaction_policy_kaz.go
git commit -m "feat(kaz): run delta compaction through the block replacer"
```

---

### Task 5: 起動の条件の hook（write と read と priority の count）

**Files:**
- Modify: `pkg/meta/base.go`（`afterWrite` の `compactionWanted` の呼び出しと、`requestBackgroundCompaction` の count を hook の戻り値にする）
- Modify: `pkg/meta/compaction_delta_kaz.go`（hook）
- Modify: `pkg/meta/compaction_policy_kaz.go`（`requestCompactionFromRead` の先頭で delta の判定）
- Test: `pkg/meta/compaction_delta_trigger_kaz_test.go`

**Interfaces:**
- Consumes: Task 4 の `kazDeltaBases`・`kazDeltaActive`
- Produces:
  - `func (m *baseMeta) kazCompactionWanted(inode Ino, indx uint32, prev, now int) (bool, int)`
    - 戻り値の int は、priority の hint に渡す count。
    - delta が無効なら `compactionWanted(prev, now), now`。
    - `now >= maxSlices` なら `true, now`。
    - 基準の数 b を覚えていて `now >= b` なら、`now−b >= kazDeltaTrigger` のとき `true, now−b`、それ以外は `false, now−b`。
    - 覚えていないか、`now < b` なら、forget してから `compactionWanted(prev, now), now`。
  - `afterWrite` は次の形にする。

    ```go
    if want, count := m.kazCompactionWanted(inode, indx, prevSlices, ph.numSlices); want {
        if ph.numSlices < maxSlices {
            m.requestBackgroundCompaction(inode, indx, count, int(attr.Tier))
        } else { /* sync, unchanged */ }
    }
    ```

  - read の hook: `requestCompactionFromRead` は、delta が有効で基準の数 b を覚えていて `count >= b` のときは、`count−b < kazDeltaTrigger` なら要求を捨てる（policy の event `delta_below_trigger`）。そうでなければ count−b を count として先に進める。覚えていなければ今のまま。

- [ ] **Step 1: テストを書く**

- `TestKazDeltaWanted`（表のテスト。env 不要、baseMeta に Config を入れて呼ぶ）:
  - delta 無効で (98, 99) なら true, 99。
  - delta 有効・b=400 で (497, 498) なら false, 98。(498, 499) なら true, 99。
  - b 無しで (400, 401) なら true（350 超）。
  - (2499, 2500) なら b に関係なく true, 2500。
- `TestKazDeltaWantedForgetsShrunk`（Review Focus 5）: b=400 で now=120 なら、forget して `compactionWanted(119, 120)`（false）を返し、その後の get が false。
- `TestKazDeltaWriteTriggerThroughWrite`: sqlite の env の mode delta で、base を書いて 1 回 compaction（b を覚える）した後、`Write` で小さい書き込みを 98 個するまでは compaction が走らない（`store.callCount()` が変わらない）。99 個目で走る（`require.Eventually`）。
- `TestKazDeltaReadTrigger`: read-min 100 の policy つきで、b を覚えた chunk を `Read` する。新しい record が 98 個なら `delta_below_trigger` が増えて要求しない。99 個なら要求する。
- `TestKazDeltaSchedulerCount`: priority scheduler のとき、hint の count が `now−b` になる（scheduler の observer で count を受け取る。今の `TestKazBlockCompactionThroughScheduler` の形）。

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./pkg/meta/ -run 'TestKazDeltaWant|TestKazDelta.*Trigger|TestKazDeltaSchedulerCount' -race -count=1`
Expected: FAIL

- [ ] **Step 3: 実装する**

上の Interfaces のとおり。`base.go` の変更は `afterWrite` の数行だけにする。

- [ ] **Step 4: 通ることを確かめる**

Run: Step 2 と同じ＋`-run 'TestKazCompactionPolicy|TestCompactionScheduler|TestKazBlock'`、さらに `make test.meta.core` の `TestWrite` 系（`go test ./pkg/meta/ -run 'Write' -count=1`）
Expected: PASS

- [ ] **Step 5: Commit**（許可がある場合）

```bash
git add pkg/meta/base.go pkg/meta/compaction_delta_kaz.go pkg/meta/compaction_policy_kaz.go pkg/meta/compaction_delta_trigger_kaz_test.go
git commit -m "feat(kaz): trigger delta compaction by records since the last plan"
```

---

### Task 6: 小さい object を丸ごと取る（pkg/chunk）と mount のつなぎ

**Files:**
- Modify: `pkg/chunk/cached_store.go`（Config に `KazWholeReadMax int` を 1 つ足す。156 行の条件に `!s.store.kazWholeRead(blockSize)` を挟む）
- Modify: `pkg/chunk/cached_store_kaz.go`（`func (store *cachedStore) kazWholeRead(blockSize int) bool`）
- Modify: `cmd/mount.go` の `getChunkConf`、または `cmd/kaz_cache_flags.go` の `applyKazCacheFlags`（mode が delta なら `KazWholeReadMax = 1 << 20`）
- Test: `pkg/chunk/cached_store_kaz_test.go`、`cmd/kaz_compaction_policy_flag_test.go`

**Interfaces:**
- Produces: `chunk.Config.KazWholeReadMax int`（0 なら無効。今と同じ）
- `kazWholeRead(bs) = conf.KazWholeReadMax > 0 && bs <= conf.KazWholeReadMax`

- [ ] **Step 1: テストを書く**

- `TestKazWholeReadSmallObject`:
  - 準備: mem の object storage を、Get の回数と範囲（off, limit）を数える wrapper で包む。cache を有効（`CacheDir: t.TempDir()`）にし、`KazWholeReadMax = 1<<20` にする。
  - 64 KiB の slice を書き、cache を消す（`store.bcache` から remove するか、新しい store で同じ blob を読む）。
  - off 8 KiB から 4 KiB を読むと、Get が 1 回で、範囲は全体（off 0）。
  - もう一度 off 40 KiB から 4 KiB を読むと、Get は 0 回（cache から読む）。
  - 内容が書いたものと同じ。
- `TestKazWholeReadDisabled`: `KazWholeReadMax = 0` なら、同じ読み込みが range の Get（off 8 KiB）になる（今の動き）。
- `TestKazWholeReadLargeObject`: `KazWholeReadMax = 1<<20` でも、4 MiB の block の小さい読み込みは range の Get のまま。
- cmd の `TestKazDeltaChunkConf`: `--kaz-compaction-mode delta` のとき `getChunkConf` の `KazWholeReadMax == 1<<20`。block のときは 0。

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./pkg/chunk/ -run TestKazWholeRead -race -count=1`、`go test ./cmd/ -run TestKazDeltaChunkConf -count=1`
Expected: FAIL

- [ ] **Step 3: 実装する**

Interfaces のとおり。cache が無効な store（`!CacheEnabled()`）は、今の range の Get のままにする（条件の左側は変えない）。

- [ ] **Step 4: 通ることを確かめる**

Run: Step 2 と同じ＋`go test ./pkg/chunk/ -run 'Kaz|TestCachedStore' -race -count=1`
Expected: PASS

- [ ] **Step 5: Commit**（許可がある場合）

```bash
git add pkg/chunk/cached_store.go pkg/chunk/cached_store_kaz.go pkg/chunk/cached_store_kaz_test.go cmd/mount.go cmd/kaz_cache_flags.go cmd/kaz_compaction_policy_flag_test.go
git commit -m "feat(kaz): read small objects whole in delta compaction mode"
```

---

### Task 7: chunk の操作との組み合わせと、vfs のデータの確認

**Files:**
- Test: `pkg/meta/compaction_delta_ops_kaz_test.go`、`pkg/vfs/compact_delta_kaz_test.go`
- 実装の変更は、テストが見つけた不具合の修正だけにする。修正したら、どのテストで見つけたかを commit message に書く。

**Interfaces:**
- Consumes: Task 1〜6 のすべて

- [ ] **Step 1: テストを書く**

`TestKazDeltaChunkOps`（sqlite と redis）。どの場合も、差分の段（N ≥ 3）のある chunk を作ってから始める。

| 操作 | 確かめること |
|---|---|
| (a) ファイルを `Unlink` して `doCleanupSlices` | 段の slice が 1 回だけ消える。base も消える |
| (b) `Truncate` で段の途中まで縮め、compaction | 見え方が 0 で、参照が漏れない（Review Focus 4） |
| (c) upstream の `m.compactChunk(inode, 0, false, true, 0)` | 見え方が同じで、段の slice が 1 回だけ消える |
| (d) `Clone` で複製し、元を消す | 複製は読めて、段の slice は消えない。複製も消すと 1 回消える |
| (e) `CopyFileRange` で段の範囲を別のファイルへ写し、元を消す | 同上 |
| (f) JSON の `DumpMeta` → 新しい engine に `LoadMeta` | 段の slice の参照の数が N（その後に record を 1 つずつ外し、最後の 1 つで消えることで確かめる） |

`TestKazDeltaDataVFS`（`pkg/vfs`）:
- 今の `newKazBlockTestVFS` を delta 用に複製する。meta は `sqlite3://` の一時ファイル（memkv では delta が block に戻るため）にし、`KazWholeReadMax` を 1 MiB にする。writeback は false と true の両方。
- 16 ブロックの base の上に、ばらばらの 60 バイトの書き込みを 99 回以上（3 回の compaction ぶん）する。
- 毎回 `Read` で全バイトが期待どおりであることを確かめる。
- 段の slice が作られたこと（written の長さが 4 KiB 未満のもの）を確かめる。
- 再び Open して読んでも同じであることを確かめる。

- [ ] **Step 2: 実行して確かめる**

Run: `JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/3 go test ./pkg/meta/ -run TestKazDeltaChunkOps -race -count=1` と `go test ./pkg/vfs/ -run 'TestKazDeltaDataVFS|TestKazBlockCompactionDataVFS' -race -count=1`
Expected: PASS。FAIL なら systematic-debugging で原因を見つけて直し、そのテストを残す。

- [ ] **Step 3: 全体の確認**

Run（Global Constraints の「全体の確認」）:
- `make test.meta.core`
- kaz と GC のテスト（Redis あり）: `JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/4 go test ./pkg/meta/ -run 'Kaz|GC|Compaction' -race -count=1`
- `go test ./pkg/chunk/ ./pkg/vfs/ -run Kaz -race -count=1`
- `go test ./cmd/ -run Kaz -count=1`
- `go vet ./pkg/meta/ ./pkg/chunk/ ./pkg/vfs/ ./cmd/`
- `gofmt -l pkg cmd`

Expected:
- 全部 PASS。gofmt の出力は空。
- 環境が無いもの（MySQL、TiKV、PostgreSQL、etcd、KeyDB など）の失敗は、接続拒否であることをログで確かめて、報告に書く。
- `git diff --stat 1.4.1-improve-kaz -- $(git ls-files | grep -v _kaz)` で、upstream 由来のファイルの変更が Architecture に挙げた場所だけであることを確かめる。

- [ ] **Step 4: ビルド**

Run: `make juicefs`、`./juicefs version`、`./juicefs mount --help | grep kaz-compaction-delta`
Expected: ビルドが成功し、4 つのフラグの help が出る。

- [ ] **Step 5: Commit**（許可がある場合）

```bash
git add pkg/meta/compaction_delta_ops_kaz_test.go pkg/vfs/compact_delta_kaz_test.go
git commit -m "test(kaz): cover delta compaction with chunk operations and VFS data"
```

---

## 完了の後（この計画の外）

- 調査のリポジトリの記録: `agent_memo.md` と `TODO.md` に、ブランチ・HEAD・テストの結果・ビルドの sha256 を書く。`docs/findings.md` には、確定した知見だけを書く。
- 本番への投入（`--kaz-compaction-mode delta --kaz-compaction-delta-min-file-size 1G` で試す案）、push、merge、リリースは、ユーザーが判断する。
