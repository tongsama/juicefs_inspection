# 同 chunk の slice commit をまとめる（`--meta-write-batch`）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** chunk ごとの commitThread が、同じ chunk の連続した commit 可能な slice を、1回の metadata transaction（`Meta.WriteSlices`）でまとめて commit できるようにする。Redis・SQL・TKV の3 engine すべてに対応し、`--meta-write-batch=N`（既定 0 = 無効）で有効にする。

**Architecture:**
- meta 層に `WriteSlices` を追加する。engine が任意の interface `sliceBatchWriter`（`doWriteSlices`）を実装していれば1回の transaction で書き、未実装なら従来の `Write` を1件ずつ呼ぶ。
- 未適用が確実なエラー（ENOENT・EPERM・ENOSPC・EDQUOT）では、1件ずつに戻して正確な n と errno を返す。適用されたか分からないエラー（EIO 等）では再実行しない。
- writer の commitThread は、先頭から連続して done の slice を最大 N 件集め、Phase 1 の `commitMu` と mtime の下限の下で `WriteSlices` を呼ぶ。

**Tech Stack:** Go 1.25.11（goenv）、go-redis、xorm、JuiceFS の TKV（memkv）。

**Spec:** `docs/superpowers/specs/2026-10-07-meta-write-batch-design.md`（調査リポジトリ）。

**作業ディレクトリ:** 本体 `/home/kwatanabe/tmp_local/juicefs_inspection/juicefs`、ブランチ `feat/range-flush`（HEAD 18e641b8）。以下のパスは、この本体からの相対パスとする。

## Global Constraints

- 既定は `--meta-write-batch=0`（無効）。無効のときは従来の `Meta.Write` だけを呼び、挙動を変えない。値の範囲は 0〜1024。
- 同じ chunk の中の作成順、growing slice の dep、fsync・read-after-write の意味を変えない。
- commit が失敗した slice には正確な errno を返す。実際の保存失敗を隠さない。
- EIO・ネットワークエラーなど、適用されたかどうか分からないエラーでは、batch を再実行しない（slice の二重登録を避ける）。
- metadata の形式・changelog の形式（`WRITE(...)`）を変えない。
- 新規・変更する関数には、目的を書いたコメントを付ける。新規の .go ファイルには Apache 2.0 ヘッダを付ける（`AGENTS.md` の形式。年は 2026）。
- Redis のテストは一時 Redis（127.0.0.1:6379）で行う。**本番の Redis（ポート 56379）には接続しない。**
- 実機での計測は Redis だけ。SQL・TKV は、SQLite と memkv の単体テストで確認する。PostgreSQL・MySQL は source review のみ。
- **commit・push はユーザーの指示があるまで行わない。** commit に Co-Authored-By を付けない。各タスク末尾の「commit」は、ユーザーの指示があった場合だけ行う。
- 本番の mount・VM・metadata・cache・staging には触れない。

## Review Focus

1. **EXEC 後のネットワークエラー（適用済みかどうか不明）**: 再実行せず、batch 全体を失敗として返すこと。Task 2 の `TestWriteSlicesNoRetryOnUnknownError` で確認する。
2. **batch の途中に、エラーを持つ slice（staging の失敗など）がある場合**: そこで batch を切り、その slice は従来どおり単独で失敗処理されること。Task 5 の `TestMetaWriteBatchStopsAtFailedSlice` で確認する。
3. **open していない inode（open-file 未登録、`f == nil`）への WriteSlices**: lock なしでも正しく書けること。Task 2 の共通テスト（Create のあと Open せずに書く）で確認する。
4. **batch が compaction の閾値（100件ごとの区切り・350件超・2,500件）をまたぐ場合**: 1件ずつと同じ判定になること。Task 1 の `TestCompactionWanted` と、共通の処理 `afterWrite` を通る既存の `TestWriteWaitsForCompactionDeleteQueue`（Task 2 Step 6）で確認する。
5. **utimes が batch の commit 中に入った場合**: 明示した mtime が下限で上書きされないこと。Task 5 で、既存の `TestMtimeExplicitPastTimeWins` を batch 有効でも回して確認する（`JFS_TEST_META_WRITE_BATCH`）。

---

## ファイル構成

| ファイル | 役割 | タスク |
|---|---|---|
| `pkg/meta/interface.go` | `SliceWrite` 型と `Meta.WriteSlices` の宣言 | 2 |
| `pkg/meta/base.go` | `compactionWanted`、`writeLocked`（`Write` の本体を切り出し）、`WriteSlices`、`sliceBatchWriter` | 1, 2 |
| `pkg/meta/write_slices_test.go`（新規） | 3 engine 共通の `WriteSlices` テストと、engine のラッパー | 1, 2, 3, 7, 8 |
| `pkg/meta/redis.go` | `redisMeta.doWriteSlices` | 3 |
| `pkg/meta/sql.go` | `dbMeta.doWriteSlices` | 7 |
| `pkg/meta/tkv.go` | `kvMeta.doWriteSlices` | 8 |
| `pkg/vfs/vfs.go` | `Config.MetaWriteBatch` | 4 |
| `pkg/vfs/writer.go` | 値の正規化、commitThread で batch を作る | 4, 5 |
| `pkg/vfs/writer_batch_test.go`（新規） | writer の batch のテスト | 4, 5 |
| `pkg/vfs/vfs_test.go`、`writer_trace_test.go`、`writer_range_test.go` | テスト用の meta ラッパーに `WriteSlices` を追加。`JFS_TEST_META_WRITE_BATCH` | 4 |
| `pkg/fuse/fuse_test.go` | `JFS_TEST_META_WRITE_BATCH` | 4 |
| `cmd/flags.go`、`cmd/mount.go`、`cmd/writer_flush_test.go` | フラグ | 6 |
| `docs/en/reference/_common_options.mdx`、`docs/zh_cn/...` | ドキュメント | 6 |

---

### Task 1: compaction の判定を「範囲」で評価する関数に切り出す

**Files:**
- Modify: `pkg/meta/base.go`（`baseMeta.Write` の compaction 判定。2213 行付近）
- Create: `pkg/meta/write_slices_test.go`

**Interfaces:**
- Produces: `func compactionWanted(prev, now int) bool`（Task 2 で使う）

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/write_slices_test.go` を作る（Apache ヘッダ、`package meta`）。

```go
// TestCompactionWanted matches the per-slice triggers for single writes and
// evaluates every count a batch passes through.
func TestCompactionWanted(t *testing.T) {
	for _, tc := range []struct {
		prev, now int
		want      bool
	}{
		{0, 1, false}, {98, 99, true}, {99, 100, false}, {198, 199, true},
		{350, 351, true}, {349, 350, false},
		// Batches: crossing a %100==99 count or exceeding 350 anywhere in (prev, now].
		{90, 105, true}, {100, 120, false}, {340, 360, true}, {0, 0, false},
	} {
		if got := compactionWanted(tc.prev, tc.now); got != tc.want {
			t.Errorf("compactionWanted(%d, %d) = %v, want %v", tc.prev, tc.now, got, tc.want)
		}
	}
	// Equivalent to the original single-write condition.
	for n := 1; n < 3000; n++ {
		if compactionWanted(n-1, n) != (n%100 == 99 || n > 350) {
			t.Fatalf("single write mismatch at %d", n)
		}
	}
}
```

- [ ] **Step 2: 失敗を確認する**

Run: `go test ./pkg/meta/ -run TestCompactionWanted -count=1`
Expected: FAIL（`undefined: compactionWanted`）

- [ ] **Step 3: 実装する**

`pkg/meta/base.go` の `baseMeta.Write` の直前に追加する。

```go
// compactionWanted reports whether a chunk whose slice count grew from prev to
// now passed a count at which a single Write would request compaction
// (every count ending in 99, or more than 350 slices).
func compactionWanted(prev, now int) bool {
	if now > 350 {
		return true
	}
	for n := max(prev+1, 0); n <= now; n++ {
		if n%100 == 99 {
			return true
		}
	}
	return false
}
```

`baseMeta.Write` の `if numSlices%100 == 99 || numSlices > 350 {` を `if compactionWanted(numSlices-1, numSlices) {` に置き換える。

- [ ] **Step 4: 通ることを確認する**

Run: `go test ./pkg/meta/ -run 'TestCompactionWanted|TestWriteWaitsForCompactionDeleteQueue' -count=1`
Expected: PASS

- [ ] **Step 5: commit（ユーザーの指示があった場合のみ）**

---

### Task 2: `Meta.WriteSlices` と baseMeta の実装（engine は1件ずつの代替経路）

**Files:**
- Modify: `pkg/meta/interface.go`（`Slice` 型の後と `Meta` interface の `Write` の後）
- Modify: `pkg/meta/base.go`（`Write` の本体を `writeLocked` に切り出し、`WriteSlices` を追加）
- Test: `pkg/meta/write_slices_test.go`

**Interfaces:**
- Consumes: `compactionWanted`（Task 1）
- Produces:
  - `type SliceWrite struct { Off uint32; Slice Slice }`
  - `Meta.WriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time) (n int, st syscall.Errno)`
  - `type sliceBatchWriter interface { doWriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time, numSlices *int, delta *dirStat, attr *Attr) syscall.Errno }`（Task 3, 7, 8 が engine に実装する）
  - テストヘルパー `testWriteSlices(t *testing.T, m Meta)`、`newWriteSlicesMemKV(t)`（Task 3, 7, 8 で使う）

- [ ] **Step 1: 失敗するテストを書く**

`pkg/meta/write_slices_test.go` に追加する。

```go
// newWriteSlicesMeta prepares an initialized client with a session for WriteSlices tests.
func newWriteSlicesMeta(t *testing.T, m Meta) Meta {
	t.Helper()
	if err := m.Reset(); err != nil {
		t.Fatalf("reset meta: %s", err)
	}
	if err := m.Init(testFormat(), false); err != nil {
		t.Fatalf("init: %s", err)
	}
	if err := m.NewSession(false); err != nil {
		t.Fatalf("new session: %s", err)
	}
	m.OnMsg(DeleteSlice, func(args ...interface{}) error { return nil })
	m.OnMsg(CompactChunk, func(args ...interface{}) error { return nil })
	t.Cleanup(func() { _ = m.CloseSession() })
	return m
}

