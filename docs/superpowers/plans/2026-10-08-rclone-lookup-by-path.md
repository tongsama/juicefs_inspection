# rclone serve s3 のパス指定 lookup（Phase 1）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 複数のホストが同じ Drive フォルダを共有する `rclone serve s3` で、他のホストが作った object を「無い」と返さないようにする。あわせて、key を直接指定した操作で全件一覧をしないようにし、エラーの 404 化・DELETE の取りこぼし・`--no-cleanup` の未配線・`b.meta` が増え続ける問題を直す。

**Architecture:** VFS に opt-in の lookup モード（`--kaz-vfs-lookup-by-path`）を追加する。`Dir.stat` はキャッシュに無い名前を `Fs.NewObject` で1件だけ問い合わせ、見つかったものだけをキャッシュする。「無い」はキャッシュしない。serve s3 では、エラーの対応づけ、DELETE、`--no-cleanup` を直す。これらは upstream の名前のまま、条件を付けずに直す。

**Tech Stack:** Go 1.26.0（rclone v1.75.1 の go.mod。`GOTOOLCHAIN=auto` で自動取得される）、rclone の vfs・cmd/serve/s3、gofakes3 v0.0.9、testify。

**Spec:** [docs/superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md](../specs/2026-10-08-rclone-lookup-by-path-design.md)

## Global Constraints

- 作業ディレクトリは本体の `rclone/`（調査リポジトリのルートから見た位置）。以下のパスと Go コマンドはすべて `rclone/` を基準にする。
- 作業ブランチ: `1.75.1-improve-kaz`（v1.75.1 = 687d264b6 から作成済み）から `feat/kaz-vfs-lookup` を切って作業する。merge は全タスクの完了とレビューの後に、ユーザーの承認を得て行う。
- **commit はユーザーが許可した範囲でだけ行う。push はしない**（fork `tongsama/rclone` は未作成。作成と push は別途確認を取る）。commit メッセージに Co-Authored-By などの attribution 行を付けない。
- 独自オプションは `--kaz-<対象>-<内容>` とし、ヘルプの先頭に `[kaz] `、Groups に `Kaz` を入れる。既存オプションの不具合修正（エラーの対応づけ、`--no-cleanup`、`b.meta`）は upstream の名前のまま、オプションの有無に関係なく直す。
- `--kaz-vfs-lookup-by-path` の既定は false とする。false のときの VFS の動作は今と同じでなければならない。
- 新規・変更する関数には、目的が分かるコメントを付ける。言語は rclone の慣習に合わせて英語の Go doc コメントにする。
- 実保存の失敗を隠さない。一時的な失敗を 404（「無い」）にしない。
- 本番の rclone、Drive の `/rclone-s3`、JuiceFS の metadata には触らない。実際の Drive を使う検証（Task 8）は、テスト用フォルダの作成・削除のたびにユーザーの確認を取る。

## Review Focus

仕様が明示していないが、使う人が踏みやすい入力・状態。各行のテストは、それを担当するタスクに入れてある。

1. 他のホストが消したが、自ホストのキャッシュには残っている object を GET する → 古いデータを返さず、404 でもなくエラー（500）になること（Task 6 `TestKazGetObjectDeletedElsewhere`）。
2. lookup モードで、親フォルダがまだ無い key へ PUT する（`mkdirRecursive` の各階層が ENOENT になる）→ フォルダが作られ、PUT が成功すること（Task 6 `TestKazPutCreatesParents`）。
3. 自ホストで PUT した直後に、同じ key を HEAD・GET する → 見えて、サイズが正しいこと（Task 6 `TestKazPutCreatesParents`）。
4. lookup の後に List（ReadDirAll）する → 外部で作った object も含めて全件を返し、重複しないこと（Task 2 `TestKazLookupListingStillWorks`）。
5. lookup モードで、存在しない bucket を HEAD する → 今までどおり NoSuchBucket（404）になること（Task 5 `TestKazStatErrorsAreNot404`）。

## ファイル構成

| ファイル | 種別 | 責務 |
|---|---|---|
| `vfs/vfscommon/options.go` | 変更 | オプション `kaz_vfs_lookup_by_path` と `Options.KazLookupByPath` |
| `vfs/vfscommon/options_kaz_test.go` | 新規 | オプション定義のテスト |
| `fs/config/flags/flags.go` | 変更 | フラグ分類 `Kaz` の登録 |
| `fs/config/flags/flags_kaz_test.go` | 新規 | `Kaz` 分類のテスト |
| `vfs/dir_kaz_lookup.go` | 新規 | `Dir.lookup`、`Dir._armLookupCleanup` |
| `vfs/dir.go` | 変更 | `Dir.lookupArmed` フィールド、`stat` の分岐、`cacheCleanup`・`ForgetAll` の解除 |
| `vfs/file.go` | 変更 | `File.Remove` の「すでに消えている」判定、`File.kazObjectGone` |
| `vfs/dir_kaz_lookup_test.go` | 新規 | lookup モードのテストと `countingFs` |
| `cmd/serve/s3/kaz_errors.go` | 新規 | `bucketStatError`・`keyStatError` |
| `cmd/serve/s3/backend.go` | 変更 | Head・Get・Delete・BucketExists のエラー、DELETE の `b.meta` と `--no-cleanup` |
| `cmd/serve/s3/kaz_test.go` | 新規 | serve s3 のテストと `failingFs`・`newKazBackend` |

---

### Task 0: 作業ブランチの作成

**Files:** なし（Git の操作だけ）

- [ ] **Step 1: ブランチを作り、状態を確かめる**

```bash
cd rclone
git switch 1.75.1-improve-kaz
git status --short
git switch -c feat/kaz-vfs-lookup
go version
```

Expected: `git status` が空。`go version` が `go1.26.0`（`GOTOOLCHAIN=auto` による）。

- [ ] **Step 2: 変更前の基準として、対象パッケージのテストを通す**

Run: `go test ./vfs/ ./vfs/vfscommon/ ./cmd/serve/s3/ ./fs/config/flags/ 2>&1 | tail -20`
Expected: すべて `ok`。失敗するものがあれば、変更前からの失敗として記録し、ユーザーに報告してから進む。

---

### Task 1: オプション `--kaz-vfs-lookup-by-path` と分類 `Kaz`

**Files:**
- Modify: `vfs/vfscommon/options.go`（`OptionsInfo` の末尾 `vfs_metadata_extension` の後、`Options` 構造体の末尾）
- Modify: `fs/config/flags/flags.go:111-127`（`init` の `All.NewGroup` 群）
- Test: `vfs/vfscommon/options_kaz_test.go`、`fs/config/flags/flags_kaz_test.go`

**Interfaces:**
- Produces: `vfscommon.Options.KazLookupByPath bool`（config 名 `kaz_vfs_lookup_by_path`、フラグ `--kaz-vfs-lookup-by-path`、環境変数 `RCLONE_KAZ_VFS_LOOKUP_BY_PATH`）。分類 `flags.All.ByName["Kaz"]`。

