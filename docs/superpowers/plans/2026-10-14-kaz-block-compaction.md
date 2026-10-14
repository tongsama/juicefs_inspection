# ブロック単位の compaction（kaz block compaction）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--kaz-compaction-mode block` のとき、バックグラウンドの compaction が chunk 全体ではなく、変化したブロックだけをブロックごとの新しい slice として書く。既定（`upstream`）では今の動きのまま。

**Architecture:** kaz の `compaction_scheduler_lifecycle.go` の 2 か所の `m.compactChunk(...)` を振り分けの関数 `backgroundCompactChunk` に替える。block のときは、純粋な計画の関数（`planKazBlockCompaction`）で残す・消す・書くブロックを決め、ブロックごとに既存の `CompactChunk` のメッセージ（`vfs.Compact`）で書き、エンジンごとの kaz の操作 `doKazReplaceChunk`（型アサーションで探す）で chunk の先頭を置き換える。upstream の `compactChunk`、`doCompactChunk`、`engine` インターフェース、`vfs` は変えない。

**Tech Stack:** Go 1.25（`go.mod`）、JuiceFS 1.4.1 ベースの本体 `juicefs/`（`1.4.1-improve-kaz` 6d9a3095）、go-redis v9、xorm、TKV（memkv）、prometheus、testify。

**実行の結果（2026-10-14）:** subagent-driven で Task 0〜7 を実行（本体 1b73e474..1d36ae04）。計画からの変更: `zero` の補助は Fallocate の zero-range を使う、`compactChunkBlocks` は自分で upstream に戻らない（振り分けだけが選ぶ）、scheduler のテストの待ちの条件、最終レビューでログの形・件数超過時の全体の書き直し・metrics の分割（量は `kaz_compaction_block_amount_total{kind}`）を追加。下の本文は当時の計画のまま。

**Spec:** [docs/superpowers/specs/2026-10-14-kaz-block-compaction-design.md](../specs/2026-10-14-kaz-block-compaction-design.md)

## Global Constraints

- 既定は今の動きと同じ: オプションを付けないとき、バックグラウンドの compaction は upstream の `compactChunk(inode, indx, false, false, tier)` だけを呼ぶ。
- オプション名と既定値: `--kaz-compaction-mode`（`upstream`）、`--kaz-compaction-block-hidden-ratio`（`0.5`）、`--kaz-compaction-block-max-slices`（`0` = chunk のブロック数＋1）。
- block が効くのはバックグラウンドの compaction だけ。強制（`juicefs compact`、`gc --compact`）と 2,500 個の同期 compaction は upstream のまま。
- upstream 由来のファイルの変更は `pkg/meta/config.go`（項目と SelfCheck の 4 行）と `cmd/flags.go`（フラグ 3 つ）だけ。`base.go`、`redis.go`、`sql.go`、`tkv.go`、`slice.go`、`pkg/vfs` の既存のファイルは変えない。
- 1 つの id を複数のブロックで共有しない（ブロックごとに `NewSlice`）。
- 片付けの経路（deferred の retire → リモートの削除、legacy の `deleteSlice`、trash の delayed）は upstream の `doCompactChunk` と同じ条件で分ける。fsync、commit の順序、read-after-write を弱めない。保存の失敗を隠さない。
- ブロックサイズは format の値（`fmt.BlockSize` KiB）を使う。0 や chunk を割り切れない値なら upstream に戻す。
- etcd（`maxCompactSlices < 1000`）では、片付ける件数の上限を「`maxCompactSlices` − chunk のブロック数」にする。10 未満なら upstream に戻す。
- 新しい関数・型にはすべて目的のコメント（英語、既存のコードに合わせる）を付ける。
- 本体 `juicefs/` の作業ブランチは `feat/kaz-block-compaction`（`1.4.1-improve-kaz` 6d9a3095 から）。**commit はユーザーの許可がある場合だけ**（各 Task の最後の commit の手順は許可があるときに行う）。push・merge はしない。調査ルートの文書も agent は add／commit しない。
- 本番 VM、本番 Redis、object storage、cache／staging、`~/.juicefs/diagnostics/` の元のログには触れない。

## Review Focus

1. **compaction の途中に commit された件**（`doRead` の後、置き換えの前に追加された slice）が、置き換えの後も一番上に残り、見え方が変わらないこと → Task 4 の `TestKazBlockCompactionConcurrentCommit`。
2. **件数が上限を超える chunk**（`maxCompactSlices` で先頭だけを片付ける場合）で、残りの件が上に残り、見え方が変わらないこと → Task 4 の `TestKazBlockCompactionLimit`。
3. **ゼロの範囲（id 0 の件）が下の大きい slice を隠すブロック**で、object を書かずに id 0 の件で隠し、読むとゼロになること → Task 2 の `TestPlanKazBlockCompactionMask`。Task 4 の全体のテストもゼロの件を含む。
4. **ブロックより小さいファイル（データの範囲が 1 ブロック未満）**で、出力がデータの範囲で切られ、4 MiB に膨らまず、次の回は何もしないこと → Task 2 の `TestPlanKazBlockCompactionSmallFile`。
5. **オプションを付けない mount** で、upstream の compaction だけが動くこと → Task 5 の `TestKazBackgroundCompactionDefaultIsUpstream`。

---

## ファイルの構成

| ファイル | 種別 | 役割 |
|---|---|---|
| `pkg/meta/compaction_block_config_kaz.go` | 新規 | mode と 2 つのしきい値の検査と実効値 |
| `pkg/meta/compaction_block_plan_kaz.go` | 新規 | 計画の純粋な関数（メタデータに触れない） |
| `pkg/meta/compaction_block_kaz.go` | 新規 | 振り分け、ブロックの書き込み、置き換えの呼び出し、失敗の後始末、metrics、mount 時のログ |
| `pkg/meta/redis_compact_kaz.go` | 新規 | Redis の `doKazReplaceChunk` |
| `pkg/meta/sql_compact_kaz.go` | 新規 | SQL の `doKazReplaceChunk` |
| `pkg/meta/tkv_compact_kaz.go` | 新規 | TKV の `doKazReplaceChunk` |
| `pkg/meta/compaction_block_kaz_helpers_test.go` | 新規 | テスト用の偽の object store と backend の準備 |
| `pkg/meta/compaction_block_config_kaz_test.go` | 新規 | Task 1 のテスト |
| `pkg/meta/compaction_block_plan_kaz_test.go` | 新規 | Task 2 のテスト |
| `pkg/meta/compaction_block_replace_kaz_test.go` | 新規 | Task 3 のテスト |
| `pkg/meta/compaction_block_kaz_test.go` | 新規 | Task 4・5 のテスト |
| `pkg/vfs/compact_block_kaz_test.go` | 新規 | Task 6 のデータのテスト |
| `pkg/meta/compaction_policy_kaz.go` | kaz の既存 | metrics の struct に block の counter を足す |
| `pkg/meta/compaction_scheduler_lifecycle.go` | kaz の既存 | 2 か所の呼び出しを振り分けに替える、mount 時のログ |
| `cmd/kaz_compaction_policy.go`、`cmd/kaz_compaction_policy_flag_test.go` | kaz の既存 | フラグを config に写す、テスト |
| `pkg/meta/config.go` | upstream 由来 | 項目 3 つ、SelfCheck の 4 行 |
| `cmd/flags.go` | upstream 由来 | フラグ 3 つ |

---

### Task 0: 作業ブランチ

- [ ] **Step 1: ブランチを作る**

```bash
cd /home/kwatanabe/tmp_local/juicefs_inspection/juicefs
git status --short   # 何も出ないこと
git switch -c feat/kaz-block-compaction 1.4.1-improve-kaz
git log --oneline -1   # 6d9a3095
```

---

### Task 1: オプション（config とフラグ）

**Files:**
- Create: `pkg/meta/compaction_block_config_kaz.go`
- Create: `pkg/meta/compaction_block_config_kaz_test.go`
- Modify: `pkg/meta/config.go`（`KazCompactionReadMin` の後に項目 3 つ、`SelfCheck` の `ValidateKazCompactionPolicy` の後に 4 行）
- Modify: `cmd/flags.go`（`kaz-compaction-read-min` の直後にフラグ 3 つ）
- Modify: `cmd/kaz_compaction_policy.go`、`cmd/kaz_compaction_policy_flag_test.go`

**Interfaces:**
- Produces: `Config.KazCompactionMode string`、`Config.KazCompactionBlockHiddenRatio float64`、`Config.KazCompactionBlockMaxSlices int`、定数 `KazCompactionModeUpstream = "upstream"`／`KazCompactionModeBlock = "block"`、`(*Config).ValidateKazBlockCompaction() error`、`(*Config).kazBlockCompactionEnabled() bool`、`(*Config).kazBlockHiddenRatio() float64`、`(*Config).kazBlockMaxSlices(blocks int) int`、cmd の `kazBlockCompactionFromFlags(mode string, ratio float64, maxSlices int) (meta.Config, error)`。

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/compaction_block_config_kaz_test.go`:

```go
package meta

