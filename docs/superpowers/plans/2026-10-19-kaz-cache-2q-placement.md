# kaz-2q 追加: compaction の出力の扱いの選択と記録の追加 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** kaz-2q で、compaction の出力をどちらの区分に入れるかを `--kaz-cache-compaction-placement inherit|probation|replace` で選べるようにする。あわせて、追い出し済みリストのヒットを「追い出す前の区分」ごとに数え、区分の容量を由来別にも出す。

**Architecture:** 既存の kaz-2q の実装（`feat/kaz-cache-2q` bf9efe73）に足す。
- 判定は kaz のファイルに置く。
  - `pkg/chunk/disk_cache_kaz.go`：`kazCompactionPlacement` で区分を決める。
  - `pkg/chunk/cache_eviction_2q_kaz.go`：降格の印、追い出し済みリストの区分、metrics。
  - `pkg/chunk/cache_state_kaz.go`：管理ファイル version 2。
  - `pkg/vfs/compact_cache_kaz.go`：元のブロックが全部写されたかの計算。
- upstream 由来のファイルの変更は、`cmd/flags.go` のフラグ宣言だけにする。

**Tech Stack:** Go（`GOENV_VERSION=1.25.11`）、prometheus client、testify `require`。

**Spec:** [docs/superpowers/specs/2026-10-19-kaz-cache-2q-design.md](../specs/2026-10-19-kaz-cache-2q-design.md) の §8（§1〜7 は実装済み）

## Global Constraints

- 前の計画 [2026-10-19-kaz-cache-2q.md](2026-10-19-kaz-cache-2q.md) の Global Constraints をすべて引き継ぐ。主なものは次のとおり。
  - kaz-2q のときだけ効く。
  - 無改良版との互換を保つ。
  - upstream 由来のファイルには1行の hook だけを入れる。
  - staging は追い出さず、容量にも数えない。
  - commit はユーザーの許可済み。Co-Authored-By は付けない。push しない。
  - テストは `GOENV_VERSION=1.25.11 TMPDIR=$HOME/.cache/gotmp-kaz` で行う。新しいテストは `-race -count=3` で回す。既知の upstream の race（`disk_cache.go` の rawFull と checkFreeSpace、`cached_store.go:379` の upload の buf）は、スタックで新しいコードとは無関係と示せば許容する。
- 作業ブランチは `feat/kaz-cache-2q` のまま（HEAD bf9efe73 の上に積む）。
- オプションは `--kaz-cache-compaction-placement`。値は `inherit`（既定）、`probation`、`replace`。不正な値は Warn して `inherit` にし、mount は止めない。Usage の先頭に `[kaz]` を付ける。
- `inherit` の動きは今の版とまったく同じにする。どの値でも、元のブロックが一つもキャッシュに無ければ出力は入れない。atime は元の中で最も新しいものを引き継ぐ。
- 降格の印：降格で付け、昇格で外す。また降格したら、もう一度付ける。ghost_hit で常連用に入った項目には付けない。
- 追い出し済みリストの区分：0 = 不明、1 = 常連用に上がらなかった、2 = 降格を経た、3 = 常連用。
- metrics：
  - `kaz_cache_2q_events_total{event}` に `ghost_hit_demoted`／`ghost_hit_probation`／`ghost_hit_protected` を足す。ghost_hit を1回数えるたびに、区分に応じてこのどれか1つを数える。区分が不明なものは probation に数える。合計の `ghost_hit` はそのまま残す。
  - `kaz_cache_2q_bytes_by_origin{segment,origin}`（Gauge）を足す。複数の store で共有するので、差分で出す。
- 管理ファイル：version 2 では、項目に印の1バイト、追い出し済みリストの各項目に区分の1バイトを足す。version 1 も読む（印なし、区分は不明）。書くのは常に version 2。

## Review Focus