- [ ] **Step 1: 失敗するテストを書く**

`vfs/vfscommon/options_kaz_test.go`:

```go
package vfscommon

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestKazLookupByPathOption checks the kaz lookup option is registered as an
// opt-in VFS flag that is clearly marked as a kaz fork extension.
func TestKazLookupByPathOption(t *testing.T) {
	var found bool
	for _, o := range OptionsInfo {
		if o.Name != "kaz_vfs_lookup_by_path" {
			continue
		}
		found = true
		assert.Equal(t, false, o.Default)
		assert.True(t, strings.HasPrefix(o.Help, "[kaz] "), "help must start with [kaz]")
		assert.Contains(t, strings.Split(o.Groups, ","), "Kaz")
		assert.Contains(t, strings.Split(o.Groups, ","), "VFS")
	}
	require.True(t, found, "kaz_vfs_lookup_by_path must be in OptionsInfo")
	assert.False(t, Opt.KazLookupByPath, "default must be off")
}
```

`fs/config/flags/flags_kaz_test.go`:

```go
package flags

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// TestKazGroupRegistered checks the Kaz flag group exists so kaz fork flags
// can be listed together.
func TestKazGroupRegistered(t *testing.T) {
	g, ok := All.ByName["Kaz"]
	assert.True(t, ok)
	if ok {
		assert.Contains(t, g.Help, "kaz")
	}
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./vfs/vfscommon/ -run TestKazLookupByPathOption -v; go test ./fs/config/flags/ -run TestKazGroupRegistered -v`
Expected: 1つ目はコンパイルエラー（`Opt.KazLookupByPath undefined`）、2つ目は FAIL（`ok` が false）。

- [ ] **Step 3: 実装する**

`vfs/vfscommon/options.go` の `OptionsInfo` の末尾（`vfs_metadata_extension` の要素の後、`}}` の前）を次のようにする:

```go
}, {
	Name:    "vfs_metadata_extension",
	Default: "",
	Help:    "Set the extension to read metadata from.",
	Groups:  "VFS",
}, {
	Name:    "kaz_vfs_lookup_by_path",
	Default: false,
	Help:    "[kaz] Look up names missing from the directory cache with a single object lookup instead of listing the directory, and never cache misses. For remotes whose NewObject returns ErrorIsDir for directories (e.g. drive, local)",
	Groups:  "VFS,Kaz",
}}
```

`Options` 構造体の末尾に追加する:

```go
	MetadataExtension  string        `config:"vfs_metadata_extension"` // if set respond to files with this extension with metadata
	KazLookupByPath    bool          `config:"kaz_vfs_lookup_by_path"` // kaz: resolve uncached names with NewObject instead of listing, never cache misses
}
```

`fs/config/flags/flags.go` の `init` の最後（`All.NewGroup("Metrics", ...)` の次）に追加する:

```go
	All.NewGroup("Metrics", "Flags to control the Metrics HTTP endpoint.")
	All.NewGroup("Kaz", "Flags added by the kaz fork (not in upstream rclone)")
```

- [ ] **Step 4: テストが通ることと、フラグが出ることを確かめる**

Run: `go test ./vfs/vfscommon/ -run TestKazLookupByPathOption -v; go test ./fs/config/flags/ -run TestKazGroupRegistered -v; go run . serve s3 --help 2>&1 | grep -n kaz`
Expected: 2つとも PASS。help に `--kaz-vfs-lookup-by-path` と `[kaz]` が出る。

- [ ] **Step 5: commit（ユーザーが許可している場合のみ）**

```bash
git add vfs/vfscommon/options.go vfs/vfscommon/options_kaz_test.go fs/config/flags/flags.go fs/config/flags/flags_kaz_test.go
git commit -m "vfs: add --kaz-vfs-lookup-by-path option and Kaz flag group"
```

---

### Task 2: `Dir.stat` の lookup モード

**Files:**
- Create: `vfs/dir_kaz_lookup.go`
- Modify: `vfs/dir.go:26-45`（`Dir` 構造体）、`vfs/dir.go:861-909`（`stat`）
- Test: `vfs/dir_kaz_lookup_test.go`

**Interfaces:**
- Consumes: `vfscommon.Options.KazLookupByPath`（Task 1）
- Produces: `func (d *Dir) lookup(leaf string) (Node, error)`、`func (d *Dir) _armLookupCleanup()`（Task 3 で中身を入れる。このタスクでは空の関数として置く）、`Dir.lookupArmed bool`。テスト用の `countingFs`、`newLookupVFS`（Task 3・4 でも使う）。

- [ ] **Step 1: 失敗するテストを書く**

`vfs/dir_kaz_lookup_test.go`:

```go
package vfs

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fstest"
	"github.com/rclone/rclone/vfs/vfscommon"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// countingFs wraps an fs.Fs to count directory listings and, when
// newObjectErr is set, to make NewObject fail like a remote error would.
type countingFs struct {
	fs.Fs
	lists        atomic.Int64
	newObjectErr error // set before the VFS is used; read only afterwards
}

// List counts the listing and delegates to the wrapped Fs.
func (c *countingFs) List(ctx context.Context, dir string) (fs.DirEntries, error) {
	c.lists.Add(1)
	return c.Fs.List(ctx, dir)
}

// NewObject returns newObjectErr if set, otherwise delegates.
func (c *countingFs) NewObject(ctx context.Context, remote string) (fs.Object, error) {
	if c.newObjectErr != nil {
		return nil, c.newObjectErr
	}
	return c.Fs.NewObject(ctx, remote)
}

// newLookupVFS makes a VFS over a counting wrapper of the test remote with
// the kaz lookup mode set to lookup and the given dir cache time. Writes made
// with r.WriteObject act as another client sharing the remote.
func newLookupVFS(t *testing.T, lookup bool, dirCacheTime time.Duration) (*fstest.Run, *countingFs, *VFS) {
	r := fstest.NewRun(t)
	cf := &countingFs{Fs: r.Fremote}
	opt := vfscommon.Opt
	opt.KazLookupByPath = lookup
	opt.DirCacheTime = fs.Duration(dirCacheTime)
	opt.PollInterval = 0
	v := New(context.Background(), cf, &opt)
	t.Cleanup(func() { cleanupVFS(t, v) })
	return r, cf, v
}

var errKazBoom = errors.New("kaz boom")

// TestKazLookupSeesExternalCreate checks an object created by another client
// after the directory was cached is found, without listing any directory.
func TestKazLookupSeesExternalCreate(t *testing.T) {
	r, cf, v := newLookupVFS(t, true, time.Hour)
	ctx := context.Background()
	r.WriteObject(ctx, "dir/a", "aaa", t1)
	_, err := v.Stat("dir/a")
	require.NoError(t, err)

	r.WriteObject(ctx, "dir/b", "bbbb", t1)
	node, err := v.Stat("dir/b")
	require.NoError(t, err)
	assert.True(t, node.IsFile())
	assert.Equal(t, int64(4), node.Size())
	assert.Equal(t, int64(0), cf.lists.Load(), "lookup mode must not list directories")
}

// TestKazLookupDoesNotCacheMiss checks a miss is not remembered, so an object
// created after the miss becomes visible on the next Stat.
func TestKazLookupDoesNotCacheMiss(t *testing.T) {
	r, _, v := newLookupVFS(t, true, time.Hour)
	ctx := context.Background()
	r.WriteObject(ctx, "dir/a", "aaa", t1)
	_, err := v.Stat("dir/c")
	assert.True(t, errors.Is(err, ENOENT))

	r.WriteObject(ctx, "dir/c", "c", t1)
	_, err = v.Stat("dir/c")
	assert.NoError(t, err)
}

// TestKazLookupDirectory checks intermediate directories are resolved through
// ErrorIsDir and cached as directories.
func TestKazLookupDirectory(t *testing.T) {
	r, cf, v := newLookupVFS(t, true, time.Hour)
	r.WriteObject(context.Background(), "a/b/c/file", "x", t1)
	node, err := v.Stat("a/b")
	require.NoError(t, err)
	assert.True(t, node.IsDir())
	node, err = v.Stat("a/b/c/file")
	require.NoError(t, err)
	assert.True(t, node.IsFile())
	assert.Equal(t, int64(0), cf.lists.Load())
}

// TestKazLookupError checks a remote error is returned as is instead of
// being turned into "not found".
func TestKazLookupError(t *testing.T) {
	r, cf, v := newLookupVFS(t, true, time.Hour)
	r.WriteObject(context.Background(), "dir/a", "aaa", t1)
	cf.newObjectErr = errKazBoom
	_, err := v.Stat("dir/a")
	assert.True(t, errors.Is(err, errKazBoom), "got %v", err)
	assert.False(t, errors.Is(err, ENOENT))
}

// TestKazLookupOffUnchanged checks the upstream behaviour is kept when the
// option is off: within the dir cache time an external create is not seen.
func TestKazLookupOffUnchanged(t *testing.T) {
	r, cf, v := newLookupVFS(t, false, time.Hour)
	ctx := context.Background()
	r.WriteObject(ctx, "dir/a", "aaa", t1)
	_, err := v.Stat("dir/a")
	require.NoError(t, err)
	r.WriteObject(ctx, "dir/b", "b", t1)
	_, err = v.Stat("dir/b")
	assert.True(t, errors.Is(err, ENOENT))
	assert.Greater(t, cf.lists.Load(), int64(0))
}

// TestKazLookupListingStillWorks checks a directory listing after lookups
// returns every object once, including ones created by another client.
func TestKazLookupListingStillWorks(t *testing.T) {
	r, cf, v := newLookupVFS(t, true, time.Hour)
	ctx := context.Background()
	r.WriteObject(ctx, "dir/a", "aaa", t1)
	_, err := v.Stat("dir/a")
	require.NoError(t, err)
	r.WriteObject(ctx, "dir/b", "bb", t1)

	node, err := v.Stat("dir")
	require.NoError(t, err)
	nodes, err := node.(*Dir).ReadDirAll()
	require.NoError(t, err)
	var names []string
	for _, n := range nodes {
		names = append(names, n.Name())
	}
	assert.Equal(t, []string{"a", "b"}, names)
	assert.Greater(t, cf.lists.Load(), int64(0), "ReadDirAll must still list")
}

// TestKazLookupConcurrent checks concurrent lookups of one name end up with a
// single cached node.
func TestKazLookupConcurrent(t *testing.T) {
	r, _, v := newLookupVFS(t, true, time.Hour)
	r.WriteObject(context.Background(), "dir/a", "aaa", t1)
	const n = 20
	nodes := make([]Node, n)
	var wg sync.WaitGroup
	for i := range n {
		wg.Add(1)
		go func() {
			defer wg.Done()
			node, err := v.Stat("dir/a")
			assert.NoError(t, err)
			nodes[i] = node
		}()
	}
	wg.Wait()
	again, err := v.Stat("dir/a")
	require.NoError(t, err)
	for i := range n {
		assert.Same(t, again, nodes[i])
	}
}
```

注意: `t1` は既存の `vfs/vfs_test.go` で定義済みの時刻。`assert.Same` は同じポインタであることを確かめる。

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./vfs/ -run 'TestKazLookup' -v 2>&1 | tail -30`
Expected: `TestKazLookupOffUnchanged` は PASS。`SeesExternalCreate`・`DoesNotCacheMiss`・`Error`・`Directory` は FAIL（ENOENT や、lists > 0）。`Concurrent` と `ListingStillWorks` は PASS してもよい（既存の動作でも成り立つため）。

- [ ] **Step 3: 実装する**

`vfs/dir.go` の `Dir` 構造体の `virtual` の次に、フィールドを追加する:

```go
	items   map[string]Node   // directory entries - can be empty but not nil
	virtual map[string]vState // virtual directory entries - may be nil

	// lookupArmed is true while cleanupTimer is scheduled for entries added by
	// a kaz lookup (--kaz-vfs-lookup-by-path). Protected by mu.
	lookupArmed bool
```

`vfs/dir.go` の `stat` を次のように変える（`_readDir` を lookup モードでは呼ばない。最後の `ENOENT` の前で lookup する）:

```go
func (d *Dir) stat(leaf string) (Node, error) {
	d.mu.Lock()
	// In kaz lookup mode the directory is never listed to answer a Stat: a
	// cached entry is used as is and a missing one is looked up directly.
	if !d.vfs.Opt.KazLookupByPath {
		err := d._readDir()
		if err != nil {
			d.mu.Unlock()
			return nil, err
		}
	}
	item, ok := d.items[leaf]
	d.mu.Unlock()
```

（この後の metadata ファイルの処理と、正規化による一致の処理は変えない。）関数の最後を次のようにする:

```go
	if !ok {
		if d.vfs.Opt.KazLookupByPath {
			return d.lookup(leaf)
		}
		return nil, ENOENT
	}
	return item, nil
}
```

`vfs/dir_kaz_lookup.go` を新規に作る:

```go
package vfs

import (
	"errors"
	"path"
	"time"

	"github.com/rclone/rclone/fs"
)