import (
	"math"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestKazBlockCompactionConfig checks validation and effective values of the block mode options.
func TestKazBlockCompactionConfig(t *testing.T) {
	var zero Config
	require.NoError(t, zero.ValidateKazBlockCompaction())
	require.False(t, zero.kazBlockCompactionEnabled())
	require.Equal(t, 0.5, zero.kazBlockHiddenRatio())
	require.Equal(t, 17, zero.kazBlockMaxSlices(16))

	c := Config{KazCompactionMode: KazCompactionModeBlock, KazCompactionBlockHiddenRatio: 0.25, KazCompactionBlockMaxSlices: 9}
	require.NoError(t, c.ValidateKazBlockCompaction())
	require.True(t, c.kazBlockCompactionEnabled())
	require.Equal(t, 0.25, c.kazBlockHiddenRatio())
	require.Equal(t, 9, c.kazBlockMaxSlices(16))
	require.NoError(t, (&Config{KazCompactionMode: KazCompactionModeUpstream}).ValidateKazBlockCompaction())

	for _, bad := range []Config{
		{KazCompactionMode: "blocks"},
		{KazCompactionBlockHiddenRatio: -0.1},
		{KazCompactionBlockHiddenRatio: 1.5},
		{KazCompactionBlockHiddenRatio: math.NaN()},
		{KazCompactionBlockMaxSlices: -1},
	} {
		require.Error(t, bad.ValidateKazBlockCompaction(), "%+v", bad)
	}
}

// TestKazBlockCompactionSelfCheck falls back to upstream compaction on invalid settings.
func TestKazBlockCompactionSelfCheck(t *testing.T) {
	c := DefaultConf()
	c.KazCompactionMode = "bogus"
	c.KazCompactionBlockHiddenRatio = 2
	c.SelfCheck()
	require.Equal(t, "", c.KazCompactionMode)
	require.Zero(t, c.KazCompactionBlockHiddenRatio)
	require.Zero(t, c.KazCompactionBlockMaxSlices)
}
```

`cmd/kaz_compaction_policy_flag_test.go` の末尾に追記:

```go
// TestKazBlockCompactionFlags keeps upstream compaction by default and parses the block mode.
func TestKazBlockCompactionFlags(t *testing.T) {
	def := metaConfFromArgs(t)
	require.Equal(t, meta.KazCompactionModeUpstream, def.KazCompactionMode)
	require.Equal(t, 0.5, def.KazCompactionBlockHiddenRatio)
	require.Equal(t, 0, def.KazCompactionBlockMaxSlices)
	c := metaConfFromArgs(t, "--kaz-compaction-mode", "block", "--kaz-compaction-block-hidden-ratio", "0.3", "--kaz-compaction-block-max-slices", "9")
	require.Equal(t, meta.KazCompactionModeBlock, c.KazCompactionMode)
	require.Equal(t, 0.3, c.KazCompactionBlockHiddenRatio)
	require.Equal(t, 9, c.KazCompactionBlockMaxSlices)
}

// TestKazBlockCompactionFromFlags rejects values that would silently mean the default or nothing.
func TestKazBlockCompactionFromFlags(t *testing.T) {
	_, err := kazBlockCompactionFromFlags("block", 0.5, 0)
	require.NoError(t, err)
	for _, tc := range []struct {
		mode  string
		ratio float64
		max   int
	}{{"blocks", 0.5, 0}, {"block", 0, 0}, {"block", 1.1, 0}, {"block", 0.5, -1}} {
		_, err := kazBlockCompactionFromFlags(tc.mode, tc.ratio, tc.max)
		require.Error(t, err, "%+v", tc)
	}
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `cd juicefs && go test ./pkg/meta -run 'TestKazBlockCompaction(Config|SelfCheck)' -count=1 && go test ./cmd -run 'TestKazBlockCompaction' -count=1`
Expected: コンパイルエラー（`KazCompactionMode` などが未定義）

- [ ] **Step 3: 実装**

`pkg/meta/config.go` の `KazCompactionReadMin int` の直後:

```go
	// [kaz] block compaction; "" or "upstream" keeps upstream compaction.
	// KazCompactionMode "block" rewrites only the blocks changed since the last
	// background compaction, one new slice per block. The two thresholds switch a
	// chunk back to a whole rewrite (still one slice per block); 0 means the default.
	KazCompactionMode             string
	KazCompactionBlockHiddenRatio float64
	KazCompactionBlockMaxSlices   int
```

`SelfCheck` の `ValidateKazCompactionPolicy` の if の直後:

```go
	if err := c.ValidateKazBlockCompaction(); err != nil {
		logger.Warnf("%s; falling back to upstream compaction", err)
		c.KazCompactionMode, c.KazCompactionBlockHiddenRatio, c.KazCompactionBlockMaxSlices = "", 0, 0
	}
```

`pkg/meta/compaction_block_config_kaz.go`（ライセンスのヘッダーは `compaction_policy_kaz.go` と同じ）:

```go
package meta

import "fmt"

// Modes of --kaz-compaction-mode. Upstream is the default and keeps the
// upstream compaction of the whole merged range into one slice.
const (
	KazCompactionModeUpstream = "upstream"
	KazCompactionModeBlock    = "block"

	kazBlockDefaultHiddenRatio = 0.5
)

// ValidateKazBlockCompaction rejects an unknown mode or out-of-range thresholds.
// Zero thresholds mean "use the default", so Go callers that never set them keep defaults.
func (c *Config) ValidateKazBlockCompaction() error {
	switch c.KazCompactionMode {
	case "", KazCompactionModeUpstream, KazCompactionModeBlock:
	default:
		return fmt.Errorf("invalid kaz-compaction-mode %q (want upstream or block)", c.KazCompactionMode)
	}
	if r := c.KazCompactionBlockHiddenRatio; r != 0 && !(r > 0 && r <= 1) {
		return fmt.Errorf("invalid kaz-compaction-block-hidden-ratio %v (want 0 < ratio <= 1)", r)
	}
	if c.KazCompactionBlockMaxSlices < 0 {
		return fmt.Errorf("invalid kaz-compaction-block-max-slices %d (want >= 0)", c.KazCompactionBlockMaxSlices)
	}
	return nil
}

// kazBlockCompactionEnabled reports whether background compaction should use the block mode.
func (c *Config) kazBlockCompactionEnabled() bool {
	return c.KazCompactionMode == KazCompactionModeBlock
}

// kazBlockHiddenRatio returns the hidden-data share above which a chunk is rewritten whole.
func (c *Config) kazBlockHiddenRatio() float64 {
	if c.KazCompactionBlockHiddenRatio == 0 {
		return kazBlockDefaultHiddenRatio
	}
	return c.KazCompactionBlockHiddenRatio
}

// kazBlockMaxSlices returns the record limit after a partial rewrite; 0 means blocks+1.
func (c *Config) kazBlockMaxSlices(blocks int) int {
	if c.KazCompactionBlockMaxSlices == 0 {
		return blocks + 1
	}
	return c.KazCompactionBlockMaxSlices
}
```

`cmd/flags.go` の `kaz-compaction-read-min` の `&cli.IntFlag{...},` の直後:

```go
		&cli.StringFlag{
			Name:  "kaz-compaction-mode",
			Value: "upstream",
			Usage: "[kaz] how background compaction rewrites a chunk: upstream (the whole merged range as one new slice) or block (only the blocks changed since the last compaction, one new slice per block); forced and synchronous compaction stay upstream",
		},
		&cli.Float64Flag{
			Name:  "kaz-compaction-block-hidden-ratio",
			Value: 0.5,
			Usage: "[kaz] with --kaz-compaction-mode block, rewrite the whole chunk (one slice per block) when more than this share of the kept slices' data would be hidden by newer data (0 < ratio <= 1)",
		},
		&cli.IntFlag{
			Name:  "kaz-compaction-block-max-slices",
			Value: 0,
			Usage: "[kaz] with --kaz-compaction-mode block, rewrite the whole chunk when more than this many slice records would remain (0 = blocks per chunk + 1, 17 for 4 MiB blocks)",
		},
```

`cmd/kaz_compaction_policy.go` に追記し、`applyKazCompactionPolicyFlags` の末尾で呼ぶ:

```go
// kazBlockCompactionFromFlags checks the block compaction flags. Unlike the Go
// config, a hidden ratio of 0 on the command line is out of range.
func kazBlockCompactionFromFlags(mode string, ratio float64, maxSlices int) (meta.Config, error) {
	if ratio == 0 {
		return meta.Config{}, fmt.Errorf("invalid kaz-compaction-block-hidden-ratio 0 (want 0 < ratio <= 1)")
	}
	conf := meta.Config{KazCompactionMode: mode, KazCompactionBlockHiddenRatio: ratio, KazCompactionBlockMaxSlices: maxSlices}
	if err := conf.ValidateKazBlockCompaction(); err != nil {
		return meta.Config{}, err
	}
	return conf, nil
}
```

`applyKazCompactionPolicyFlags` の `conf.KazCompactionReadMin = readMin` の後:

```go
	block, err := kazBlockCompactionFromFlags(c.String("kaz-compaction-mode"), c.Float64("kaz-compaction-block-hidden-ratio"), c.Int("kaz-compaction-block-max-slices"))
	if err != nil {
		logger.Fatalf("%s", err)
	}
	conf.KazCompactionMode = block.KazCompactionMode
	conf.KazCompactionBlockHiddenRatio = block.KazCompactionBlockHiddenRatio
	conf.KazCompactionBlockMaxSlices = block.KazCompactionBlockMaxSlices
```

関数のコメントも「kaz compaction policy と block compaction のフラグを写す」に直す。

- [ ] **Step 4: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'TestKazBlockCompaction(Config|SelfCheck)|TestKazCompactionPolicyConfig' -count=1 && go test ./cmd -run 'TestKaz' -count=1`
Expected: PASS

- [ ] **Step 5: commit（ユーザーの許可がある場合）**

```bash
git add pkg/meta/compaction_block_config_kaz.go pkg/meta/compaction_block_config_kaz_test.go pkg/meta/config.go cmd/flags.go cmd/kaz_compaction_policy.go cmd/kaz_compaction_policy_flag_test.go
git commit -m "meta, cmd: add the kaz block compaction options (default keeps upstream compaction)"
```

---

### Task 2: 計画の純粋な関数

**Files:**
- Create: `pkg/meta/compaction_block_plan_kaz.go`
- Create: `pkg/meta/compaction_block_kaz_helpers_test.go`（偽の object store。Task 3〜5 でも使う）
- Create: `pkg/meta/compaction_block_plan_kaz_test.go`

**Interfaces:**
- Consumes: upstream の `slice`、`Slice`、`buildSlice(ss []*slice) []Slice`（`slice.go`）。
- Produces:
  - `type kazBlockOut struct { pos, length uint32; mask bool; pieces []Slice }`
  - `type kazBlockPlan struct { full bool; kept, removed []*slice; outs []kazBlockOut; hidden float64 }`
  - `func planKazBlockCompaction(ss []*slice, blockSize uint32, hiddenRatio float64, maxSlices int) *kazBlockPlan`（片付ける件が無ければ nil）
  - テスト用: `newKazFakeStore() *kazFakeStore`、`(*kazFakeStore).compact(args ...interface{}) error`（CompactChunk の callback）、`(*kazFakeStore).view(ss []*slice, n uint32) []byte`、`(*kazFakeStore).callCount() int`、`(*kazFakeStore).callIDs() []uint64`、`kazFakeStore.fail func(id uint64) error`、`kazFakeStore.during func()`、`applyKazPlan(t, f, p, next *uint64) []*slice`、`kazMarshal(ss []*slice) []byte`、`kazIDs(ss []*slice) []uint64`

**計画の規則（設計書 §5）:**
1. 全件の最小〜最大を「データの範囲」とする。
2. 後の件に完全に隠れた件は、ブロックに触れずに「消す」。
3. 小さい件（長さ < B で、ブロックを満たす件でもない）は「消す」。触れるブロックを `T` に入れる。
4. 残りのうち、触れるブロックがすべて `T` にある件も「消す」。
5. 消す件が無ければ nil。
6. 「残す」件のデータの見えない割合が hiddenRatio を超えるか、残す件数＋`|T|` が maxSlices を超えたら全体の書き直し（`T` = 範囲の全ブロック、全件を消す）。
7. `T` の各ブロックを、元の全件を重ねた見え方から `[max(b*B, start), min((b+1)*B, end))` で切り出す。全部が穴なら、残す件がかかるときだけ mask にする。

- [ ] **Step 1: テストの補助（偽の object store）を書く**

`pkg/meta/compaction_block_kaz_helpers_test.go`:

```go
package meta

import (
	"sync"
	"testing"

	"github.com/stretchr/testify/require"
)

// kazFakeStore stands in for the object store in block-compaction tests: slices
// written by a test hold deterministic bytes, slices built by compaction are kept.
type kazFakeStore struct {
	mu     sync.Mutex
	data   map[uint64][]byte
	calls  []uint64
	fail   func(id uint64) error // optional failure injected before a slice is stored
	during func()                // optional hook run once inside the first CompactChunk call
}

// newKazFakeStore returns an empty fake store.
func newKazFakeStore() *kazFakeStore {
	return &kazFakeStore{data: make(map[uint64][]byte)}
}

// kazFakeByte is the content of byte off of a test-written slice id (never zero).
func kazFakeByte(id uint64, off uint32) byte {
	return byte((id*131+uint64(off)*7)%251) + 1
}

// byteAtLocked returns byte off of slice id; holes (id 0) read as zero.
func (f *kazFakeStore) byteAtLocked(id uint64, off uint32) byte {
	if id == 0 {
		return 0
	}
	if d, ok := f.data[id]; ok {
		return d[off]
	}
	return kazFakeByte(id, off)
}

// compact is a CompactChunk callback: it materializes the pieces into slice id.
func (f *kazFakeStore) compact(args ...interface{}) error {
	pieces, id := args[0].([]Slice), args[1].(uint64)
	f.mu.Lock()
	hook, fail := f.during, f.fail
	f.during = nil
	f.mu.Unlock()
	if hook != nil {
		hook()
	}
	if fail != nil {
		if err := fail(id); err != nil {
			return err
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	var buf []byte
	for _, p := range pieces {
		for i := uint32(0); i < p.Len; i++ {
			buf = append(buf, f.byteAtLocked(p.Id, p.Off+i))
		}
	}
	f.data[id] = buf
	f.calls = append(f.calls, id)
	return nil
}

// view returns what a reader sees in [0, n) of a chunk holding records ss.
func (f *kazFakeStore) view(ss []*slice, n uint32) []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]byte, n)
	var pos uint32
	for _, p := range buildSlice(ss) {
		for i := uint32(0); i < p.Len && pos+i < n; i++ {
			out[pos+i] = f.byteAtLocked(p.Id, p.Off+i)
		}
		pos += p.Len
	}
	return out
}

// callCount returns how many slices compaction has written.
func (f *kazFakeStore) callCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

// callIDs returns the ids of slices compaction has written.
func (f *kazFakeStore) callIDs() []uint64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]uint64(nil), f.calls...)
}

// applyKazPlan returns the records a chunk holds after plan p, writing each
// data output into f under a fresh id taken from next.
func applyKazPlan(t *testing.T, f *kazFakeStore, p *kazBlockPlan, next *uint64) []*slice {
	t.Helper()
	out := append([]*slice(nil), p.kept...)
	for _, o := range p.outs {
		if o.mask {
			out = append(out, &slice{pos: o.pos, len: o.length})
			continue
		}
		*next++
		require.NoError(t, f.compact(o.pieces, *next, uint8(0)))
		out = append(out, &slice{pos: o.pos, id: *next, size: o.length, len: o.length})
	}
	return out
}

// kazMarshal encodes records the way engines store a chunk.
func kazMarshal(ss []*slice) []byte {
	buf := make([]byte, 0, len(ss)*sliceBytes)
	for _, s := range ss {
		buf = append(buf, marshalSlice(s.pos, s.id, s.size, s.off, s.len)...)
	}
	return buf
}

