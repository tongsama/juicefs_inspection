# kaz-2q 追加: 出力の区分を割合で決める（A-c）と、読まれていない常連を先に降格する（B） 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--kaz-cache-compaction-placement share` と `--kaz-cache-compaction-placement-share-ratio`（A-c）、`--kaz-cache-demote-unread-first`（B）を足す。

**Architecture:** 既存の kaz-2q（`feat/kaz-cache-2q` 57b3de8a）に足す。判断は kaz のファイル（`pkg/chunk/cache_eviction_2q_kaz.go`、`disk_cache_kaz.go`、`cache_state_kaz.go`、`metrics_kaz.go`、`cache_config_kaz.go`、`pkg/vfs/compact_cache_kaz.go`、`cmd/kaz_cache_flags.go`）に置く。upstream 由来のファイルは `cmd/flags.go` のフラグ宣言と `Config` のフィールドだけ。

**Tech Stack:** Go（`GOENV_VERSION=1.25.11`）、prometheus client、testify。

**Spec:** [docs/superpowers/specs/2026-10-19-kaz-cache-2q-design.md](../specs/2026-10-19-kaz-cache-2q-design.md) の §9（§1〜8 は実装済み）

## Global Constraints

- 前の計画（[2026-10-19-kaz-cache-2q.md](2026-10-19-kaz-cache-2q.md)、[2026-10-19-kaz-cache-2q-placement.md](2026-10-19-kaz-cache-2q-placement.md)）の Global Constraints をすべて引き継ぐ（kaz-2q のときだけ効く、既定の動きを変えない、無改良版との互換、upstream 由来のファイルは hook のみ、staging は追い出さず数えない、Task ごとの commit は許可済み・Co-Authored-By なし・push なし、テストは `GOENV_VERSION=1.25.11 TMPDIR=$HOME/.cache/gotmp-kaz`、新しいテストは `-race -count=3`、既知の upstream の race はスタックで区別）。
- ブランチは `feat/kaz-cache-2q` のまま（57b3de8a の上）。
- `--kaz-cache-compaction-placement` に `share` を足す。既定は `inherit` のまま。
- `--kaz-cache-compaction-placement-share-ratio`: 既定 0.5、0 < x ≤ 1、範囲外は Warn して 0.5。`share` 以外では使わない。
- `share`: 出力ブロックごとに、穴以外のバイトを分母、常連用にある元のブロックから写されたバイト（piece をまたいだ和集合）を分子にする。比 ≥ ratio なら常連用、未満なら新入り用（新しい側）。分母 0 なら新入り用。元が一つもキャッシュに無ければキャッシュしない。atime は元の最大。
- `--kaz-cache-demote-unread-first`（真偽値、既定オフ）。
- 読まれた実績の印: ユーザーの読み込みによる昇格・ghost_hit での常連用入り・常連用にいる間のユーザーの読み込みで付く。降格で外れる。compaction の出力を常連用に入れるときは、常連用にある元のブロックのどれかに印があれば付ける。印はオン・オフに関係なく記録する。
- B オン: 降格は「印なしを atime の古い順」→「印ありを atime の古い順」。オフ: 今と同じ（atime の古い順）。
- metrics: events に `demote_unread`／`demote_read`（合計の `demote` は残す、和は一致、mount 直後から 0 で出す）。Gauge `kaz_cache_2q_protected_unread_bytes`（複数 store で差分の公開）。
- 管理ファイル version 3: 項目に印を保存。version 1・2 も読み、その項目はすべて印なしとする（実装後の見直し、spec §9.5）。書くのは常に version 3。

## Review Focus

- **v2 → v3 の更新:** 本番には 57b3de8a が書いた v2 のファイルがある。新しい版で区分・atime・ghost・降格の印が引き継がれ、読まれた実績の印がすべて印なしで読まれること（Task 2、実装後の見直し）。
- **既定（inherit、B オフ）の回帰:** 既存の kaz のテストがそのまま通ること（Task 1・2）。
- **印の変化と heap の順序:** B オンで、読まれたときに印が付いて heap の位置が正しく直ること。降格・昇格・ghost_hit・compaction の出力・reset・再起動の後も、印と heap と Gauge が一致すること（Task 2 の乱数の不変条件テスト）。
- **share の端:** 穴だけの出力、元が新入り用だけ、元が出力の複数ブロックにまたがる、短い最後のブロック（Task 1）。
- **降格の合計:** `demote_unread + demote_read == demote`（Task 2）。

---

### Task 1: A-c（`share` と share-ratio）

**Files:**
- Modify: `pkg/chunk/cached_store.go`（`Config` にフィールド 1 つ）、`pkg/chunk/cache_config_kaz.go`、`cmd/flags.go`、`cmd/kaz_cache_flags.go`
- Modify: `pkg/vfs/compact_cache_kaz.go`（`KazBlockRef` に、その出力ブロックに写された元ブロックのバイト数を足す）
- Modify: `pkg/chunk/disk_cache_kaz.go`（`kazCompactionPlacement` に `share` の判定）、必要なら `pkg/chunk/cached_store_kaz.go`
- Test: `pkg/chunk/cache_config_kaz_test.go`、`pkg/chunk/cached_store_kaz_test.go`、`pkg/vfs/compact_cache_kaz_test.go`、`cmd` の kaz フラグのテスト