// newWriteSlicesMemKV returns a fresh MemKV client.
func newWriteSlicesMemKV(t *testing.T) Meta {
	m, err := newKVMeta("memkv", "jfs-write-slices-"+t.Name(), testConfig())
	if err != nil {
		t.Fatalf("create meta: %s", err)
	}
	return newWriteSlicesMeta(t, m)
}

// sliceShapes builds count 4 KiB slices at consecutive 4 KiB offsets of a chunk,
// allocating real slice IDs.
func sliceShapes(t *testing.T, m Meta, count int, gap uint32) []SliceWrite {
	t.Helper()
	ws := make([]SliceWrite, count)
	for i := range ws {
		var id uint64
		if st := m.NewSlice(Background(), &id); st != 0 {
			t.Fatalf("new slice: %s", st)
		}
		ws[i] = SliceWrite{Off: uint32(i) * (4096 + gap), Slice: Slice{Id: id, Size: 4096, Len: 4096}}
	}
	return ws
}

// chunkLayout returns the visible slices of a chunk with IDs replaced by their write order.
func chunkLayout(t *testing.T, m Meta, inode Ino, indx uint32, ws []SliceWrite) []Slice {
	t.Helper()
	order := make(map[uint64]uint64, len(ws))
	for i, w := range ws {
		order[w.Slice.Id] = uint64(i + 1)
	}
	var ss []Slice
	if st := m.Read(Background(), inode, indx, &ss); st != 0 {
		t.Fatalf("read: %s", st)
	}
	for i := range ss {
		ss[i].Id = order[ss[i].Id]
	}
	return ss
}

// createIn creates a file under a new directory and returns both inodes.
func createIn(t *testing.T, m Meta, name string) (dir, file Ino) {
	t.Helper()
	ctx := Background()
	var attr Attr
	if st := m.Mkdir(ctx, RootInode, name, 0755, 0, 0, &dir, &attr); st != 0 {
		t.Fatalf("mkdir %s: %s", name, st)
	}
	if st := m.Create(ctx, dir, "f", 0644, 0, 0, &file, &attr); st != 0 {
		t.Fatalf("create %s/f: %s", name, st)
	}
	return dir, file
}

// testWriteSlices checks that WriteSlices matches sequential Write calls, and reports
// partial failures with the index and errno of the first failing slice.
func testWriteSlices(t *testing.T, m Meta) {
	ctx := Background()
	mtime := time.Unix(1700000000, 123)

	t.Run("matches sequential writes", func(t *testing.T) {
		_, a := createIn(t, m, "seq")
		_, b := createIn(t, m, "batch")
		wa := sliceShapes(t, m, 5, 4096)
		wb := sliceShapes(t, m, 5, 4096)
		// Overlap: the last slice overwrites the first; order must be kept.
		wa[4].Off, wb[4].Off = 0, 0
		for _, w := range wa {
			if st := m.Write(ctx, a, 1, w.Off, w.Slice, mtime); st != 0 {
				t.Fatalf("write: %s", st)
			}
		}
		if n, st := m.WriteSlices(ctx, b, 1, wb, mtime); n != len(wb) || st != 0 {
			t.Fatalf("WriteSlices = %d, %s", n, st)
		}
		if la, lb := chunkLayout(t, m, a, 1, wa), chunkLayout(t, m, b, 1, wb); !reflect.DeepEqual(la, lb) {
			t.Fatalf("layout differs:\nseq   %+v\nbatch %+v", la, lb)
		}
		var aa, ab Attr
		if st := m.GetAttr(ctx, a, &aa); st != 0 {
			t.Fatal(st)
		}
		if st := m.GetAttr(ctx, b, &ab); st != 0 {
			t.Fatal(st)
		}
		// Offsets 0, 8192, 16384, 24576 and an overwrite at 0, in chunk 1.
		if aa.Length != ab.Length || ab.Length != ChunkSize+24576+4096 {
			t.Fatalf("length seq=%d batch=%d", aa.Length, ab.Length)
		}
		if ab.Mtime != mtime.Unix() || ab.Mtimensec != uint32(mtime.Nanosecond()) {
			t.Fatalf("mtime %d.%d, want %v", ab.Mtime, ab.Mtimensec, mtime)
		}
	})

	t.Run("single slice uses Write", func(t *testing.T) {
		_, f := createIn(t, m, "single")
		ws := sliceShapes(t, m, 1, 0)
		if n, st := m.WriteSlices(ctx, f, 0, ws, mtime); n != 1 || st != 0 {
			t.Fatalf("WriteSlices = %d, %s", n, st)
		}
		if l := chunkLayout(t, m, f, 0, ws); len(l) != 1 || l[0].Id != 1 {
			t.Fatalf("layout %+v", l)
		}
	})

	t.Run("quota stops at the first slice over the limit", func(t *testing.T) {
		dir, f := createIn(t, m, "quota")
		if err := m.HandleQuota(ctx, QuotaSet, "/quota", DirQuotaType,
			map[string]*Quota{"/quota": {MaxSpace: 3 * 4096, MaxInodes: 100}}, false, false, false); err != nil {
			t.Fatalf("set quota: %s", err)
		}
		m.getBase().loadQuotas()
		_ = dir
		ws := sliceShapes(t, m, 4, 0)
		n, st := m.WriteSlices(ctx, f, 0, ws, mtime)
		if n != 3 || st != syscall.EDQUOT {
			t.Fatalf("WriteSlices = %d, %s; want 3, EDQUOT", n, st)
		}
		if l := chunkLayout(t, m, f, 0, ws); len(l) != 3 {
			t.Fatalf("layout after partial failure %+v", l)
		}
	})

	t.Run("missing inode", func(t *testing.T) {
		ws := sliceShapes(t, m, 3, 0)
		if n, st := m.WriteSlices(ctx, Ino(1<<40), 0, ws, mtime); n != 0 || st != syscall.ENOENT {
			t.Fatalf("WriteSlices = %d, %s; want 0, ENOENT", n, st)
		}
	})

	t.Run("directory inode", func(t *testing.T) {
		dir, _ := createIn(t, m, "notfile")
		ws := sliceShapes(t, m, 3, 0)
		if n, st := m.WriteSlices(ctx, dir, 0, ws, mtime); n != 0 || st != syscall.EPERM {
			t.Fatalf("WriteSlices = %d, %s; want 0, EPERM", n, st)
		}
	})

	t.Run("open file", func(t *testing.T) {
		_, f := createIn(t, m, "opened")
		var attr Attr
		if st := m.Open(ctx, f, syscall.O_RDWR, &attr); st != 0 {
			t.Fatalf("open: %s", st)
		}
		defer m.Close(ctx, f)
		ws := sliceShapes(t, m, 4, 0)
		if n, st := m.WriteSlices(ctx, f, 0, ws, mtime); n != 4 || st != 0 {
			t.Fatalf("WriteSlices = %d, %s", n, st)
		}
		// The open-file chunk cache must not keep the pre-batch slice list.
		if l := chunkLayout(t, m, f, 0, ws); len(l) != 4 {
			t.Fatalf("stale chunk cache %+v", l)
		}
	})
}

// TestWriteSlicesMemKV runs the shared WriteSlices checks on MemKV.
func TestWriteSlicesMemKV(t *testing.T) {
	testWriteSlices(t, newWriteSlicesMemKV(t))
}
```

- [ ] **Step 2: 失敗を確認する**

Run: `go test ./pkg/meta/ -run 'TestWriteSlicesMemKV' -count=1`
Expected: FAIL（`m.WriteSlices undefined`）

- [ ] **Step 3: interface を追加する**

`pkg/meta/interface.go` の `Slice` 型の直後:

```go
// SliceWrite is one slice to append to a chunk, starting at Off within the chunk.
type SliceWrite struct {
	Off   uint32
	Slice Slice
}
```

`Meta` interface の `Write(...)` の直後:

```go
	// WriteSlices appends slices to chunk indx in order, using one metadata
	// transaction when the engine supports it. The first n slices are committed;
	// if n < len(slices), slices[n] failed with st and the rest were not written.
	WriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time) (n int, st syscall.Errno)
```

- [ ] **Step 4: `Write` の本体を切り出し、`WriteSlices` を実装する**

`pkg/meta/base.go` の `baseMeta.Write` を、次の3つに置き換える（ログの文言・WARN の条件は従来のまま）。

```go
// sliceBatchWriter is implemented by engines that can append several slices of
// one chunk in a single transaction, all or nothing.
type sliceBatchWriter interface {
	doWriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time, numSlices *int, delta *dirStat, attr *Attr) syscall.Errno
}

// writePhases records where a metadata write spent its time, for slow-write reports.
type writePhases struct {
	numSlices                         int
	backend, stat, compact            time.Duration
}