// kazIDs lists the slice ids of records in order.
func kazIDs(ss []*slice) []uint64 {
	ids := make([]uint64, len(ss))
	for i, s := range ss {
		ids[i] = s.id
	}
	return ids
}
```

- [ ] **Step 2: 失敗するテストを書く**

`pkg/meta/compaction_block_plan_kaz_test.go`:

```go
package meta

import (
	"math/rand"
	"testing"

	"github.com/stretchr/testify/require"
)

const kazTB = uint32(4096) // block size used by the fixed cases

// kazRec builds a record that references a whole fresh slice.
func kazRec(pos uint32, id uint64, l uint32) *slice {
	return &slice{pos: pos, id: id, size: l, len: l}
}

// TestPlanKazBlockCompactionKeepsView applies random plans repeatedly and checks
// that what a reader sees never changes and that records stay bounded.
func TestPlanKazBlockCompactionKeepsView(t *testing.T) {
	for _, bs := range []uint32{1 << 10, 4 << 10, 8 << 10} {
		rng := rand.New(rand.NewSource(int64(bs)))
		chunkLen := bs * 16
		for round := 0; round < 300; round++ {
			f := newKazFakeStore()
			next := uint64(1 << 40)
			id := uint64(1)
			var ss []*slice
			if rng.Intn(2) == 0 { // a large base, like an upstream compaction output
				ss = append(ss, kazRec(0, id, chunkLen))
				id++
			}
			for gen := 0; gen < 4; gen++ {
				for i := rng.Intn(30); i > 0; i-- {
					pos := uint32(rng.Intn(int(chunkLen)))
					l := min(uint32(1+rng.Intn(int(bs)*2)), chunkLen-pos)
					switch rng.Intn(8) {
					case 0: // zero range
						ss = append(ss, &slice{pos: pos, len: l})
					case 1: // reference into a larger slice
						extra := uint32(rng.Intn(1000))
						ss = append(ss, &slice{pos: pos, id: id, size: l + extra, off: extra / 2, len: l})
						id++
					default:
						ss = append(ss, kazRec(pos, id, l))
						id++
					}
				}
				before := f.view(ss, chunkLen)
				p := planKazBlockCompaction(ss, bs, 0.5, 17)
				if p == nil {
					continue
				}
				ss = applyKazPlan(t, f, p, &next)
				require.Equal(t, before, f.view(ss, chunkLen), "bs=%d round=%d gen=%d", bs, round, gen)
				require.LessOrEqual(t, len(ss), 17, "bs=%d round=%d gen=%d", bs, round, gen)
			}
		}
	}
}

// TestPlanKazBlockCompactionNoSmall does nothing on a chunk of block-shaped records.
func TestPlanKazBlockCompactionNoSmall(t *testing.T) {
	ss := []*slice{kazRec(0, 1, kazTB), kazRec(kazTB, 2, kazTB), kazRec(2*kazTB, 3, 100)}
	require.Nil(t, planKazBlockCompaction(ss, kazTB, 0.5, 17))
}

// TestPlanKazBlockCompactionPartial rewrites only the touched blocks above a large base,
// and a later round replaces the earlier block record it fully covers.
func TestPlanKazBlockCompactionPartial(t *testing.T) {
	base := kazRec(0, 1, 16*kazTB)
	ss := []*slice{base, kazRec(2*kazTB+100, 2, 300), kazRec(3*kazTB+10, 3, 20), kazRec(9*kazTB, 4, 50), kazRec(2*kazTB+200, 5, 10)}
	p := planKazBlockCompaction(ss, kazTB, 0.5, 17)
	require.NotNil(t, p)
	require.False(t, p.full)
	require.Equal(t, []*slice{base}, p.kept)
	require.Equal(t, []uint64{2, 3, 4, 5}, kazIDs(p.removed))
	require.Len(t, p.outs, 3)
	for i, b := range []uint32{2, 3, 9} {
		require.Equal(t, b*kazTB, p.outs[i].pos)
		require.Equal(t, kazTB, p.outs[i].length)
		require.False(t, p.outs[i].mask)
	}
	f := newKazFakeStore()
	next := uint64(100)
	before := f.view(ss, 16*kazTB)
	ss = applyKazPlan(t, f, p, &next)
	require.Equal(t, before, f.view(ss, 16*kazTB))

	ss = append(ss, kazRec(2*kazTB+5, 6, 5), kazRec(5*kazTB, 7, 5))
	p = planKazBlockCompaction(ss, kazTB, 0.5, 17)
	require.NotNil(t, p)
	require.False(t, p.full)
	require.Equal(t, []uint64{1, 102, 103}, kazIDs(p.kept)) // base, block 3, block 9
	require.Equal(t, []uint64{101, 6, 7}, kazIDs(p.removed)) // old block 2, then the new writes
	require.Len(t, p.outs, 2)
}

// TestPlanKazBlockCompactionFullByHidden rewrites every block once most of the base is hidden.
func TestPlanKazBlockCompactionFullByHidden(t *testing.T) {
	ss := []*slice{kazRec(0, 1, 16*kazTB)}
	for i := uint32(0); i < 9; i++ {
		ss = append(ss, kazRec(i*kazTB+1, uint64(10+i), 10))
	}
	p := planKazBlockCompaction(ss, kazTB, 0.5, 17)
	require.NotNil(t, p)
	require.True(t, p.full)
	require.Empty(t, p.kept)
	require.Len(t, p.removed, 10)
	require.Len(t, p.outs, 16)
}

// TestPlanKazBlockCompactionFullByCount rewrites every block when too many records would remain.
func TestPlanKazBlockCompactionFullByCount(t *testing.T) {
	ss := []*slice{kazRec(0, 1, 16*kazTB), kazRec(kazTB, 2, 10), kazRec(5*kazTB, 3, 10), kazRec(7*kazTB, 4, 10)}
	p := planKazBlockCompaction(ss, kazTB, 0.5, 3)
	require.NotNil(t, p)
	require.True(t, p.full)
	require.Len(t, p.outs, 16)
}

// TestPlanKazBlockCompactionMask hides kept data under a block of zero records without an object.
func TestPlanKazBlockCompactionMask(t *testing.T) {
	ss := []*slice{kazRec(0, 1, 16*kazTB), {pos: 4 * kazTB, len: kazTB / 2}, {pos: 4*kazTB + kazTB/2, len: kazTB / 2}}
	p := planKazBlockCompaction(ss, kazTB, 0.5, 17)
	require.NotNil(t, p)
	require.Equal(t, []kazBlockOut{{pos: 4 * kazTB, length: kazTB, mask: true}}, p.outs)
	f := newKazFakeStore()
	next := uint64(100)
	before := f.view(ss, 16*kazTB)
	after := applyKazPlan(t, f, p, &next)
	require.Equal(t, before, f.view(after, 16*kazTB))
	require.Zero(t, f.callCount())
}

// TestPlanKazBlockCompactionSmallFile keeps outputs within the data extent of a tiny file.
func TestPlanKazBlockCompactionSmallFile(t *testing.T) {
	ss := []*slice{kazRec(0, 1, 1000), kazRec(500, 2, 2500)}
	p := planKazBlockCompaction(ss, kazTB, 0.5, 17)
	require.NotNil(t, p)
	require.Len(t, p.outs, 1)
	require.Equal(t, uint32(0), p.outs[0].pos)
	require.Equal(t, uint32(3000), p.outs[0].length)
	f := newKazFakeStore()
	next := uint64(100)
	after := applyKazPlan(t, f, p, &next)
	require.Nil(t, planKazBlockCompaction(after, kazTB, 0.5, 17))
}

// TestPlanKazBlockCompactionFullyHidden drops a record covered by later ones without rewriting.
func TestPlanKazBlockCompactionFullyHidden(t *testing.T) {
	ss := []*slice{kazRec(0, 1, 2*kazTB), kazRec(0, 2, 2*kazTB)}
	p := planKazBlockCompaction(ss, kazTB, 0.5, 17)
	require.NotNil(t, p)
	require.Equal(t, []uint64{1}, kazIDs(p.removed))
	require.Equal(t, []uint64{2}, kazIDs(p.kept))
	require.Empty(t, p.outs)
}
```

- [ ] **Step 3: 失敗することを確かめる**

Run: `go test ./pkg/meta -run 'TestPlanKazBlockCompaction' -count=1`
Expected: コンパイルエラー（`planKazBlockCompaction` などが未定義）

- [ ] **Step 4: 実装**

`pkg/meta/compaction_block_plan_kaz.go`（ライセンスのヘッダー付き）:

```go
package meta

import "sort"

// kazBlockOut is one record that block compaction places above the kept records.
type kazBlockOut struct {
	pos    uint32  // chunk offset where the record starts
	length uint32  // bytes the record covers
	mask   bool    // zero record (id 0) hiding kept data; no object is written
	pieces []Slice // visible pieces copied into a new slice when mask is false
}

// kazBlockPlan is the outcome of planning one block compaction of a chunk.
// The chunk after compaction holds kept (in their original order), then outs.
type kazBlockPlan struct {
	full    bool          // every block of the data extent is rewritten
	kept    []*slice      // records left unchanged
	removed []*slice      // records dropped from the chunk
	outs    []kazBlockOut // new records in ascending chunk order
	hidden  float64       // share of kept data hidden in the partial result
}

// kazBlockGeom answers block questions for one chunk: block size and data extent.
type kazBlockGeom struct {
	size, start, end uint32
}

// first returns the block holding the first byte of s.
func (g kazBlockGeom) first(s *slice) uint32 { return s.pos / g.size }

// last returns the block holding the last byte of s (s.len > 0).
func (g kazBlockGeom) last(s *slice) uint32 { return (s.pos + s.len - 1) / g.size }

// span returns the part of block b inside the data extent.
func (g kazBlockGeom) span(b uint32) (uint32, uint32) {
	return max(b*g.size, g.start), min((b+1)*g.size, g.end)
}

// fills reports whether s covers exactly the in-extent part of its block, the
// shape block compaction writes.
func (g kazBlockGeom) fills(s *slice) bool {
	lo, hi := g.span(g.first(s))
	return s.pos == lo && s.pos+s.len == hi
}

// small reports whether s is a record block compaction merges away.
func (g kazBlockGeom) small(s *slice) bool {
	return s.len < g.size && !g.fills(s)
}

// within reports whether every block s touches is in t.
func (g kazBlockGeom) within(s *slice, t map[uint32]bool) bool {
	for b := g.first(s); b <= g.last(s); b++ {
		if !t[b] {
			return false
		}
	}
	return true
}

// outside counts the bytes of [a, b) in blocks not in t.
func (g kazBlockGeom) outside(a, b uint32, t map[uint32]bool) uint64 {
	var n uint64
	for a < b {
		blk := a / g.size
		e := min((blk+1)*g.size, b)
		if !t[blk] {
			n += uint64(e - a)
		}
		a = e
	}
	return n
}

// hiddenShare returns the share of the kept records' data that a reader would no
// longer see once the blocks in t are rewritten above them.
func (g kazBlockGeom) hiddenShare(kept []*slice, t map[uint32]bool) float64 {
	var total, visible uint64
	for _, s := range kept {
		if s.id > 0 {
			total += uint64(s.len)
		}
	}
	if total == 0 {
		return 0
	}
	var pos uint32
	for _, p := range buildSlice(kept) {
		if p.Id > 0 {
			visible += g.outside(pos, pos+p.Len, t)
		}
		pos += p.Len
	}
	return 1 - float64(visible)/float64(total)
}

// kazFullyHidden marks records that later records cover completely (and empty
// ones); dropping them does not change what a reader sees.
func kazFullyHidden(ss []*slice) []bool {
	hidden := make([]bool, len(ss))
	var cov [][2]uint32 // sorted, merged ranges covered by later records
	for i := len(ss) - 1; i >= 0; i-- {
		lo, hi := ss[i].pos, ss[i].pos+ss[i].len
		k := sort.Search(len(cov), func(k int) bool { return cov[k][0] > lo }) - 1
		hidden[i] = lo == hi || (k >= 0 && cov[k][1] >= hi)
		if lo < hi {
			cov = kazAddRange(cov, lo, hi)
		}
	}
	return hidden
}