**Interfaces:**
- Produces: `const KazPlacementShare = "share"`、`Config.KazCacheCompactionPlacementShareRatio float64`、`KazBlockRef.Bytes uint32`（その出力ブロックに写された、この元ブロックの和集合のバイト数）、`kazPlacementSource` に `bytes` を足す。

- [ ] **Step 1: 失敗するテストを書く**
  - config: `share` が通る。ratio の `0`／`-0.1`／`1.5` は Warn して 0.5。`1` と `0.3` はそのまま残る。kaz-2q 以外では検証しない。
  - vfs `TestKazCompactionSourcesBytes`: `Bytes` が piece をまたいだ和集合のバイト数になる（重なる piece、複数の出力ブロックにまたがる元ブロック、短い最後のブロック）。
  - chunk `TestKazCompactionPlacementShare`
    - 常連用の元 3 MiB と新入り用の元 1 MiB → ratio 0.5 で常連用、ratio 0.8 で新入り用（新しい側）。
    - 常連用の元が無い → 新入り用。
    - 元が一つもキャッシュに無い → キャッシュしない。
    - 穴だけ（分母 0）→ 新入り用。
    - atime は元の最大。
  - cmd: `--kaz-cache-compaction-placement-share-ratio` が Config に渡る。
- [ ] **Step 2: 失敗を確認する**
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 通ることを確認する**: 新しいテストを `-race -count=3`。既存の `TestKazCompactionPlacementModes`、`TestKazCompactionOutput*`、`TestKazCompactionSources*`、`TestKazCompactInheritsCache`、`TestCompact`、`TestKazBlockCompactionDataVFS`、`go test ./cmd -run Kaz`、`go build .`。
- [ ] **Step 5: Commit**: `feat(kaz): add share placement for compaction output`

### Task 2: B（読まれた実績の印と、印なしを先に降格）、metrics、管理ファイル v3

**Files:**
- Modify: `pkg/chunk/cached_store.go`（`Config` にフィールド 1 つ）、`pkg/chunk/cache_config_kaz.go`、`cmd/flags.go`、`cmd/kaz_cache_flags.go`
- Modify: `pkg/chunk/cache_eviction_2q_kaz.go`（印、heap の順序、降格）、`pkg/chunk/metrics_kaz.go`、`pkg/chunk/cache_state_kaz.go`、`pkg/chunk/disk_cache_kaz.go`（compaction の出力に印を載せる）
- Test: `pkg/chunk/cache_eviction_2q_kaz_test.go`、`pkg/chunk/cache_state_kaz_test.go`、`pkg/chunk/disk_cache_kaz_test.go`、`cmd` の kaz フラグのテスト

**Interfaces:**
- Consumes: Task 1 の `kazPlacementSource`（印を足すなら同じ構造体に）
- Produces: `Config.KazCacheDemoteUnreadFirst bool`、`kazItem` と `kazSaved` と `kazExpectation` の印、`kazStateVersion = 3`、events `demote_unread`／`demote_read`、Gauge `kaz_cache_2q_protected_unread_bytes`

- [ ] **Step 1: 失敗するテストを書く**
  - `TestKaz2QReadMark`: 印が、相関期間の後の昇格、ghost_hit、常連用での get で付く。降格で外れる。compaction の出力は、常連用の元に印があれば付き、無ければ付かない。
  - `TestKaz2QDemoteUnreadFirst`: オンで、atime が新しい印なしが、atime が古い印ありより先に降格される。オフでは atime の古い順のまま。印なしが無くなったら印ありを古い順に降格する。
  - `TestKaz2QDemoteEvents`: `demote_unread + demote_read == demote`。mount 直後から 0 で出る。
  - `TestKaz2QUnreadGaugeConsistent`: 乱数の操作（add、get、昇格、降格、追い出し、ghost_hit、compaction の出力、reset と再追加、metrics の付け直し、2 store）の後も、Gauge と内部の印なしの容量が一致し、heap の不変条件が保たれる。
  - `TestKazCacheStateV3RoundTrip`、`TestKazCacheStateReadsV2`（v2 の項目は印なしで読む。実装後の見直し）、`TestKazCacheStateReadsV1`（既存のテストを v3 の読み込みで通す）。
  - config／cmd: `--kaz-cache-demote-unread-first` が Config に渡る。kaz-2q 以外では無視。
- [ ] **Step 2: 失敗を確認する**
- [ ] **Step 3: 実装する**
- [ ] **Step 4: 通ることを確認する**: 新しいテストを `-race -count=3`。`go test ./pkg/chunk -run TestKaz`、`go test ./pkg/chunk`、`go test ./pkg/vfs -run 'Compact|Kaz'`、`go test ./cmd -run Kaz`、`go vet`、`go build .`。
- [ ] **Step 5: Commit**: `feat(kaz): demote unread protected blocks first and persist read marks`