// Write commits one slice under the open-file lock.
func (m *baseMeta) Write(ctx Context, inode Ino, indx uint32, off uint32, slice Slice, mtime time.Time) (st syscall.Errno) {
	start := time.Now()
	defer m.timeit("Write", start)
	var lockWait time.Duration
	var ph writePhases
	defer func() {
		if total := time.Since(start); total >= time.Second {
			logger.Warnf("slow metadata write inode=%d chunk=%d slice=%d slices=%d total=%s lock_wait=%s doWrite=%s stat=%s compact=%s errno=%s",
				inode, indx, slice.Id, ph.numSlices, total, lockWait, ph.backend, ph.stat, ph.compact, st)
		}
	}()
	logger.Debugf("metadata write inode=%d chunk=%d slice=%d phase=lock_wait", inode, indx, slice.Id)
	f := m.of.find(inode)
	if f != nil {
		lockStart := time.Now()
		f.Lock()
		defer f.Unlock()
		lockWait = time.Since(lockStart)
	}
	defer func() { m.of.InvalidateChunk(inode, indx) }()
	logger.Debugf("metadata write inode=%d chunk=%d slice=%d phase=doWrite lock_wait=%s", inode, indx, slice.Id, lockWait)
	st = m.writeLocked(ctx, inode, indx, off, slice, mtime, &ph)
	logger.Debugf("metadata write inode=%d chunk=%d slice=%d phase=done errno=%s", inode, indx, slice.Id, st)
	return st
}

// writeLocked commits one slice and applies its statistics and compaction
// triggers; the caller holds the open-file lock (if the file is open) and
// invalidates the chunk cache.
func (m *baseMeta) writeLocked(ctx Context, inode Ino, indx uint32, off uint32, slice Slice, mtime time.Time, ph *writePhases) syscall.Errno {
	var delta dirStat
	var attr Attr
	phaseStart := time.Now()
	st := m.en.doWrite(ctx, inode, indx, off, slice, mtime, &ph.numSlices, &delta, &attr)
	ph.backend = time.Since(phaseStart)
	if st == 0 {
		logger.Debugf("metadata write inode=%d chunk=%d slice=%d phase=stat slices=%d doWrite=%s", inode, indx, slice.Id, ph.numSlices, ph.backend)
		m.afterWrite(ctx, inode, indx, &attr, delta, ph.numSlices-1, ph)
	}
	return st
}

// afterWrite applies the statistics of committed slices and requests
// compaction when the chunk's slice count passed a trigger.
func (m *baseMeta) afterWrite(ctx Context, inode Ino, indx uint32, attr *Attr, delta dirStat, prevSlices int, ph *writePhases) {
	phaseStart := time.Now()
	m.updateParentStat(ctx, inode, attr.Parent, delta.length, delta.space)
	m.updateUserGroupStat(ctx, attr.Uid, attr.Gid, delta.space, 0)
	ph.stat = time.Since(phaseStart)
	if compactionWanted(prevSlices, ph.numSlices) {
		if ph.numSlices < maxSlices {
			m.requestBackgroundCompaction(inode, indx, ph.numSlices, int(attr.Tier))
		} else {
			logger.Debugf("metadata write inode=%d chunk=%d phase=compact slices=%d", inode, indx, ph.numSlices)
			phaseStart = time.Now()
			m.compactChunk(inode, indx, true, false, int(attr.Tier))
			ph.compact = time.Since(phaseStart)
		}
	}
}

// batchErrorUnapplied reports errors that a write transaction returns before
// changing anything, so its slices can safely be retried one by one.
func batchErrorUnapplied(st syscall.Errno) bool {
	return st == syscall.ENOENT || st == syscall.EPERM || st == syscall.ENOSPC || st == syscall.EDQUOT
}

// WriteSlices appends slices to chunk indx in creation order. Engines that
// implement sliceBatchWriter commit them in one transaction; otherwise, or for
// a single slice, they are written one by one with Write.
func (m *baseMeta) WriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time) (int, syscall.Errno) {
	bw, ok := m.en.(sliceBatchWriter)
	if len(slices) <= 1 || !ok {
		for i, w := range slices {
			if st := m.Write(ctx, inode, indx, w.Off, w.Slice, mtime); st != 0 {
				return i, st
			}
		}
		return len(slices), 0
	}
	start := time.Now()
	defer m.timeit("WriteSlices", start)
	first := slices[0].Slice.Id
	var lockWait time.Duration
	var ph writePhases
	var st syscall.Errno
	defer func() {
		if total := time.Since(start); total >= time.Second {
			logger.Warnf("slow metadata write batch inode=%d chunk=%d first_slice=%d count=%d slices=%d total=%s lock_wait=%s doWrite=%s stat=%s compact=%s errno=%s",
				inode, indx, first, len(slices), ph.numSlices, total, lockWait, ph.backend, ph.stat, ph.compact, st)
		}
	}()
	logger.Debugf("metadata write batch inode=%d chunk=%d first_slice=%d count=%d phase=lock_wait", inode, indx, first, len(slices))
	f := m.of.find(inode)
	if f != nil {
		lockStart := time.Now()
		f.Lock()
		defer f.Unlock()
		lockWait = time.Since(lockStart)
	}
	defer func() { m.of.InvalidateChunk(inode, indx) }()
	var delta dirStat
	var attr Attr
	phaseStart := time.Now()
	st = bw.doWriteSlices(ctx, inode, indx, slices, mtime, &ph.numSlices, &delta, &attr)
	ph.backend = time.Since(phaseStart)
	logger.Debugf("metadata write batch inode=%d chunk=%d first_slice=%d count=%d phase=done lock_wait=%s doWrite=%s slices=%d errno=%s",
		inode, indx, first, len(slices), lockWait, ph.backend, ph.numSlices, st)
	if st == 0 {
		m.afterWrite(ctx, inode, indx, &attr, delta, ph.numSlices-len(slices), &ph)
		return len(slices), 0
	}
	if !batchErrorUnapplied(st) {
		// The transaction may have been applied; retrying could register slices twice.
		return 0, st
	}
	for i, w := range slices {
		var one writePhases
		if st = m.writeLocked(ctx, inode, indx, w.Off, w.Slice, mtime, &one); st != 0 {
			return i, st
		}
	}
	return len(slices), 0
}
```

注:
- `ph.numSlices-1` は、従来の単発の条件 `numSlices%100==99 || numSlices>350` と同じになる（Task 1 で確認済み）。
- TKV で同じ slice が既にある場合、従来の `doWrite` は WARN を出して何もせず返す（numSlices は既存の値）。この場合も `compactionWanted(n-1, n)` の判定は従来と同じ値になる。
- `debug` ログの書式（`metadata write inode= ... phase=`）は、`metadata_random_io/2026-10-07/analyze_install.py` が使っている。単発の行は変えない。

- [ ] **Step 5: 適用されたか分からないエラーで再実行しないことのテストを書く（Review Focus 1）**

`pkg/meta/write_slices_test.go` に追加する。

```go
// failingBatchEngine fails batches with a fixed errno and counts single writes,
// to check which batch errors fall back to one-by-one writes.
type failingBatchEngine struct {
	engine
	err    syscall.Errno
	single atomic.Int32
}

// doWriteSlices fails without changing anything.
func (e *failingBatchEngine) doWriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time, numSlices *int, delta *dirStat, attr *Attr) syscall.Errno {
	return e.err
}

// doWrite counts single-slice writes and delegates to the real engine.
func (e *failingBatchEngine) doWrite(ctx Context, inode Ino, indx uint32, off uint32, s Slice, mtime time.Time, n *int, delta *dirStat, attr *Attr) syscall.Errno {
	e.single.Add(1)
	return e.engine.doWrite(ctx, inode, indx, off, s, mtime, n, delta, attr)
}