// kazAddRange merges [lo, hi) into the sorted, merged ranges cov.
func kazAddRange(cov [][2]uint32, lo, hi uint32) [][2]uint32 {
	out := make([][2]uint32, 0, len(cov)+1)
	i := 0
	for ; i < len(cov) && cov[i][1] < lo; i++ {
		out = append(out, cov[i])
	}
	for ; i < len(cov) && cov[i][0] <= hi; i++ {
		lo, hi = min(lo, cov[i][0]), max(hi, cov[i][1])
	}
	out = append(out, [2]uint32{lo, hi})
	return append(out, cov[i:]...)
}

// kazClipPieces returns the pieces of a chunk view (from buildSlice, starting at
// offset 0) inside [lo, hi); offsets into slices are adjusted, holes stay id 0.
func kazClipPieces(view []Slice, lo, hi uint32) []Slice {
	var out []Slice
	var pos uint32
	for _, p := range view {
		a, b := max(pos, lo), min(pos+p.Len, hi)
		if a < b {
			if p.Id == 0 {
				out = append(out, Slice{Size: b - a, Len: b - a})
			} else {
				out = append(out, Slice{Id: p.Id, Size: p.Size, Off: p.Off + (a - pos), Len: b - a})
			}
		}
		pos += p.Len
		if pos >= hi {
			break
		}
	}
	if pos < hi {
		a := max(pos, lo)
		out = append(out, Slice{Size: hi - a, Len: hi - a})
	}
	return out
}

// kazAllHoles reports whether every piece is a hole.
func kazAllHoles(pieces []Slice) bool {
	for _, p := range pieces {
		if p.Id != 0 {
			return false
		}
	}
	return true
}

// kazOverlapsAny reports whether any record overlaps [lo, hi).
func kazOverlapsAny(ss []*slice, lo, hi uint32) bool {
	for _, s := range ss {
		if s.pos < hi && s.pos+s.len > lo {
			return true
		}
	}
	return false
}

// planKazBlockCompaction decides which records of a chunk (ss, in chunk order) to
// keep, which to drop and which blocks to rewrite so that the chunk reads the
// same afterwards. It returns nil when nothing can be dropped. The rules are in
// docs/superpowers/specs/2026-10-14-kaz-block-compaction-design.md §5.
func planKazBlockCompaction(ss []*slice, blockSize uint32, hiddenRatio float64, maxSlices int) *kazBlockPlan {
	if len(ss) == 0 || blockSize == 0 {
		return nil
	}
	g := kazBlockGeom{size: blockSize, start: ss[0].pos, end: ss[0].pos + ss[0].len}
	for _, s := range ss[1:] {
		g.start, g.end = min(g.start, s.pos), max(g.end, s.pos+s.len)
	}
	hidden := kazFullyHidden(ss)
	touched := make(map[uint32]bool)
	drop := make([]bool, len(ss))
	for i, s := range ss {
		switch {
		case hidden[i]:
			drop[i] = true
		case g.small(s):
			drop[i] = true
			for b := g.first(s); b <= g.last(s); b++ {
				touched[b] = true
			}
		}
	}
	p := &kazBlockPlan{}
	for i, s := range ss {
		if drop[i] || g.within(s, touched) {
			p.removed = append(p.removed, s)
		} else {
			p.kept = append(p.kept, s)
		}
	}
	if len(p.removed) == 0 {
		return nil
	}
	p.hidden = g.hiddenShare(p.kept, touched)
	if p.hidden > hiddenRatio || len(p.kept)+len(touched) > maxSlices {
		p.full, p.kept, p.removed = true, nil, append([]*slice(nil), ss...)
		touched = make(map[uint32]bool)
		for b := g.start / g.size; b*g.size < g.end; b++ {
			touched[b] = true
		}
	}
	blocks := make([]uint32, 0, len(touched))
	for b := range touched {
		blocks = append(blocks, b)
	}
	sort.Slice(blocks, func(i, j int) bool { return blocks[i] < blocks[j] })
	view := buildSlice(ss)
	for _, b := range blocks {
		lo, hi := g.span(b)
		if hi <= lo {
			continue
		}
		pieces := kazClipPieces(view, lo, hi)
		if kazAllHoles(pieces) {
			if kazOverlapsAny(p.kept, lo, hi) {
				p.outs = append(p.outs, kazBlockOut{pos: lo, length: hi - lo, mask: true})
			}
			continue
		}
		p.outs = append(p.outs, kazBlockOut{pos: lo, length: hi - lo, pieces: pieces})
	}
	return p
}
```

- [ ] **Step 5: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'TestPlanKazBlockCompaction' -count=1 -v 2>&1 | tail -20`
Expected: PASS（全 8 件）

- [ ] **Step 6: commit（ユーザーの許可がある場合）**

```bash
git add pkg/meta/compaction_block_plan_kaz.go pkg/meta/compaction_block_plan_kaz_test.go pkg/meta/compaction_block_kaz_helpers_test.go
git commit -m "meta: plan kaz block compaction (which records to keep and which blocks to rewrite)"
```

---

### Task 3: エンジンごとの置き換えの操作

**Files:**
- Create: `pkg/meta/redis_compact_kaz.go`、`pkg/meta/sql_compact_kaz.go`、`pkg/meta/tkv_compact_kaz.go`
- Modify: `pkg/meta/compaction_block_kaz_helpers_test.go`（backend の準備を足す）
- Create: `pkg/meta/compaction_block_replace_kaz_test.go`

**Interfaces:**
- Consumes: upstream の各エンジンの内部（Redis: `txn`、`chunkKey`、`sliceRefs`、`sliceKey`、`delSlices`、`genLog`、`cleanupZeroRef(key string)`、`rdb`。SQL: `txn`、`simpleTxn`、`mustInsert`、`chunk`、`sliceRef`、`delslices`、`sqlConv`、`genLog`。TKV: `txn`、`chunkKey`、`sliceKey`、`delSliceKey`、`genLog`、`get`、`cleanupZeroRef(id, size)`、`client.txn`）、`baseMeta.enqueueCompactionDelete`。テストは `compaction_gc_test.go` の `gcTestBackend`／`newGCTestBackend`。
- Produces:
  - `type kazChunkReplacer interface { doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []Slice, delayed []byte, delayedKey uint64) syscall.Errno }`（`compaction_block_kaz.go` は Task 4 で作るので、この Task では `redis_compact_kaz.go` の先頭に置き、Task 4 で `compaction_block_kaz.go` へ移す）
  - 意味: 1 つのトランザクションで、chunk の先頭が `origin` と一致するときだけ、chunk を `replaced`＋`origin` より後ろに置き換える。`created` の参照数のキーを作る。`delayed != nil` なら `delayedKey` を名前にして delayed を記録し、`nil` なら `removed` の参照数を減らす。一致しなければ `EINVAL`。後処理は upstream の `doCompactChunk` と同じ。
  - テスト用: `newKazBlockEnv(t, backend string, blockKiB int) *kazBlockEnv`、`(*kazBlockEnv).file(t) Ino`、`.write(t, inode, pos, length uint32) uint64`、`.zero(t, inode, pos, length uint32)`、`.records(t, inode) []*slice`、`.deletedIDs() map[uint64]int`、`kazBlockBackends`

- [ ] **Step 1: backend の準備を補助に足す**

`pkg/meta/compaction_block_kaz_helpers_test.go` に追記（import に `"time"` を足す）:

```go
// kazBlockBackends lists the engines tested; redis needs JUICEFS_TEST_COMPACTION_GC_REDIS.
var kazBlockBackends = []string{"memkv", "sqlite", "redis"}

// kazBlockEnv is a metadata backend wired for block-compaction tests.
type kazBlockEnv struct {
	gcTestBackend
	store   *kazFakeStore
	mu      sync.Mutex
	deleted map[uint64]int // DeleteSlice messages per slice id
}

// newKazBlockEnv returns a backend in block mode with the given block size, a fake
// object store and synchronous slice deletion that records DeleteSlice messages.
func newKazBlockEnv(t *testing.T, backend string, blockKiB int) *kazBlockEnv {
	t.Helper()
	b := newGCTestBackend(t, backend)
	b.Lock()
	b.fmt.BlockSize = blockKiB
	b.Unlock()
	b.conf.KazCompactionMode = KazCompactionModeBlock
	b.dSliceMu.Lock()
	b.dslices = nil // deleteSlice runs synchronously and sends DeleteSlice
	b.dSliceMu.Unlock()
	e := &kazBlockEnv{gcTestBackend: b, store: newKazFakeStore(), deleted: make(map[uint64]int)}
	b.OnMsg(CompactChunk, e.store.compact)
	b.OnMsg(DeleteSlice, func(args ...interface{}) error {
		e.mu.Lock()
		e.deleted[args[0].(uint64)]++
		e.mu.Unlock()
		return nil
	})
	return e
}

// file creates an empty regular file.
func (e *kazBlockEnv) file(t *testing.T) Ino {
	t.Helper()
	var inode Ino
	var attr Attr
	require.Zero(t, e.Create(Background(), RootInode, t.Name(), 0600, 0, 0, &inode, &attr))
	return inode
}

// write appends a record of a fresh slice at chunk 0 offset pos, without commit hooks.
func (e *kazBlockEnv) write(t *testing.T, inode Ino, pos, length uint32) uint64 {
	t.Helper()
	var id uint64
	require.Zero(t, e.NewSlice(Background(), &id))
	var n int
	var d dirStat
	var attr Attr
	require.Zero(t, e.en.doWrite(Background(), inode, 0, pos, Slice{Id: id, Size: length, Len: length}, time.Now(), &n, &d, &attr))
	return id
}

// zero appends a zero record (as fallocate zero-range does) at chunk 0 offset pos.
func (e *kazBlockEnv) zero(t *testing.T, inode Ino, pos, length uint32) {
	t.Helper()
	var n int
	var d dirStat
	var attr Attr
	require.Zero(t, e.en.doWrite(Background(), inode, 0, pos, Slice{Len: length}, time.Now(), &n, &d, &attr))
}

// records returns the records of chunk 0.
func (e *kazBlockEnv) records(t *testing.T, inode Ino) []*slice {
	t.Helper()
	ss, st := e.en.doRead(Background(), inode, 0)
	require.Zero(t, st)
	return ss
}

// deletedIDs returns a copy of the DeleteSlice counts.
func (e *kazBlockEnv) deletedIDs() map[uint64]int {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make(map[uint64]int, len(e.deleted))
	for k, v := range e.deleted {
		out[k] = v
	}
	return out
}
```

- [ ] **Step 2: 失敗するテストを書く**

`pkg/meta/compaction_block_replace_kaz_test.go`:

```go
package meta

import (
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// TestKazReplaceChunkParity checks the replacement, reference counts and tail
// records on every engine through observable deletions.
func TestKazReplaceChunkParity(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			r, ok := e.en.(kazChunkReplacer)
			require.True(t, ok)
			inode := e.file(t)
			a := e.write(t, inode, 0, 8192)
			b1 := e.write(t, inode, 100, 50)
			b2 := e.write(t, inode, 200, 50)
			tail := e.write(t, inode, 300, 50) // committed after the origin was read
			ss := e.records(t, inode)
			var c uint64
			require.Zero(t, e.NewSlice(Background(), &c))
			replaced := append(kazMarshal(ss[:1]), marshalSlice(0, c, 4096, 0, 4096)...)
			require.Zero(t, r.doKazReplaceChunk(inode, 0, kazMarshal(ss[:3]), replaced, ss[1:3], []Slice{{Id: c, Size: 4096, Len: 4096}}, nil, 0))
			got := e.records(t, inode)
			require.Equal(t, []uint64{a, c, tail}, kazIDs(got))

			// Dropped records lost their only reference; kept, tail and created did not.
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			require.Equal(t, map[uint64]int{b1: 1, b2: 1}, e.deletedIDs())

			// The created slice holds exactly one reference: dropping its record frees it.
			require.Zero(t, r.doKazReplaceChunk(inode, 0, kazMarshal(got), append(kazMarshal(got[:1]), kazMarshal(got[2:])...), got[1:2], nil, nil, 0))
			require.Equal(t, []uint64{a, tail}, kazIDs(e.records(t, inode)))
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			require.Equal(t, map[uint64]int{b1: 1, b2: 1, c: 1}, e.deletedIDs())
		})
	}
}

// TestKazReplaceChunkChanged rejects a stale origin, keeps the chunk and leaves
// the new slice to be deleted.
func TestKazReplaceChunkChanged(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			r := e.en.(kazChunkReplacer)
			inode := e.file(t)
			e.write(t, inode, 0, 100)
			e.write(t, inode, 50, 100)
			ss := e.records(t, inode)
			stale := kazMarshal(ss)
			stale[4] ^= 0xff // a different slice id in the first record
			var c uint64
			require.Zero(t, e.NewSlice(Background(), &c))
			st := r.doKazReplaceChunk(inode, 0, stale, marshalSlice(0, c, 150, 0, 150), ss, []Slice{{Id: c, Size: 150, Len: 150}}, nil, 0)
			require.Equal(t, syscall.EINVAL, st)
			require.Equal(t, kazIDs(ss), kazIDs(e.records(t, inode)))
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			require.Equal(t, map[uint64]int{c: 1}, e.deletedIDs())
		})
	}
}

// TestKazReplaceChunkDelayed records dropped slices for trash instead of releasing them.
func TestKazReplaceChunkDelayed(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			r := e.en.(kazChunkReplacer)
			inode := e.file(t)
			b1 := e.write(t, inode, 0, 100)
			b2 := e.write(t, inode, 50, 100)
			ss := e.records(t, inode)
			var c uint64
			require.Zero(t, e.NewSlice(Background(), &c))
			var delayed []byte
			for _, s := range ss {
				delayed = append(delayed, e.encodeDelayedSlice(s.id, s.size)...)
			}
			require.Zero(t, r.doKazReplaceChunk(inode, 0, kazMarshal(ss), marshalSlice(0, c, 150, 0, 150), ss, []Slice{{Id: c, Size: 150, Len: 150}}, delayed, c))
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			require.Empty(t, e.deletedIDs())
			_, err := e.en.doCleanupDelayedSlices(Background(), time.Now().Add(time.Hour).Unix())
			require.NoError(t, err)
			require.Equal(t, map[uint64]int{b1: 1, b2: 1}, e.deletedIDs())
		})
	}
}
```