- **version 1 の管理ファイルを読む場合：** 本番には今、bf9efe73 が書いた version 1 のファイルがある。新しい版で mount したとき、区分と追い出し済みリストが失われずに引き継がれること（Task 2 のテスト）。
- **`inherit` の回帰：** 既存のテスト（`TestKazCompactionOutput*`、`TestKazCompactInheritsCache`）が、そのまま通ること（Task 1）。
- **由来別の Gauge のずれ：** 降格・昇格・staging の確定・reset・再走査・metrics の付け直しの後も、`bytes_by_origin` の合計が区分の容量と一致すること（Task 2 のテスト）。
- **`replace` で、元のブロックが出力の複数ブロックにまたがる場合：** 新入り用に入ること。範囲を覆うかの計算で、端のずれが起きないこと（Task 1 のテスト）。
- **ghost_hit の合計と区分別の和が一致すること**（Task 2 のテスト）。

---

### Task 1: 出力の扱いの選択（`--kaz-cache-compaction-placement`）

**Files:**
- Modify: `pkg/chunk/cached_store.go`（`Config` にフィールド1つ、`KazCacheCompactionPlacement string`）
- Modify: `pkg/chunk/cache_config_kaz.go`（値の検証）
- Modify: `cmd/flags.go`（フラグ宣言）、`cmd/kaz_cache_flags.go`（値を Config に移す）
- Modify: `pkg/chunk/cached_store_kaz.go`、`pkg/chunk/disk_cache_kaz.go`（区分の決め方）
- Modify: `pkg/vfs/compact_cache_kaz.go`（元のブロックが全部写されたか）
- Test: `pkg/chunk/cache_config_kaz_test.go`、`pkg/chunk/cached_store_kaz_test.go`、`pkg/vfs/compact_cache_kaz_test.go`

**Interfaces:**
- Produces:
  - `const KazPlacementInherit = "inherit"`、`KazPlacementProbation = "probation"`、`KazPlacementReplace = "replace"`
  - `KazBlockRef` に `Whole bool` を足す。その出力ブロックに、この元のブロックの全範囲が写されているかを表す。
  - `kazCompactionPlacement(outKey string, srcs []kazPlacementSource) bool`
    - `kazPlacementSource{key string; whole bool}` は既存の `srcKeys []string` を置き換える。
    - mode は cacheManager が Config から持つ。持たせ方はビルダーに任せる。

- [ ] **Step 1: 失敗するテストを書く**
  - `TestKaz2QConfigSelfCheck` に placement の項目を足す。`""` は `inherit` になる。`"bogus"` は Warn して `inherit` になる。3つの正しい値はそのまま残る。kaz-2q 以外では検証しない。
  - vfs `TestKazCompactionSourcesWhole`（BS = 4MiB）
    - piece が元のブロック全体（Off 0、Len 4MiB）なら、`Whole = true`。
    - Off 1MiB、Len 3MiB で、同じ元の残り（Off 0、Len 1MiB）が別の piece として同じ出力ブロックに入っていれば、合算で `Whole = true`。
    - Off 2MiB、Len 4MiB で、元のブロック 0 と 1 にまたがる場合は、どちらも `false`。
    - 元の slice の最後の短いブロック（SliceSize 5MiB、Indx 1、長さ 1MiB）を全部写した場合は、`true`。
  - chunk `TestKazCompactionPlacementModes`
    - 元が常連用1つ（`Whole = false`）と新入り用1つの組で、`inherit` なら常連用、`probation` なら新入り用、`replace` なら新入り用になる。
    - 常連用の元が `Whole = true` なら、`replace` は常連用になる。
    - 元が一つもキャッシュに無ければ、どの値でもキャッシュされない。
    - atime は、どの値でも元の中の最大値を引き継ぐ。
- [ ] **Step 2: 失敗を確認する**：`go test ./pkg/chunk -run 'TestKaz2QConfigSelfCheck|TestKazCompactionPlacementModes'` と `go test ./pkg/vfs -run TestKazCompactionSourcesWhole`
- [ ] **Step 3: 実装する**
  - 出力のブロックごとに、元のブロックへ写された範囲を piece をまたいで合算する。合算した範囲が `[0, 元のブロックの長さ)` を覆えば `Whole` とする。元のブロックの長さは `min(BS, SliceSize - Indx*BS)`。
  - mode に応じて区分を決める。
