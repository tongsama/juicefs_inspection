# kaz compaction policy 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** バックグラウンドの compaction の要求を、scheduler の手前の判断の層（Read からの起動の下限、書き込みの続く chunk の cooldown＋強制の個数）で絞り、VM のディスクイメージでの compaction の PUT と DELETE を減らす。

**Architecture:** kaz 専用の新しいファイル `pkg/meta/compaction_policy_kaz.go` に `compactionPolicy` を置き、本体（upstream 由来のコード）は `afterWrite` の 1 行、要求を出す 2 か所の呼び出し名、struct の 2 項目、metrics の登録 1 行だけを変える。policy は要求を「捨てる・すぐ渡す・保留する」に分け、保留は見回りの goroutine が渡す。既定値では policy を作らず、今の動きと完全に同じにする。

**Tech Stack:** Go 1.25.11（手元の `go version` が 1.25.11）、`github.com/prometheus/client_golang`、`urfave/cli/v2`、JuiceFS の `pkg/meta`（MemKV・SQLite・Redis）。

**Spec:** `docs/superpowers/specs/2026-10-14-kaz-compaction-policy-design.md`（調査リポジトリのルートからの相対パス）

## Global Constraints

- 作業は JuiceFS 本体 `juicefs/`（調査リポジトリとは別の Git）。ブランチは `1.4.1-improve-kaz`（8a64eb07）から作る `feat/kaz-compaction-policy`。
- **upstream の変更に強いコード**: 判断と状態は kaz 専用のファイル（`pkg/meta/compaction_policy_kaz.go`、`cmd/kaz_compaction_policy.go`）に置く。データベースごとのコード（redis.go、sql.go、tkv.go）、`compactChunk`、`compactionWanted` の中身には触れない。テストは外から見える振る舞いで書く。
- オプション名と既定値: `--kaz-compaction-read-min`（既定 5、範囲 5〜2,499）、`--kaz-compaction-cooldown`（既定 0＝無効、範囲 0 または 1s〜1h）、`--kaz-compaction-force-slices`（既定 1,000、範囲 100〜2,499）。usage の先頭は `[kaz]`。
- Go の `Config` の 0 は既定値を意味する（ReadMin 0 → 5、ForceSlices 0 → 1,000、Cooldown 0 → 無効）。
- 保留の上限 1,024 件、最後に書いた時刻の記録の上限 4,096 件。あふれたら今の動きに戻す。
- 見回りの間隔は cooldown の 1/4 を 1〜5 秒に収めた値。
- metrics 名（登録時に `juicefs_` の接頭辞が付く）: `kaz_compaction_policy_events_total{event}`（`read_below_min`、`pass`、`deferred`、`released_idle`、`released_forced`、`overflow`、`discarded_on_stop`）、`kaz_compaction_policy_deferred`、`kaz_compaction_queue_wait_seconds`、`kaz_compaction_scheduled_slices`。
- 2,500 個での同期 compaction、`juicefs compact`、`gc --compact`、`m.compacting` は変えない。fsync・read-after-write・commit の順序・slice の削除には触れない。
- テストは `TMPDIR` をルートのファイルシステムに置く（WSL の /tmp は tmpfs で既存テストが環境依存で落ちる）: `export TMPDIR=$HOME/tmp_local/gotmp && mkdir -p $TMPDIR`。本番の Redis（56379）には接続しない。Redis のテストは `JUICEFS_TEST_COMPACTION_GC_REDIS` に一時 Redis を指定したときだけ動く。
- 新規・変更する関数と型には目的のコメントを付ける。ライセンスヘッダは既存ファイル（`pkg/meta/compaction_scheduler.go` の先頭 15 行）と同じものを付ける。
- **commit はユーザーが許可したときだけ行う**（調査リポジトリの方針: 明示の依頼なしに agent が add／commit／push しない）。commit するときのメッセージは英語で既存の形（`meta: ...`、`cmd: ...`）に合わせ、`Co-Authored-By` の行を付けない。push はしない。

## Review Focus

- 保留中の chunk を持つファイルが削除された（もう存在しない inode）: 見回りで要求を渡しても、compaction は何もせず終わり、パニックにも CompactChunk の呼び出しにもならない（Task 4 にテスト。テスト用の backend の削除キューは容量 1 で埋まっているため、実際の Unlink ではなく存在しない inode で代える）。
- 同じプロセスで session を止めてもう一度始めた（`stopPriorityCompactor` → `startPriorityCompactor`）: 古い policy は閉じ、新しい policy が付く。古い保留は捨てる（Task 4 にテスト）。
- `--kaz-compaction-cooldown` の書き間違い（`abc`、`1x`）: `utils.Duration` のように黙って 0 にせず、mount の前に拒否する（Task 5 にテスト）。
- force を read の下限より小さくした（例: read-min 500、force 100）: Read からの要求は下限で捨てるが、commit からの要求は force で渡る（Task 2 にテスト）。
- ほかの client が同じ chunk に書いている: その commit はこのプロセスに見えないので「書いていない」扱いになり、要求はすぐ渡る（今の動き）。記録のない chunk への要求がすぐ渡ることを Task 2 のテストで固定する。

---

### Task 1: 設定の項目と検査

**Files:**
- Modify: `pkg/meta/config.go`（`Config` struct に 3 項目、`SelfCheck` に 4 行）
- Create: `pkg/meta/compaction_policy_kaz.go`
- Test: `pkg/meta/compaction_policy_kaz_test.go`

**Interfaces:**
- Produces:
  - `Config.KazCompactionReadMin int`、`Config.KazCompactionCooldown time.Duration`、`Config.KazCompactionForceSlices int`
  - `func (c *Config) ValidateKazCompactionPolicy() error`
  - `func (c *Config) kazCompactionSettings() (readMin int, cooldown time.Duration, force int)`
  - `func (c *Config) kazCompactionPolicyEnabled() bool`
  - 定数 `kazCompactionDefaultReadMin`（5）、`kazCompactionDefaultForce`（1000）、`kazCompactionMaxDeferred`（1024）、`kazCompactionMaxTracked`（4096）ほか

- [ ] **Step 1: ブランチを作る**

```bash
cd ~/tmp_local/juicefs_inspection/juicefs
git status --short   # 何も出ないことを確かめる
git switch -c feat/kaz-compaction-policy 1.4.1-improve-kaz
```

- [ ] **Step 2: 失敗するテストを書く**

`pkg/meta/compaction_policy_kaz_test.go` を作る（ライセンスヘッダは `pkg/meta/compaction_scheduler.go` の先頭 15 行をそのまま写す）。

```go
package meta

import (
	"testing"
	"time"
)

// TestKazCompactionPolicyConfig checks the ranges, the zero-value defaults and the SelfCheck fallback.
func TestKazCompactionPolicyConfig(t *testing.T) {
	var zero Config
	if err := zero.ValidateKazCompactionPolicy(); err != nil {
		t.Fatalf("zero config rejected: %v", err)
	}
	if r, cd, f := zero.kazCompactionSettings(); r != 5 || cd != 0 || f != 1000 {
		t.Fatalf("zero settings = %d/%s/%d, want 5/0s/1000", r, cd, f)
	}
	if zero.kazCompactionPolicyEnabled() {
		t.Fatal("zero config must keep the upstream behavior")
	}
	for _, c := range []Config{
		{KazCompactionReadMin: 5}, {KazCompactionReadMin: 2499},
		{KazCompactionCooldown: time.Second}, {KazCompactionCooldown: time.Hour},
		{KazCompactionForceSlices: 100}, {KazCompactionForceSlices: 2499},
	} {
		if err := c.ValidateKazCompactionPolicy(); err != nil {
			t.Errorf("%+v rejected: %v", c, err)
		}
	}
	for _, c := range []Config{
		{KazCompactionReadMin: -1}, {KazCompactionReadMin: 4}, {KazCompactionReadMin: 2500},
		{KazCompactionCooldown: -time.Second}, {KazCompactionCooldown: 999 * time.Millisecond}, {KazCompactionCooldown: time.Hour + time.Second},
		{KazCompactionForceSlices: -1}, {KazCompactionForceSlices: 99}, {KazCompactionForceSlices: 2500},
	} {
		if err := c.ValidateKazCompactionPolicy(); err == nil {
			t.Errorf("%+v accepted", c)
		}
	}
	for _, tc := range []struct {
		c    Config
		want bool
	}{
		{Config{KazCompactionReadMin: 5}, false},
		{Config{KazCompactionReadMin: 100}, true},
		{Config{KazCompactionCooldown: time.Minute}, true},
		{Config{KazCompactionForceSlices: 700}, false},
	} {
		if got := tc.c.kazCompactionPolicyEnabled(); got != tc.want {
			t.Errorf("%+v enabled=%v, want %v", tc.c, got, tc.want)
		}
	}
	bad := Config{KazCompactionReadMin: 3, KazCompactionCooldown: time.Minute, KazCompactionForceSlices: 700, MaxDeletes: 1}
	bad.SelfCheck()
	if bad.KazCompactionReadMin != 0 || bad.KazCompactionCooldown != 0 || bad.KazCompactionForceSlices != 0 {
		t.Fatalf("SelfCheck kept invalid policy: %+v", bad)
	}
}
```