// lookup resolves leaf with a single Fs.NewObject call instead of listing
// the directory. It is used when --kaz-vfs-lookup-by-path is set.
//
// A found object or directory is cached as a normal (non virtual) entry so
// a later listing or ForgetAll can replace or drop it. A miss is not cached,
// so objects created by other clients sharing the remote are seen at once.
// Any error other than "not found" is returned as is so callers do not
// mistake a remote failure for a missing object.
//
// It must be called without d.mu held; the remote call runs unlocked so
// lookups of other names in the same directory are not serialised.
func (d *Dir) lookup(leaf string) (Node, error) {
	d.mu.RLock()
	dirPath := d.path
	d.mu.RUnlock()
	remote := path.Join(dirPath, leaf)

	o, err := d.f.NewObject(d.vfs.ctx, remote)
	var entry fs.DirEntry
	switch {
	case err == nil:
		entry = o
	case errors.Is(err, fs.ErrorIsDir):
		// The remote does not give a directory's modtime here; use now as
		// VFS.New does for the root directory.
		entry = fs.NewDir(remote, time.Now())
	case errors.Is(err, fs.ErrorObjectNotFound), errors.Is(err, fs.ErrorDirNotFound):
		return nil, ENOENT
	default:
		return nil, err
	}

	d.mu.Lock()
	defer d.mu.Unlock()
	if node, ok := d.items[leaf]; ok {
		// Another lookup, a listing or a create got there first.
		return node, nil
	}
	var node Node
	if obj, ok := entry.(fs.Object); ok {
		node = newFile(d, d.path, obj, leaf)
	} else {
		node = newDir(d.vfs, d.f, d, entry.(fs.Directory))
	}
	d.items[leaf] = node
	d._armLookupCleanup()
	return node, nil
}

// _armLookupCleanup schedules the cache cleanup for entries added by lookup.
// It is filled in by the cache lifetime task. Must be called with d.mu held.
func (d *Dir) _armLookupCleanup() {
}
```

- [ ] **Step 4: テストが通ることを確かめる**

Run: `go test ./vfs/ -run 'TestKazLookup' -race -v 2>&1 | tail -30`
Expected: すべて PASS、race の報告なし。

- [ ] **Step 5: 既存の vfs テストが壊れていないことを確かめる**

Run: `go test ./vfs/ 2>&1 | tail -5`
Expected: `ok`。

- [ ] **Step 6: commit（ユーザーが許可している場合のみ）**

```bash
git add vfs/dir.go vfs/dir_kaz_lookup.go vfs/dir_kaz_lookup_test.go
git commit -m "vfs: resolve uncached names by NewObject in kaz lookup mode"
```

---

### Task 3: lookup で入れたエントリの寿命

背景: 既存の `cleanupTimer` は `_readDir` のときにしか再設定されない（`vfs/dir.go:574`）。lookup モードでは一覧をしないので、作成時の1回で止まる。単純に毎回再設定すると、使われ続けるディレクトリでは掃除が永久に先送りされる。また、`ForgetAll` で親から外れた古い `Dir` のタイマーが動き続けないようにする必要がある。そこで「掃除の後、最初に lookup で追加したときに1回だけタイマーを仕掛ける」方式にする。

**Files:**
- Modify: `vfs/dir_kaz_lookup.go`（`_armLookupCleanup` の中身）
- Modify: `vfs/dir.go:77-92`（`cacheCleanup`）、`vfs/dir.go:214-245`（`ForgetAll`）
- Test: `vfs/dir_kaz_lookup_test.go`（追記）

**Interfaces:**
- Consumes: `Dir.lookupArmed`、`Dir._armLookupCleanup()`、`newLookupVFS`（Task 2）

- [ ] **Step 1: 失敗するテストを書く**（`vfs/dir_kaz_lookup_test.go` の末尾に追記）

```go
// dirItemCount returns how many entries dir currently caches.
func dirItemCount(d *Dir) int {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return len(d.items)
}