// TestWriteSlicesNoRetryOnUnknownError returns an unknown batch failure as is,
// and retries one by one only for errors raised before anything was written.
func TestWriteSlicesNoRetryOnUnknownError(t *testing.T) {
	m := newWriteSlicesMemKV(t)
	b := m.getBase()
	orig := b.en
	defer func() { b.en = orig }()
	for _, tc := range []struct {
		err       syscall.Errno
		wantN     int
		wantSt    syscall.Errno
		wantSingle int32
	}{
		{syscall.EIO, 0, syscall.EIO, 0},
		{syscall.EDQUOT, 3, 0, 3}, // the real engine accepts each slice individually
	} {
		fe := &failingBatchEngine{engine: orig, err: tc.err}
		b.en = fe
		_, f := createIn(t, m, "retry-"+tc.err.Error())
		n, st := m.WriteSlices(Background(), f, 0, sliceShapes(t, m, 3, 0), time.Now())
		if n != tc.wantN || st != tc.wantSt || fe.single.Load() != tc.wantSingle {
			t.Errorf("%s: n=%d st=%s single=%d; want %d %s %d", tc.err, n, st, fe.single.Load(), tc.wantN, tc.wantSt, tc.wantSingle)
		}
		b.en = orig
	}
}
```

注: memkv の `kvMeta` は、Task 8 までは `doWriteSlices` を持たない。このテストではラッパーが持つので、batch の経路を通る。`createIn` の名前は、`syscall.Errno.Error()` の文字列にスペースを含むので、実装時に `fmt.Sprint(int(tc.err))` を使ってよい。

- [ ] **Step 6: 通ることを確認する**

Run: `go test ./pkg/meta/ -run 'TestWriteSlices|TestCompactionWanted|TestWriteWaitsForCompactionDeleteQueue' -count=1`
Expected: PASS

Run: `go test ./pkg/meta/ -run 'TestMemKVClient|TestSQLiteClient' -count=1`
Expected: PASS（`Write` の切り出しで既存の挙動が変わっていないこと）

- [ ] **Step 7: commit（ユーザーの指示があった場合のみ）**

---

### Task 3: Redis の `doWriteSlices`

**Files:**
- Modify: `pkg/meta/redis.go`（`redisMeta.doWrite` の直後）
- Test: `pkg/meta/write_slices_test.go`

**Interfaces:**
- Consumes: `SliceWrite`、`sliceBatchWriter`、`testWriteSlices`（Task 2）
- Produces: `func (m *redisMeta) doWriteSlices(...) syscall.Errno`

- [ ] **Step 1: 一時 Redis を起動する**

agent_memo の手順（apt download と dpkg-deb の展開。システムにはインストールしない）で、scratchpad に展開した redis-server を 127.0.0.1:6379 で起動する。

```bash
R=<scratchpad>/redis
$R/x/usr/bin/redis-server --bind 127.0.0.1 --port 6379 --dir $R/data --save '' --appendonly no --daemonize yes --pidfile $R/redis.pid --logfile $R/redis.log
$R/x/usr/bin/redis-cli -p 6379 ping   # PONG
```

- [ ] **Step 2: 失敗するテストを書く**

```go
// TestWriteSlicesRedis runs the shared WriteSlices checks on Redis and requires
// the single-transaction implementation.
func TestWriteSlicesRedis(t *testing.T) {
	m, err := newRedisMeta("redis", "127.0.0.1:6379/11", testConfig())
	if err != nil {
		t.Skipf("redis not available: %s", err)
	}
	if _, ok := m.getBase().en.(sliceBatchWriter); !ok {
		t.Fatal("redis engine does not implement doWriteSlices")
	}
	testWriteSlices(t, newWriteSlicesMeta(t, m))
}
```

- [ ] **Step 3: 失敗を確認する**

Run: `go test ./pkg/meta/ -run TestWriteSlicesRedis -count=1 -v`
Expected: FAIL（`redis engine does not implement doWriteSlices`）

- [ ] **Step 4: 実装する**

```go
// doWriteSlices appends slices to one chunk and updates the inode in a single
// transaction: one RPUSH with every slice, one SET of the attributes.
func (m *redisMeta) doWriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time, numSlices *int, delta *dirStat, attr *Attr) syscall.Errno {
	return errno(m.txn(ctx, func(tx *redis.Tx) error {
		*delta = dirStat{}
		*attr = Attr{}
		a, err := tx.Get(ctx, m.inodeKey(inode)).Bytes()
		if err != nil {
			return err
		}
		m.parseAttr(a, attr)
		if attr.Typ != TypeFile {
			return syscall.EPERM
		}
		oldLength := attr.Length
		for _, w := range slices {
			if newleng := uint64(indx)*ChunkSize + uint64(w.Off) + uint64(w.Slice.Len); newleng > attr.Length {
				attr.Length = newleng
			}
		}
		delta.length = int64(attr.Length - oldLength)
		delta.space = align4K(attr.Length) - align4K(oldLength)
		if err := m.checkQuota(ctx, delta.space, 0, attr.Uid, attr.Gid, m.getParents(ctx, tx, inode, attr.Parent)...); err != 0 {
			return err
		}
		now := time.Now()
		attr.Mtime = mtime.Unix()
		attr.Mtimensec = uint32(mtime.Nanosecond())
		attr.Ctime = now.Unix()
		attr.Ctimensec = uint32(now.Nanosecond())
		vals := make([]interface{}, len(slices))
		for i, w := range slices {
			vals[i] = marshalSlice(w.Off, w.Slice.Id, w.Slice.Size, w.Slice.Off, w.Slice.Len)
		}
		var rpush *redis.IntCmd
		_, err = tx.TxPipelined(ctx, func(pipe redis.Pipeliner) error {
			rpush = pipe.RPush(ctx, m.chunkKey(inode, indx), vals...)
			pipe.Set(ctx, m.inodeKey(inode), m.marshal(attr), 0)
			if delta.space > 0 {
				pipe.IncrBy(ctx, m.usedSpaceKey(), delta.space)
			}
			for _, w := range slices {
				m.genLog(ctx, pipe, now, "WRITE(%d,%d,%d,%d,%d,%d,%d):%d", inode, indx, w.Off, w.Slice.Id, w.Slice.Len, attr.Mtime, attr.Mtimensec, *numSlices)
			}
			return nil
		})
		if err == nil {
			*numSlices = int(rpush.Val())
		}
		return err
	}, m.inodeKey(inode)))
}
```

注: 長さを slice ごとに順に伸ばす計算と、最終の長さとの差をまとめて取る計算は、同じ結果になる（delta.space は `align4K` の差が telescoping するため）。changelog の `:%d` には、従来の `doWrite` と同じく、pipeline を組む時点の `*numSlices` を渡す（従来の挙動を変えない）。

- [ ] **Step 5: 通ることを確認する**

Run: `go test ./pkg/meta/ -run 'TestWriteSlices' -count=1 -v`
Expected: PASS（MemKV・Redis・NoRetry）

- [ ] **Step 6: Redis の既存テストを回す**

Run: `go test ./pkg/meta/ -run 'TestRedisClient' -count=1`
Expected: PASS（baseline と同じ。失敗があれば 18e641b8 の archive でも同じテストを回して比べる）

- [ ] **Step 7: commit（ユーザーの指示があった場合のみ）**

---

### Task 4: VFS の設定と、テスト用 meta ラッパーの `WriteSlices` 対応

**Files:**
- Modify: `pkg/vfs/vfs.go`（Config）
- Modify: `pkg/vfs/writer.go`（`NewDataWriter` の正規化）
- Modify: `pkg/vfs/vfs_test.go`（`failingWriteMeta`、`createTestVFS`）
- Modify: `pkg/vfs/writer_trace_test.go`（`traceCountingMeta`）
- Modify: `pkg/vfs/writer_range_test.go`（`gatedWriteMeta`）
- Modify: `pkg/fuse/fuse_test.go`
- Create: `pkg/vfs/writer_batch_test.go`

**Interfaces:**
- Consumes: `meta.SliceWrite`、`Meta.WriteSlices`（Task 2）
- Produces: `vfs.Config.MetaWriteBatch int`

- [ ] **Step 1: 失敗するテストを書く**

`pkg/vfs/writer_batch_test.go`（新規、Apache ヘッダ）:

```go
// TestMetaWriteBatchNormalization keeps valid sizes and disables invalid ones.
func TestMetaWriteBatchNormalization(t *testing.T) {
	v, _ := createTestVFS(nil, "")
	for _, tc := range []struct{ in, want int }{{0, 0}, {1, 1}, {64, 64}, {1024, 1024}, {-1, 0}, {1025, 0}} {
		conf := *v.Conf
		conf.MetaWriteBatch = tc.in
		w := NewDataWriter(&conf, v.Meta, v.writer.(*dataWriter).store, v.reader).(*dataWriter)
		require.Equal(t, tc.want, w.conf.MetaWriteBatch, tc.in)
	}
}
```

- [ ] **Step 2: 失敗を確認する**

Run: `go test ./pkg/vfs/ -run TestMetaWriteBatchNormalization -count=1`
Expected: FAIL（`conf.MetaWriteBatch undefined`）

- [ ] **Step 3: 実装する**

`pkg/vfs/vfs.go` の Config の `WriterFlushScope` の次:

```go
	MetaWriteBatch       int           // Maximum slices of one chunk committed per metadata transaction; 0 disables batching.
```

`pkg/vfs/writer.go` の `NewDataWriter` の scope の正規化の後:

```go
	if conf.MetaWriteBatch < 0 || conf.MetaWriteBatch > 1024 {
		logger.Warnf("invalid meta write batch %d: disabling batching (valid range 0..1024)", conf.MetaWriteBatch)
		conf.MetaWriteBatch = 0
	}
```

- [ ] **Step 4: テスト用の meta ラッパーに `WriteSlices` を足す**

batch が有効なテストでも、ラッパーの差し込み（失敗・数える・gate）が効くようにする。

`pkg/vfs/vfs_test.go`:

```go
// WriteSlices rejects the whole batch like Write rejects a single slice.
func (m *failingWriteMeta) WriteSlices(ctx meta.Context, inode meta.Ino, indx uint32, slices []meta.SliceWrite, mtime time.Time) (int, syscall.Errno) {
	return 0, m.err
}
```

`createTestVFS` の Config に追加:

```go
		// JFS_TEST_META_WRITE_BATCH=N reruns the suite with batched slice commits.
		MetaWriteBatch: testMetaWriteBatch(),
```

同じファイルに:

```go
// testMetaWriteBatch reads the batch size for suite reruns; unset or invalid means disabled.
func testMetaWriteBatch() int {
	n, _ := strconv.Atoi(os.Getenv("JFS_TEST_META_WRITE_BATCH"))
	return n
}
```

（`strconv` の import を足す。）

`pkg/vfs/writer_trace_test.go`:

```go
// WriteSlices delegates persistence and counts the slices it committed.
func (m *traceCountingMeta) WriteSlices(ctx meta.Context, inode Ino, indx uint32, slices []meta.SliceWrite, mtime time.Time) (int, syscall.Errno) {
	n, err := m.Meta.WriteSlices(ctx, inode, indx, slices, mtime)
	m.writes.Add(int32(n))
	return n, err
}
```

`pkg/vfs/writer_range_test.go`（`gatedWriteMeta.Write` と同じ gate を通す）:

```go
// WriteSlices passes the same gate as Write before committing the batch.
func (m *gatedWriteMeta) WriteSlices(ctx meta.Context, inode Ino, indx uint32, slices []meta.SliceWrite, mtime time.Time) (int, syscall.Errno) {
	m.mu.Lock()
	g := m.gates[indx]
	m.mu.Unlock()
	select {
	case m.started <- indx:
	default:
	}
	if g != nil {
		<-g
	}
	return m.Meta.WriteSlices(ctx, inode, indx, slices, mtime)
}
```

`pkg/fuse/fuse_test.go` の Config に、`WriterFlushScope` の行の次として:

```go
		MetaWriteBatch:   metaWriteBatchFromEnv(),