- [ ] **Step 3: 失敗することを確かめる**

Run: `go test ./pkg/meta -run 'TestKazReplaceChunk' -count=1`
Expected: コンパイルエラー（`kazChunkReplacer` が未定義）

- [ ] **Step 4: Redis の実装**

`pkg/meta/redis_compact_kaz.go`（ライセンスのヘッダー付き）:

```go
package meta

import (
	"fmt"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
)

// kazChunkReplacer replaces the leading records of a chunk with an arbitrary new
// list in one transaction (kaz block compaction). It mirrors doCompactChunk,
// which can only put a single new record, and lives outside the engine interface
// so upstream changes to that interface do not touch it.
type kazChunkReplacer interface {
	// doKazReplaceChunk replaces origin (the chunk's leading records) by replaced
	// when they still match, creates a reference for each created slice and either
	// records delayed (trash, keyed by delayedKey) or drops one reference of each
	// removed record. It returns EINVAL when the chunk no longer starts with origin.
	doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []Slice, delayed []byte, delayedKey uint64) syscall.Errno
}

// doKazReplaceChunk follows redisMeta.doCompactChunk with a list of new records.
func (m *redisMeta) doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []Slice, delayed []byte, delayedKey uint64) syscall.Errno {
	var rs []*redis.IntCmd // trash disabled: check reference of slices
	if delayed == nil {
		rs = make([]*redis.IntCmd, len(removed))
	}
	key := m.chunkKey(inode, indx)
	ctx := Background()
	st := errno(m.txn(ctx, func(tx *redis.Tx) error {
		n := len(origin) / sliceBytes
		vals, err := tx.LRange(ctx, key, 0, int64(n-1)).Result()
		if err != nil {
			return err
		}
		if len(vals) != n {
			return syscall.EINVAL
		}
		for i, val := range vals {
			if val != string(origin[i*sliceBytes:(i+1)*sliceBytes]) {
				return syscall.EINVAL
			}
		}
		_, err = tx.TxPipelined(ctx, func(pipe redis.Pipeliner) error {
			pipe.LTrim(ctx, key, int64(n), -1)
			for i := len(replaced) / sliceBytes; i > 0; i-- {
				pipe.LPush(ctx, key, replaced[(i-1)*sliceBytes:i*sliceBytes])
			}
			for _, c := range created {
				pipe.HSet(ctx, m.sliceRefs(), m.sliceKey(c.Id, c.Size), "0") // create the key to tracking it
			}
			if delayed != nil {
				if len(delayed) > 0 {
					pipe.HSet(ctx, m.delSlices(), fmt.Sprintf("%d_%d", delayedKey, time.Now().Unix()), delayed)
				}
			} else {
				for i, s := range removed {
					if s.id > 0 {
						rs[i] = pipe.HIncrBy(ctx, m.sliceRefs(), m.sliceKey(s.id, s.size), -1)
					}
				}
			}
			m.genLog(ctx, pipe, time.Now(), "KAZCOMPACTCHUNK(%d,%d,%d,%d,%d)", inode, indx, n, len(replaced)/sliceBytes, len(created))
			return nil
		})
		return err
	}, key))
	// there could be false-negative that the replacement is successful, double-check
	if st != 0 && st != syscall.EINVAL && len(created) > 0 {
		if e := m.rdb.HGet(ctx, m.sliceRefs(), m.sliceKey(created[0].Id, created[0].Size)).Err(); e == nil {
			st = 0
		} else if e == redis.Nil {
			st = syscall.EINVAL
		}
	}
	if st == syscall.EINVAL {
		for _, c := range created {
			m.rdb.HIncrBy(ctx, m.sliceRefs(), m.sliceKey(c.Id, c.Size), -1)
		}
	} else if st == 0 {
		for _, c := range created {
			m.cleanupZeroRef(m.sliceKey(c.Id, c.Size))
		}
		if delayed == nil {
			for i, s := range removed {
				if s.id > 0 && rs[i].Err() == nil && rs[i].Val() < 0 {
					m.enqueueCompactionDelete(s.id, s.size)
				}
			}
		}
	}
	return st
}
```

- [ ] **Step 5: SQL の実装**

`pkg/meta/sql_compact_kaz.go`:

```go
package meta

import (
	"bytes"
	"syscall"
	"time"

	"xorm.io/xorm"
)

// doKazReplaceChunk follows dbMeta.doCompactChunk with a list of new records.
func (m *dbMeta) doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []Slice, delayed []byte, delayedKey uint64) syscall.Errno {
	st := errno(m.txn(func(s *xorm.Session) error {
		var c2 = chunk{Inode: inode, Indx: indx}
		_, err := s.ForUpdate().MustCols("indx").Get(&c2)
		if err != nil {
			return err
		}
		if len(c2.Slices) < len(origin) || !bytes.Equal(origin, c2.Slices[:len(origin)]) {
			logger.Infof("chunk %d:%d was changed %d -> %d", inode, indx, len(origin), len(c2.Slices))
			return syscall.EINVAL
		}
		c2.Slices = append(append([]byte{}, replaced...), c2.Slices[len(origin):]...)
		if _, err := s.Cols("slices").Where("Inode = ? AND indx = ?", inode, indx).Update(c2); err != nil {
			return err
		}
		for _, c := range created {
			if err = mustInsert(s, sliceRef{c.Id, c.Size, 1}); err != nil { // create the key to tracking it
				return err
			}
		}
		if delayed != nil {
			if len(delayed) > 0 {
				if err = mustInsert(s, &delslices{delayedKey, time.Now().Unix(), delayed}); err != nil {
					return err
				}
			}
		} else {
			for _, s_ := range removed {
				if s_.id == 0 {
					continue
				}
				if _, err := s.Exec(m.sqlConv("update chunk_ref set refs=refs-1 where chunkid=? AND size=?"), s_.id, s_.size); err != nil {
					return err
				}
			}
		}
		m.genLog(Background(), s, time.Now().UnixNano(), "KAZCOMPACTCHUNK(%d,%d,%d,%d,%d)", inode, indx, len(origin)/sliceBytes, len(replaced)/sliceBytes, len(created))
		return nil
	}, inode))
	// there could be false-negative that the replacement is successful, double-check
	if st != 0 && st != syscall.EINVAL && len(created) > 0 {
		var ok bool
		if err := m.simpleTxn(Background(), func(s *xorm.Session) error {
			var e error
			ok, e = s.Get(&sliceRef{Id: created[0].Id})
			return e
		}); err == nil {
			if ok {
				st = 0
			} else {
				st = syscall.EINVAL
			}
		}
	}
	if st == syscall.EINVAL {
		_ = m.txn(func(s *xorm.Session) error {
			for _, c := range created {
				if err := mustInsert(s, &sliceRef{c.Id, c.Size, 0}); err != nil {
					return err
				}
			}
			return nil
		})
	} else if st == 0 && delayed == nil {
		for _, s := range removed {
			if s.id == 0 {
				continue
			}
			var ref = sliceRef{Id: s.id}
			var ok bool
			err := m.simpleTxn(Background(), func(s *xorm.Session) error {
				var e error
				ok, e = s.Get(&ref)
				return e
			})
			if err == nil && ok && ref.Refs <= 0 {
				m.enqueueCompactionDelete(s.id, s.size)
			}
		}
	}
	return st
}
```

注意: upstream の EINVAL の後始末は `sliceRef{id, size, 0}` の insert（新しい id は txn が失敗していれば未作成）。txn の途中で失敗して一部の行が残ることは無い（同じ txn）。

- [ ] **Step 6: TKV の実装**

`pkg/meta/tkv_compact_kaz.go`:

```go
package meta

import (
	"bytes"
	"syscall"
	"time"
)

// doKazReplaceChunk follows kvMeta.doCompactChunk with a list of new records.
func (m *kvMeta) doKazReplaceChunk(inode Ino, indx uint32, origin, replaced []byte, removed []*slice, created []Slice, delayed []byte, delayedKey uint64) syscall.Errno {
	st := errno(m.txn(Background(), func(tx *kvTxn) error {
		buf2 := tx.get(m.chunkKey(inode, indx))
		if len(buf2) < len(origin) || !bytes.Equal(origin, buf2[:len(origin)]) {
			logger.Infof("chunk %d:%d was changed %d -> %d", inode, indx, len(origin), len(buf2))
			return syscall.EINVAL
		}
		buf2 = append(append([]byte{}, replaced...), buf2[len(origin):]...)
		if len(buf2) == 0 {
			tx.delete(m.chunkKey(inode, indx))
		} else {
			tx.set(m.chunkKey(inode, indx), buf2)
		}
		for _, c := range created {
			tx.set(m.sliceKey(c.Id, c.Size), make([]byte, 8)) // create the key to tracking it
		}
		if delayed != nil {
			if len(delayed) > 0 {
				tx.set(m.delSliceKey(time.Now().Unix(), delayedKey), delayed)
			}
		} else {
			for _, s := range removed {
				if s.id > 0 {
					tx.incrBy(m.sliceKey(s.id, s.size), -1)
				}
			}
		}
		m.genLog(tx, time.Now(), "KAZCOMPACTCHUNK(%d,%d,%d,%d,%d)", inode, indx, len(origin)/sliceBytes, len(replaced)/sliceBytes, len(created))
		return nil
	}, inode))
	// there could be false-negative that the replacement is successful, double-check
	if st != 0 && st != syscall.EINVAL && len(created) > 0 {
		if refs, e := m.get(m.sliceKey(created[0].Id, created[0].Size)); e == nil {
			if len(refs) > 0 {
				st = 0
			} else {
				st = syscall.EINVAL
			}
		}
	}
	if st == syscall.EINVAL {
		_ = m.txn(Background(), func(tx *kvTxn) error {
			for _, c := range created {
				tx.incrBy(m.sliceKey(c.Id, c.Size), -1)
			}
			return nil
		})
	} else if st == 0 {
		for _, c := range created {
			m.cleanupZeroRef(c.Id, c.Size)
		}
		if delayed == nil {
			var refs int64
			for _, s := range removed {
				if s.id > 0 && m.client.txn(Background(), func(tx *kvTxn) error {
					refs = tx.incrBy(m.sliceKey(s.id, s.size), 0)
					return nil
				}, 0) == nil && refs < 0 {
					m.enqueueCompactionDelete(s.id, s.size)
				}
			}
		}
	}
	return st
}
```