// TestKazLookupEntriesExpire checks entries added by lookup are dropped after
// about twice the dir cache time, and that this repeats on the same Dir after
// a cleanup, so a deletion by another client is eventually seen.
//
// It uses the root directory because the root Dir is never replaced: its
// first expiry comes from the timer set when the Dir is created, so only the
// second round shows the timer is armed again by a lookup.
func TestKazLookupEntriesExpire(t *testing.T) {
	r, _, v := newLookupVFS(t, true, 200*time.Millisecond)
	ctx := context.Background()
	obj := r.WriteObject(ctx, "a", "aaa", t1)
	root, err := v.Root()
	require.NoError(t, err)

	for round := range 2 {
		_, err = v.Stat("a")
		require.NoError(t, err, "round %d", round)
		require.Equal(t, 1, dirItemCount(root), "round %d", round)
		require.Eventually(t, func() bool { return dirItemCount(root) == 0 },
			3*time.Second, 20*time.Millisecond, "round %d: lookup entry must expire", round)
	}

	// Removed by another client: once expired the lookup reports not found.
	o, err := r.Fremote.NewObject(ctx, obj.Path)
	require.NoError(t, err)
	require.NoError(t, o.Remove(ctx))
	_, err = v.Stat("a")
	assert.True(t, errors.Is(err, ENOENT))
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./vfs/ -run TestKazLookupEntriesExpire -v 2>&1 | tail -20`
Expected: 2周目の `Eventually` で FAIL（1周目の掃除で root のタイマーが止まり、再設定されないのでエントリが残る）。

- [ ] **Step 3: 実装する**

`vfs/dir_kaz_lookup.go` の `_armLookupCleanup` を次のようにする:

```go
// _armLookupCleanup schedules the directory cache cleanup after the first
// lookup hit since the last cleanup, so entries added by lookup expire after
// at most DirCacheTime*2. Arming only once per cleanup keeps a busy
// directory from postponing its cleanup forever, and a Dir dropped from its
// parent is not re-armed. Must be called with d.mu held.
func (d *Dir) _armLookupCleanup() {
	if d.lookupArmed {
		return
	}
	d.lookupArmed = true
	d.cleanupTimer.Reset(time.Duration(d.vfs.Opt.DirCacheTime * 2))
}
```

`vfs/dir.go` の `cacheCleanup` を次のようにする:

```go
	when := time.Now()

	d.mu.Lock()
	_, stale := d._age(when)
	// The timer has fired; the next lookup hit arms it again.
	d.lookupArmed = false
	d.mu.Unlock()

	if stale {
		d.ForgetAll()
	}
```

`vfs/dir.go` の `ForgetAll` の、エントリを消す分岐を次のようにする:

```go
	if !hasVirtual {
		d.read = time.Time{}
		d.items = make(map[string]Node)
		d.cleanupTimer.Stop()
		// The timer is stopped; let the next lookup hit arm it again.
		d.lookupArmed = false
	} else {
```

- [ ] **Step 4: テストが通ることを確かめる**

Run: `go test ./vfs/ -run 'TestKazLookup' -race -count=3 -v 2>&1 | tail -30`
Expected: すべて PASS（3回とも）。race の報告なし。

- [ ] **Step 5: 既存の vfs テスト**

Run: `go test ./vfs/ 2>&1 | tail -5`
Expected: `ok`。

- [ ] **Step 6: commit（ユーザーが許可している場合のみ）**

```bash
git add vfs/dir.go vfs/dir_kaz_lookup.go vfs/dir_kaz_lookup_test.go
git commit -m "vfs: expire kaz lookup entries after twice the dir cache time"
```

---

### Task 4: すでに消えている object の Remove を成功にする

背景: lookup モードでは、他のホストが消した object がキャッシュに残ることがある。その object を Remove すると backend が失敗を返し、`File.Remove` はキャッシュからエントリを消さない（`vfs/file.go:681-695`）。そのため、古いエントリがいつまでも残る。lookup モードのときだけ、失敗後に `NewObject` で存在を確かめ直し、無ければ成功にする。backend ごとのエラーの種類に頼らないための方法である。

**Files:**
- Modify: `vfs/file.go:658-697`（`File.Remove`）。同じファイルに `kazObjectGone` を追加する。
- Test: `vfs/dir_kaz_lookup_test.go`（追記）

**Interfaces:**
- Consumes: `newLookupVFS`（Task 2）
- Produces: `func (f *File) kazObjectGone(d *Dir) bool`

- [ ] **Step 1: 失敗するテストを書く**（追記）

```go
// TestKazRemoveAlreadyGone checks removing a cached file that another client
// already deleted succeeds and drops the stale entry.
func TestKazRemoveAlreadyGone(t *testing.T) {
	r, _, v := newLookupVFS(t, true, time.Hour)
	ctx := context.Background()
	obj := r.WriteObject(ctx, "dir/a", "aaa", t1)
	_, err := v.Stat("dir/a")
	require.NoError(t, err)

	o, err := r.Fremote.NewObject(ctx, obj.Path)
	require.NoError(t, err)
	require.NoError(t, o.Remove(ctx))

	require.NoError(t, v.Remove("dir/a"))
	_, err = v.Stat("dir/a")
	assert.True(t, errors.Is(err, ENOENT))
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./vfs/ -run TestKazRemoveAlreadyGone -v 2>&1 | tail -15`
Expected: FAIL（`v.Remove` が backend の「no such file」エラーを返す）。

- [ ] **Step 3: 実装する**

`vfs/file.go` の `File.Remove` の、エラーをログに出すブロックを次のようにする:

```go
	if err != nil {
		if wasWriting {
			// Ignore error deleting file if was writing it as it may not be uploaded yet
			err = nil
			fs.Debugf(f._path(), "Ignoring File.Remove file error as uploading: %v", err)
		} else if d.vfs.Opt.KazLookupByPath && f.kazObjectGone(d) {
			// Another client sharing the remote removed it first.
			fs.Debugf(f._path(), "File.Remove: object already gone, treating as removed: %v", err)
			err = nil
		} else {
			fs.Debugf(f._path(), "File.Remove file error: %v", err)
		}
	}
```

`File.Remove` の直後に追加する:

```go
// kazObjectGone reports whether the remote no longer has this file's object.
// It is used in kaz lookup mode to treat a failed remove as done when another
// client sharing the remote deleted the object first. Errors other than
// "object not found" (e.g. rate limits) report false so the original remove
// error is kept.
func (f *File) kazObjectGone(d *Dir) bool {
	_, err := d.f.NewObject(f.ctx, f.Path())
	return errors.Is(err, fs.ErrorObjectNotFound)
}
```

`vfs/file.go` の import に `"errors"` が無ければ追加する。

- [ ] **Step 4: テストが通ることを確かめる**

Run: `go test ./vfs/ -run 'TestKaz' -race -v 2>&1 | tail -20; go test ./vfs/ 2>&1 | tail -3`
Expected: すべて PASS。`ok`。

- [ ] **Step 5: commit（ユーザーが許可している場合のみ）**

```bash
git add vfs/file.go vfs/dir_kaz_lookup_test.go
git commit -m "vfs: treat removing an already deleted object as success in kaz lookup mode"
```

---

### Task 5: serve s3 で「無い」のときだけ 404 にする

**Files:**
- Create: `cmd/serve/s3/kaz_errors.go`
- Modify: `cmd/serve/s3/backend.go`（`HeadObject` 140-152 行、`GetObject` 198-210 行、`deleteObject` 480-483 行、`BucketExists` 540-551 行）
- Test: `cmd/serve/s3/kaz_test.go`

**Interfaces:**
- Consumes: `vfscommon.Options.KazLookupByPath`（Task 1）
- Produces: `func bucketStatError(bucket string, err error) error`、`func keyStatError(key string, err error) error`。テスト用の `failingFs`、`newKazBackend`（Task 6 でも使う）。

- [ ] **Step 1: 失敗するテストを書く**

`cmd/serve/s3/kaz_test.go`:

```go
package s3

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/rclone/gofakes3"
	_ "github.com/rclone/rclone/backend/local"
	"github.com/rclone/rclone/cmd/serve/proxy"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fstest"
	"github.com/rclone/rclone/vfs/vfscommon"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// failingFs wraps an fs.Fs and, while fail is set, makes List and NewObject
// return errKaz like a remote rate limit would.
type failingFs struct {
	fs.Fs
	fail atomic.Bool
}

var errKaz = errors.New("kaz: simulated remote failure")

// List fails while fail is set, otherwise delegates.
func (f *failingFs) List(ctx context.Context, dir string) (fs.DirEntries, error) {
	if f.fail.Load() {
		return nil, errKaz
	}
	return f.Fs.List(ctx, dir)
}

// NewObject fails while fail is set, otherwise delegates.
func (f *failingFs) NewObject(ctx context.Context, remote string) (fs.Object, error) {
	if f.fail.Load() {
		return nil, errKaz
	}
	return f.Fs.NewObject(ctx, remote)
}

// newKazBackend serves a fresh local directory containing "bucket" through a
// failingFs. lookup sets --kaz-vfs-lookup-by-path; tweak may change the serve
// options. Files written straight to root/bucket act as another host sharing
// the remote.
func newKazBackend(t *testing.T, lookup bool, tweak func(*Options)) (*s3Backend, *failingFs, string) {
	fstest.Initialise()
	ctx := context.Background()
	root := t.TempDir()
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket"), 0777))
	base, err := fs.NewFs(ctx, root)
	require.NoError(t, err)
	ff := &failingFs{Fs: base}

	vfsOpt := vfscommon.Opt
	vfsOpt.KazLookupByPath = lookup
	vfsOpt.DirCacheTime = fs.Duration(time.Hour)
	vfsOpt.PollInterval = 0
	opt := Opt
	opt.HTTP.ListenAddr = []string{endpoint}
	if tweak != nil {
		tweak(&opt)
	}
	w, err := newServer(ctx, ff, &opt, &vfsOpt, &proxy.Opt)
	require.NoError(t, err)
	t.Cleanup(func() { _ = w.Shutdown() })
	return newBackend(w), ff, root
}

// TestKazStatErrorsAreNot404 checks only "not found" becomes NoSuchKey or
// NoSuchBucket and other remote errors are passed through, with and without
// lookup mode.
func TestKazStatErrorsAreNot404(t *testing.T) {
	ctx := context.Background()
	for _, lookup := range []bool{false, true} {
		t.Run(map[bool]string{false: "list", true: "lookup"}[lookup], func(t *testing.T) {
			b, ff, root := newKazBackend(t, lookup, nil)
			require.NoError(t, os.WriteFile(filepath.Join(root, "bucket", "obj"), []byte("x"), 0666))

			_, err := b.HeadObject(ctx, "bucket", "missing")
			assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey), "missing key: %v", err)
			_, err = b.HeadObject(ctx, "nobucket", "obj")
			assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchBucket), "missing bucket: %v", err)
			exists, err := b.BucketExists(ctx, "nobucket")
			assert.NoError(t, err)
			assert.False(t, exists)

			// Make every remote call fail and forget what the VFS cached.
			ff.fail.Store(true)
			v, err := b.s.getVFS(ctx)
			require.NoError(t, err)
			root2, err := v.Root()
			require.NoError(t, err)
			root2.ForgetAll()

			_, err = b.HeadObject(ctx, "bucket", "obj")
			assert.ErrorIs(t, err, errKaz, "HEAD")
			_, err = b.GetObject(ctx, "bucket", "obj", nil)
			assert.ErrorIs(t, err, errKaz, "GET")
			err = b.deleteObject(ctx, "bucket", "obj")
			assert.ErrorIs(t, err, errKaz, "DELETE")
			_, err = b.BucketExists(ctx, "bucket")
			assert.ErrorIs(t, err, errKaz, "BucketExists")
		})
	}
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./cmd/serve/s3/ -run TestKazStatErrorsAreNot404 -v 2>&1 | tail -30`
Expected: 前半（missing）は PASS、後半の `ErrorIs(..., errKaz)` が FAIL（NoSuchKey・NoSuchBucket・false が返る）。

- [ ] **Step 3: 実装する**

`cmd/serve/s3/kaz_errors.go`:

```go
package s3