```

同じファイルに:

```go
// metaWriteBatchFromEnv reads JFS_TEST_META_WRITE_BATCH for suite reruns with batched commits.
func metaWriteBatchFromEnv() int {
	n, _ := strconv.Atoi(os.Getenv("JFS_TEST_META_WRITE_BATCH"))
	return n
}
```

- [ ] **Step 5: 通ることを確認する**

Run: `go test ./pkg/vfs/ -run 'TestMetaWriteBatchNormalization|TestVFSReadFlushError|TestWriterReuseWindow|TestRangeFlush' -count=1`
Expected: PASS

- [ ] **Step 6: commit（ユーザーの指示があった場合のみ）**

---

### Task 5: commitThread で同じ chunk の slice をまとめる

**Files:**
- Modify: `pkg/vfs/writer.go`（`commitThread`）
- Test: `pkg/vfs/writer_batch_test.go`

**Interfaces:**
- Consumes: `Config.MetaWriteBatch`（Task 4）、`Meta.WriteSlices`（Task 2）、Phase 1 の `commitMu`・`mtimeFloor`・`mtimeGen`
- Produces: なし（最終の挙動）

- [ ] **Step 1: テスト用の meta ラッパーを書く**

`pkg/vfs/writer_batch_test.go` に追加する。

```go
// batchRecordingMeta records metadata commits, optionally holds the first one,
// and can inject a result for the first batch.
type batchRecordingMeta struct {
	meta.Meta
	mu      sync.Mutex
	calls   [][]uint64 // slice IDs per metadata call, in call order
	mtimes  []time.Time
	hold    chan struct{} // when set, the first call waits until it is closed
	held    chan struct{} // closed when the first call starts waiting
	inject  func(slices []meta.SliceWrite) (int, syscall.Errno, bool)
	once    sync.Once
}

// record notes a call and waits on the hold gate for the first call.
func (m *batchRecordingMeta) record(ids []uint64, mtime time.Time) {
	m.mu.Lock()
	m.calls = append(m.calls, ids)
	m.mtimes = append(m.mtimes, mtime)
	first := len(m.calls) == 1
	m.mu.Unlock()
	if first && m.hold != nil {
		close(m.held)
		<-m.hold
	}
}

// Write records a single-slice commit.
func (m *batchRecordingMeta) Write(ctx meta.Context, inode Ino, indx, off uint32, s meta.Slice, mtime time.Time) syscall.Errno {
	m.record([]uint64{s.Id}, mtime)
	return m.Meta.Write(ctx, inode, indx, off, s, mtime)
}

// WriteSlices records a batch and applies an injected result once, if any.
func (m *batchRecordingMeta) WriteSlices(ctx meta.Context, inode Ino, indx uint32, slices []meta.SliceWrite, mtime time.Time) (int, syscall.Errno) {
	ids := make([]uint64, len(slices))
	for i, w := range slices {
		ids[i] = w.Slice.Id
	}
	m.record(ids, mtime)
	if m.inject != nil {
		var n int
		var st syscall.Errno
		var used bool
		m.once.Do(func() { n, st, used = m.inject(slices) })
		if used {
			return n, st
		}
	}
	return m.Meta.WriteSlices(ctx, inode, indx, slices, mtime)
}

// snapshot returns the recorded calls.
func (m *batchRecordingMeta) snapshot() [][]uint64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([][]uint64(nil), m.calls...)
}

// newBatchTestFile opens a file on a test VFS with batching of size n, slice
// timers that never fire, and a reuse window of 1 so new gaps make new slices.
func newBatchTestFile(t *testing.T, n int) (*rangeTestFile, *batchRecordingMeta) {
	t.Helper()
	f := newRangeTestFile(t, WriterFlushScopeFile)
	f.v.Conf.MetaWriteBatch = n
	f.v.Conf.WriterReuseWindow = 1
	rm := &batchRecordingMeta{Meta: f.v.Meta, hold: make(chan struct{}), held: make(chan struct{})}
	f.v.writer.(*dataWriter).m = rm
	return f, rm
}

// awaitAllDone waits until every pending slice of chunk indx finished its data upload.
func awaitAllDone(t *testing.T, fw *fileWriter, indx uint32) {
	t.Helper()
	deadline := time.Now().Add(rangeTestTimeout)
	for {
		fw.Lock()
		done := true
		for _, s := range fw.chunks[indx].slices {
			done = done && s.done
		}
		fw.Unlock()
		if done {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("slices did not finish")
		}
		time.Sleep(time.Millisecond)
	}
}
```

- [ ] **Step 2: 失敗するテストを書く（まとめと作成順）**

```go
// TestMetaWriteBatchCombinesSameChunk commits consecutive done slices of a chunk
// in one call, keeping creation order for overlapping writes.
func TestMetaWriteBatchCombinesSameChunk(t *testing.T) {
	f, rm := newBatchTestFile(t, 64)
	f.write(t, 3<<20, []byte("s0"))                          // S0
	f.write(t, 0, bytes.Repeat([]byte{'a'}, 4096))           // S1
	f.write(t, 1<<20, []byte("s2"))                          // S2: freezes S0 (window 1), whose commit is held
	<-rm.held
	f.write(t, 0, bytes.Repeat([]byte{'b'}, 4096))           // S3: overlaps the frozen S1 -> new slice
	fw := f.fileWriter(t)
	fsynced := make(chan syscall.Errno, 1)
	go func() { fsynced <- f.v.Fsync(f.ctx, f.ino, 0, f.fh) }()
	awaitAllDone(t, fw, 0)
	close(rm.hold)
	select {
	case eno := <-fsynced:
		require.Zero(t, eno)
	case <-time.After(rangeTestTimeout):
		t.Fatal("fsync did not finish")
	}
	calls := rm.snapshot()
	require.Len(t, calls, 2, "S0 alone, then S1..S3 together: %v", calls)
	require.Len(t, calls[1], 3)
	require.Equal(t, bytes.Repeat([]byte{'b'}, 4096), f.read(t, 0, 4096), "creation order inside the batch")
}

// TestMetaWriteBatchDisabled never calls WriteSlices when batching is off.
func TestMetaWriteBatchDisabled(t *testing.T) {
	f, rm := newBatchTestFile(t, 0)
	rm.hold = nil
	for i := 0; i < 5; i++ {
		f.write(t, uint64(i)<<20, []byte("x"))
	}
	f.fsync(t)
	for _, c := range rm.snapshot() {
		require.Len(t, c, 1)
	}
}
```

- [ ] **Step 3: 失敗を確認する**

Run: `go test ./pkg/vfs/ -run 'TestMetaWriteBatch' -count=1 -v`
Expected: `TestMetaWriteBatchCombinesSameChunk` が FAIL（calls が4件）。`TestMetaWriteBatchDisabled` は PASS（回帰ガード）。

- [ ] **Step 4: commitThread を実装する**

`pkg/vfs/writer.go` の `commitThread` のループ本体を、次のように変える。dep の待ちまでは従来のまま。

```go
	// the slices should be committed in the order that are created
	for len(c.slices) > 0 {
		s := c.slices[0]
		for !s.done {
			if s.notify.WaitWithTimeout(time.Millisecond*100) && !s.freezed && time.Since(s.started) > f.w.conf.SliceFlushWait*2 {
				s.freeze("commit_age")
			}
		}
		for s.dep != nil && !s.dep.committed {
			f.commitcond.WaitWithTimeout(time.Millisecond * 100)
		}
		batch := c.commitBatch()
		err := s.err
		f.Unlock()

		committed := 0
		var mtime time.Time
		var gen uint64
		ordered := err == 0
		if ordered {
			// Chunks commit concurrently; choosing the mtime and applying it in the
			// same order keeps an older slice from moving the file mtime back.
			f.commitMu.Lock()
			f.Lock()
			mtime, gen = s.lastMod, f.mtimeGen
			for _, b := range batch[1:] {
				if b.lastMod.After(mtime) {
					mtime = b.lastMod
				}
			}
			if mtime.Before(f.mtimeFloor) {
				mtime = f.mtimeFloor
			}
			f.Unlock()
			committed, err = c.commitSlices(batch, mtime)
		}

		f.Lock()
		if committed > 0 && gen == f.mtimeGen {
			f.raiseMtimeFloor(mtime)
		}
		if ordered {
			f.commitMu.Unlock()
		}
		for _, b := range batch[:committed] {
			c.markCommitted(b)
		}
		if err != 0 {
			// A failure raised before anything was written affects only that slice;
			// any other failure may have applied the whole batch, so none of the
			// remaining slices may be sent again.
			end := committed + 1
			if !metaErrorUnapplied(err) {
				end = len(batch)
			}
			if err == syscall.ENOENT || err == syscall.ENOSPC || err == syscall.EDQUOT {
				for _, failed := range batch[committed:end] {
					go func(id uint64, length int) {
						_ = f.w.store.Remove(id, length)
					}(failed.id, int(failed.length))
				}
			} else {
				logger.Warnf("write inode:%d error: %s", f.inode, err)
				err = syscall.EIO
			}
			f.err = err
			logger.Errorf("write inode:%d indx:%d %s", f.inode, c.indx, err)
			for _, failed := range batch[committed:end] {
				c.markCommitted(failed)
			}
			committed = end
		}
		c.slices = c.slices[committed:]
	}
	f.freeChunk(c)
	f.Unlock()
}

// commitBatch returns the head slice and, when batching is enabled, the
// following slices that are already done without error, up to the batch size.
// Only a chunk's first slice can have a dependency, so later ones need no wait.
// The caller holds the file lock.
func (c *chunkWriter) commitBatch() []*sliceWriter {
	head := c.slices[0]
	limit := c.file.w.conf.MetaWriteBatch
	if head.err != 0 || limit <= 1 {
		return c.slices[:1]
	}
	n := 1
	for n < len(c.slices) && n < limit && c.slices[n].done && c.slices[n].err == 0 {
		n++
	}
	return c.slices[:n:n]
}