- [ ] **Step 3: 失敗を確かめる**

Run: `cd ~/tmp_local/juicefs_inspection/juicefs && go test ./pkg/meta -run TestKazCompactionPolicyConfig -count=1`
Expected: コンパイルエラー（`KazCompactionReadMin` などが未定義）

- [ ] **Step 4: 実装する**

`pkg/meta/config.go` の `Config` struct で、`CompactionScheduler` の行の直後に足す。

```go
	KazCompactionReadMin     int           // [kaz] reads request compaction from this many slices; 0 means 5 (upstream)
	KazCompactionCooldown    time.Duration // [kaz] hold compaction of a chunk written within this period; 0 disables it
	KazCompactionForceSlices int           // [kaz] compact a held chunk once it has this many slices; 0 means 1000
```

`pkg/meta/config.go` の `SelfCheck` で、`ValidateCompactionGC` の `if` ブロックの直後に足す。

```go
	if err := c.ValidateKazCompactionPolicy(); err != nil {
		logger.Warnf("%s; falling back to the upstream compaction triggers", err)
		c.KazCompactionReadMin, c.KazCompactionCooldown, c.KazCompactionForceSlices = 0, 0, 0
	}
```

`pkg/meta/compaction_policy_kaz.go` を作る（ライセンスヘッダは同上）。

```go
package meta

import (
	"fmt"
	"time"
)

// Defaults and bounds of the kaz compaction policy. The defaults keep the
// upstream behavior: a read requests compaction from 5 slices and nothing is held.
const (
	kazCompactionDefaultReadMin = 5
	kazCompactionMaxReadMin     = maxSlices - 1
	kazCompactionDefaultForce   = 1000
	kazCompactionMinForce       = 100
	kazCompactionMaxForce       = maxSlices - 1
	kazCompactionMinCooldown    = time.Second
	kazCompactionMaxCooldown    = time.Hour
	kazCompactionMaxDeferred    = 1024
	kazCompactionMaxTracked     = 4096
)

// ValidateKazCompactionPolicy rejects policy knobs outside their documented ranges.
// Zero values mean "use the default", so Go callers that never set them keep upstream behavior.
func (c *Config) ValidateKazCompactionPolicy() error {
	if v := c.KazCompactionReadMin; v != 0 && (v < kazCompactionDefaultReadMin || v > kazCompactionMaxReadMin) {
		return fmt.Errorf("invalid kaz-compaction-read-min %d (want %d..%d)", v, kazCompactionDefaultReadMin, kazCompactionMaxReadMin)
	}
	if v := c.KazCompactionCooldown; v != 0 && (v < kazCompactionMinCooldown || v > kazCompactionMaxCooldown) {
		return fmt.Errorf("invalid kaz-compaction-cooldown %s (want 0 or %s..%s)", v, kazCompactionMinCooldown, kazCompactionMaxCooldown)
	}
	if v := c.KazCompactionForceSlices; v != 0 && (v < kazCompactionMinForce || v > kazCompactionMaxForce) {
		return fmt.Errorf("invalid kaz-compaction-force-slices %d (want %d..%d)", v, kazCompactionMinForce, kazCompactionMaxForce)
	}
	return nil
}

// kazCompactionSettings returns the effective read minimum, cooldown and force threshold.
func (c *Config) kazCompactionSettings() (readMin int, cooldown time.Duration, force int) {
	readMin, cooldown, force = c.KazCompactionReadMin, c.KazCompactionCooldown, c.KazCompactionForceSlices
	if readMin == 0 {
		readMin = kazCompactionDefaultReadMin
	}
	if force == 0 {
		force = kazCompactionDefaultForce
	}
	return
}

// kazCompactionPolicyEnabled reports whether any knob changes the upstream behavior.
func (c *Config) kazCompactionPolicyEnabled() bool {
	readMin, cooldown, _ := c.kazCompactionSettings()
	return readMin != kazCompactionDefaultReadMin || cooldown > 0
}
```

- [ ] **Step 5: 通ることを確かめる**

Run: `go test ./pkg/meta -run TestKazCompactionPolicyConfig -count=1 -v`
Expected: `--- PASS: TestKazCompactionPolicyConfig`

- [ ] **Step 6: commit（ユーザーが許可した場合のみ）**

```bash
gofmt -l pkg/meta   # 何も出ないこと
git add pkg/meta/config.go pkg/meta/compaction_policy_kaz.go pkg/meta/compaction_policy_kaz_test.go
git commit -m "meta: add the kaz compaction policy settings"
```

---

### Task 2: policy の本体と metrics の入れ物

**Files:**
- Modify: `pkg/meta/compaction_policy_kaz.go`（追記）
- Test: `pkg/meta/compaction_policy_kaz_test.go`（追記）

**Interfaces:**
- Consumes: Task 1 の定数、既存の `compactionKey{inode Ino; chunk uint32}`（compaction_scheduler.go）、パッケージの `logger`
- Produces:
  - `type kazCompactionMetrics struct { events *prometheus.CounterVec; deferred prometheus.Gauge; queueWait, slices prometheus.Histogram }`
  - `func newKazCompactionMetrics() *kazCompactionMetrics`、`func (k *kazCompactionMetrics) register(reg prometheus.Registerer)`、`func (k *kazCompactionMetrics) observeStart(wait time.Duration, count int)`
  - `type compactionPolicy struct`（フィールド `now func() time.Time` と `forward func(Ino, uint32, int, int)` はテストで差し替える）
  - `func newCompactionPolicy(readMin int, cooldown time.Duration, force int, forward func(inode Ino, chunk uint32, count, tier int), metrics *kazCompactionMetrics) *compactionPolicy`
  - メソッド `request(inode Ino, chunk uint32, count, tier int, fromRead bool)`、`onCommit(inode Ino, chunk uint32, count int)`、`patrol()`、`start()`、`close()`

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/compaction_policy_kaz_test.go` の import を次にする。

```go
import (
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)
```

ファイルの末尾に追記する。

```go
// policyRecorder collects forwarded requests in order.
type policyRecorder struct {
	mu  sync.Mutex
	got []string
}

// forward records one request as "inode/chunk/count/tier".
func (r *policyRecorder) forward(inode Ino, chunk uint32, count, tier int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.got = append(r.got, fmt.Sprintf("%d/%d/%d/%d", inode, chunk, count, tier))
}

// take returns and clears the recorded requests.
func (r *policyRecorder) take() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	got := r.got
	r.got = nil
	return got
}

// fakeClock is a manually advanced clock for the policy.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

// now returns the current fake time.
func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

// add advances the fake time.
func (c *fakeClock) add(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// newTestPolicy builds a policy with a fake clock, a recorder and fresh metrics; it does not start the patrol.
func newTestPolicy(readMin int, cooldown time.Duration, force int) (*compactionPolicy, *policyRecorder, *fakeClock, *kazCompactionMetrics) {
	rec := &policyRecorder{}
	clk := &fakeClock{t: time.Unix(1_000_000, 0)}
	m := newKazCompactionMetrics()
	p := newCompactionPolicy(readMin, cooldown, force, rec.forward, m)
	p.now = clk.now
	return p, rec, clk, m
}

// policyEvents reads one event counter.
func policyEvents(m *kazCompactionMetrics, name string) float64 {
	return testutil.ToFloat64(m.events.WithLabelValues(name))
}

// expectForwarded fails unless exactly want was forwarded since the last call.
func expectForwarded(t *testing.T, rec *policyRecorder, want ...string) {
	t.Helper()
	got := rec.take()
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("forwarded %v, want %v", got, want)
	}
}