- [ ] **Step 7: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'TestKazReplaceChunk' -count=1 -v 2>&1 | grep -E '^(=== RUN|--- |PASS|FAIL|ok)'`
Expected: memkv・sqlite は PASS、redis は SKIP（Task 7 で一時 Redis を使って流す）。
もし `TestKazReplaceChunkDelayed` の delayed の処理で期待と違う削除になったら、upstream の `doCleanupDelayedSlices` の挙動を読み、テストの期待ではなく実装の誤りかを先に確かめる（systematic-debugging）。

- [ ] **Step 8: commit（ユーザーの許可がある場合）**

```bash
git add pkg/meta/redis_compact_kaz.go pkg/meta/sql_compact_kaz.go pkg/meta/tkv_compact_kaz.go pkg/meta/compaction_block_replace_kaz_test.go pkg/meta/compaction_block_kaz_helpers_test.go
git commit -m "meta: replace a chunk's leading records with a new list on redis, sql and tkv (kaz)"
```

---

### Task 4: ブロック方式の本体（書き込み、置き換え、後始末、metrics）

**Files:**
- Create: `pkg/meta/compaction_block_kaz.go`（`kazChunkReplacer` を `redis_compact_kaz.go` から移す）
- Modify: `pkg/meta/redis_compact_kaz.go`（インターフェースの定義を削除）
- Modify: `pkg/meta/compaction_policy_kaz.go`（metrics の struct に `block` を足す）
- Create: `pkg/meta/compaction_block_kaz_test.go`

**Interfaces:**
- Consumes: Task 1 の config、Task 2 の `planKazBlockCompaction`、Task 3 の `kazChunkReplacer`、upstream の `m.compacting`、`m.en.doRead`、`maxCompactSlices`、`NewSlice`、`newMsg(CompactChunk, []Slice, uint64, uint8)`、`GetAttr`、`toTrash`、`encodeDelayedSlice`、`deleteSlice`、`m.of.InvalidateChunk`。
- Produces:
  - `func (m *baseMeta) compactChunkBlocks(r kazChunkReplacer, inode Ino, indx uint32, tier int)`
  - `func (m *baseMeta) kazBlockSize() uint32`（0 = 使えない）
  - `func kazBlockCompactLimit(blockSize uint32) (int, bool)`
  - `func (m *baseMeta) kazBlockEvent(event string, n int)`、metrics `kaz_compaction_block_events_total{event}`（event: `noop`、`partial`、`full`、`wasted`、`error`、`fallback_upstream`、`blocks_written`、`masks`、`removed`）
  - DEBUG のログ: 新しい slice ごとに upstream と同じ形の `compaction inode=%d chunk=%d slice=%d phase=object slices=%d bytes=%d`（既存の `comp_puts.py` が数えられる）、1 回ごとに `compaction block inode=%d chunk=%d mode=%s removed=%d kept=%d blocks=%d mask=%d hidden=%.2f errno=%s`

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/compaction_block_kaz_test.go`:

```go
package meta

import (
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"github.com/stretchr/testify/require"
)

const kazEB = uint32(4096) // block size of the env (4 KiB)

// kazRun runs one block compaction of chunk 0 and returns the chunk's records.
func kazRun(t *testing.T, e *kazBlockEnv, inode Ino) []*slice {
	t.Helper()
	e.compactChunkBlocks(e.en.(kazChunkReplacer), inode, 0, 0)
	return e.records(t, inode)
}

// kazBlockEventCount reads one block-compaction event counter.
func kazBlockEventCount(e *kazBlockEnv, event string) float64 {
	return testutil.ToFloat64(e.kazCompactMetrics.block.WithLabelValues(event))
}

// TestKazBlockCompactionPartial rewrites only touched blocks, keeps the view and
// releases exactly the dropped slices, then rewrites a changed block again.
func TestKazBlockCompactionPartial(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			inode := e.file(t)
			base := e.write(t, inode, 0, 16*kazEB)
			small := []uint64{
				e.write(t, inode, 2*kazEB+100, 300),
				e.write(t, inode, 3*kazEB+10, 20),
				e.write(t, inode, 9*kazEB, 50),
				e.write(t, inode, 2*kazEB+200, 10),
			}
			e.zero(t, inode, 9*kazEB+100, 10)
			before := e.store.view(e.records(t, inode), 16*kazEB)
			after := kazRun(t, e, inode)
			require.Equal(t, before, e.store.view(after, 16*kazEB))
			require.Equal(t, 3, e.store.callCount())
			require.Len(t, after, 4)
			require.Equal(t, base, after[0].id)
			for i, b := range []uint32{2, 3, 9} {
				require.Equal(t, b*kazEB, after[i+1].pos)
				require.Equal(t, kazEB, after[i+1].len)
			}
			require.Equal(t, float64(1), kazBlockEventCount(e, "partial"))
			require.Equal(t, float64(3), kazBlockEventCount(e, "blocks_written"))
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			want := map[uint64]int{}
			for _, id := range small {
				want[id] = 1
			}
			require.Equal(t, want, e.deletedIDs())

			// A new write in block 2 replaces that block's record; others stay.
			e.write(t, inode, 2*kazEB+7, 3)
			before = e.store.view(e.records(t, inode), 16*kazEB)
			again := kazRun(t, e, inode)
			require.Equal(t, before, e.store.view(again, 16*kazEB))
			require.Equal(t, 4, e.store.callCount())
			require.Equal(t, []uint64{base, after[2].id, after[3].id}, kazIDs(again[:3]))
		})
	}
}

// TestKazBlockCompactionFull rewrites every block once most of the base is hidden.
func TestKazBlockCompactionFull(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			inode := e.file(t)
			base := e.write(t, inode, 0, 16*kazEB)
			for i := uint32(0); i < 9; i++ {
				e.write(t, inode, i*kazEB+1, 10)
			}
			before := e.store.view(e.records(t, inode), 16*kazEB)
			after := kazRun(t, e, inode)
			require.Equal(t, before, e.store.view(after, 16*kazEB))
			require.Len(t, after, 16)
			require.Equal(t, 16, e.store.callCount())
			require.Equal(t, float64(1), kazBlockEventCount(e, "full"))
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			require.Equal(t, 1, e.deletedIDs()[base])

			// Nothing small is left: the next run writes nothing.
			require.Equal(t, kazIDs(after), kazIDs(kazRun(t, e, inode)))
			require.Equal(t, 16, e.store.callCount())
			require.Equal(t, float64(1), kazBlockEventCount(e, "noop"))
		})
	}
}

// TestKazBlockCompactionWriteFailure leaves metadata untouched and deletes the
// slices already written when one block fails.
func TestKazBlockCompactionWriteFailure(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			inode := e.file(t)
			e.write(t, inode, 0, 16*kazEB)
			for _, b := range []uint32{1, 4, 7} {
				e.write(t, inode, b*kazEB+3, 30)
			}
			var failed atomic.Uint64 // blocks are written concurrently
			e.store.fail = func(id uint64) error {
				if failed.CompareAndSwap(0, id) {
					return errors.New("injected upload failure")
				}
				return nil
			}
			before := kazIDs(e.records(t, inode))
			require.Equal(t, before, kazIDs(kazRun(t, e, inode)))
			written := e.store.callIDs()
			require.Len(t, written, 2)
			deleted := e.deletedIDs()
			for _, id := range written {
				require.Equal(t, 1, deleted[id])
			}
			require.Zero(t, deleted[failed.Load()])
			require.Equal(t, float64(1), kazBlockEventCount(e, "error"))
		})
	}
}

// TestKazBlockCompactionConcurrentCommit keeps a record committed during the
// compaction on top of the new block records.
func TestKazBlockCompactionConcurrentCommit(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			inode := e.file(t)
			e.write(t, inode, 0, 16*kazEB)
			e.write(t, inode, 5*kazEB+1, 10)
			e.write(t, inode, 6*kazEB+1, 10)
			origin := e.records(t, inode)
			var late uint64
			var lateErr error
			e.store.during = func() {
				if st := e.NewSlice(Background(), &late); st != 0 {
					lateErr = st
					return
				}
				var n int
				var d dirStat
				var attr Attr
				if st := e.en.doWrite(Background(), inode, 0, 5*kazEB, Slice{Id: late, Size: 100, Len: 100}, time.Now(), &n, &d, &attr); st != 0 {
					lateErr = st
				}
			}
			after := kazRun(t, e, inode)
			require.NoError(t, lateErr)
			require.Equal(t, late, after[len(after)-1].id)
			want := e.store.view(append(origin, &slice{pos: 5 * kazEB, id: late, size: 100, len: 100}), 16*kazEB)
			require.Equal(t, want, e.store.view(after, 16*kazEB))
		})
	}
}

// TestKazBlockCompactionLimit compacts only the leading records when the chunk
// exceeds the engine's limit and keeps the rest on top.
func TestKazBlockCompactionLimit(t *testing.T) {
	old := maxCompactSlices
	maxCompactSlices = 100 // as for etcd
	t.Cleanup(func() { maxCompactSlices = old })
	e := newKazBlockEnv(t, "memkv", 1024) // 1 MiB blocks: 64 per chunk, limit 36
	inode := e.file(t)
	for i := uint32(0); i < 50; i++ {
		e.write(t, inode, (i%3)<<20+i*100, 50)
	}
	before := e.store.view(e.records(t, inode), 3<<20)
	after := kazRun(t, e, inode)
	require.Equal(t, before, e.store.view(after, 3<<20))
	require.Len(t, after, 3+14)
}

// TestKazBlockCompactionTrash records dropped slices for the trash instead of releasing them.
func TestKazBlockCompactionTrash(t *testing.T) {
	for _, backend := range kazBlockBackends {
		t.Run(backend, func(t *testing.T) {
			e := newKazBlockEnv(t, backend, 4)
			e.Lock()
			e.fmt.TrashDays = 1
			e.Unlock()
			inode := e.file(t)
			e.write(t, inode, 0, 16*kazEB)
			s1 := e.write(t, inode, kazEB+1, 10)
			s2 := e.write(t, inode, 8*kazEB+1, 10)
			kazRun(t, e, inode)
			require.NoError(t, e.en.doCleanupSlices(Background(), nil))
			require.Empty(t, e.deletedIDs())
			_, err := e.en.doCleanupDelayedSlices(Background(), time.Now().Add(time.Hour).Unix())
			require.NoError(t, err)
			require.Equal(t, map[uint64]int{s1: 1, s2: 1}, e.deletedIDs())
		})
	}
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./pkg/meta -run 'TestKazBlockCompaction(Partial|Full|WriteFailure|ConcurrentCommit|Limit|Trash)' -count=1`
Expected: コンパイルエラー（`compactChunkBlocks`、`kazCompactMetrics.block` が未定義）

- [ ] **Step 3: metrics を足す**

`pkg/meta/compaction_policy_kaz.go` の `kazCompactionMetrics` に項目 `block *prometheus.CounterVec` を足し、`newKazCompactionMetrics` で:

```go
		block: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "kaz_compaction_block_events_total",
			Help: "Kaz block compaction outcomes (noop, partial, full, wasted, error, fallback_upstream) and amounts (blocks_written, masks, removed).",
		}, []string{"event"}),
```

`register` を `reg.MustRegister(k.events, k.queueWait, k.slices, k.block)` にする。

- [ ] **Step 4: 本体を実装**

`redis_compact_kaz.go` から `kazChunkReplacer` の定義（コメントごと）を切り取り、`pkg/meta/compaction_block_kaz.go` に置く（ライセンスのヘッダー付き）:

```go
package meta

import (
	"errors"
	"strings"
	"sync"
	"syscall"
	"time"
)

// kazChunkReplacer ... (Task 3 の定義をそのまま移す)

const (
	// kazUpstreamMaxCompactSlices is the upstream value of maxCompactSlices;
	// engines lower it (etcd: 100) to bound their transaction size.
	kazUpstreamMaxCompactSlices = 1000
	// kazBlockMinCompactSlices is the smallest useful record limit after making
	// room for the new references of a lowered engine limit.
	kazBlockMinCompactSlices = 10
	// kazBlockWriteConcurrency bounds the blocks written at once by one compaction.
	kazBlockWriteConcurrency = 16
)

// kazBlockSize returns the volume block size in bytes, or 0 when it cannot split
// a chunk into whole blocks (block compaction then falls back to upstream).
func (m *baseMeta) kazBlockSize() uint32 {
	f := m.getFormat()
	if f == nil || f.BlockSize <= 0 {
		return 0
	}
	bs := uint64(f.BlockSize) << 10
	if bs > ChunkSize || ChunkSize%bs != 0 {
		return 0
	}
	return uint32(bs)
}

// kazBlockCompactLimit returns how many leading records one block compaction may
// take. Engines that lowered maxCompactSlices (etcd) also need room for one new
// reference per block; ok is false when too little room is left.
func kazBlockCompactLimit(blockSize uint32) (int, bool) {
	if maxCompactSlices >= kazUpstreamMaxCompactSlices {
		return maxCompactSlices, true
	}
	limit := maxCompactSlices - int(ChunkSize/uint64(blockSize))
	return limit, limit >= kazBlockMinCompactSlices
}

// kazBlockEvent adds n to one block-compaction counter.
func (m *baseMeta) kazBlockEvent(event string, n int) {
	if m.kazCompactMetrics != nil && n > 0 {
		m.kazCompactMetrics.block.WithLabelValues(event).Add(float64(n))
	}
}

// compactChunkBlocks rewrites only the blocks of a chunk that small records
// touch, one new slice per block, and replaces the leading records through r.
// It shares m.compacting with upstream compactChunk, so a chunk is never
// compacted twice at once by this client.
func (m *baseMeta) compactChunkBlocks(r kazChunkReplacer, inode Ino, indx uint32, tier int) {
	k := uint64(inode) + (uint64(indx) << 40)
	m.Lock()
	if (m.sessCtx != nil && m.sessCtx.Canceled()) || len(m.compacting) > 10 || m.compacting[k] {
		m.Unlock()
		return
	}
	m.compacting[k] = true
	m.Unlock()
	defer func() {
		m.Lock()
		delete(m.compacting, k)
		m.Unlock()
	}()

	ss, st := m.en.doRead(Background(), inode, indx)
	if st != 0 {
		return
	}
	if ss == nil {
		logger.Errorf("Corrupt value for inode %d chunk indx %d", inode, indx)
		return
	}
	bs := m.kazBlockSize()
	limit, _ := kazBlockCompactLimit(bs) // the caller checked ok
	if len(ss) > limit {
		ss = ss[:limit]
	}
	plan := planKazBlockCompaction(ss, bs, m.conf.kazBlockHiddenRatio(), m.conf.kazBlockMaxSlices(int(ChunkSize/uint64(bs))))
	if plan == nil {
		m.kazBlockEvent("noop", 1)
		return
	}
	if tier == -1 {
		var attr Attr
		if eno := m.GetAttr(Background(), inode, &attr); eno != 0 {
			return
		}
		tier = int(attr.Tier)
	}
	created, err := m.writeKazBlocks(inode, indx, plan.outs, tier)
	if err != nil {
		for _, c := range created {
			m.deleteSlice(c.Id, c.Size)
		}
		if !strings.Contains(err.Error(), "not exist") && !strings.Contains(err.Error(), "not found") {
			logger.Warnf("block compaction %d %d with %d slices: %s", inode, indx, len(plan.removed), err)
		}
		m.kazBlockEvent("error", 1)
		return
	}

	replaced := make([]byte, 0, (len(plan.kept)+len(plan.outs))*sliceBytes)
	for _, s := range plan.kept {
		replaced = append(replaced, marshalSlice(s.pos, s.id, s.size, s.off, s.len)...)
	}
	masks, ci := 0, 0
	for _, o := range plan.outs {
		if o.mask {
			replaced = append(replaced, marshalSlice(o.pos, 0, 0, 0, o.length)...)
			masks++
			continue
		}
		c := created[ci]
		ci++
		replaced = append(replaced, marshalSlice(o.pos, c.Id, c.Size, 0, c.Size)...)
	}
	origin := make([]byte, 0, len(ss)*sliceBytes)
	for _, s := range ss {
		origin = append(origin, marshalSlice(s.pos, s.id, s.size, s.off, s.len)...)
	}
	var delayed []byte
	var delayedKey uint64
	if m.toTrash(0) {
		delayed = make([]byte, 0, len(plan.removed)*12)
		for _, s := range plan.removed {
			if s.id > 0 {
				delayed = append(delayed, m.encodeDelayedSlice(s.id, s.size)...)
			}
		}
		if len(created) > 0 {
			delayedKey = created[0].Id
		} else if len(delayed) > 0 {
			if st = m.NewSlice(Background(), &delayedKey); st != 0 {
				m.kazBlockEvent("error", 1)
				return
			}
		}
	}

	start := time.Now()
	st = r.doKazReplaceChunk(inode, indx, origin, replaced, plan.removed, created, delayed, delayedKey)
	mode := "partial"
	if plan.full {
		mode = "full"
	}
	logger.Debugf("compaction block inode=%d chunk=%d mode=%s removed=%d kept=%d blocks=%d mask=%d hidden=%.2f metadata=%s errno=%s",
		inode, indx, mode, len(plan.removed), len(plan.kept), len(created), masks, plan.hidden, time.Since(start), st)
	switch st {
	case 0:
		m.of.InvalidateChunk(inode, indx)
		m.kazBlockEvent(mode, 1)
		m.kazBlockEvent("blocks_written", len(created))
		m.kazBlockEvent("masks", masks)
		m.kazBlockEvent("removed", len(plan.removed))
	case syscall.EINVAL:
		logger.Infof("block compaction for %d:%d is wasted, delete %d new slices", inode, indx, len(created))
		for _, c := range created {
			m.deleteSlice(c.Id, c.Size)
		}
		m.kazBlockEvent("wasted", 1)
	default:
		logger.Warnf("block compaction %d %d: %s; %d new slices may be orphaned, will be cleaned by gc", inode, indx, st, len(created))
		m.kazBlockEvent("error", 1)
	}
}

// writeKazBlocks allocates one slice per data output and writes them through the
// CompactChunk message, at most kazBlockWriteConcurrency at a time, so a chunk's
// blocks upload in parallel as upstream's single slice does. It returns the
// slices written successfully (in output order) and the joined errors.
func (m *baseMeta) writeKazBlocks(inode Ino, indx uint32, outs []kazBlockOut, tier int) ([]Slice, error) {
	var data []kazBlockOut
	for _, o := range outs {
		if !o.mask {
			data = append(data, o)
		}
	}
	ids := make([]uint64, len(data))
	for i := range ids {
		if st := m.NewSlice(Background(), &ids[i]); st != 0 {
			return nil, st
		}
	}
	errs := make([]error, len(data))
	sem := make(chan struct{}, kazBlockWriteConcurrency)
	var wg sync.WaitGroup
	for i, o := range data {
		logger.Debugf("compaction inode=%d chunk=%d slice=%d phase=object slices=%d bytes=%d", inode, indx, ids[i], len(o.pieces), o.length)
		sem <- struct{}{}
		wg.Add(1)
		go func(i int, o kazBlockOut) {
			defer wg.Done()
			defer func() { <-sem }()
			errs[i] = m.newMsg(CompactChunk, o.pieces, ids[i], uint8(tier))
		}(i, o)
	}
	wg.Wait()
	var written []Slice
	var err error
	for i, o := range data {
		if errs[i] != nil {
			err = errors.Join(err, errs[i])
			continue
		}
		written = append(written, Slice{Id: ids[i], Size: o.length, Len: o.length})
	}
	return written, err
}
```

`redis_compact_kaz.go` の import から未使用になったものが無いことを確かめる（`syscall` は引き続き使う）。

- [ ] **Step 5: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'TestKazBlockCompaction|TestPlanKazBlockCompaction|TestKazReplaceChunk' -count=1 -race`
Expected: PASS（redis の subtest は SKIP）、DATA RACE なし

- [ ] **Step 6: commit（ユーザーの許可がある場合）**

```bash
git add pkg/meta/compaction_block_kaz.go pkg/meta/compaction_block_kaz_test.go pkg/meta/redis_compact_kaz.go pkg/meta/compaction_policy_kaz.go
git commit -m "meta: rewrite only changed blocks in kaz block compaction, one slice per block"
```

---

### Task 5: 振り分けと mount 時のログ

**Files:**
- Modify: `pkg/meta/compaction_block_kaz.go`（`backgroundCompactChunk`、`logKazCompactionMode`）
- Modify: `pkg/meta/compaction_scheduler_lifecycle.go`（2 か所の呼び出しと、ログの呼び出し）
- Modify: `pkg/meta/compaction_block_kaz_test.go`（テストを足す）

**Interfaces:**
- Consumes: Task 4 の `compactChunkBlocks`、`kazBlockSize`、`kazBlockCompactLimit`、`kazBlockEvent`。
- Produces: `func (m *baseMeta) backgroundCompactChunk(inode Ino, indx uint32, tier int)`、`func (m *baseMeta) logKazCompactionMode()`。

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/compaction_block_kaz_test.go` に追記:

```go
// TestKazBackgroundCompactionDefaultIsUpstream merges a chunk into one slice
// when the block mode is not selected.
func TestKazBackgroundCompactionDefaultIsUpstream(t *testing.T) {
	e := newKazBlockEnv(t, "memkv", 4)
	e.conf.KazCompactionMode = ""
	inode := e.file(t)
	e.write(t, inode, 0, 16*kazEB)
	e.write(t, inode, 2*kazEB+1, 10)
	e.write(t, inode, 9*kazEB+1, 10)
	e.backgroundCompactChunk(inode, 0, 0)
	require.Len(t, e.records(t, inode), 1)
	require.Equal(t, 1, e.store.callCount())
	require.Zero(t, kazBlockEventCount(e, "partial"))
}

// TestKazBackgroundCompactionBlockMode takes the block path when selected.
func TestKazBackgroundCompactionBlockMode(t *testing.T) {
	e := newKazBlockEnv(t, "memkv", 4)
	inode := e.file(t)
	e.write(t, inode, 0, 16*kazEB)
	e.write(t, inode, 2*kazEB+1, 10)
	e.write(t, inode, 9*kazEB+1, 10)
	e.backgroundCompactChunk(inode, 0, 0)
	require.Len(t, e.records(t, inode), 3)
	require.Equal(t, 2, e.store.callCount())
}

// TestKazBackgroundCompactionFallsBack uses upstream compaction when the block
// size cannot split a chunk.
func TestKazBackgroundCompactionFallsBack(t *testing.T) {
	e := newKazBlockEnv(t, "memkv", 0)
	inode := e.file(t)
	e.write(t, inode, 0, 16*kazEB)
	e.write(t, inode, 2*kazEB+1, 10)
	e.backgroundCompactChunk(inode, 0, 0)
	require.Len(t, e.records(t, inode), 1)
	require.Equal(t, float64(1), kazBlockEventCount(e, "fallback_upstream"))
}

// TestKazBlockCompactionThroughScheduler reaches the block path from a request
// to the priority scheduler.
func TestKazBlockCompactionThroughScheduler(t *testing.T) {
	e := newKazBlockEnv(t, "memkv", 4)
	e.conf.CompactionScheduler = "priority"
	e.startPriorityCompactor()
	t.Cleanup(e.stopPriorityCompactor)
	inode := e.file(t)
	e.write(t, inode, 0, 16*kazEB)
	e.write(t, inode, 2*kazEB+1, 10)
	e.write(t, inode, 9*kazEB+1, 10)
	e.requestBackgroundCompaction(inode, 0, 3, 0)
	// The condition runs in another goroutine, so it must not call require.
	require.Eventually(t, func() bool {
		ss, st := e.en.doRead(Background(), inode, 0)
		return st == 0 && len(ss) == 3
	}, 5*time.Second, 10*time.Millisecond)
	require.Equal(t, 2, e.store.callCount())
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./pkg/meta -run 'TestKazBackgroundCompaction|TestKazBlockCompactionThroughScheduler' -count=1`
Expected: コンパイルエラー（`backgroundCompactChunk` が未定義）

- [ ] **Step 3: 実装**

`pkg/meta/compaction_block_kaz.go` に追記:

```go
// backgroundCompactChunk runs one background compaction of a chunk: the kaz
// block mode when selected and usable, otherwise upstream compactChunk. Forced
// and synchronous compaction call compactChunk directly and never come here.
func (m *baseMeta) backgroundCompactChunk(inode Ino, indx uint32, tier int) {
	if m.conf.kazBlockCompactionEnabled() {
		r, ok := m.en.(kazChunkReplacer)
		if bs := m.kazBlockSize(); ok && bs > 0 {
			if _, fits := kazBlockCompactLimit(bs); fits {
				m.compactChunkBlocks(r, inode, indx, tier)
				return
			}
		}
		m.kazBlockEvent("fallback_upstream", 1)
	}
	m.compactChunk(inode, indx, false, false, tier)
}

// logKazCompactionMode states once at startup which background compaction runs.
func (m *baseMeta) logKazCompactionMode() {
	if !m.conf.kazBlockCompactionEnabled() {
		logger.Infof("kaz compaction mode: upstream")
		return
	}
	bs := m.kazBlockSize()
	_, ok := m.en.(kazChunkReplacer)
	_, fits := kazBlockCompactLimit(max(bs, 1))
	if !ok || bs == 0 || !fits {
		logger.Warnf("kaz compaction mode: block is not usable here (engine support %t, block size %d, room for new references %t); using upstream compaction", ok, bs, fits)
		return
	}
	blocks := int(ChunkSize / uint64(bs))
	logger.Infof("kaz compaction mode: block (block size %d, hidden-ratio %.2f, max-slices %d)", bs, m.conf.kazBlockHiddenRatio(), m.conf.kazBlockMaxSlices(blocks))
}
```