// commitSlices writes the metadata of a batch without the file lock and
// invalidates the reader for the slices that were committed. It returns how
// many leading slices were committed and the errno of the next one, if any.
func (c *chunkWriter) commitSlices(batch []*sliceWriter, mtime time.Time) (int, syscall.Errno) {
	f := c.file
	start := time.Now()
	var n int
	var err syscall.Errno
	if len(batch) == 1 {
		s := batch[0]
		logger.Debugf("slice commit inode=%d chunk=%d slice=%d phase=metadata", f.inode, c.indx, s.id)
		if err = f.w.m.Write(meta.Background(), f.inode, c.indx, s.off, meta.Slice{Id: s.id, Size: s.length, Off: s.soff, Len: s.slen}, mtime); err == 0 {
			n = 1
		}
	} else {
		ws := make([]meta.SliceWrite, len(batch))
		for i, s := range batch {
			ws[i] = meta.SliceWrite{Off: s.off, Slice: meta.Slice{Id: s.id, Size: s.length, Off: s.soff, Len: s.slen}}
			logger.Debugf("slice commit inode=%d chunk=%d slice=%d phase=metadata batch=%d", f.inode, c.indx, s.id, len(batch))
		}
		n, err = f.w.m.WriteSlices(meta.Background(), f.inode, c.indx, ws, mtime)
	}
	if elapsed := time.Since(start); elapsed >= time.Second {
		logger.Warnf("slow slice commit inode=%d chunk=%d slice=%d metadata=%s errno=%s batch=%d", f.inode, c.indx, batch[0].id, elapsed, err, len(batch))
	}
	for _, s := range batch[:n] {
		f.w.reader.Invalidate(f.inode, uint64(c.indx)*meta.ChunkSize+uint64(s.off), uint64(s.slen))
	}
	return n, err
}

// metaErrorUnapplied reports metadata errors raised before a transaction wrote
// anything; other errors leave it unknown whether the batch was applied.
func metaErrorUnapplied(err syscall.Errno) bool {
	return err == syscall.ENOENT || err == syscall.EPERM || err == syscall.ENOSPC || err == syscall.EDQUOT
}

// markCommitted records that a slice's commit finished (successfully or not)
// and wakes the waiters that depend on it; the caller holds the file lock.
func (c *chunkWriter) markCommitted(s *sliceWriter) {
	f := c.file
	s.committed = true
	if s.growing {
		f.commitcond.Broadcast()
	}
	if f.rangewaiting > 0 {
		// Range barriers wait for specific slices rather than for every chunk to drain.
		f.flushcond.Broadcast()
	}
}
```

注:
- 従来の `slow slice commit` の WARN 文には `batch=` が加わる。`analyze_install.py` は WARN を文言の先頭80文字で集計するので、影響しない。
- `s.err != 0`（staging の失敗等）の場合、`ordered` は false で batch は1件。`committed = 0`、`err = s.err` で、従来と同じ失敗処理になる（batch が1件なので `end` は 1）。
- 適用されたか分からないエラー（`metaErrorUnapplied` が false）では、batch の残りもすべて失敗扱いにして再送しない。staging は削除しない（従来の EIO と同じ）。
- 従来、`f.w.reader.Invalidate` は commit の errno に関係なく、metadata を書いたときに呼んでいた。新しい実装では、成功した slice だけで呼ぶ。失敗した slice の metadata は書かれていないので、reader のバッファに影響はない。

- [ ] **Step 5: 通ることを確認する**

Run: `go test ./pkg/vfs/ -run 'TestMetaWriteBatch|TestMtime|TestRangeFlush|TestRangePreflush|TestFlush|TestWriter' -count=1`
Expected: PASS

- [ ] **Step 6: 途中の失敗・全体の失敗・mtime のテストを足す（Review Focus 2 を含む）**

```go
// heldBatch writes S0 (held), then three more slices that end up in one batch.
func heldBatch(t *testing.T, rm *batchRecordingMeta, f *rangeTestFile) {
	t.Helper()
	f.write(t, 3<<20, []byte("s0"))
	f.write(t, 0, []byte("a1"))
	f.write(t, 1<<20, []byte("b2"))
	<-rm.held
	f.write(t, 2<<20, []byte("c3"))
}

// TestMetaWriteBatchPartialFailure keeps the committed prefix, reports the
// failing slice's errno and still commits the slices after it.
func TestMetaWriteBatchPartialFailure(t *testing.T) {
	f, rm := newBatchTestFile(t, 64)
	rm.inject = func(slices []meta.SliceWrite) (int, syscall.Errno, bool) {
		require.Zero(t, rm.Meta.Write(meta.Background(), f.ino, 0, slices[0].Off, slices[0].Slice, time.Now()))
		return 1, syscall.EDQUOT, true
	}
	heldBatch(t, rm, f)
	fsynced := make(chan syscall.Errno, 1)
	go func() { fsynced <- f.v.Fsync(f.ctx, f.ino, 0, f.fh) }()
	awaitAllDone(t, f.fileWriter(t), 0)
	close(rm.hold)
	require.Equal(t, syscall.EDQUOT, <-fsynced)
	batch := rm.snapshot()[1]
	ids := committedIDs(t, f)
	require.True(t, ids[batch[0]], "the committed prefix stays")
	require.False(t, ids[batch[1]], "the failed slice must not be registered")
	require.True(t, ids[batch[2]], "slices after an unapplied failure are still committed")
}

// committedIDs returns the slice IDs registered in chunk 0 of the test file.
// Reads through the VFS would return the file's sticky error instead.
func committedIDs(t *testing.T, f *rangeTestFile) map[uint64]bool {
	t.Helper()
	var ss []meta.Slice
	require.Zero(t, f.v.Meta.Read(meta.Background(), f.ino, 0, &ss))
	ids := map[uint64]bool{}
	for _, s := range ss {
		ids[s.Id] = true
	}
	return ids
}

// TestMetaWriteBatchUnknownFailure sends no slice of a batch whose outcome is
// unknown again, and marks the file failed with EIO.
func TestMetaWriteBatchUnknownFailure(t *testing.T) {
	f, rm := newBatchTestFile(t, 64)
	rm.inject = func(slices []meta.SliceWrite) (int, syscall.Errno, bool) { return 0, syscall.EIO, true }
	heldBatch(t, rm, f)
	fsynced := make(chan syscall.Errno, 1)
	go func() { fsynced <- f.v.Fsync(f.ctx, f.ino, 0, f.fh) }()
	awaitAllDone(t, f.fileWriter(t), 0)
	close(rm.hold)
	require.Equal(t, syscall.EIO, <-fsynced)
	calls := rm.snapshot()
	inBatch := map[uint64]bool{}
	for _, id := range calls[1] {
		inBatch[id] = true
	}
	for _, c := range calls[2:] {
		for _, id := range c {
			require.False(t, inBatch[id], "slice %d of an unknown-outcome batch was resent: %v", id, calls)
		}
	}
}

// TestMetaWriteBatchStopsAtFailedSlice cuts a batch before a slice whose data upload failed.
func TestMetaWriteBatchStopsAtFailedSlice(t *testing.T) {
	f, rm := newBatchTestFile(t, 64)
	heldBatch(t, rm, f)
	fw := f.fileWriter(t)
	fsynced := make(chan syscall.Errno, 1)
	go func() { fsynced <- f.v.Fsync(f.ctx, f.ino, 0, f.fh) }()
	awaitAllDone(t, fw, 0)
	fw.Lock()
	fw.chunks[0].slices[2].err = syscall.EIO // the "b2" slice: as if its upload failed
	fw.Unlock()
	close(rm.hold)
	require.Equal(t, syscall.EIO, <-fsynced)
	for _, c := range rm.snapshot() {
		require.LessOrEqual(t, len(c), 1, "no batch may include or skip past the failed slice: %v", rm.snapshot())
	}
}