import (
	"errors"

	"github.com/rclone/gofakes3"
	"github.com/rclone/rclone/vfs"
)

// bucketStatError maps the error from a VFS Stat of a bucket. Only "does not
// exist" becomes NoSuchBucket; any other error (for example a remote rate
// limit or network failure) is returned as is, which gofakes3 reports as a
// 500 InternalError instead of hiding the failure behind a 404.
func bucketStatError(bucket string, err error) error {
	if errors.Is(err, vfs.ENOENT) {
		return gofakes3.BucketNotFound(bucket)
	}
	return err
}

// keyStatError maps the error from a VFS Stat of an object key in the same
// way as bucketStatError, using NoSuchKey for "does not exist".
func keyStatError(key string, err error) error {
	if errors.Is(err, vfs.ENOENT) {
		return gofakes3.KeyNotFound(key)
	}
	return err
}
```

`cmd/serve/s3/backend.go` を変更する。

`HeadObject` と `GetObject` の bucket の Stat:

```go
	_, err = _vfs.Stat(bucketName)
	if err != nil {
		return nil, bucketStatError(bucketName, err)
	}
```

`HeadObject` と `GetObject` の object の Stat:

```go
	node, err := _vfs.Stat(fp)
	if err != nil {
		return nil, keyStatError(objectName, err)
	}
```

`deleteObject` の bucket の Stat:

```go
	_, err = _vfs.Stat(bucketName)
	if err != nil {
		return bucketStatError(bucketName, err)
	}
```

`BucketExists`:

```go
	_, err = _vfs.Stat(name)
	if err != nil {
		if errors.Is(err, vfs.ENOENT) {
			return false, nil
		}
		// Not knowing is not "absent": report the failure (500).
		return false, err
	}
```

`backend.go` の import に `"errors"` が無ければ追加する（`vfs` は既に import されている）。

- [ ] **Step 4: テストが通ることを確かめる**

Run: `go test ./cmd/serve/s3/ -run TestKazStatErrorsAreNot404 -race -v 2>&1 | tail -20; go test ./cmd/serve/s3/ 2>&1 | tail -5`
Expected: PASS。既存のテストも `ok`。

- [ ] **Step 5: commit（ユーザーが許可している場合のみ）**

```bash
git add cmd/serve/s3/kaz_errors.go cmd/serve/s3/kaz_test.go cmd/serve/s3/backend.go
git commit -m "serve s3: only report NoSuchKey/NoSuchBucket when the path does not exist"
```

---

### Task 6: DELETE（`b.meta` の削除、`--no-cleanup`）と複数ホスト相当の動作

**Files:**
- Modify: `cmd/serve/s3/backend.go`（`deleteObject` 489-497 行）
- Test: `cmd/serve/s3/kaz_test.go`（追記）

**Interfaces:**
- Consumes: `newKazBackend`、`failingFs`（Task 5）、lookup モード（Task 2〜4）
- Produces: なし

- [ ] **Step 1: 失敗するテストを書く**（`cmd/serve/s3/kaz_test.go` に追記。import に `"io"` と `"strings"` を加える）

```go
// TestKazDeleteRemovesMeta checks DELETE drops the in-memory user metadata so
// it does not grow without bound.
func TestKazDeleteRemovesMeta(t *testing.T) {
	ctx := context.Background()
	b, _, _ := newKazBackend(t, true, nil)
	_, err := b.PutObject(ctx, "bucket", "d/obj", map[string]string{"X-Amz-Meta-Crc32c": "AAAAAA=="}, strings.NewReader("abc"), 3)
	require.NoError(t, err)
	fp, err := bucketObjectPath("bucket", "d/obj")
	require.NoError(t, err)
	_, ok := b.meta.Load(fp)
	require.True(t, ok)

	require.NoError(t, b.deleteObject(ctx, "bucket", "d/obj"))
	_, ok = b.meta.Load(fp)
	assert.False(t, ok, "meta must be dropped on delete")
}

// TestNoCleanupKeepsParents checks --no-cleanup keeps the emptied parent
// directories and that the default still removes them.
func TestNoCleanupKeepsParents(t *testing.T) {
	ctx := context.Background()
	for _, noCleanup := range []bool{false, true} {
		b, _, root := newKazBackend(t, true, func(o *Options) { o.NoCleanup = noCleanup })
		_, err := b.PutObject(ctx, "bucket", "d1/d2/obj", map[string]string{}, strings.NewReader("abc"), 3)
		require.NoError(t, err)
		require.NoError(t, b.deleteObject(ctx, "bucket", "d1/d2/obj"))
		_, statErr := os.Stat(filepath.Join(root, "bucket", "d1", "d2"))
		if noCleanup {
			assert.NoError(t, statErr, "--no-cleanup must keep the parent")
		} else {
			assert.True(t, os.IsNotExist(statErr), "default must remove the empty parent")
		}
	}
}