// TestCompactionPolicyReadMin drops small read requests but never commit requests.
func TestCompactionPolicyReadMin(t *testing.T) {
	p, rec, _, m := newTestPolicy(100, 0, 1000)
	p.request(1, 0, 99, 2, true)
	expectForwarded(t, rec)
	p.request(1, 0, 100, 2, true)
	p.request(1, 1, 6, 2, false)
	expectForwarded(t, rec, "1/0/100/2", "1/1/6/2")
	if policyEvents(m, "read_below_min") != 1 || policyEvents(m, "pass") != 2 {
		t.Fatalf("events below=%v pass=%v", policyEvents(m, "read_below_min"), policyEvents(m, "pass"))
	}
}

// TestCompactionPolicyForceBelowReadMin pins the order: the read minimum applies before force.
func TestCompactionPolicyForceBelowReadMin(t *testing.T) {
	p, rec, _, _ := newTestPolicy(500, time.Minute, 100)
	p.onCommit(1, 0, 300)
	p.request(1, 0, 300, 0, true)
	expectForwarded(t, rec)
	p.request(1, 0, 300, 0, false)
	expectForwarded(t, rec, "1/0/300/0")
}

// TestCompactionPolicyCooldownHoldsBusyChunk holds a recently written chunk until it stays idle for the cooldown.
func TestCompactionPolicyCooldownHoldsBusyChunk(t *testing.T) {
	p, rec, clk, m := newTestPolicy(5, time.Minute, 700)
	p.request(9, 0, 99, 1, false) // never written here (or by another client): passes
	expectForwarded(t, rec, "9/0/99/1")
	p.onCommit(1, 0, 99)
	p.request(1, 0, 99, 3, false)
	expectForwarded(t, rec)
	if policyEvents(m, "deferred") != 1 || testutil.ToFloat64(m.deferred) != 1 {
		t.Fatalf("deferred=%v gauge=%v", policyEvents(m, "deferred"), testutil.ToFloat64(m.deferred))
	}
	clk.add(30 * time.Second)
	p.onCommit(1, 0, 120)
	p.patrol()
	clk.add(59 * time.Second)
	p.patrol()
	expectForwarded(t, rec)
	clk.add(time.Second)
	p.patrol()
	expectForwarded(t, rec, "1/0/120/3")
	p.patrol()
	expectForwarded(t, rec)
	if policyEvents(m, "released_idle") != 1 || testutil.ToFloat64(m.deferred) != 0 {
		t.Fatalf("released=%v gauge=%v", policyEvents(m, "released_idle"), testutil.ToFloat64(m.deferred))
	}
}

// TestCompactionPolicyHeldRequestUpdates keeps one entry per chunk with the largest count and the latest tier.
func TestCompactionPolicyHeldRequestUpdates(t *testing.T) {
	p, rec, clk, m := newTestPolicy(5, time.Minute, 700)
	p.onCommit(1, 0, 10)
	p.request(1, 0, 400, 1, false)
	p.request(1, 0, 360, 2, true)
	expectForwarded(t, rec)
	if policyEvents(m, "deferred") != 1 {
		t.Fatalf("deferred=%v, want 1", policyEvents(m, "deferred"))
	}
	clk.add(time.Minute)
	p.patrol()
	expectForwarded(t, rec, "1/0/400/2")
}

// TestCompactionPolicyForce passes at the force threshold, on a request or on a commit.
func TestCompactionPolicyForce(t *testing.T) {
	p, rec, _, m := newTestPolicy(5, time.Minute, 700)
	p.onCommit(1, 0, 350)
	p.request(1, 0, 350, 4, false)
	p.onCommit(1, 0, 699)
	expectForwarded(t, rec)
	p.onCommit(1, 0, 700)
	expectForwarded(t, rec, "1/0/700/4")
	if policyEvents(m, "released_forced") != 1 {
		t.Fatalf("released_forced=%v", policyEvents(m, "released_forced"))
	}
	p.onCommit(2, 0, 10)
	p.request(2, 0, 700, 0, false)
	expectForwarded(t, rec, "2/0/700/0")
}

// TestCompactionPolicyCapacity falls back to the upstream behavior when its maps are full.
func TestCompactionPolicyCapacity(t *testing.T) {
	p, rec, _, m := newTestPolicy(5, time.Minute, 700)
	for i := 0; i < kazCompactionMaxDeferred; i++ {
		p.onCommit(Ino(i+1), 0, 99)
		p.request(Ino(i+1), 0, 99, 0, false)
	}
	expectForwarded(t, rec)
	p.onCommit(100000, 0, 99)
	p.request(100000, 0, 99, 0, false)
	expectForwarded(t, rec, "100000/0/99/0")
	if policyEvents(m, "overflow") != 1 {
		t.Fatalf("overflow=%v", policyEvents(m, "overflow"))
	}
	for i := 0; i < kazCompactionMaxTracked; i++ {
		p.onCommit(Ino(200000+i), 0, 1)
	}
	p.onCommit(999999, 0, 99) // not tracked: the map is full
	p.request(999999, 0, 99, 0, false)
	expectForwarded(t, rec, "999999/0/99/0")
}

// TestCompactionPolicyCloseDiscards drops held work on close and ignores later calls.
func TestCompactionPolicyCloseDiscards(t *testing.T) {
	p, rec, clk, m := newTestPolicy(100, time.Minute, 700)
	p.onCommit(1, 0, 99)
	p.request(1, 0, 99, 0, false)
	p.onCommit(2, 0, 99)
	p.request(2, 0, 99, 0, false)
	p.close()
	p.close()
	if policyEvents(m, "discarded_on_stop") != 2 || testutil.ToFloat64(m.deferred) != 0 {
		t.Fatalf("discarded=%v gauge=%v", policyEvents(m, "discarded_on_stop"), testutil.ToFloat64(m.deferred))
	}
	clk.add(time.Hour)
	p.request(3, 0, 200, 0, true)
	p.request(3, 0, 200, 0, false)
	p.onCommit(1, 0, 900)
	p.patrol()
	expectForwarded(t, rec)
}

// TestCompactionPolicyPatrolGoroutine releases an idle chunk from the real patrol and stops it on close.
func TestCompactionPolicyPatrolGoroutine(t *testing.T) {
	rec := &policyRecorder{}
	p := newCompactionPolicy(5, time.Second, 700, rec.forward, nil)
	p.start()
	p.onCommit(1, 0, 99)
	p.request(1, 0, 99, 0, false)
	deadline := time.Now().Add(5 * time.Second)
	for {
		rec.mu.Lock()
		n := len(rec.got)
		rec.mu.Unlock()
		if n > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("patrol did not release the idle chunk")
		}
		time.Sleep(50 * time.Millisecond)
	}
	done := make(chan struct{})
	go func() { p.close(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("close did not stop the patrol")
	}
}

// TestCompactionPolicyConcurrent exercises requests, commits and patrols together for the race detector.
func TestCompactionPolicyConcurrent(t *testing.T) {
	p, _, clk, _ := newTestPolicy(50, time.Minute, 300)
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 1000; i++ {
				ino, chunk := Ino(g%3+1), uint32(i%7)
				switch i % 4 {
				case 0:
					p.onCommit(ino, chunk, i%400)
				case 1:
					p.request(ino, chunk, i%400, g, false)
				case 2:
					p.request(ino, chunk, i%400, g, true)
				default:
					clk.add(time.Second)
					p.patrol()
				}
			}
		}(g)
	}
	wg.Wait()
	p.close()
}
```

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./pkg/meta -run 'TestCompactionPolicy' -count=1`
Expected: コンパイルエラー（`newCompactionPolicy`、`newKazCompactionMetrics` が未定義）

- [ ] **Step 3: 実装する**

`pkg/meta/compaction_policy_kaz.go` の import を次にする。

```go
import (
	"fmt"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)
```

ファイルの末尾に追記する。