// TestMetaWriteBatchMtime passes the newest write time of the batch.
func TestMetaWriteBatchMtime(t *testing.T) {
	f, rm := newBatchTestFile(t, 64)
	f.write(t, 3<<20, []byte("s0"))
	f.write(t, 0, []byte("a1"))
	f.write(t, 1<<20, []byte("b2"))
	<-rm.held
	time.Sleep(20 * time.Millisecond)
	newest := time.Now()
	f.write(t, 2<<20, []byte("c3"))
	fsynced := make(chan syscall.Errno, 1)
	go func() { fsynced <- f.v.Fsync(f.ctx, f.ino, 0, f.fh) }()
	awaitAllDone(t, f.fileWriter(t), 0)
	close(rm.hold)
	require.Zero(t, <-fsynced)
	rm.mu.Lock()
	defer rm.mu.Unlock()
	require.False(t, rm.mtimes[len(rm.mtimes)-1].Before(newest))
}
```

`TestMetaWriteBatchStopsAtFailedSlice` では、batch は「S1 だけ」→「S2 は失敗として単独」→「S3 だけ」になる。どの呼び出しも1件以下であることで確認する。

ファイルがエラー状態になると VFS の Read は sticky な errno を返すので、途中の失敗のテストは `committedIDs`（meta の chunk の slice ID）で確認する。

- [ ] **Step 7: 通ることを確認する**

Run: `go test ./pkg/vfs/ -run 'TestMetaWriteBatch' -count=1 -v`
Expected: PASS

- [ ] **Step 8: 変異で確認する**

次のそれぞれで、対応するテストが落ちることを確認してから元に戻す。
- `commitBatch` で `limit <= 1` の判定を `true` にする（まとめない）→ `CombinesSameChunk` が落ちる。
- `commitBatch` で `c.slices[n].err == 0` の条件を外す → `StopsAtFailedSlice` が落ちる。
- `end` を常に `len(batch)` にする（未適用のエラーでも残りを失敗扱いにする）→ `PartialFailure` が落ちる（c3 が登録されない）。
- `end` を常に `committed + 1` にする（不明なエラーでも残りを再送する）→ `UnknownFailure` が落ちる。
- mtime の最大値の計算を外す（`mtime = s.lastMod` のみ）→ `Mtime` が落ちる。

- [ ] **Step 9: 既存の suite を batch 有効でも回す**

```bash
JFS_TEST_META_WRITE_BATCH=64 go test ./pkg/vfs/ -count=1
JFS_TEST_META_WRITE_BATCH=64 JFS_TEST_WRITER_FLUSH_SCOPE=range go test ./pkg/vfs/ -count=1
JFS_TEST_META_WRITE_BATCH=64 go test ./pkg/fuse/ -count=1
```
Expected: ok（Redis のテストのため一時 Redis が必要）

- [ ] **Step 10: commit（ユーザーの指示があった場合のみ）**

---

### Task 6: CLI のフラグとドキュメント

**Files:**
- Modify: `cmd/flags.go`（`writer-flush-scope` の次）
- Modify: `cmd/mount.go`（`getVfsConf`）
- Modify: `cmd/writer_flush_test.go`
- Modify: `docs/en/reference/_common_options.mdx`、`docs/zh_cn/reference/_common_options.mdx`

**Interfaces:**
- Consumes: `vfs.Config.MetaWriteBatch`（Task 4）

- [ ] **Step 1: 失敗するテストを書く**

`cmd/writer_flush_test.go` に追加する。

```go
// TestMetaWriteBatchOption carries --meta-write-batch into the VFS configuration, disabled by default.
func TestMetaWriteBatchOption(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want int
	}{
		{nil, 0}, {[]string{"--meta-write-batch", "64"}, 64}, {[]string{"--meta-write-batch", "1024"}, 1024},
	} {
		set := flag.NewFlagSet("batch-test", flag.ContinueOnError)
		for _, f := range clientFlags(1) {
			if err := f.Apply(set); err != nil {
				t.Fatal(err)
			}
		}
		if err := set.Parse(tc.args); err != nil {
			t.Fatal(err)
		}
		cfg := getVfsConf(cli.NewContext(nil, set, nil), meta.DefaultConf(), &meta.Format{}, &chunk.Config{})
		if cfg.MetaWriteBatch != tc.want {
			t.Fatalf("%v: batch=%d, want %d", tc.args, cfg.MetaWriteBatch, tc.want)
		}
	}
}

// TestMetaWriteBatchOptionRejectsInvalid rejects sizes outside 0..1024 before a command runs.
func TestMetaWriteBatchOptionRejectsInvalid(t *testing.T) {
	for _, input := range []string{"-1", "1025"} {
		called := false
		app := &cli.App{Flags: clientFlags(1), Action: func(*cli.Context) error { called = true; return nil }}
		if err := app.Run([]string{"test", "--meta-write-batch", input}); err == nil || called {
			t.Fatalf("%s: accepted=%v called=%v", input, err == nil, called)
		}
	}
}
```

- [ ] **Step 2: 失敗を確認する**

Run: `go test ./cmd/ -run 'TestMetaWriteBatchOption' -count=1`
Expected: FAIL（`flag provided but not defined: -meta-write-batch`）

- [ ] **Step 3: 実装する**

`cmd/flags.go`（`writer-flush-scope` の後）:

```go
		&cli.IntFlag{
			Name:  "meta-write-batch",
			Value: 0,
			Usage: "maximum slices of one chunk committed in one metadata transaction (0 disables, up to 1024); applies to every commit including fsync",
			Action: func(_ *cli.Context, value int) error {
				if value < 0 || value > 1024 {
					return fmt.Errorf("meta-write-batch must be between 0 and 1024")
				}
				return nil
			},
		},
```

`cmd/mount.go` の `getVfsConf`（`WriterFlushScope` の次）:

```go
		MetaWriteBatch:     c.Int("meta-write-batch"),
```

docs（en、`--writer-flush-scope` の行の次）:

```markdown
|`--meta-write-batch=0`|Local investigation branch: maximum number of consecutive pending slices of one chunk committed in a single metadata transaction. Default `0` commits one slice per transaction (prior behavior). Batching applies to every commit, including fsync and close, and keeps the creation order within a chunk. If a batch fails before writing anything (no such file, permission, no space or quota exceeded), its slices are retried one by one so each gets its exact error; a failure whose outcome is unknown (I/O or network error) is not retried and marks the file failed, as for a single slice. Implemented for Redis, SQL and TKV engines.|
```

docs（zh_cn、`--writer-flush-scope` の行の次）:

```markdown
|`--meta-write-batch=0`|本地调查分支：单个元数据事务中提交的同一 chunk 连续待提交 slice 的最大数量。默认 `0` 每个事务提交一个 slice（原有行为）。批量提交适用于所有提交，包括 fsync 和 close，并保持 chunk 内的创建顺序。若批次在写入任何内容前失败（文件不存在、权限、空间不足或超出配额），会逐个重试其中的 slice，使每个 slice 得到准确的错误；结果未知的失败（I/O 或网络错误）不会重试，并像单个 slice 一样将文件标记为失败。支持 Redis、SQL 和 TKV 引擎。|
```

注: SQL と TKV の実装は Task 7・8 で入る。commit 1 の時点では docs から「SQL and TKV」を外すか、Task 8 の後で足す。commit 1 の docs: `Implemented for Redis; other engines commit the slices one by one.`

- [ ] **Step 4: 通ることを確認する**

Run: `go test ./cmd/ -run 'TestMetaWriteBatchOption|TestWriterFlush' -count=1`
Expected: PASS

- [ ] **Step 5: commit（ユーザーの指示があった場合のみ）**

---

### Checkpoint A: commit 1 の検証と計測用バイナリ（Task 1〜6 の後）

- [ ] 一時 Redis を起動した状態で、次を順に実行する。結果は scratchpad に保存する。
  - 対象テストの race を3回: `go test -race -count=3 -run 'TestMetaWriteBatch|TestRangeFlush|TestRangePreflush|TestMtime|TestWriterFlushScope|TestFlush|TestWriter|TestVFSReadFlushError' ./pkg/vfs/`
  - `go test -race -count=3 -run 'TestWriteSlices|TestCompactionWanted' ./pkg/meta/`
  - pkg/meta: `TestMemKVClient`・`TestSQLiteClient`・`TestRedisClient` と、`make test.meta.core` 相当（`go test ./pkg/meta/...`）。18e641b8 の archive（baseline）でも同じものを回して比べる。
  - pkg/vfs・pkg/fuse を、batch（0／64）と scope（file／range）の4つの組み合わせで回す。pkg/fs。cmd の対象テスト。
  - build、gofmt、`git diff --check`。
- [ ] 既存の失敗（pkg/chunk の `stageFull`／`checkFreeSpace` の DATA RACE、`TestSmallPUTDiagnostics` の flaky）は baseline と区別して報告する。
- [ ] テストが作業ツリーに作る `pkg/vfs/?_journal=WAL&_timeout=5000&cache=shared` を削除する（既存テストの副産物）。
- [ ] ユーザーに結果を報告し、commit の指示を待つ。commit の後、その commit から計測用バイナリを `~/tmp_local/juicefs-builds/` にビルドする（`-s -w`、version の revision に `<sha8>-metabatch` を入れる）。
- [ ] 計測はユーザーが行う（qcow2、`--writer-flush-scope=range --meta-write-batch=64`）。agent は transaction 数、batch の大きさの分布（`metadata write batch` の行）、Fallocate と Read の待ち、所要時間を、前回の range 版と比べる。

---

### Task 7: SQL の `doWriteSlices`

**Files:**
- Modify: `pkg/meta/sql.go`（`dbMeta.doWrite` の直後）
- Test: `pkg/meta/write_slices_test.go`

**Interfaces:**
- Consumes: `SliceWrite`、`sliceBatchWriter`、`testWriteSlices`、`newWriteSlicesMeta`（Task 2）

- [ ] **Step 1: 失敗するテストを書く**

```go
// TestWriteSlicesSQLite runs the shared WriteSlices checks on SQLite and requires
// the single-transaction implementation.
func TestWriteSlicesSQLite(t *testing.T) {
	m, err := newSQLMeta("sqlite3", path.Join(t.TempDir(), "jfs-write-slices.db"), testConfig())
	if err != nil {
		t.Fatalf("create meta: %s", err)
	}
	if _, ok := m.getBase().en.(sliceBatchWriter); !ok {
		t.Fatal("sql engine does not implement doWriteSlices")
	}
	testWriteSlices(t, newWriteSlicesMeta(t, m))
}
```

- [ ] **Step 2: 失敗を確認する**

Run: `go test ./pkg/meta/ -run TestWriteSlicesSQLite -count=1`
Expected: FAIL（`sql engine does not implement doWriteSlices`）

- [ ] **Step 3: 実装する**

```go
// doWriteSlices appends slices to one chunk in a single transaction: one
// upsert of the concatenated slices, one multi-row insert of their references
// and one update of the inode.
func (m *dbMeta) doWriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time, numSlices *int, delta *dirStat, attr *Attr) syscall.Errno {
	return errno(m.txn(func(s *xorm.Session) error {
		*delta = dirStat{}
		nodeAttr := node{Inode: inode}
		ok, err := s.ForUpdate().Get(&nodeAttr)
		if err != nil {
			return err
		}
		if !ok {
			return syscall.ENOENT
		}
		if nodeAttr.Type != TypeFile {
			return syscall.EPERM
		}
		oldLength := nodeAttr.Length
		for _, w := range slices {
			if newleng := uint64(indx)*ChunkSize + uint64(w.Off) + uint64(w.Slice.Len); newleng > nodeAttr.Length {
				nodeAttr.Length = newleng
			}
		}
		delta.length = int64(nodeAttr.Length - oldLength)
		delta.space = align4K(nodeAttr.Length) - align4K(oldLength)
		if err := m.checkQuota(ctx, delta.space, 0, nodeAttr.Uid, nodeAttr.Gid, m.getParents(s, inode, nodeAttr.Parent)...); err != 0 {
			return err
		}
		now := time.Now().UnixNano()
		nodeAttr.setMtime(mtime.UnixNano())
		nodeAttr.setCtime(now)
		m.parseAttr(&nodeAttr, attr)

		buf := make([]byte, 0, len(slices)*sliceBytes)
		refs := make([]interface{}, len(slices))
		for i, w := range slices {
			buf = append(buf, marshalSlice(w.Off, w.Slice.Id, w.Slice.Size, w.Slice.Off, w.Slice.Len)...)
			refs[i] = sliceRef{w.Slice.Id, w.Slice.Size, 1}
		}
		var insert bool // no compaction check for a newly inserted chunk
		if err = m.upsertSlice(s, inode, indx, buf, &insert); err != nil {
			return err
		}
		if err = mustInsert(s, refs...); err != nil {
			return err
		}
		_, err = s.Cols("length", "mtime", "ctime", "mtimensec", "ctimensec").Update(&nodeAttr, &node{Inode: inode})
		if err == nil && !insert {
			ck := chunk{Inode: inode, Indx: indx}
			_, _ = s.MustCols("indx").Get(&ck)
			*numSlices = len(ck.Slices) / sliceBytes
		}
		if err == nil {
			for _, w := range slices {
				m.genLog(ctx, s, now, "WRITE(%d,%d,%d,%d,%d,%d,%d):%d", inode, indx, w.Off, w.Slice.Id, w.Slice.Len, attr.Mtime, attr.Mtimensec, *numSlices)
			}
		}
		return err
	}, inode))
}
```

注: MySQL で chunk の行が新規 insert になった場合、`numSlices` は従来どおり 0 のまま（compaction 判定をしない）。baseMeta は `compactionWanted(0-k, 0)` を評価し、false になる。

- [ ] **Step 4: 通ることを確認する**

Run: `go test ./pkg/meta/ -run 'TestWriteSlicesSQLite|TestSQLiteClient' -count=1`
Expected: PASS

- [ ] **Step 5: PostgreSQL・MySQL を source review する**

`upsertSlice` の postgres／mysql の分岐（`sql.go:1570-1593`）と `mustInsert` の複数行 insert が、連結した buf と複数の sliceRef で従来と同じ結果になることを、コードで確認して memo に記録する（実行はしない）。

- [ ] **Step 6: commit（ユーザーの指示があった場合のみ）**

---

### Task 8: TKV の `doWriteSlices`

**Files:**
- Modify: `pkg/meta/tkv.go`（`kvMeta.doWrite` の直後）
- Test: `pkg/meta/write_slices_test.go`

- [ ] **Step 1: 失敗するテストを書く**

`TestWriteSlicesMemKV` を、実装の有無も確かめる形に変える。

```go
// TestWriteSlicesMemKV runs the shared WriteSlices checks on MemKV and requires
// the single-transaction implementation.
func TestWriteSlicesMemKV(t *testing.T) {
	m := newWriteSlicesMemKV(t)
	if _, ok := m.getBase().en.(sliceBatchWriter); !ok {
		t.Fatal("tkv engine does not implement doWriteSlices")
	}
	testWriteSlices(t, m)
}