// TestKazOtherHostVisible checks objects written and deleted directly on the
// shared remote (another host) are seen by HEAD, GET and DELETE in lookup
// mode, even though the directory is already cached.
func TestKazOtherHostVisible(t *testing.T) {
	ctx := context.Background()
	b, _, root := newKazBackend(t, true, nil)
	dir := filepath.Join(root, "bucket", "chunks", "0", "4")
	require.NoError(t, os.MkdirAll(dir, 0777))
	require.NoError(t, os.WriteFile(filepath.Join(dir, "4000_0_3"), []byte("abc"), 0666))
	_, err := b.HeadObject(ctx, "bucket", "chunks/0/4/4000_0_3")
	require.NoError(t, err)

	// Another host writes a new object into the cached directory.
	require.NoError(t, os.WriteFile(filepath.Join(dir, "4096_0_5"), []byte("hello"), 0666))
	obj, err := b.HeadObject(ctx, "bucket", "chunks/0/4/4096_0_5")
	require.NoError(t, err)
	assert.Equal(t, int64(5), obj.Size)
	obj, err = b.GetObject(ctx, "bucket", "chunks/0/4/4096_0_5", nil)
	require.NoError(t, err)
	data, err := io.ReadAll(obj.Contents)
	require.NoError(t, err)
	_ = obj.Contents.Close()
	assert.Equal(t, "hello", string(data))

	// DELETE removes it from the remote, then it is gone.
	require.NoError(t, b.deleteObject(ctx, "bucket", "chunks/0/4/4096_0_5"))
	_, statErr := os.Stat(filepath.Join(dir, "4096_0_5"))
	assert.True(t, os.IsNotExist(statErr), "DELETE must remove the object on the remote")
	_, err = b.HeadObject(ctx, "bucket", "chunks/0/4/4096_0_5")
	assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey))

	// Deleted by another host first: DELETE still succeeds.
	require.NoError(t, os.Remove(filepath.Join(dir, "4000_0_3")))
	assert.NoError(t, b.deleteObject(ctx, "bucket", "chunks/0/4/4000_0_3"))
}

// TestKazGetObjectDeletedElsewhere checks a GET of an object still cached
// here but deleted by another host fails instead of returning data or 404.
func TestKazGetObjectDeletedElsewhere(t *testing.T) {
	ctx := context.Background()
	b, _, root := newKazBackend(t, true, nil)
	p := filepath.Join(root, "bucket", "obj")
	require.NoError(t, os.WriteFile(p, []byte("abc"), 0666))
	_, err := b.HeadObject(ctx, "bucket", "obj")
	require.NoError(t, err)
	require.NoError(t, os.Remove(p))

	obj, err := b.GetObject(ctx, "bucket", "obj", nil)
	if err == nil {
		_, err = io.ReadAll(obj.Contents)
		_ = obj.Contents.Close()
	}
	assert.Error(t, err)
	assert.False(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey), "must not look like a clean 404: %v", err)
}

// TestKazPutCreatesParents checks a PUT into directories that do not exist
// yet works in lookup mode and the object is visible right away.
func TestKazPutCreatesParents(t *testing.T) {
	ctx := context.Background()
	b, _, root := newKazBackend(t, true, nil)
	_, err := b.PutObject(ctx, "bucket", "chunks/0/9/9000_0_4", map[string]string{}, strings.NewReader("data"), 4)
	require.NoError(t, err)
	obj, err := b.HeadObject(ctx, "bucket", "chunks/0/9/9000_0_4")
	require.NoError(t, err)
	assert.Equal(t, int64(4), obj.Size)
	got, err := os.ReadFile(filepath.Join(root, "bucket", "chunks", "0", "9", "9000_0_4"))
	require.NoError(t, err)
	assert.Equal(t, "data", string(got))
}
```

- [ ] **Step 2: 失敗することを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestKaz|TestNoCleanup' -v 2>&1 | tail -40`
Expected: `TestKazDeleteRemovesMeta` が FAIL（meta が残る）。`TestNoCleanupKeepsParents` の true の場合が FAIL（親が消える）。その他は Task 2〜5 の実装で PASS するはず。PASS しない場合は原因を調べてから進む（このタスクで直すのは DELETE の2点だけ）。

- [ ] **Step 3: 実装する**

`cmd/serve/s3/backend.go` の `deleteObject` の末尾を次のようにする:

```go
	// S3 does not report an error when attempting to delete a key that does not exist, so
	// we need to skip IsNotExist errors.
	if err := _vfs.Remove(fp); err != nil && !os.IsNotExist(err) {
		return err
	}
	// The user metadata lives only in memory; drop it with the object so it
	// does not grow without bound.
	b.meta.Delete(fp)

	if b.s.opt.NoCleanup {
		return nil
	}
	// FIXME: unsafe operation
	rmdirRecursive(fp, _vfs)
	return nil
}
```

- [ ] **Step 4: テストが通ることを確かめる**

Run: `go test ./cmd/serve/s3/ -race -v -run 'TestKaz|TestNoCleanup' 2>&1 | tail -30; go test ./cmd/serve/s3/ 2>&1 | tail -5`
Expected: すべて PASS。既存のテストも `ok`。

- [ ] **Step 5: commit（ユーザーが許可している場合のみ）**

```bash
git add cmd/serve/s3/backend.go cmd/serve/s3/kaz_test.go
git commit -m "serve s3: honour --no-cleanup and drop metadata on delete"
```

---

### Task 7: JuiceFS 側の扱いの確認、全体テスト、ビルド

**Files:**
- 調査リポジトリ: `rclone_dir_cache/2026-10-08/juicefs-error-handling-ja.md`（新規。調査リポジトリのルート基準）

- [ ] **Step 1: JuiceFS が 404・500・503 を区別するかをソースで確かめる**

調査リポジトリのルートで:

```bash
grep -rn "NoSuchKey\|StatusNotFound\|IsNotExist\|SlowDown\|StatusServiceUnavailable" juicefs/pkg/object/s3.go juicefs/pkg/object/restful.go juicefs/pkg/chunk/cached_store.go | head -40
```

`cached_store.go` の GET・PUT の再試行の分岐で、エラーの種類によって再試行するかどうかを変えているかを読む。結果を `rclone_dir_cache/2026-10-08/juicefs-error-handling-ja.md` に、観測（file:line）と結論に分けて書く。区別していなければ「500 のままとする」と書く。区別していれば、503 SlowDown を返す追加タスクの案をユーザーに示し、承認を得てから別に行う（この計画には含めない）。

- [ ] **Step 2: 対象パッケージのテストを race つきで通す**