```go
// kazCompactionMetrics groups the observations exported for the kaz compaction policy.
type kazCompactionMetrics struct {
	events    *prometheus.CounterVec
	deferred  prometheus.Gauge
	queueWait prometheus.Histogram
	slices    prometheus.Histogram
}

// newKazCompactionMetrics creates unregistered collectors; register attaches them.
func newKazCompactionMetrics() *kazCompactionMetrics {
	return &kazCompactionMetrics{
		events: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "kaz_compaction_policy_events_total",
			Help: "Decisions of the kaz compaction policy: read_below_min, pass, deferred, released_idle, released_forced, overflow, discarded_on_stop.",
		}, []string{"event"}),
		deferred: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "kaz_compaction_policy_deferred",
			Help: "Chunks whose background compaction is currently held by the kaz compaction policy.",
		}),
		queueWait: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name:    "kaz_compaction_queue_wait_seconds",
			Help:    "Time from queuing a chunk in the priority compaction scheduler to the start of its compaction.",
			Buckets: prometheus.ExponentialBuckets(0.01, 2, 18),
		}),
		slices: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name:    "kaz_compaction_scheduled_slices",
			Help:    "Slice count recorded in the priority scheduler hint when its compaction starts.",
			Buckets: []float64{5, 10, 20, 50, 100, 200, 350, 500, 700, 1000, 1500, 2000, 2500},
		}),
	}
}

// register attaches the collectors to reg.
func (k *kazCompactionMetrics) register(reg prometheus.Registerer) {
	reg.MustRegister(k.events, k.deferred, k.queueWait, k.slices)
}

// observeStart records one compaction start taken from the priority scheduler.
func (k *kazCompactionMetrics) observeStart(wait time.Duration, count int) {
	k.queueWait.Observe(wait.Seconds())
	k.slices.Observe(float64(count))
}

// heldCompaction is one background compaction request held by the policy.
type heldCompaction struct {
	count, tier int
	since       time.Time
}

// compactionPolicy decides when advisory background compaction requests reach
// the scheduler. It never touches metadata: dropping or holding a request only
// leaves more slices in a chunk until a later request, commit or patrol.
type compactionPolicy struct {
	readMin  int
	cooldown time.Duration
	force    int
	forward  func(inode Ino, chunk uint32, count, tier int)
	now      func() time.Time
	metrics  *kazCompactionMetrics

	mu         sync.Mutex
	closed     bool
	started    bool
	held       map[compactionKey]*heldCompaction
	lastCommit map[compactionKey]time.Time
	stopCh     chan struct{}
	done       chan struct{}
}

// newCompactionPolicy builds a policy; start launches its patrol when a cooldown is set.
func newCompactionPolicy(readMin int, cooldown time.Duration, force int, forward func(inode Ino, chunk uint32, count, tier int), metrics *kazCompactionMetrics) *compactionPolicy {
	return &compactionPolicy{
		readMin: readMin, cooldown: cooldown, force: force, forward: forward, now: time.Now, metrics: metrics,
		held:       make(map[compactionKey]*heldCompaction),
		lastCommit: make(map[compactionKey]time.Time),
		stopCh:     make(chan struct{}),
		done:       make(chan struct{}),
	}
}

// event counts n decisions when metrics are attached.
func (p *compactionPolicy) event(name string, n int) {
	if p.metrics != nil && n > 0 {
		p.metrics.events.WithLabelValues(name).Add(float64(n))
	}
}

// heldChanged publishes the number of held chunks; the caller holds mu.
func (p *compactionPolicy) heldChanged() {
	if p.metrics != nil {
		p.metrics.deferred.Set(float64(len(p.held)))
	}
}

// pass forwards one request; callers must not hold mu. forward is read under mu
// so tests may swap it while the patrol goroutine runs.
func (p *compactionPolicy) pass(event string, inode Ino, chunk uint32, count, tier int) {
	p.mu.Lock()
	forward := p.forward
	p.mu.Unlock()
	p.event(event, 1)
	forward(inode, chunk, count, tier)
}

// request handles one compaction request; fromRead applies the read minimum first.
func (p *compactionPolicy) request(inode Ino, chunk uint32, count, tier int, fromRead bool) {
	if fromRead && count < p.readMin {
		p.event("read_below_min", 1)
		return
	}
	key := compactionKey{inode, chunk}
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	if p.cooldown <= 0 {
		p.mu.Unlock()
		p.pass("pass", inode, chunk, count, tier)
		return
	}
	held := p.held[key]
	if held != nil && held.count > count {
		count = held.count
	}
	last, tracked := p.lastCommit[key]
	if count >= p.force || !tracked || p.now().Sub(last) >= p.cooldown {
		if held != nil {
			delete(p.held, key)
			p.heldChanged()
		}
		p.mu.Unlock()
		p.pass("pass", inode, chunk, count, tier)
		return
	}
	if held != nil {
		held.count, held.tier = count, tier
		p.mu.Unlock()
		return
	}
	if len(p.held) >= kazCompactionMaxDeferred {
		p.mu.Unlock()
		p.pass("overflow", inode, chunk, count, tier)
		return
	}
	p.held[key] = &heldCompaction{count: count, tier: tier, since: p.now()}
	p.heldChanged()
	p.mu.Unlock()
	p.event("deferred", 1)
	logger.Debugf("kaz compaction hold inode=%d chunk=%d slices=%d", inode, chunk, count)
}

// onCommit records a commit to the chunk and releases its held request at the force threshold.
func (p *compactionPolicy) onCommit(inode Ino, chunk uint32, count int) {
	if p.cooldown <= 0 {
		return
	}
	key := compactionKey{inode, chunk}
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	if _, ok := p.lastCommit[key]; ok || len(p.lastCommit) < kazCompactionMaxTracked {
		p.lastCommit[key] = p.now()
	}
	held := p.held[key]
	if held == nil {
		p.mu.Unlock()
		return
	}
	if count > held.count {
		held.count = count
	}
	if held.count < p.force {
		p.mu.Unlock()
		return
	}
	h := *held
	delete(p.held, key)
	p.heldChanged()
	waited := p.now().Sub(h.since)
	p.mu.Unlock()
	logger.Debugf("kaz compaction force inode=%d chunk=%d slices=%d held=%s", inode, chunk, h.count, waited)
	p.pass("released_forced", inode, chunk, h.count, h.tier)
}

// patrol forgets chunks idle for the cooldown and releases their held requests.
func (p *compactionPolicy) patrol() {
	type release struct {
		key  compactionKey
		held heldCompaction
	}
	var out []release
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	now := p.now()
	for key, last := range p.lastCommit {
		if now.Sub(last) >= p.cooldown {
			delete(p.lastCommit, key)
		}
	}
	for key, held := range p.held {
		if _, busy := p.lastCommit[key]; !busy {
			out = append(out, release{key, *held})
			delete(p.held, key)
		}
	}
	if len(out) > 0 {
		p.heldChanged()
	}
	p.mu.Unlock()
	for _, r := range out {
		logger.Debugf("kaz compaction release inode=%d chunk=%d slices=%d held=%s", r.key.inode, r.key.chunk, r.held.count, now.Sub(r.held.since))
		p.pass("released_idle", r.key.inode, r.key.chunk, r.held.count, r.held.tier)
	}
}

// patrolInterval returns a quarter of the cooldown, kept between 1s and 5s.
func (p *compactionPolicy) patrolInterval() time.Duration {
	return min(max(p.cooldown/4, time.Second), 5*time.Second)
}

// start launches the patrol goroutine when a cooldown is configured.
func (p *compactionPolicy) start() {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.cooldown <= 0 || p.started || p.closed {
		return
	}
	p.started = true
	go func() {
		defer close(p.done)
		ticker := time.NewTicker(p.patrolInterval())
		defer ticker.Stop()
		for {
			select {
			case <-p.stopCh:
				return
			case <-ticker.C:
				p.patrol()
			}
		}
	}()
}

// close discards held requests, stops the patrol and makes later calls no-ops.
func (p *compactionPolicy) close() {
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return
	}
	p.closed = true
	discarded := len(p.held)
	p.held = make(map[compactionKey]*heldCompaction)
	p.lastCommit = make(map[compactionKey]time.Time)
	p.heldChanged()
	started := p.started
	p.mu.Unlock()
	p.event("discarded_on_stop", discarded)
	close(p.stopCh)
	if started {
		<-p.done
	}
}
```