// TestWriteSlicesTKVDuplicate skips a slice already in the chunk, as Write does,
// and appends the rest of the batch.
func TestWriteSlicesTKVDuplicate(t *testing.T) {
	m := newWriteSlicesMemKV(t)
	_, f := createIn(t, m, "dup")
	ctx := Background()
	ws := sliceShapes(t, m, 3, 4096)
	if st := m.Write(ctx, f, 0, ws[1].Off, ws[1].Slice, time.Now()); st != 0 {
		t.Fatal(st)
	}
	if n, st := m.WriteSlices(ctx, f, 0, ws, time.Now()); n != 3 || st != 0 {
		t.Fatalf("WriteSlices = %d, %s", n, st)
	}
	var ss []Slice
	if st := m.Read(ctx, f, 0, &ss); st != 0 {
		t.Fatal(st)
	}
	ids := map[uint64]int{}
	for _, s := range ss {
		if s.Id != 0 {
			ids[s.Id]++
		}
	}
	if len(ids) != 3 {
		t.Fatalf("visible slices %+v", ss)
	}
}
```

- [ ] **Step 2: 失敗を確認する**

Run: `go test ./pkg/meta/ -run 'TestWriteSlicesMemKV|TestWriteSlicesTKVDuplicate' -count=1`
Expected: `TestWriteSlicesMemKV` が FAIL（`tkv engine does not implement doWriteSlices`）

- [ ] **Step 3: 実装する**

```go
// doWriteSlices appends slices to one chunk in a single transaction, skipping
// slices the chunk already holds (as doWrite does), and sets the chunk and the
// inode once.
func (m *kvMeta) doWriteSlices(ctx Context, inode Ino, indx uint32, slices []SliceWrite, mtime time.Time, numSlices *int, delta *dirStat, attr *Attr) syscall.Errno {
	return errno(m.txn(ctx, func(tx *kvTxn) error {
		*delta = dirStat{}
		*attr = Attr{}
		rs := tx.gets(m.inodeKey(inode), m.chunkKey(inode, indx))
		if rs[0] == nil {
			return syscall.ENOENT
		}
		m.parseAttr(rs[0], attr)
		if attr.Typ != TypeFile {
			return syscall.EPERM
		}
		if len(rs[1])%sliceBytes != 0 {
			logger.Errorf("Invalid chunk value for inode %d indx %d: %d", inode, indx, len(rs[1]))
			return syscall.EIO
		}
		val := rs[1]
		oldLength := attr.Length
		var added []SliceWrite
		for _, w := range slices {
			buf := marshalSlice(w.Off, w.Slice.Id, w.Slice.Size, w.Slice.Off, w.Slice.Len)
			dup := false
			for i := 0; i < len(val); i += sliceBytes {
				if bytes.Equal(val[i:i+sliceBytes], buf) {
					dup = true
					break
				}
			}
			if dup {
				logger.Warnf("Write same slice for inode %d indx %d sliceId %d", inode, indx, w.Slice.Id)
				continue
			}
			if newleng := uint64(indx)*ChunkSize + uint64(w.Off) + uint64(w.Slice.Len); newleng > attr.Length {
				attr.Length = newleng
			}
			val = append(val, buf...)
			added = append(added, w)
		}
		if len(added) == 0 {
			*numSlices = len(val) / sliceBytes
			return nil
		}
		delta.length = int64(attr.Length - oldLength)
		delta.space = align4K(attr.Length) - align4K(oldLength)
		if err := m.checkQuota(ctx, delta.space, 0, attr.Uid, attr.Gid, m.getParents(tx, inode, attr.Parent)...); err != 0 {
			return err
		}
		now := time.Now()
		attr.Mtime = mtime.Unix()
		attr.Mtimensec = uint32(mtime.Nanosecond())
		attr.Ctime = now.Unix()
		attr.Ctimensec = uint32(now.Nanosecond())
		tx.set(m.inodeKey(inode), m.marshal(attr))
		tx.set(m.chunkKey(inode, indx), val)
		*numSlices = len(val) / sliceBytes
		for _, w := range added {
			m.genLog(tx, now, "WRITE(%d,%d,%d,%d,%d,%d,%d):%d", inode, indx, w.Off, w.Slice.Id, w.Slice.Len, attr.Mtime, attr.Mtimensec, *numSlices)
		}
		return nil
	}, inode))
}
```

注:
- `val := rs[1]` に append すると、`rs[1]` の裏の配列を書き換える可能性がある。`tx.gets` の戻り値を後で使わないことを確認する。不安なら `val := append([]byte(nil), rs[1]...)` にする。
- 重複が1件もなく、すべてが重複だった場合、従来の `doWrite` は inode を更新せずに nil を返す。上の実装もそれに合わせて、何も set しない。
- `len(added) == 0` の場合の `afterWrite` の compaction 判定は `compactionWanted(n-k, n)` になり、1件ずつの場合（毎回 `compactionWanted(n-1, n)`）とわずかに異なる可能性がある。重複は再送時だけの稀なケースなので許容し、memo に記録する。

- [ ] **Step 4: 通ることを確認する**

Run: `go test ./pkg/meta/ -run 'TestWriteSlices|TestMemKVClient' -count=1`
Expected: PASS

- [ ] **Step 5: docs の engine の記述を更新する**

Task 6 の docs で「Redis only」にしていた場合、`Implemented for Redis, SQL and TKV engines.`（zh_cn は「支持 Redis、SQL 和 TKV 引擎。」）に戻す。

- [ ] **Step 6: commit（ユーザーの指示があった場合のみ）**

---

### Checkpoint B: commit 2 の検証

- [ ] Checkpoint A と同じ一式を回す（pkg/meta は memkv・SQLite・Redis すべて）。
- [ ] `go test -race -count=3 -run 'TestWriteSlices' ./pkg/meta/`
- [ ] 結果・既存の失敗との区別・PostgreSQL／MySQL の source review の結果を、agent_memo.md と TODO.md に記録し、ユーザーに報告する。commit はユーザーの指示を待つ。