Run（`rclone/` で）: `go test -race ./vfs/... ./cmd/serve/s3/ ./fs/config/flags/ 2>&1 | tail -20`
Expected: すべて `ok`。失敗したら、Task 0 Step 2 の基準と比べ、変更前からの失敗かどうかを分けて報告する。

- [ ] **Step 3: vet とビルド**

Run:
```bash
go vet ./vfs/... ./cmd/serve/s3/
go build -o /tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/6a9e01c1-737b-4bca-8455-b98b426f8f9a/scratchpad/rclone-kaz .
/tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/6a9e01c1-737b-4bca-8455-b98b426f8f9a/scratchpad/rclone-kaz version | head -3
/tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/6a9e01c1-737b-4bca-8455-b98b426f8f9a/scratchpad/rclone-kaz serve s3 --help | grep -n "kaz-vfs-lookup-by-path\|no-cleanup"
```
Expected: vet の指摘なし。version が `rclone v1.75.1` 系。help に2つのフラグが出る。

（scratchpad のパスはセッションごとに変わる。実行するセッションの scratchpad を使うこと。）

- [ ] **Step 4: 調査リポジトリの記録を更新する（commit はユーザーの許可を得てから）**

- `TODO.md` の rclone の項目: Phase 1 の実装状況、本体のブランチと HEAD の SHA。
- `agent_memo.md`: 実装の要点、テスト結果、JuiceFS のエラー処理の確認結果。
- `docs/superpowers/README.md` の一覧表: 実装計画へのリンクと状態。

---

### Task 8: 実際の Drive での検証（本番のフォルダは使わない）

各ステップの前に、実行するコマンドをユーザーに見せて確認を取る。auth key は検証用に生成した値を使い、本番の値を使わない。本番の起動スクリプトは読まない。

- [ ] **Step 1: テスト用フォルダを作る（ユーザーの確認後）**

```bash
/usr/bin/rclone mkdir gdrive_kwatan:/rclone-s3-test/bucket
```

- [ ] **Step 2: 改修版の serve s3 を2つ立てる（ホスト A・B の代わり）**

`$BIN` は Task 7 でビルドしたバイナリ、`$S` は scratchpad。`KEY` は `openssl rand -hex 16` などで作った検証用の値。

```bash
KEY=$(openssl rand -hex 16)
$BIN serve s3 gdrive_kwatan:/rclone-s3-test --addr 127.0.0.1:19090 --auth-key kaz,$KEY \
  --kaz-vfs-lookup-by-path --no-cleanup --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off \
  --tpslimit 5 -vv --log-file $S/serveA.log &
$BIN serve s3 gdrive_kwatan:/rclone-s3-test --addr 127.0.0.1:19091 --auth-key kaz,$KEY \
  --kaz-vfs-lookup-by-path --no-cleanup --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off \
  --tpslimit 5 -vv --log-file $S/serveB.log &
```

- [ ] **Step 3: A で PUT した object を B で読む・消す**

S3 クライアントには rclone 自身の S3 backend を使う（接続文字列で指定し、設定ファイルは変えない）:

```bash
SA=":s3,provider=Rclone,endpoint=http://127.0.0.1:19090,access_key_id=kaz,secret_access_key=$KEY:"
SB=":s3,provider=Rclone,endpoint=http://127.0.0.1:19091,access_key_id=kaz,secret_access_key=$KEY:"
echo hello > $S/obj1
/usr/bin/rclone copyto $S/obj1 "${SB}bucket/chunks/0/1/1000_0_6"      # B で先にディレクトリを読ませる
/usr/bin/rclone lsf "${SA}bucket/chunks/0/1/"                         # A でもキャッシュさせる
/usr/bin/rclone copyto $S/obj1 "${SA}bucket/chunks/0/1/1001_0_6"      # A の PUT
/usr/bin/rclone cat "${SB}bucket/chunks/0/1/1001_0_6"                 # B から即座に読めること
/usr/bin/rclone deletefile "${SB}bucket/chunks/0/1/1001_0_6"          # B の DELETE
/usr/bin/rclone lsf gdrive_kwatan:/rclone-s3-test/bucket/chunks/0/1/  # Drive から消えていること
```

Expected: `cat` が `hello` を返す。最後の `lsf` に `1001_0_6` が無い。

注意: rclone の S3 クライアントの `cat` は HEAD を先に送ることがある。HEAD・GET の両方が成功していることを、ログで確かめる。

- [ ] **Step 4: ログを確かめる**

```bash
grep -c "Re-reading directory" $S/serveA.log $S/serveB.log
grep -n "Dir.Stat error\|InternalError" $S/serveA.log $S/serveB.log | head
```

Expected: `Re-reading directory` は、Step 3 で明示的に一覧（`lsf`）した分だけ出る。key 指定の操作では増えない。エラーは無い。Drive API の回数は、`-vv` の pacer・HTTP のログから、操作ごとにおおよそ数える。

- [ ] **Step 5: 片付ける（ユーザーの確認後）**

2つの serve を SIGTERM で止める。テスト用フォルダを消す: `/usr/bin/rclone purge gdrive_kwatan:/rclone-s3-test`。

- [ ] **Step 6: 記録**

結果を `rclone_dir_cache/<実施日>/drive-verification-ja.md` に書く（コマンド、Expected と実際、ログの件数）。KEY やトークンは書かない。

（余力があれば、scratchpad の sqlite を metadata にした JuiceFS を2つの serve の上に format・mount し、2つの mount の間で書いたファイルを読めることを確かめる。必要になった時点で手順をユーザーに示す。）

---

### Task 9: 本番への適用の手順書と記録

- [ ] **Step 1: 手順書を書く**

調査リポジトリに `rclone_dir_cache/<実施日>/deploy-runbook-ja.md` を作る。内容:
- 改修版バイナリの置き場所（Phase 3 までは手元でビルドしたもの。ファイル名に `kaz` を含め、`/usr/bin/rclone` は置き換えない）と SHA-256。
- 起動オプションへの追加: `--kaz-vfs-lookup-by-path --no-cleanup`。既存のオプション（`--poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off --tpslimit 20 --low-level-retries 1` など）は変えない。
- 各ホストで1台ずつ SIGTERM で止めて、起動し直す。止まっている時間が約14秒を超えると、JuiceFS の読み込みが EIO になり得る（`docs/findings.md` の rclone の停止の節）。
- 元に戻す方法: 元のバイナリと元のオプションで起動し直す。
- 適用後に見るもの: rclone のログの `Re-reading directory` の頻度、`InternalError`、JuiceFS の EIO・`timeout awaiting response headers`、Drive の rate limit。
- 起動スクリプトには OAuth の秘密が平文で入っているので、手順書には中身を写さない。

- [ ] **Step 2: 記録を更新する**

`TODO.md`、`agent_memo.md`、`docs/findings.md`（rclone の節に、dir cache の問題と対策を追記）を更新する。commit はユーザーの許可を得てから行う。