- [ ] **Step 4: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'TestCompactionPolicy|TestKazCompactionPolicyConfig' -count=1 -v`
Expected: 全部 PASS（`TestCompactionPolicyPatrolGoroutine` は約 1〜2 秒）

Run: `go test -race ./pkg/meta -run 'TestCompactionPolicy' -count=3`
Expected: `ok`、`WARNING: DATA RACE` が出ない

- [ ] **Step 5: commit（ユーザーが許可した場合のみ）**

```bash
gofmt -l pkg/meta
git add pkg/meta/compaction_policy_kaz.go pkg/meta/compaction_policy_kaz_test.go
git commit -m "meta: add the kaz compaction policy that drops, passes or holds compaction requests"
```

---

### Task 3: priority scheduler の開始時の観測

**Files:**
- Modify: `pkg/meta/compaction_scheduler.go`（kaz のコード）
- Test: `pkg/meta/compaction_scheduler_test.go`（追記）

**Interfaces:**
- Consumes: 既存の `newCompactionScheduler(run func(Ino, uint32, int), workers, capacity int) *compactionScheduler`、`schedule(inode Ino, chunk uint32, count, tier int) bool`、`close()`、`wait()`
- Produces: `func (s *compactionScheduler) setObserver(f func(wait time.Duration, count int))`。`compactionHint` に `queued time.Time`、`compactionScheduler` に `observe func(time.Duration, int)`。

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/compaction_scheduler_test.go` の末尾に追記する（`time` が import にあることを確かめ、無ければ足す）。

```go
// TestCompactionSchedulerObserver reports the queue wait and the hinted slice count of each start.
func TestCompactionSchedulerObserver(t *testing.T) {
	started := make(chan struct{}, 4)
	s := newCompactionScheduler(func(Ino, uint32, int) { started <- struct{}{} }, 1, 16)
	defer func() { s.close(); s.wait() }()
	type observation struct {
		wait  time.Duration
		count int
	}
	got := make(chan observation, 4)
	s.setObserver(func(wait time.Duration, count int) { got <- observation{wait, count} })
	if !s.schedule(1, 2, 150, 0) {
		t.Fatal("schedule rejected")
	}
	select {
	case o := <-got:
		if o.count != 150 || o.wait < 0 {
			t.Fatalf("observation %+v, want count 150", o)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("observer not called")
	}
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("run not called")
	}
}
```

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./pkg/meta -run TestCompactionSchedulerObserver -count=1`
Expected: コンパイルエラー（`setObserver` が未定義）

- [ ] **Step 3: 実装する**

`pkg/meta/compaction_scheduler.go`:

1. import に `"time"` を足す。
2. `compactionHint` に足す: `queued time.Time // when the hint last entered a pending list`
3. `compactionScheduler` に足す: `observe func(wait time.Duration, count int) // optional start observer, read under mu`
4. `schedule` の `if hint.element == nil {` の中で、`hint.bucket = bucket` の前に `hint.queued = time.Now()` を足す。
5. `worker` で `generation, tier := hint.generation, hint.tier` の行を次の 2 行にする。

```go
		generation, tier := hint.generation, hint.tier
		count, wait, observe := hint.count, time.Since(hint.queued), s.observe
```

  その後の `s.mu.Unlock()` と `s.run(...)` の間に足す。

```go
		if observe != nil {
			observe(wait, count)
		}
```

6. `worker` の再登録（`if generation != hint.generation {` の中）で、`hint.element = state.pending[hint.bucket].PushBack(hint)` の前に `hint.queued = time.Now()` を足す。
7. ファイルの末尾に足す。

```go
// setObserver installs a callback that sees each start's queue wait and hinted slice count.
func (s *compactionScheduler) setObserver(f func(wait time.Duration, count int)) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.observe = f
}
```

- [ ] **Step 4: 通ることを確かめる**

Run: `go test -race ./pkg/meta -run 'TestCompactionScheduler|TestPriorityCompactor' -count=1 -v`
Expected: 既存の scheduler のテストと新しいテストが全部 PASS、新しいコードの DATA RACE なし

- [ ] **Step 5: commit（ユーザーが許可した場合のみ）**

```bash
gofmt -l pkg/meta
git add pkg/meta/compaction_scheduler.go pkg/meta/compaction_scheduler_test.go
git commit -m "meta: let the priority compaction scheduler report queue wait and slice count"
```

---

### Task 4: 本体へのつなぎ込み

**Files:**
- Modify: `pkg/meta/base.go`（struct に 2 項目、`newBaseMeta` に 1 行、`InitMetrics` に 1 行、`Read` の呼び出し名 1 か所、`afterWrite` に 1 行と呼び出し名 1 か所）
- Modify: `pkg/meta/compaction_scheduler_lifecycle.go`（kaz のコード）
- Modify: `pkg/meta/compaction_policy_kaz.go`（baseMeta のメソッドを追記）
- Test: `pkg/meta/compaction_policy_kaz_wiring_test.go`（新規）

**Interfaces:**
- Consumes: Task 1〜3 のすべて、既存の `requestBackgroundCompaction(inode Ino, chunk uint32, count, tier int)`、テスト用の `newGCTestBackend(t, backend) gcTestBackend`（`*baseMeta` を埋め込む）と `createGCTestFile`
- Produces: baseMeta のメソッド `startCompactionPolicy()`、`stopCompactionPolicy()`、`requestCompactionFromRead(inode Ino, chunk uint32, count, tier int)`、`requestCompactionFromCommit(inode Ino, chunk uint32, count, tier int)`、`notifyCompactionCommit(inode Ino, chunk uint32, count int)`。フィールド `kazCompact atomic.Pointer[compactionPolicy]`、`kazCompactMetrics *kazCompactionMetrics`

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/compaction_policy_kaz_wiring_test.go` を作る（ライセンスヘッダは同上）。

```go
package meta

import (
	"errors"
	"testing"
	"time"

	dto "github.com/prometheus/client_model/go"
)

// errPolicyTestCompaction makes CompactChunk abort without changing metadata;
// "not found" keeps compactChunk from logging a warning.
var errPolicyTestCompaction = errors.New("kaz policy test: not found")

// newPolicyTestBackend returns a backend whose compaction callback only signals,
// configured by mutate before the policy and scheduler start.
func newPolicyTestBackend(t *testing.T, backend string, mutate func(*Config)) (gcTestBackend, chan struct{}) {
	t.Helper()
	b := newGCTestBackend(t, backend)
	b.conf.CompactionScheduler = "priority"
	mutate(b.conf)
	dispatched := make(chan struct{}, 8192)
	b.OnMsg(CompactChunk, func(...interface{}) error {
		dispatched <- struct{}{}
		return errPolicyTestCompaction
	})
	b.startPriorityCompactor()
	t.Cleanup(b.stopPriorityCompactor)
	return b, dispatched
}

// rawSlices appends slices to chunk 0 without the commit hooks, from offset index start.
func rawSlices(t *testing.T, b gcTestBackend, inode Ino, start, n int) {
	t.Helper()
	var attr Attr
	for i := start; i < start+n; i++ {
		var id uint64
		if st := b.NewSlice(Background(), &id); st != 0 {
			t.Fatal(st)
		}
		var count int
		var delta dirStat
		if st := b.en.doWrite(Background(), inode, 0, uint32(i*4096), Slice{Id: id, Size: 4096, Len: 4096}, time.Now(), &count, &delta, &attr); st != 0 {
			t.Fatal(st)
		}
	}
}

// writeSlices appends slices to chunk 0 through the public Write path, so afterWrite runs.
func writeSlices(t *testing.T, b gcTestBackend, inode Ino, start, n int) {
	t.Helper()
	for i := start; i < start+n; i++ {
		var id uint64
		if st := b.NewSlice(Background(), &id); st != 0 {
			t.Fatal(st)
		}
		if st := b.Write(Background(), inode, 0, uint32(i*4096), Slice{Id: id, Size: 4096, Len: 4096}, time.Now()); st != 0 {
			t.Fatal(st)
		}
	}
}

// expectDispatch waits for one compaction callback.
func expectDispatch(t *testing.T, ch chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(3 * time.Second):
		t.Fatal("compaction was not dispatched")
	}
}

// expectNoDispatch fails if a compaction callback arrives within a short window.
func expectNoDispatch(t *testing.T, ch chan struct{}) {
	t.Helper()
	select {
	case <-ch:
		t.Fatal("compaction was dispatched")
	case <-time.After(300 * time.Millisecond):
	}
}