- [ ] **Step 4: 通ることを確認する**
  - 新しいテストを `-race -count=3` で回す。
  - 既存の `TestKazCompactionOutput*`、`TestKazCompactionSourcesMapping`、`TestKazCompactInheritsCache`、`TestCompact`、`TestKazBlockCompactionDataVFS` が PASS すること（`inherit` の回帰）。
  - `go test ./cmd -run Kaz`、`go build .`
- [ ] **Step 5: Commit**：`feat(kaz): select compaction output placement for kaz-2q`

### Task 2: 降格の印、追い出し済みリストの区分、由来別の容量、管理ファイル version 2

**Files:**
- Modify: `pkg/chunk/cache_eviction_2q_kaz.go`、`pkg/chunk/metrics_kaz.go`、`pkg/chunk/cache_state_kaz.go`
- Test: `pkg/chunk/cache_eviction_2q_kaz_test.go`、`pkg/chunk/cache_state_kaz_test.go`、`pkg/chunk/disk_cache_kaz_test.go`（metrics）

**Interfaces:**
- Consumes: Task 1 は不要（独立）。
- Produces:
  - `kazItem` と `kazSaved` に `demoted bool` を持たせる。
  - `kazGhost` に `prior kazGhostPrior`（uint8、0〜3）を持たせる。
  - `kazStateVersion = 2`。v1 も読む。
  - metrics `kaz_cache_2q_bytes_by_origin{segment,origin}` と、events `ghost_hit_demoted`／`ghost_hit_probation`／`ghost_hit_protected`。

- [ ] **Step 1: 失敗するテストを書く**
  - `TestKaz2QDemotedMark`
    - 降格した項目は印付きになる。昇格すると外れる。また降格すると付く。
    - 印付きのまま新入り用から追い出すと、追い出し済みリストの区分は 2 になる。
    - 印なしで新入り用から追い出すと 1、常連用から直接追い出すと 3 になる。
    - ghost_hit で入った項目は印なし。
  - `TestKaz2QGhostHitByPrior`：区分 1／2／3 のそれぞれから ghost_hit させると、対応する event が1ずつ増える。合計の `ghost_hit` は3つの和と等しい。
  - `TestKaz2QBytesByOrigin`：乱数の操作（add、get、staging の確定、降格、追い出し、remove、reset と再追加、metrics の付け直し）の後でも、segment ごとの `bytes_by_origin` の和が `kaz_cache_2q_bytes{segment}` と一致する。2つの store で共有しても合計が合う。
  - `TestKazCacheStateV2RoundTrip`：印と、追い出し済みリストの区分が、保存と読み込みで一致する。
  - `TestKazCacheStateReadsV1`
    - version 1 の形式で書いたファイル（テストの中で v1 のエンコードを組み立てる）を読むと、区分・atime・追い出し済みリストは引き継がれる。印はなし、追い出し済みリストの区分は 0 になる。
    - 区分 0 の ghost_hit は `ghost_hit_probation` に数える。
- [ ] **Step 2: 失敗を確認する**：`go test ./pkg/chunk -run 'TestKaz2QDemotedMark|TestKaz2QGhostHitByPrior|TestKaz2QBytesByOrigin|TestKazCacheStateV2RoundTrip|TestKazCacheStateReadsV1'`
- [ ] **Step 3: 実装する**：上の Interfaces と Global Constraints のとおり。v1 を読む処理は、v2 と同じ関数の中で version によって分岐させる。
- [ ] **Step 4: 通ることを確認する**：新しいテストを `-race -count=3` で回す。`go test ./pkg/chunk -run 'TestKaz'` と `go test ./pkg/chunk` が PASS し、`go vet ./pkg/chunk` も通ること。
- [ ] **Step 5: Commit**：`feat(kaz): record ghost hit origin by prior segment and cache bytes by origin`