`pkg/meta/compaction_scheduler_lifecycle.go`:
- `startPriorityCompactor` の `m.startCompactionPolicy()` の次の行に `m.logKazCompactionMode()` を足す（コメントの「attaches the kaz compaction policy」に「and logs the compaction mode」を足す）。
- scheduler の callback の `m.compactChunk(inode, chunk, false, false, tier)` を `m.backgroundCompactChunk(inode, chunk, tier)` に替える。
- `requestBackgroundCompaction` の最後の `go m.compactChunk(inode, chunk, false, false, tier)` を `go m.backgroundCompactChunk(inode, chunk, tier)` に替える。

- [ ] **Step 4: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'Kaz|Compaction' -count=1 -race -skip '^(TestLoadDump|TestMySQLClient|TestTiKVClient)$'`
Expected: PASS（既存の policy と scheduler のテストも含む）

- [ ] **Step 5: commit（ユーザーの許可がある場合）**

```bash
git add pkg/meta/compaction_block_kaz.go pkg/meta/compaction_block_kaz_test.go pkg/meta/compaction_scheduler_lifecycle.go
git commit -m "meta: route background compaction through the kaz mode switch"
```

---

### Task 6: データのテスト（vfs、writeback の有効と無効）

**Files:**
- Create: `pkg/vfs/compact_block_kaz_test.go`

**Interfaces:**
- Consumes: Task 5 までの全体（`Meta.Read` の slice 5 個以上でのバックグラウンドの要求 → `backgroundCompactChunk`）、既存の `vfs.Compact`、`chunk.NewCachedStore`、`NewVFS`。

- [ ] **Step 1: テストを書く**

`pkg/vfs/compact_block_kaz_test.go`（ライセンスのヘッダー付き）:

```go
package vfs

import (
	"bytes"
	"fmt"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/juicedata/juicefs/pkg/chunk"
	"github.com/juicedata/juicefs/pkg/meta"
	"github.com/juicedata/juicefs/pkg/object"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/stretchr/testify/require"
)

// newKazBlockTestVFS builds a VFS in kaz block-compaction mode whose CompactChunk
// messages run vfs.Compact, recording the new slice ids and their lengths.
func newKazBlockTestVFS(t *testing.T, writeback bool) (*VFS, *sync.Map) {
	t.Helper()
	metaConf := meta.DefaultConf()
	metaConf.MountPoint = "/jfs"
	metaConf.KazCompactionMode = meta.KazCompactionModeBlock
	m := meta.NewClient("memkv://", metaConf)
	format := &meta.Format{Name: "test", UUID: uuid.New().String(), Storage: "mem", BlockSize: 4, Compression: "lz4", DirStats: true}
	require.NoError(t, m.Init(format, true))
	cconf := &chunk.Config{
		BlockSize: format.BlockSize * 1024, Compress: format.Compression, MaxUpload: 2, MaxDownload: 200,
		BufferSize: 30 << 20, CacheSize: 10 << 20, CacheDir: "memory",
	}
	if writeback {
		cconf.CacheDir = t.TempDir()
		cconf.Writeback = true
		cconf.WritebackThresholdSize = cconf.BlockSize + 1 // stage every block
	}
	blob, _ := object.CreateStorage("mem", "", "", "", "")
	registry := prometheus.NewRegistry()
	store := chunk.NewCachedStore(blob, *cconf, registry)
	conf := &Config{Meta: metaConf, Format: *format, Version: "Juicefs", Chunk: cconf, FuseOpts: &FuseOptions{}}
	v := NewVFS(conf, m, store, prometheus.WrapRegistererWithPrefix("juicefs_", registry), registry)
	written := &sync.Map{} // new slice id -> length
	m.OnMsg(meta.CompactChunk, func(args ...interface{}) error {
		err := Compact(*cconf, store, args[0].([]meta.Slice), args[1].(uint64), args[2].(uint8))
		if err == nil {
			var n uint32
			for _, s := range args[0].([]meta.Slice) {
				n += s.Len
			}
			written.Store(args[1].(uint64), n)
		}
		return err
	})
	return v, written
}

// TestKazBlockCompactionDataVFS writes a base and small updates, lets a read
// trigger background block compaction, and checks every byte afterwards.
func TestKazBlockCompactionDataVFS(t *testing.T) {
	for _, wb := range []bool{false, true} {
		t.Run(fmt.Sprintf("writeback=%t", wb), func(t *testing.T) {
			v, written := newKazBlockTestVFS(t, wb)
			ctx := NewLogContext(meta.NewContext(10, 1, []uint32{2, 3}))
			const B = 4096
			fe, fh, st := v.Create(ctx, 1, "image", 0644, 0, syscall.O_RDWR)
			require.Zero(t, st)
			want := make([]byte, 16*B)
			for i := range want {
				want[i] = byte(i%251 + 1)
			}
			require.Zero(t, v.Write(ctx, fe.Inode, want, 0, fh))
			require.Zero(t, v.Flush(ctx, fe.Inode, fh, 0))
			for i, off := range []int{2*B + 100, 5*B + 7, 11*B + 50, 2*B + 3000, 5*B + 4000} {
				upd := bytes.Repeat([]byte{byte(200 + i)}, 60)
				require.Zero(t, v.Write(ctx, fe.Inode, upd, uint64(off), fh))
				require.Zero(t, v.Flush(ctx, fe.Inode, fh, 0))
				copy(want[off:], upd)
			}
			buf := make([]byte, len(want))
			n, st := v.Read(ctx, fe.Inode, buf, 0, fh) // 6 records: requests compaction
			require.Zero(t, st)
			require.Equal(t, want, buf[:n])

			// Blocks 2, 5 and 11 are rewritten as three one-block slices.
			require.Eventually(t, func() bool {
				var slices []meta.Slice
				if v.Meta.Read(ctx, fe.Inode, 0, &slices) != 0 {
					return false
				}
				seen := 0
				for _, s := range slices {
					if _, ok := written.Load(s.Id); ok {
						seen++
					}
				}
				return seen == 3
			}, 10*time.Second, 20*time.Millisecond)
			written.Range(func(_, l any) bool {
				require.Equal(t, uint32(B), l.(uint32))
				return true
			})
			v.Release(ctx, fe.Inode, fh)

			_, fh, st = v.Open(ctx, fe.Inode, syscall.O_RDONLY)
			require.Zero(t, st)
			defer v.Release(ctx, fe.Inode, fh)
			buf = make([]byte, len(want))
			n, st = v.Read(ctx, fe.Inode, buf, 0, fh)
			require.Zero(t, st)
			require.Equal(t, want, buf[:n])
		})
	}
}
```

確認事項（書くときにソースで確かめる）: `v.Open` の戻り値（`entry, fh, err`）、`v.Meta` のフィールド名、`chunk.Config.WritebackThresholdSize` の型（int）。違えば合わせる。`Meta.Read` は `Read(ctx Context, inode Ino, indx uint32, slices *[]Slice) syscall.Errno`。

- [ ] **Step 2: 流す**

Run: `go test ./pkg/vfs -run 'TestKazBlockCompactionDataVFS' -count=1 -race -v 2>&1 | tail -15`
Expected: PASS（2 つの subtest）。Task 5 までが正しければ最初から通る。通らない場合は、テストの誤りか本体の誤りかを systematic-debugging で切り分ける。

- [ ] **Step 3: 振り分けを外して失敗することを確かめる（テストが効いていることの確認）**

一時的に `pkg/meta/compaction_block_kaz.go` の `backgroundCompactChunk` の先頭に `m.compactChunk(inode, indx, false, false, tier); return` を入れて流し、`seen == 3` の待ちで FAIL することを確かめる。確かめたら元に戻す（`git diff` で戻ったことを確認）。

- [ ] **Step 4: commit（ユーザーの許可がある場合）**

```bash
git add pkg/vfs/compact_block_kaz_test.go
git commit -m "vfs: check data after kaz block compaction with and without writeback"
```

---

### Task 7: 全体の確認、ビルド、記録

**Files:**
- Modify（調査ルート、commit しない）: `docs/findings.md`、`TODO.md`、`agent_memo.md`、`docs/superpowers/specs/2026-10-14-kaz-block-compaction-design.md`（状態の行）

- [ ] **Step 1: 一時 Redis で Redis の subtest を流す**

前回と同じく scratchpad の一時 Redis を使う（本番の Redis 56379 には接続しない）。

```bash
REDIS=$(ls /tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/*/scratchpad/redis-root/usr/bin/redis-server 2>/dev/null | head -1); echo "$REDIS"
"$REDIS" --port 6379 --save '' --appendonly no --daemonize yes --dir "$(mktemp -d)"
cd /home/kwatanabe/tmp_local/juicefs_inspection/juicefs
JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/9 go test ./pkg/meta -run 'TestKazReplaceChunk|TestKazBlockCompaction|TestCompactionGC' -count=1 -race -v 2>&1 | grep -E '^(---|ok|FAIL)' | grep -v SKIP
```

Expected: redis の subtest が PASS（SKIP ではない）。redis-server が見つからない場合は、ユーザーに一時 Redis の用意を確認する。

- [ ] **Step 2: core テストと全体**

```bash
go vet ./pkg/meta ./pkg/vfs ./cmd
go test ./pkg/meta -count=1 -skip '^(TestLoadDump|TestMySQLClient|TestTiKVClient)$'
rm -rf pkg/meta/test_badger
go test ./pkg/vfs ./cmd -count=1
redis-cli -p 6379 shutdown nosave   # 一時 Redis を止める（redis-cli も同じ場所）
```

Expected: すべて ok。`TestRedisClient` などは 127.0.0.1:6379 の一時 Redis を使う。

- [ ] **Step 3: upstream 由来のファイルの差分を確かめる**

```bash
git diff --stat 1.4.1-improve-kaz -- pkg/meta/base.go pkg/meta/redis.go pkg/meta/sql.go pkg/meta/tkv.go pkg/meta/slice.go pkg/vfs/compact.go cmd/mount.go
git diff --stat 1.4.1-improve-kaz -- pkg/meta/config.go cmd/flags.go
```

Expected: 1 つめは差分なし。2 つめは config.go（約 +12）と flags.go（約 +15）だけ。

- [ ] **Step 4: ビルド**

```bash
make juicefs
./juicefs version
sha256sum juicefs
./juicefs mount --help | grep -A2 'kaz-compaction-mode'
```

Expected: `1.4.1+2026-10-1x.<commit>` の版、help に 3 つのオプション。

- [ ] **Step 5: 最終レビュー**

superpowers:requesting-code-review で、ブランチ全体（`1.4.1-improve-kaz..feat/kaz-block-compaction`）を見てもらう。見る点: §5 の規則と見え方の一致、各エンジンの参照数と delayed の扱いが upstream の `doCompactChunk` と同じか、失敗時の後始末、`m.compacting` の共有、upstream 由来のファイルの差分の小ささ。

- [ ] **Step 6: 記録（調査ルート、commit しない）**

- `TODO.md`: ブロック単位の compaction の項に、実装の commit、テストの結果、ビルドの版と sha256、次（ユーザーの実機計測。手順は設計書 §8）を書く。
- `agent_memo.md`: 同じ内容を 1 項目で。memo の行数が 1,000 行に近ければ分割を検討する。
- `docs/findings.md`: 実機計測の前なので、「設計と実装済み、効果は未計測（見積もり −47〜−68%、compaction の PUT）」とだけ書く。
- 設計書の状態の行を「実装済み（本体 `feat/kaz-block-compaction`、未 push・未 merge）、実機計測待ち」にする。

- [ ] **Step 7: ユーザーへの報告**

実装の commit、テスト（Redis を含む）、ビルドの版と sha256、upstream 由来のファイルの差分、レビューの結果、実機計測の手順（`--kaz-compaction-read-min 100 --kaz-compaction-mode block --debug`、VM を止めて staging を空にしてから mount、計測後も 10〜15 分 slice の行き先を追う、`juicefs gc` は `--delete` なしでユーザーが判断）を伝える。merge と push はユーザーの指示を待つ。