// drain empties the dispatch channel.
func drain(ch chan struct{}) {
	for {
		select {
		case <-ch:
		default:
			return
		}
	}
}

// usePolicyClock swaps the running policy's clock for a fake one.
func usePolicyClock(t *testing.T, b gcTestBackend) *fakeClock {
	t.Helper()
	p := b.kazCompact.Load()
	if p == nil {
		t.Fatal("policy not attached")
	}
	clk := &fakeClock{t: time.Now()}
	p.mu.Lock()
	p.now = clk.now
	p.mu.Unlock()
	return clk
}

// TestKazCompactionPolicyDefaultsUnchanged keeps the upstream read trigger when no knob is set.
func TestKazCompactionPolicyDefaultsUnchanged(t *testing.T) {
	for _, backend := range []string{"memkv", "sqlite"} {
		t.Run(backend, func(t *testing.T) {
			b, dispatched := newPolicyTestBackend(t, backend, func(*Config) {})
			if b.kazCompact.Load() != nil {
				t.Fatal("policy attached with default settings")
			}
			inode, _ := createGCTestFile(t, b, "defaults")
			rawSlices(t, b, inode, 2, 4) // 6 slices
			var s []Slice
			if st := b.Read(Background(), inode, 0, &s); st != 0 {
				t.Fatal(st)
			}
			expectDispatch(t, dispatched)
		})
	}
}

// TestKazCompactionPolicyReadMinWiring drops read requests below the minimum on real engines.
func TestKazCompactionPolicyReadMinWiring(t *testing.T) {
	for _, backend := range []string{"memkv", "sqlite"} {
		t.Run(backend, func(t *testing.T) {
			b, dispatched := newPolicyTestBackend(t, backend, func(c *Config) { c.KazCompactionReadMin = 100 })
			inode, _ := createGCTestFile(t, b, "readmin")
			rawSlices(t, b, inode, 2, 4) // 6 slices
			var s []Slice
			if st := b.Read(Background(), inode, 0, &s); st != 0 {
				t.Fatal(st)
			}
			expectNoDispatch(t, dispatched)
			rawSlices(t, b, inode, 6, 94) // 100 slices
			if st := b.Read(Background(), inode, 0, &s); st != 0 {
				t.Fatal(st)
			}
			expectDispatch(t, dispatched)
		})
	}
}

// TestKazCompactionPolicyCooldownWiring holds a commit-triggered request until idle, and forces it at the threshold.
func TestKazCompactionPolicyCooldownWiring(t *testing.T) {
	b, dispatched := newPolicyTestBackend(t, "memkv", func(c *Config) {
		c.KazCompactionCooldown = time.Minute
		c.KazCompactionForceSlices = 150
	})
	clk := usePolicyClock(t, b)
	inode, _ := createGCTestFile(t, b, "cooldown")
	writeSlices(t, b, inode, 2, 97) // 99 slices: crosses compactionWanted
	expectNoDispatch(t, dispatched)
	clk.add(61 * time.Second)
	b.kazCompact.Load().patrol()
	expectDispatch(t, dispatched)
	drain(dispatched)

	inode2, _ := createGCTestFile(t, b, "force")
	writeSlices(t, b, inode2, 2, 97) // held at 99
	expectNoDispatch(t, dispatched)
	writeSlices(t, b, inode2, 99, 51) // 150 slices: force
	expectDispatch(t, dispatched)
}

// TestKazCompactionPolicyBatchCommit sees batch commits (doWriteSlices) through afterWrite.
func TestKazCompactionPolicyBatchCommit(t *testing.T) {
	for _, backend := range []string{"memkv", "sqlite", "redis"} {
		t.Run(backend, func(t *testing.T) {
			b, dispatched := newPolicyTestBackend(t, backend, func(c *Config) {
				c.KazCompactionCooldown = time.Minute
				c.KazCompactionForceSlices = 150
			})
			usePolicyClock(t, b)
			inode, _ := createGCTestFile(t, b, "batch")
			writeSlices(t, b, inode, 2, 97) // held at 99
			expectNoDispatch(t, dispatched)
			batch := make([]SliceWrite, 0, 60)
			for i := 99; i < 159; i++ {
				var id uint64
				if st := b.NewSlice(Background(), &id); st != 0 {
					t.Fatal(st)
				}
				batch = append(batch, SliceWrite{Off: uint32(i * 4096), Slice: Slice{Id: id, Size: 4096, Len: 4096}})
			}
			if n, st, _ := b.WriteSlices(Background(), inode, 0, batch, time.Now()); st != 0 || n != len(batch) {
				t.Fatalf("WriteSlices n=%d st=%v", n, st)
			}
			expectDispatch(t, dispatched)
		})
	}
}

// TestKazCompactionPolicyKeepsSyncCompaction still compacts synchronously at maxSlices.
func TestKazCompactionPolicyKeepsSyncCompaction(t *testing.T) {
	b, dispatched := newPolicyTestBackend(t, "memkv", func(c *Config) {
		c.KazCompactionReadMin = kazCompactionMaxReadMin
		c.KazCompactionCooldown = time.Hour
		c.KazCompactionForceSlices = kazCompactionMaxForce
	})
	rec := &policyRecorder{}
	p := b.kazCompact.Load()
	p.mu.Lock()
	p.forward = rec.forward // background requests never reach CompactChunk
	p.mu.Unlock()
	inode, _ := createGCTestFile(t, b, "sync")
	writeSlices(t, b, inode, 2, maxSlices-3) // 2499 slices
	expectNoDispatch(t, dispatched)
	writeSlices(t, b, inode, maxSlices-1, 1) // 2500: synchronous compaction inside Write
	select {
	case <-dispatched:
	default:
		t.Fatal("synchronous compaction did not run inside Write")
	}
}

// TestKazCompactionPolicyDeletedFile releases a held request whose file no longer exists:
// the real compaction finds nothing to do, without panics or a CompactChunk call.
// A missing inode stands in for a deleted file; the GC test backend's prefilled
// deletion queue would block a real Unlink.
func TestKazCompactionPolicyDeletedFile(t *testing.T) {
	b, dispatched := newPolicyTestBackend(t, "memkv", func(c *Config) { c.KazCompactionCooldown = time.Minute })
	clk := usePolicyClock(t, b)
	p := b.kazCompact.Load()
	p.onCommit(999999, 0, 99)
	p.request(999999, 0, 99, 0, false)
	clk.add(time.Hour)
	p.patrol()
	if got := policyEvents(b.kazCompactMetrics, "released_idle"); got != 1 {
		t.Fatalf("released_idle=%v, want 1", got)
	}
	expectNoDispatch(t, dispatched)
}

// TestKazCompactionPolicyRestart replaces the policy when the session restarts in the same process.
func TestKazCompactionPolicyRestart(t *testing.T) {
	b, _ := newPolicyTestBackend(t, "memkv", func(c *Config) { c.KazCompactionCooldown = time.Minute })
	old := b.kazCompact.Load()
	old.onCommit(1, 0, 99)
	old.request(1, 0, 99, 0, false)
	b.stopPriorityCompactor()
	if b.kazCompact.Load() != nil {
		t.Fatal("policy still attached after stop")
	}
	b.startPriorityCompactor()
	if p := b.kazCompact.Load(); p == nil || p == old {
		t.Fatal("restart did not attach a new policy")
	}
	if got := policyEvents(b.kazCompactMetrics, "discarded_on_stop"); got != 1 {
		t.Fatalf("discarded_on_stop=%v, want 1", got)
	}
}

// TestKazCompactionSchedulerMetrics records start observations from the attached priority scheduler.
func TestKazCompactionSchedulerMetrics(t *testing.T) {
	b, dispatched := newPolicyTestBackend(t, "memkv", func(*Config) {})
	inode, _ := createGCTestFile(t, b, "metrics")
	rawSlices(t, b, inode, 2, 4)
	var s []Slice
	if st := b.Read(Background(), inode, 0, &s); st != 0 {
		t.Fatal(st)
	}
	expectDispatch(t, dispatched)
	var m dto.Metric
	if err := b.kazCompactMetrics.slices.Write(&m); err != nil {
		t.Fatal(err)
	}
	if m.GetHistogram().GetSampleCount() == 0 {
		t.Fatal("scheduled slices histogram is empty")
	}
}
```

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./pkg/meta -run 'TestKazCompaction' -count=1`
Expected: コンパイルエラー（`b.kazCompact`、`b.kazCompactMetrics` が未定義）

- [ ] **Step 3: 本体（base.go）を変える**

1. baseMeta の struct で `priorityCompactor atomic.Pointer[compactionScheduler]` の次の行に足す: `kazCompact        atomic.Pointer[compactionPolicy]`
2. 同じ struct で `compactionGCEvents *prometheus.CounterVec` の次の行に足す: `kazCompactMetrics  *kazCompactionMetrics`
3. `newBaseMeta` の struct リテラルで、`compactionGCEvents: prometheus.NewCounterVec(...)` の項目（`}, []string{"event"}),` で終わる）の直後に足す: `kazCompactMetrics: newKazCompactionMetrics(),`
4. `InitMetrics` で `reg.MustRegister(m.compactionGCEvents)` の直後に足す: `m.kazCompactMetrics.register(reg)`
5. `Read` の `m.requestBackgroundCompaction(inode, indx, len(ss), tierID)` を `m.requestCompactionFromRead(inode, indx, len(ss), tierID)` にする。
6. `afterWrite` で `if compactionWanted(prevSlices, ph.numSlices) {` の直前に `m.notifyCompactionCommit(inode, indx, ph.numSlices)` を足し、その中の `m.requestBackgroundCompaction(inode, indx, ph.numSlices, int(attr.Tier))` を `m.requestCompactionFromCommit(inode, indx, ph.numSlices, int(attr.Tier))` にする。

- [ ] **Step 4: kaz のファイルを変える**

`pkg/meta/compaction_policy_kaz.go` の末尾に足す。

```go
// startCompactionPolicy attaches the kaz policy when its knobs differ from upstream.
func (m *baseMeta) startCompactionPolicy() {
	if m.conf.ReadOnly || !m.conf.kazCompactionPolicyEnabled() || m.kazCompact.Load() != nil {
		return
	}
	if err := m.conf.ValidateKazCompactionPolicy(); err != nil {
		logger.Warnf("%s; keeping the upstream compaction triggers", err)
		return
	}
	readMin, cooldown, force := m.conf.kazCompactionSettings()
	p := newCompactionPolicy(readMin, cooldown, force, m.requestBackgroundCompaction, m.kazCompactMetrics)
	if !m.kazCompact.CompareAndSwap(nil, p) {
		return
	}
	p.start()
	logger.Infof("kaz compaction policy: read-min=%d cooldown=%s force-slices=%d", readMin, cooldown, force)
}

// stopCompactionPolicy detaches the policy and discards its held requests.
func (m *baseMeta) stopCompactionPolicy() {
	if p := m.kazCompact.Swap(nil); p != nil {
		p.close()
	}
}

// requestCompactionFromRead routes a read-triggered request through the policy, if any.
func (m *baseMeta) requestCompactionFromRead(inode Ino, chunk uint32, count, tier int) {
	if p := m.kazCompact.Load(); p != nil {
		p.request(inode, chunk, count, tier, true)
		return
	}
	m.requestBackgroundCompaction(inode, chunk, count, tier)
}

// requestCompactionFromCommit routes a commit-triggered request through the policy, if any.
func (m *baseMeta) requestCompactionFromCommit(inode Ino, chunk uint32, count, tier int) {
	if p := m.kazCompact.Load(); p != nil {
		p.request(inode, chunk, count, tier, false)
		return
	}
	m.requestBackgroundCompaction(inode, chunk, count, tier)
}

// notifyCompactionCommit tells the policy that a commit reached the chunk.
func (m *baseMeta) notifyCompactionCommit(inode Ino, chunk uint32, count int) {
	if p := m.kazCompact.Load(); p != nil {
		p.onCommit(inode, chunk, count)
	}
}
```

`pkg/meta/compaction_scheduler_lifecycle.go` の 2 つの関数を次にする（コメントも更新する）。

```go
// startPriorityCompactor attaches the kaz compaction policy and, for the priority
// scheduler, request-derived workers, independently of periodic background jobs.
func (m *baseMeta) startPriorityCompactor() {
	m.startCompactionPolicy()
	if m.conf.CompactionScheduler != "priority" || m.conf.ReadOnly || m.priorityCompactor.Load() != nil {
		return
	}
	s := newCompactionScheduler(func(inode Ino, chunk uint32, tier int) {
		m.compactChunk(inode, chunk, false, false, tier)
	}, 11, 1024)
	if m.kazCompactMetrics != nil {
		s.setObserver(m.kazCompactMetrics.observeStart)
	}
	m.priorityCompactor.Store(s)
}

// stopPriorityCompactor detaches the kaz policy first, then joins scheduler
// callbacks before the metadata connection can be shut down.
func (m *baseMeta) stopPriorityCompactor() {
	m.stopCompactionPolicy()
	if s := m.priorityCompactor.Swap(nil); s != nil {
		s.close()
		s.wait()
	}
}
```

- [ ] **Step 5: 通ることを確かめる**

Run: `go test ./pkg/meta -run 'TestKazCompaction|TestCompactionPolicy|TestPriorityCompactor|TestCompactionScheduler' -count=1 -v`
Expected: 全部 PASS。`TestKazCompactionPolicyBatchCommit/redis` は `SKIP`（環境変数なし）。

Run: `go test -race ./pkg/meta -run 'TestKazCompaction|TestCompactionPolicy' -count=3`
Expected: `ok`、新しいコードの DATA RACE なし

- [ ] **Step 6: 一時 Redis で batch のテストを流す**

agent_memo（2026-10-07）の手順で、Redis 8 を scratchpad に展開して `127.0.0.1:6379` で起動する（OS にはインストールしない。本番 56379 には接続しない）。

Run: `JUICEFS_TEST_COMPACTION_GC_REDIS=127.0.0.1:6379/15 go test ./pkg/meta -run 'TestKazCompactionPolicyBatchCommit' -count=1 -v`
Expected: `redis` を含めて PASS。終わったら一時 Redis を止める。

- [ ] **Step 7: commit（ユーザーが許可した場合のみ）**

```bash
gofmt -l pkg/meta
git add pkg/meta/base.go pkg/meta/compaction_scheduler_lifecycle.go pkg/meta/compaction_policy_kaz.go pkg/meta/compaction_policy_kaz_wiring_test.go
git commit -m "meta: route background compaction requests through the kaz policy"
```

---

### Task 5: mount のオプション

**Files:**
- Create: `cmd/kaz_compaction_policy.go`
- Modify: `cmd/flags.go`（`storageFlags` の `compaction-scheduler` の直後に 3 項目）
- Modify: `cmd/mount.go`（`getMetaConf` に 1 行）
- Test: `cmd/kaz_compaction_policy_flag_test.go`

**Interfaces:**
- Consumes: Task 1 の `Config` の 3 項目と `ValidateKazCompactionPolicy`
- Produces: `func parseKazCompactionCooldown(s string) (time.Duration, error)`、`func applyKazCompactionPolicyFlags(c *cli.Context, conf *meta.Config)`

- [ ] **Step 1: 失敗するテストを書く**

`cmd/kaz_compaction_policy_flag_test.go`（ライセンスヘッダは同上）:

```go
package cmd

import (
	"testing"
	"time"

	"github.com/juicedata/juicefs/pkg/meta"
	"github.com/stretchr/testify/require"
	"github.com/urfave/cli/v2"
)

// metaConfFromArgs runs the mount flag set and returns the metadata config it builds.
func metaConfFromArgs(t *testing.T, args ...string) *meta.Config {
	var conf *meta.Config
	app := &cli.App{Flags: cmdMount().Flags, Action: func(c *cli.Context) error {
		conf = getMetaConf(c, "/kaz-policy-test", false)
		return nil
	}}
	require.NoError(t, app.Run(append([]string{"juicefs"}, args...)))
	return conf
}

// TestKazCompactionPolicyFlags keeps the upstream behavior by default and parses the opt-in values.
func TestKazCompactionPolicyFlags(t *testing.T) {
	c := metaConfFromArgs(t)
	require.Equal(t, 5, c.KazCompactionReadMin)
	require.Zero(t, c.KazCompactionCooldown)
	require.Equal(t, 1000, c.KazCompactionForceSlices)

	c = metaConfFromArgs(t, "--kaz-compaction-read-min", "100", "--kaz-compaction-cooldown", "1m", "--kaz-compaction-force-slices", "700")
	require.Equal(t, 100, c.KazCompactionReadMin)
	require.Equal(t, time.Minute, c.KazCompactionCooldown)
	require.Equal(t, 700, c.KazCompactionForceSlices)

	c = metaConfFromArgs(t, "--kaz-compaction-cooldown", "90")
	require.Equal(t, 90*time.Second, c.KazCompactionCooldown)
}

// TestParseKazCompactionCooldown rejects malformed durations instead of reading them as 0.
func TestParseKazCompactionCooldown(t *testing.T) {
	for in, want := range map[string]time.Duration{"0": 0, "1m": time.Minute, "90": 90 * time.Second, "1.5": 1500 * time.Millisecond} {
		got, err := parseKazCompactionCooldown(in)
		require.NoError(t, err, in)
		require.Equal(t, want, got, in)
	}
	for _, in := range []string{"", "abc", "1x", "1d"} {
		_, err := parseKazCompactionCooldown(in)
		require.Error(t, err, in)
	}
}
```

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./cmd -run 'TestKazCompactionPolicyFlags|TestParseKazCompactionCooldown' -count=1`
Expected: コンパイルエラー（`parseKazCompactionCooldown` が未定義）

- [ ] **Step 3: 実装する**

`cmd/kaz_compaction_policy.go`（ライセンスヘッダは同上）:

```go
package cmd

import (
	"fmt"
	"strconv"
	"time"

	"github.com/juicedata/juicefs/pkg/meta"
	"github.com/urfave/cli/v2"
)

// parseKazCompactionCooldown reads --kaz-compaction-cooldown as seconds or a Go
// duration, and reports malformed text instead of reading it as 0.
func parseKazCompactionCooldown(s string) (time.Duration, error) {
	if v, err := strconv.ParseFloat(s, 64); err == nil {
		return time.Duration(v * float64(time.Second)), nil
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, fmt.Errorf("invalid kaz-compaction-cooldown %q: %w", s, err)
	}
	return d, nil
}

// applyKazCompactionPolicyFlags copies the kaz compaction policy flags into conf
// and exits on invalid values, like the other compaction modes.
func applyKazCompactionPolicyFlags(c *cli.Context, conf *meta.Config) {
	cooldown, err := parseKazCompactionCooldown(c.String("kaz-compaction-cooldown"))
	if err != nil {
		logger.Fatalf("%s", err)
	}
	conf.KazCompactionReadMin = c.Int("kaz-compaction-read-min")
	conf.KazCompactionCooldown = cooldown
	conf.KazCompactionForceSlices = c.Int("kaz-compaction-force-slices")
	if err := conf.ValidateKazCompactionPolicy(); err != nil {
		logger.Fatalf("%s", err)
	}
}
```

`cmd/flags.go` の `storageFlags` で、`Name: "compaction-scheduler"` の `&cli.StringFlag{...},` の直後に足す。

```go
		&cli.IntFlag{
			Name:  "kaz-compaction-read-min",
			Value: 5,
			Usage: "[kaz] request background compaction from a read only when the chunk has at least this many slices (5 keeps the upstream behavior)",
		},
		&cli.StringFlag{
			Name:  "kaz-compaction-cooldown",
			Value: "0",
			Usage: "[kaz] hold background compaction of a chunk until it has not been written for this long (0 disables it; 1s-1h)",
		},
		&cli.IntFlag{
			Name:  "kaz-compaction-force-slices",
			Value: 1000,
			Usage: "[kaz] with --kaz-compaction-cooldown, compact a held chunk once it has this many slices (100-2499)",
		},
```

`cmd/mount.go` の `getMetaConf` で、`ValidateCompactionScheduler` の `if` ブロックの直後に足す: `applyKazCompactionPolicyFlags(c, conf)`

- [ ] **Step 4: 通ることを確かめる**

Run: `go test ./cmd -run 'TestKazCompactionPolicyFlags|TestParseKazCompactionCooldown|TestCombinedVMOptions' -count=1 -v`
Expected: 全部 PASS

- [ ] **Step 5: commit（ユーザーが許可した場合のみ）**

```bash
gofmt -l cmd
git add cmd/kaz_compaction_policy.go cmd/kaz_compaction_policy_flag_test.go cmd/flags.go cmd/mount.go
git commit -m "cmd: add --kaz-compaction-read-min, --kaz-compaction-cooldown and --kaz-compaction-force-slices"
```

---

### Task 6: 全体の確認、ビルド、記録

**Files:**
- 本体の変更なし（確認とビルドのみ）
- Modify（調査リポジトリ、commit しない）: `TODO.md`、`agent_memo.md`

- [ ] **Step 1: 静的な確認**

```bash
cd ~/tmp_local/juicefs_inspection/juicefs
gofmt -l pkg cmd           # 何も出ないこと
go vet ./pkg/meta ./cmd    # 新しい指摘が無いこと（既存の指摘は記録する）
git diff --stat 1.4.1-improve-kaz -- pkg/meta/base.go pkg/meta/config.go cmd/flags.go cmd/mount.go
```

Expected: upstream 由来のファイルの差分が、計画どおりの数行（base.go 約 6 行、config.go 約 7 行、flags.go 約 15 行、mount.go 1 行）に収まっている。

- [ ] **Step 2: meta の core のテスト**

```bash
export TMPDIR=$HOME/tmp_local/gotmp && mkdir -p $TMPDIR
SKIP_NON_CORE=true go test -count=1 -timeout=12m ./pkg/meta/... 2>&1 | tail -30
```

Expected: `ok  github.com/juicedata/juicefs/pkg/meta`。失敗があれば、`git stash` で変更を外した 1.4.1-improve-kaz でも同じく失敗するか確かめ、既存の失敗と新しい失敗を分けて記録する。

- [ ] **Step 3: 新しいテストを race で 3 回**

```bash
go test -race -count=3 ./pkg/meta -run 'TestKazCompaction|TestCompactionPolicy|TestCompactionScheduler|TestPriorityCompactor'
go test -race -count=3 ./cmd -run 'TestKazCompactionPolicyFlags|TestParseKazCompactionCooldown'
```

Expected: `ok`。DATA RACE が出たら、新しいコードの分か、既存の分（`utils.WithTimeout`、`rawFull` など）かを分けて記録する。

- [ ] **Step 4: ビルド**

```bash
make juicefs
./juicefs version
./juicefs mount --help | grep -A1 kaz-compaction
sha256sum juicefs
```

Expected: 3 つのオプションが help に出る。version と sha256 を控える。

- [ ] **Step 5: 調査リポジトリに記録する（commit しない）**

`TODO.md` の「PUT を減らす」の項目に、実装の結果（ブランチ、各 commit、テストの結果、ビルドの version と sha256、既存の失敗の扱い）を追記する。`agent_memo.md` に同じ要点を 1〜2 行で追記する。

- [ ] **Step 6: ユーザーに実機の計測を依頼する**

ユーザーに次を伝える（mount の操作はユーザーが行う）。

- ビルドした `juicefs`（version と sha256）を使い、今の起動オプションに `--kaz-compaction-read-min 100 --kaz-compaction-cooldown 1m --kaz-compaction-force-slices 700` と、計測のための `--debug` を足して mount し直す。
- 2026-10-13 と同じ手順（VM ubuntu2604-desktop-mt を起動、ほぼ起動の時刻、アプリの起動、落ち着いた時刻）。accesslog と metrics-before／after は agent が取る。
- 比べる値: compaction の回数、PUT と DELETE、`kaz_compaction_policy_events_total`、`kaz_compaction_queue_wait_seconds`、`kaz_compaction_scheduled_slices`、Read 前の flush の待ち、同期 compaction の有無。机上の見込み（約 −88%）との差も記録する。

---

## 実行後の追記（2026-10-14）

Task 1〜6 は計画どおり実行した（本体 9e54ec94〜7e32cde7、ledger は `juicefs/.superpowers/sdd/2026-10-14-kaz-compaction-policy/progress.md`）。最終 review の Minor の一部を直した（11faab81、850498d5）。実機計測で cooldown が PUT の総数を増やすとわかり、ユーザーの承認で削除した（49908265）。この計画の Task 2・4・5 の cooldown と force に関する部分は、現在のコードには残っていない。
