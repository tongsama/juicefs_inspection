# rclone の kaz 改修を公式 v1.75.2 へ載せ直す（rclone-v1.75.2-kaz.1）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `1.75.1-improve-kaz`（fe0572749、`rclone-v1.75.1-kaz.2`）の kaz 改修を、公式 v1.75.2（be8d6044e）の上に機能ごとのコミットとして載せ直し、`rclone-v1.75.2-kaz.1` として出せる状態にする。

**Architecture:** v1.75.2 から新しいベースのブランチ `1.75.2-improve-kaz` を切る。kaz のコミットを機能ごとに cherry-pick で当て直す（積み重ねた作業ブランチを3本）。v1.75.2 で意味が変わったところは、その場でコミットの中身を書き直す。要らなくなったコミットは当てない。新しく要る修正は、別のコミットで足す。最後に、作業ブランチを順に `--no-ff` で `1.75.2-improve-kaz` へ merge する。v1.75.1 のときと同じ形の履歴になる。

**Tech Stack:** Go 1.26.9（upstream v1.75.2 の CI と同じ。手元の go は 1.26.0 なので `GOTOOLCHAIN=go1.26.9`）、rclone の `cmd/serve/s3`・`vfs`・`backend/drive`、gofakes3 v0.0.10、minio-go v7、testify。

**Spec:** [調査報告 docs/inspection/rclone_v1752_port/2026-10-13/port-investigation-ja.md](../../inspection/rclone_v1752_port/2026-10-13/port-investigation-ja.md)。各機能の仕様は変えない（[lookup](../specs/2026-10-08-rclone-lookup-by-path-design.md)、[persist-metadata](../specs/2026-10-08-rclone-s3-persist-metadata-design.md)、[一覧](../specs/2026-10-09-rclone-s3-list-by-key-order-design.md)）。

## Global Constraints

- 作業ディレクトリは本体の `rclone/`（調査リポジトリのルートから見た位置）。以下のパスと Go コマンドはすべて `rclone/` を基準にする。
- 既存の `1.75.1-improve-kaz`、`feat/kaz-*`、`merge/kaz-s3-cancel-get-list` は変更しない（書き換え・削除・force push をしない）。
- ブランチ名: ベースは `1.75.2-improve-kaz`（v1.75.2 から）。作業ブランチは `port-1.75.2/kaz-vfs-lookup` → `port-1.75.2/kaz-s3-persist-metadata` → `port-1.75.2/kaz-s3-cancel-get` → `port-1.75.2/kaz-s3-list` の順に、前のブランチの先端から切って積み重ねる（既存の `feat/kaz-vfs-lookup` と名前が衝突しないよう `port-1.75.2/` を使う）。
- **commit は作業ブランチでだけ行う（ユーザーがタスクごとの commit を許可した場合）。`1.75.2-improve-kaz` への merge（Task 9）は、ユーザーの承認を得てから行う。push はしない。** commit メッセージに Co-Authored-By などの attribution 行を付けない。cherry-pick したコミットは元の author・メッセージを保つ（`git cherry-pick -x` は使わない。元の commit は push 済みの `kaz/1.75.1-improve-kaz` にあるので、対応は Task 9 の記録に残す）。
- rclone/ のローカルの git user は tongsama <getpow2@gmail.com>（設定済み）。
- kaz のオプション名・既定値・ヘルプの `[kaz] `・`Groups: "Kaz"` は v1.75.1-kaz.2 と同じ。オプションは1つも消さない（`--kaz-s3-list-by-key-order` は受け付けるだけにする）。
- upstream のファイルへの変更は、できるだけ kaz_*.go の関数を呼ぶ数行のフックにとどめる（次の upstream の版で衝突する範囲を小さくし、upstream の関数の形が変わったらコンパイルエラーで分かるようにする）。upstream のコードを書き換えて置き換えるのは、upstream の関数の中身に kaz の分岐がどうしても要るときだけにする。
- upstream v1.75.2 のセキュリティ修正・動作を、kaz の経路で迂回しない。とくに `Dir.checkName`（unsafe な名前の拒否、2f43b99ab）。
- 「ENOENT のときだけ 404（NoSuchKey／NoSuchBucket）、他は 500」の方針を、v1.75.2 で増えた Stat の経路（CopyObject、CreateMultipartUpload）にも当てる。
- `--kaz-s3-persist-metadata` のときは、メタデータを tenant の LRU（`tenant.meta`）に入れない。オフのときは upstream の動作（`storeMeta`／`getMeta`／`removeMeta`）のまま。
- Go のコマンドは `GOTOOLCHAIN=go1.26.9` を付けて実行する（初回に公式の toolchain を module proxy から取得する）。`TestS3Minio` は docker が要るので `-skip TestS3Minio` で除く。
- 新規・変更する関数・型には、目的が分かる英語の Go doc コメントを付ける（rclone の慣習）。
- 本番の rclone、Drive の `/rclone-s3`、JuiceFS の metadata には触らない。実 Drive での確認（Task 10）はユーザーの承認を得てテスト用フォルダだけで行う。

## Review Focus

調査で見つかったが、移植のコミットの機械的な当て直しでは見落としやすい点。各行のテストは担当のタスクに入れてある。

1. serve nfs を `--kaz-vfs-lookup-by-path --vfs-cache-mode off` で動かし、クライアントが `..` を名前に使う → 公開した root の外に読み書きできないこと（Task 1 `TestKazPathTraversalLookup`、`TestKazLookupUnsafeName`）。
2. persist で multipart の part が逆順に届く、または part が0個のまま Complete → どちらもメタデータが object に付くこと（Task 4 `TestKazPersistMultipartOutOfOrder`、`TestKazPersistMultipartNoParts`）。
3. persist で CreateMultipartUpload の直後は、backend に何も作らないこと（upstream の `--multipart-max-uploads` の前提。Task 4 `TestKazPersistMultipartCreateMakesNothing`）。
4. CopyObject・CreateMultipartUpload の Stat が「無い」以外の理由で失敗 → 404 ではなく 500（Task 2 `TestKazStatErrorsAreNot404` の追加分）。
5. ListObjects の途中でクライアントが切る → 次のディレクトリへ進む前に止まること（Task 6 `TestKazListStopsOnCancel`）。

---

### Task 0: 準備（ベースのブランチと基準のテスト）

**Files:** なし（ブランチの作成と確認だけ）

- [ ] **Step 1: 状態を確かめる**

```bash
cd rclone
git status --short          # 何も出ないこと
git fetch origin --tags
git rev-parse v1.75.2       # be8d6044e... であること
git worktree list           # scratchpad の wt-merge／wt-list／wt-vfs があれば、調査用なので触らない
```

- [ ] **Step 2: ベースのブランチを切る**

```bash
git switch -c 1.75.2-improve-kaz v1.75.2
git switch -c port-1.75.2/kaz-vfs-lookup
```

- [ ] **Step 3: Go 1.26.9 と基準のテスト**

```bash
GOTOOLCHAIN=go1.26.9 go version     # go1.26.9 であること
GOTOOLCHAIN=go1.26.9 go build ./...
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ ./cmd/serve/nfs/ ./vfs/ ./vfs/vfscommon/ ./backend/drive/ ./fs/config/flags/ -skip TestS3Minio -count=1
```

Expected: すべて ok。失敗があれば、v1.75.2 の素の状態での失敗として記録し、以降のタスクで「増えていない」ことの基準にする。

---

### Task 1: lookup モードを載せ直し、unsafe な名前の拒否を通す

**Files:**
- Cherry-pick: c27d12d03 36851a2f6 54c2d1668 b63c16b81 4d8608da9 c5ae06e24 6e8be6048 ea1910336 078531045 0e050fab1 9b75066b4（この順）
- Modify: `vfs/dir_kaz_lookup.go`（`lookup`、`kazCreateNoLookup`）、`vfs/vfs.go`（O_TRUNC の近道）
- Test: `vfs/dir_kaz_lookup_test.go`、`cmd/serve/nfs/kaz_filesystem_test.go`（新規）

**Interfaces:**
- Consumes: v1.75.2 の `Dir.checkName(name string) error`（vfs/dir.go）、`sanitize.Leaf(string) error`（lib/sanitize）
- Produces: 後のタスクは `vfscommon.Options.KazLookupByPath`、serve s3 の `keyStatError(key string, err error) error`・`bucketStatError(bucket string, err error) error`（cmd/serve/s3/kaz_errors.go）、`Options.NoCleanup` を使う

- [ ] **Step 1: 11 個のコミットを当てる**

```bash
git cherry-pick c27d12d03 36851a2f6 54c2d1668 b63c16b81 4d8608da9
```

衝突したら、kaz 側の変更を v1.75.2 の文脈へ移す（import 文や周りの行がずれているだけのはず）。`git cherry-pick --continue` で元のメッセージのまま進める。

- [ ] **Step 2: c5ae06e24 を書き直して当てる**

```bash
git cherry-pick -n c5ae06e24
```

v1.75.2 の `deleteObject` は既に `b.tenant(ctx).removeMeta(fp)` を呼ぶ（19068b660）。kaz の `b.meta.Delete(fp)` とその上のコメントは入れない。`--no-cleanup` の部分は残す。結果の `deleteObject` の末尾は次の形にする。

```go
	if err := _vfs.Remove(fp); err != nil && !os.IsNotExist(err) {
		return err
	}
	b.tenant(ctx).removeMeta(fp)

	if b.s.opt.NoCleanup {
		return nil
	}
	// FIXME: unsafe operation
	rmdirRecursive(fp, _vfs)
	return nil
```

（v1.75.2 の removeMeta の位置・コメントがこれと違えば、upstream の行はそのままにし、`NoCleanup` の3行だけを `rmdirRecursive` の前に足す。）

テスト `TestKazDeleteRemovesMeta`（cmd/serve/s3/kaz_test.go）は、upstream の動作の回帰テストとして tenant の API に直す。

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
	_, ok := b.tenant(ctx).meta.Get(fp)
	require.True(t, ok)

	require.NoError(t, b.deleteObject(ctx, "bucket", "d/obj"))
	_, ok = b.tenant(ctx).meta.Get(fp)
	assert.False(t, ok, "meta must be dropped on delete")
}
```

`newKazBackend` の最後の `return newBackend(w), ff, root` は `return w.backend, ff, root` にする（v1.75.2 の server は自分の backend で reaper を動かすので、別の backend を作らない）。

```bash
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -run 'TestKazDeleteRemovesMeta|TestNoCleanupKeepsParents' -count=1
git commit -C c5ae06e24   # 元のメッセージ・author のまま
```

Expected: PASS。メッセージの「drop metadata on delete」は upstream と重なるが、元のコミットとの対応のためそのまま残す。

- [ ] **Step 3: 残りの5個を当てる**

```bash
git cherry-pick 6e8be6048 ea1910336 078531045 0e050fab1 9b75066b4
GOTOOLCHAIN=go1.26.9 go test ./vfs/ ./vfs/vfscommon/ ./fs/config/flags/ ./cmd/serve/s3/ -skip TestS3Minio -count=1
```

Expected: ok（Task 0 の基準から失敗が増えていない）。

- [ ] **Step 4: 失敗するテストを書く（unsafe な名前）**

`vfs/dir_kaz_lookup_test.go` の末尾に足す。

```go
// TestKazLookupUnsafeName checks lookup mode refuses the names upstream's
// Dir.checkName refuses, so neither Stat nor the O_TRUNC create shortcut can
// reach outside the directory.
func TestKazLookupUnsafeName(t *testing.T) {
	for _, mode := range []vfscommon.CacheMode{vfscommon.CacheModeOff, vfscommon.CacheModeWrites} {
		for _, name := range []string{"..", ".", "../escape", "a/..", "sub/../.."} {
			t.Run(fmt.Sprintf("%v/%q", mode, name), func(t *testing.T) {
				_, _, v := newLookupVFSMode(t, true, time.Hour, mode)
				root, err := v.Root()
				require.NoError(t, err)

				_, err = v.Stat(name)
				assert.Error(t, err, "VFS.Stat")
				_, err = root.Stat(name)
				assert.Error(t, err, "Dir.Stat")
				h, err := v.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
				if err == nil {
					_ = h.Close()
				}
				assert.Error(t, err, "OpenFile O_CREATE|O_TRUNC")

				root.mu.RLock()
				defer root.mu.RUnlock()
				for leaf := range root.items {
					assert.NoError(t, sanitize.Leaf(leaf), "unsafe item %q cached in root", leaf)
				}
			})
		}
	}
}
```

import に `fmt`、`os`、`github.com/rclone/rclone/lib/sanitize` が無ければ足す。`newLookupVFSMode` は既存の補助関数（同じファイル、cache mode を指定して lookup モードの VFS を作る）。

`cmd/serve/nfs/kaz_filesystem_test.go` を新しく作る。

```go
//go:build unix

package nfs

import (
	"io"
	"os"
	"strings"
	"testing"

	_ "github.com/rclone/rclone/backend/memory"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fs/operations"
	"github.com/rclone/rclone/fstest"
	"github.com/rclone/rclone/vfs"
	"github.com/rclone/rclone/vfs/vfscommon"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestKazPathTraversalLookup is TestPathTraversal with --kaz-vfs-lookup-by-path
// and both cache modes: the lookup and O_TRUNC create shortcuts must not let a
// ".." name reach outside the directory served.
func TestKazPathTraversalLookup(t *testing.T) {
	for _, mode := range []vfscommon.CacheMode{vfscommon.CacheModeOff, vfscommon.CacheModeWrites} {
		t.Run(mode.String(), func(t *testing.T) {
			ctx := t.Context()
			modTime := fstest.Time("2001-02-03T04:05:06.499999999Z")
			remote := ":memory:nfs-kaz-traversal-" + mode.String()

			outside, err := fs.NewFs(ctx, remote)
			require.NoError(t, err)
			_, err = operations.Rcat(ctx, outside, "outside-secret.txt", io.NopCloser(strings.NewReader("SECRET")), modTime, nil)
			require.NoError(t, err)
			f, err := fs.NewFs(ctx, remote+"/served-root")
			require.NoError(t, err)
			_, err = operations.Rcat(ctx, f, "inside.txt", io.NopCloser(strings.NewReader("INSIDE")), modTime, nil)
			require.NoError(t, err)

			opt := vfscommon.Opt
			opt.CacheMode = mode
			opt.KazLookupByPath = true
			v := vfs.New(ctx, f, &opt)
			defer v.Shutdown()
			bfs := &FS{vfs: v}

			_, err = bfs.OpenFile(bfs.Join("", ".."), os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
			assert.Error(t, err)
			_, err = bfs.OpenFile(bfs.Join("", "../outside-write.txt"), os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
			assert.Error(t, err)
			_, err = bfs.Create(bfs.Join("", "../outside-write.txt"))
			assert.Error(t, err)
			_, err = bfs.Open(bfs.Join("", "../outside-secret.txt"))
			assert.Error(t, err)
			_, err = bfs.Stat(bfs.Join("", ".."))
			assert.Error(t, err)
			assert.Error(t, bfs.Remove(bfs.Join("", "../outside-secret.txt")))

			fstest.CheckListingWithPrecision(t, outside, []fstest.Item{
				fstest.NewItem("outside-secret.txt", "SECRET", modTime),
				fstest.NewItem("served-root/inside.txt", "INSIDE", modTime),
			}, []string{"served-root"}, fs.GetModifyWindow(ctx, outside))
		})
	}
}
```

（`bfs.Stat` が FS に無ければ `bfs.Lstat` を使う。v1.75.2 の `cmd/serve/nfs/filesystem.go` で名前を確かめる。）

- [ ] **Step 5: 失敗を確かめる**

```bash
GOTOOLCHAIN=go1.26.9 go test ./vfs/ -run TestKazLookupUnsafeName -count=1
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/nfs/ -run TestKazPathTraversalLookup -count=1
```

Expected: どちらも FAIL（cache off で `..` の作成が成功し、`Unexpected file ""` などが出る）。

- [ ] **Step 6: 修正する**

`vfs/dir_kaz_lookup.go` の `lookup` の先頭（`d.mu.RLock()` の前）に足す。import に `github.com/rclone/rclone/lib/sanitize`。

```go
	// A name which isn't a single safe path element can't be a child of
	// this directory; joining it would reach outside it (see checkName).
	if sanitize.Leaf(leaf) != nil {
		return nil, ENOENT
	}
```

`kazCreateNoLookup` の先頭に足す。

```go
	// Same refusal of unsafe names as Dir.Create.
	if err := d.checkName(name); err != nil {
		return nil, err
	}
```

`vfs/vfs.go` の O_CREATE|O_TRUNC の近道（`kazCreateNoLookup` を呼ぶ `if`）の条件に `leaf != ""` を足し、空の名前は通常の経路（upstream と同じ EPERM などのエラー）に回す。

- [ ] **Step 7: 通ることを確かめる**

```bash
GOTOOLCHAIN=go1.26.9 go test ./vfs/ ./cmd/serve/nfs/ -race -count=1
```

Expected: ok（upstream の `TestDirUnsafeName`・`TestPathTraversal` も含めて通る）。

- [ ] **Step 8: Commit**

```bash
git add vfs/dir_kaz_lookup.go vfs/vfs.go vfs/dir_kaz_lookup_test.go cmd/serve/nfs/kaz_filesystem_test.go
git commit -m "vfs: refuse unsafe names in kaz lookup mode like Dir.Create does"
```

---

### Task 2: v1.75.2 で増えた Stat の経路も「無い」だけを 404 にする

**Files:**
- Modify: `cmd/serve/s3/backend.go`（`CopyObject`）、`cmd/serve/s3/multipart.go`（`CreateMultipartUpload`）
- Test: `cmd/serve/s3/kaz_test.go`（`TestKazStatErrorsAreNot404`）

**Interfaces:**
- Consumes: Task 1 の `keyStatError`、`bucketStatError`、テスト補助 `newKazBackend(t, lookup bool, tweak func(*Options)) (*s3Backend, *failingFs, string)`、`failingFs.fail`、`errKaz`

- [ ] **Step 1: 失敗するテストを書く**

`TestKazStatErrorsAreNot404` の中で、GET・DELETE などを確かめているところ（`ff.fail.Store(true)` の後）に足す。

```go
			_, err = b.CopyObject(ctx, "bucket", "obj", "bucket", "copy", map[string]string{})
			assert.ErrorIs(t, err, errKaz, "CopyObject")
			assert.False(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey), "CopyObject must not be NoSuchKey")
			_, err = b.CreateMultipartUpload(ctx, "bucket", "mp", map[string]string{})
			assert.ErrorIs(t, err, errKaz, "CreateMultipartUpload")
			assert.False(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchBucket), "CreateMultipartUpload must not be NoSuchBucket")
```

（lookup=false のとき、bucket の Stat が dir cache で済んで List も NewObject も呼ばない場合がある。そのときに `errKaz` にならないなら、既存の GET の確かめ方に合わせて、`bucket` の dir cache を落とす既存の手順をそのまま使う。）

- [ ] **Step 2: 失敗を確かめる**

```bash
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -run TestKazStatErrorsAreNot404 -count=1
```

Expected: FAIL（CopyObject は NoSuchKey、CreateMultipartUpload は NoSuchBucket になる）。

- [ ] **Step 3: 直す**

`CopyObject` の Stat を分ける。

```go
	cStat, err := _vfs.Stat(fp)
	if err != nil {
		return result, keyStatError(srcKey, err)
	}
	if !cStat.IsFile() {
		return result, gofakes3.KeyNotFound(srcKey)
	}
```

`CreateMultipartUpload` の bucket の Stat:

```go
	if _, err := _vfs.Stat(bucketName); err != nil {
		return "", bucketStatError(bucketName, err)
	}
```

- [ ] **Step 4: 通ることを確かめる**

```bash
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -skip TestS3Minio -race -count=1
```

Expected: ok。

- [ ] **Step 5: Commit**

```bash
git add cmd/serve/s3/backend.go cmd/serve/s3/multipart.go cmd/serve/s3/kaz_test.go
git commit -m "serve s3: only report NoSuchKey/NoSuchBucket from CopyObject and CreateMultipartUpload when the path does not exist"
```

---

### Task 3: persist-metadata を載せ直す（tenant と遅延 Create に合わせて書き直す）

**Files:**
- Cherry-pick: 875d6df78 71638bb73 cf5d14328、書き直し 116f16f51、続けて 4f108ba16 0d9115bbb e1509bad8
- Modify: `cmd/serve/s3/backend.go`、`cmd/serve/s3/multipart.go`、`cmd/serve/s3/kaz_metadata.go`
- Test: `cmd/serve/s3/kaz_metadata_test.go`

**Interfaces:**
- Consumes: v1.75.2 の `(*tenant).storeMeta(_vfs *vfs.VFS, fp string, meta map[string]string) error`、`(*tenant).getMeta(ctx, fp string, node vfs.Node) (map[string]string, bool)`、`multipartUpload.sink()`・`close()`、kaz の `kazSetUploadMetadata(h vfs.Handle, meta map[string]string) error`、`kazObjectUserMetadata(ctx, node vfs.Node) (map[string]string, error)`
- Produces: `(*s3Backend).kazStoreMeta(t *tenant, v *vfs.VFS, fp string, meta map[string]string) error`、`(*s3Backend).kazUserMeta(ctx context.Context, fp string, node vfs.Node) (map[string]string, bool, error)`、`kazApplyModtime(v *vfs.VFS, fp string, meta map[string]string) error`、`(*multipartUpload).createSink() (vfs.Handle, error)`、フィールド `multipartUpload.kazPersistMeta bool`

- [ ] **Step 1: そのまま当たるコミットを当てる**

```bash
git switch -c port-1.75.2/kaz-s3-persist-metadata
git cherry-pick 875d6df78 71638bb73 cf5d14328
```

cf5d14328 の s3.go の衝突は、v1.75.2 で増えたオプション（`metadata_max_objects` など）の後ろに kaz の項目を足す形で解く。

- [ ] **Step 2: 116f16f51 を書き直して当てる**

```bash
git cherry-pick -n 116f16f51
```

衝突と、v1.75.2 に無い `b.meta`／`storeModtime` の参照を、次のとおりに直す。

(a) `kaz_metadata.go` に足す（`storeModtime` の代わり）。

```go
// kazStoreMeta stores meta for the object at fp like tenant.storeMeta, except
// that with --kaz-s3-persist-metadata the metadata already travelled with the
// upload, so only the modification time is applied and nothing is kept in
// memory.
func (b *s3Backend) kazStoreMeta(t *tenant, v *vfs.VFS, fp string, meta map[string]string) error {
	if !b.s.opt.KazPersistMetadata {
		return t.storeMeta(v, fp, meta)
	}
	return kazApplyModtime(v, fp, meta)
}

// kazApplyModtime sets the modification time of the object at fp from
// "X-Amz-Meta-Mtime" or "mtime" in meta, as tenant.storeMeta does.
func kazApplyModtime(v *vfs.VFS, fp string, meta map[string]string) error {
	for _, key := range []string{"X-Amz-Meta-Mtime", "mtime"} {
		val, ok := meta[key]
		if !ok {
			continue
		}
		ti, err := swift.FloatStringToTime(val)
		if err != nil {
			continue
		}
		return v.Chtimes(fp, ti, ti)
	}
	return nil
}
```

import に `github.com/ncw/swift/v2`（backend.go と同じ import パス）と `github.com/rclone/rclone/vfs` を足す。

(b) `HeadObject` と `GetObject` のメタデータ: upstream の行を1行だけ差し替える形にし、分岐は kaz_metadata.go に置く（次の upstream の変更で衝突する範囲を小さくし、getMeta の形が変わったらコンパイルエラーで気づけるようにする）。

kaz_metadata.go に足す。

```go
// kazUserMeta returns the user metadata of node, the object at fp: from the
// object itself with --kaz-s3-persist-metadata, otherwise what this user
// stored in memory (tenant.getMeta).
func (b *s3Backend) kazUserMeta(ctx context.Context, fp string, node vfs.Node) (map[string]string, bool, error) {
	if b.s.opt.KazPersistMetadata {
		user, err := kazObjectUserMetadata(ctx, node)
		return user, err == nil, err
	}
	meta, ok := b.tenant(ctx).getMeta(ctx, fp, node)
	return meta, ok, nil
}
```

HeadObject の upstream の

```go
	if metaMap, ok := b.tenant(ctx).getMeta(ctx, fp, node); ok {
		maps.Copy(meta, metaMap)
	}
```

を次にする（GetObject も同じ。GetObject では戻り値が名前付きの `err` なので、`metaMap, ok, kerr :=` とせず、既存の defer が本文を閉じるよう `err` に代入して返す）。

```go
	metaMap, ok, err := b.kazUserMeta(ctx, fp, node)
	if err != nil {
		return nil, err
	}
	if ok {
		maps.Copy(meta, metaMap)
	}
```

（HeadObject で `err` が既に宣言済みなら `:=` を `=` にし、`metaMap`・`ok` は `var` で宣言する。）

(c) `PutObject` の末尾: upstream の `return result, b.tenant(ctx).storeMeta(_vfs, fp, meta)` を `return result, b.kazStoreMeta(b.tenant(ctx), _vfs, fp, meta)` にする。kaz.1 の `if !b.s.opt.KazPersistMetadata { b.meta.Store(...) }` と mtime の2つの `if` は入れない。PutObject の中の `kazSetUploadMetadata` のブロック（`io.Copy` の前）は kaz の形のまま残す。

(d) `TouchObject` の `b.tenant(ctx).storeMeta(...)` も `b.kazStoreMeta(b.tenant(ctx), ...)` にする。

(e) `CopyObject`: 同じ key のとき（Task 2 で分けた Stat の後）。

```go
	if srcBucket == dstBucket && srcKey == dstKey {
		if b.s.opt.KazPersistMetadata {
			// Replacing only the metadata of a stored object is not supported.
			return result, gofakes3.ErrNotImplemented
		}
		return result, b.tenant(ctx).storeMeta(_vfs, fp, meta)
	}
```

(f) `multipart.go`: kaz.1 の CreateMultipartUpload で `_vfs.Create` してメタデータを付けるブロックは入れない（upstream は Create しない）。代わりに:

`multipartUpload` 構造体に足す。

```go
	kazPersistMeta bool // set the metadata on the sink before its first write (--kaz-s3-persist-metadata)
```

CreateMultipartUpload で `up.vfs = _vfs` の隣に `up.kazPersistMeta = b.s.opt.KazPersistMetadata`。

新しいメソッド（`sink()` の直前）:

```go
// createSink creates the handle the parts are written to. With
// --kaz-s3-persist-metadata the metadata travels with the upload, so it is
// set before the first write. Call with up.mu held.
func (up *multipartUpload) createSink() (vfs.Handle, error) {
	fh, err := up.vfs.Create(up.streamFp)
	if err != nil {
		return nil, err
	}
	if up.kazPersistMeta {
		if err := kazSetUploadMetadata(fh, up.meta); err != nil {
			// Abandon the upload instead of committing an empty object.
			if aborter, ok := fh.(interface{ CloseWithError(error) error }); ok {
				_ = aborter.CloseWithError(err)
			} else {
				_ = fh.Close()
			}
			return nil, err
		}
	}
	return fh, nil
}
```

`sink()` と `close()` の `fh, err := up.vfs.Create(up.streamFp)` の2か所を `fh, err := up.createSink()` にする（`up.vfs.Create` の戻り値の型が `vfs.Handle` であることを確かめる。違えば createSink の戻り値の型を合わせる）。

(g) `CompleteMultipartUpload`: upstream の `_ = up.tenant.storeMeta(up.vfs, up.fp, up.meta)` を `_ = b.kazStoreMeta(up.tenant, up.vfs, up.fp, up.meta)` にする。kaz.1 の `b.meta.Store`・`storeModtime` の部分は入れない。

(h) テスト（kaz_metadata_test.go）の `a.meta.Range(...)` による「b.meta が空」の確認を、tenant の LRU に直す（2か所）。

```go
	assert.Zero(t, a.tenant(ctx).meta.Len(), "tenant metadata must stay empty")
```

`newKazPersistBackend` の `return newBackend(w)` は `return w.backend` にする。

- [ ] **Step 3: テストを流して commit**

```bash
GOTOOLCHAIN=go1.26.9 go build ./cmd/serve/s3/
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -skip TestS3Minio -race -count=1
git commit -C 116f16f51
```

Expected: ok（`TestKazPersistMultipart` を含む。xattr が使えない tmp では一部 skip になるので、skip の数も記録する）。

- [ ] **Step 4: 残りを当てる**

```bash
git cherry-pick 4f108ba16 0d9115bbb e1509bad8
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ ./vfs/ ./backend/drive/ -skip TestS3Minio -race -count=1
```

Expected: ok。

---

### Task 4: persist と upstream の multipart・LRU の組み合わせのテスト

**Files:**
- Test: `cmd/serve/s3/kaz_metadata_test.go`

**Interfaces:**
- Consumes: Task 3 の `createSink`、`kazStoreMeta`、テスト補助 `newKazPersistBackend(t, root string, dirCacheTime time.Duration) *s3Backend`、`kazRequireXattrs(t, dir string)`、upstream の `newMultipartTestServerVFS(t, backing string, disableStreaming bool, tweak func(*Options), vfsOpt *vfscommon.Options, disableFeatures ...string) (*minio.Core, fs.Fs, string)`

- [ ] **Step 1: テストを書く**

kaz_metadata_test.go の末尾に足す。既存の `TestKazPersistMultipart` と同じく、upstream のテスト補助 `newMultipartTestServerVFS`（multipart_test.go、minio の Core で HTTP 越しに送る）を使う。bucket は `root` の下の `test-N` ディレクトリとして作られる。

```go
// kazPersistMultipartServer starts a server with --kaz-s3-persist-metadata
// and --vfs-cache-mode off on root, returning the client and bucket name.
func kazPersistMultipartServer(t *testing.T, root string) (*minio.Core, string) {
	t.Helper()
	vfsOpt := vfscommon.Opt
	vfsOpt.CacheMode = vfscommon.CacheModeOff
	core, _, bucket := newMultipartTestServerVFS(t, root, false, func(o *Options) { o.KazPersistMetadata = true }, &vfsOpt)
	return core, bucket
}

// TestKazPersistMultipartOutOfOrder checks the metadata reaches the object
// when a later part arrives first and is buffered before the sink exists.
func TestKazPersistMultipartOutOfOrder(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	core, bucket := kazPersistMultipartServer(t, root)
	uploadID, err := core.NewMultipartUpload(ctx, bucket, "mp", minio.PutObjectOptions{UserMetadata: map[string]string{"crc32c": "42"}})
	require.NoError(t, err)
	part1 := []byte(random.String(5 * 1024 * 1024))
	part2 := []byte("tail")
	p2, err := core.PutObjectPart(ctx, bucket, "mp", uploadID, 2, bytes.NewReader(part2), int64(len(part2)), minio.PutObjectPartOptions{})
	require.NoError(t, err)
	p1, err := core.PutObjectPart(ctx, bucket, "mp", uploadID, 1, bytes.NewReader(part1), int64(len(part1)), minio.PutObjectPartOptions{})
	require.NoError(t, err)
	_, err = core.CompleteMultipartUpload(ctx, bucket, "mp", uploadID, []minio.CompletePart{{PartNumber: 1, ETag: p1.ETag}, {PartNumber: 2, ETag: p2.ETag}}, minio.PutObjectOptions{})
	require.NoError(t, err)

	obj, err := newKazPersistBackend(t, root, time.Hour+3*time.Second).HeadObject(ctx, bucket, "mp")
	require.NoError(t, err)
	assert.Equal(t, "42", obj.Metadata["X-Amz-Meta-Crc32c"])
	assert.Equal(t, int64(len(part1)+len(part2)), obj.Size)
}

// TestKazPersistMultipartNoParts checks completing an upload with no parts
// creates an empty object which still carries the metadata.
func TestKazPersistMultipartNoParts(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	core, bucket := kazPersistMultipartServer(t, root)
	uploadID, err := core.NewMultipartUpload(ctx, bucket, "empty", minio.PutObjectOptions{UserMetadata: map[string]string{"crc32c": "0"}})
	require.NoError(t, err)
	_, err = core.CompleteMultipartUpload(ctx, bucket, "empty", uploadID, nil, minio.PutObjectOptions{})
	require.NoError(t, err)

	obj, err := newKazPersistBackend(t, root, time.Hour+3*time.Second).HeadObject(ctx, bucket, "empty")
	require.NoError(t, err)
	assert.Equal(t, "0", obj.Metadata["X-Amz-Meta-Crc32c"])
	assert.Zero(t, obj.Size)
}

// TestKazPersistMultipartCreateMakesNothing checks that, as upstream
// requires, creating an upload with --kaz-s3-persist-metadata makes nothing
// on the remote until the first part arrives.
func TestKazPersistMultipartCreateMakesNothing(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	core, bucket := kazPersistMultipartServer(t, root)
	_, err := core.NewMultipartUpload(ctx, bucket, "d/mp", minio.PutObjectOptions{UserMetadata: map[string]string{"crc32c": "1"}})
	require.NoError(t, err)
	var files []string
	require.NoError(t, filepath.WalkDir(filepath.Join(root, bucket), func(p string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			files = append(files, p)
		}
		return err
	}))
	assert.Empty(t, files, "no object may exist before the first part")
}

// TestKazPersistCopySameKey checks a metadata-only copy is refused with 501
// and a copy of a missing object is NoSuchKey, and nothing is kept in the
// tenant's metadata store.
func TestKazPersistCopySameKey(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket"), 0777))
	a := newKazPersistBackend(t, root, time.Hour)
	_, err := a.PutObject(ctx, "bucket", "k", map[string]string{"X-Amz-Meta-Crc32c": "3"}, strings.NewReader("abc"), 3)
	require.NoError(t, err)
	_, err = a.CopyObject(ctx, "bucket", "k", "bucket", "k", map[string]string{"X-Amz-Meta-Crc32c": "4"})
	assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNotImplemented), "same key copy: %v", err)
	_, err = a.CopyObject(ctx, "bucket", "missing", "bucket", "missing", map[string]string{})
	assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey), "missing source: %v", err)
	assert.Zero(t, a.tenant(ctx).meta.Len(), "tenant metadata must stay empty")
}
```

（gofakes3 が最後以外の part に最小サイズを求めるので、part 1 は 5 MiB にしている。`CompleteMultipartUpload` に空の part の一覧を渡せない場合は、minio の Core が送る XML を確かめ、upstream の multipart_test.go の「part が0個の Complete」のテストと同じ送り方にする。）

- [ ] **Step 2: テストを流す**

```bash
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -run 'TestKazPersist' -race -count=1 -v 2>&1 | grep -E '^(=== RUN|--- |ok|FAIL|PASS)'
```

Expected: すべて PASS（xattr が無い環境では skip）。FAIL したら Task 3 の (f) を見直す（テストを弱めない）。

※ 本来は Task 3 の前に書いて失敗を見るのが TDD の順だが、Task 3 の前は `b.meta` の参照でパッケージがコンパイルできないので、このタスクは Task 3 の後に置いている。テストが本当に効いていることは、Step 2 の後に `createSink` の `if up.kazPersistMeta` を一時的に外して `TestKazPersistMultipartOutOfOrder` と `NoParts` が FAIL することを確かめ、元に戻して確認する（この一時変更は commit しない）。

- [ ] **Step 3: Commit**

```bash
git add cmd/serve/s3/kaz_metadata_test.go
git commit -m "serve s3: test --kaz-s3-persist-metadata with streamed multipart uploads"
```

---

### Task 5: cancel-get を載せ直す

**Files:**
- Cherry-pick: 72dfdb817
- Test: `cmd/serve/s3/kaz_get_test.go`

- [ ] **Step 1: 当てる**

```bash
git switch -c port-1.75.2/kaz-s3-cancel-get
git cherry-pick 72dfdb817
```

衝突の解き方: s3.go のオプション表は、persist の項目の後ろに足す。backend.go の GetObject は、upstream の `hash := getFileHashByte(node, b.s.etagHash(_vfs))` を残し、kaz の `rdr`／`rnge` の組み立て（直接 open の経路と通常の経路）を入れる。kaz の `getFileHashByte(node, b.s.etagHashType)` は入れない。

- [ ] **Step 2: テスト**

```bash
GOTOOLCHAIN=go1.26.9 go vet ./cmd/serve/s3/
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -skip TestS3Minio -race -count=1
```

Expected: ok（`kaz_get_test.go` を含む）。`grep -n etagHashType cmd/serve/s3/*.go` で、kaz のコードが古い `etagHashType` を新しく使っていないことも確かめる（upstream が残している使い方は除く）。

- [ ] **Step 3: commit を確かめる**

衝突を解いた場合は `git cherry-pick --continue`（元のメッセージ・author のまま）。

---

### Task 6: 一覧は upstream に任せ、取り消しとログを足す

**Files:**
- Modify: `cmd/serve/s3/s3.go`（オプション）、`cmd/serve/s3/list.go`（ctx とログ）、`cmd/serve/s3/backend.go`（`listPage` の呼び出し）
- Create: `cmd/serve/s3/kaz_list_test.go`（調査で移したテストをもとにする）

**Interfaces:**
- Consumes: v1.75.2 の `lister`、`newLister`、`(*s3Backend).listPage`、Task 1 の `newKazBackend`、`failingFs.onList`
- Produces: `listPage(ctx context.Context, _vfs *vfs.VFS, bucket, fdPath, name string, addPrefix bool, page gofakes3.ListBucketPage, response *gofakes3.ObjectList) error`

- [ ] **Step 1: 受け付けるだけのオプションを足す**

```bash
git switch -c port-1.75.2/kaz-s3-list
```

s3.go の kaz の項目の最後に足す。

```go
}, {
	Name:    "kaz_s3_list_by_key_order",
	Default: false,
	Help:    "[kaz] Accepted for compatibility and ignored: since rclone v1.75.2 ListObjects always walks the directories in S3 key order and stops when the page is full",
	Groups:  "Kaz",
```

`Options` に `KazListByKeyOrder bool \`config:"kaz_s3_list_by_key_order"\`` を足す（どこからも読まない。読まないことをフィールドのコメントに書く）。

- [ ] **Step 2: テストを置く**

`docs/inspection/rclone_v1752_port/2026-10-13/kazport_list_test.go.txt`（調査リポジトリ）の中身を `cmd/serve/s3/kaz_list_test.go` として置き、次のとおり直す。
- 調査用に複製した `failingFs`・`errKaz`・`newKazBackend` は消す（Task 1 で kaz_test.go に入っている）。
- lookup=false だけにしていたところは、`for _, lookup := range []bool{false, true}` に戻す（元の kaz_list_test.go と同じ）。
- `kazByKeyOrder` の tweak は使わない（オプションは何もしない）。
- `TestKazListStopsOnCancel` を入れる（元の kaz_list_test.go の 245 行目からの関数を、`kazByKeyOrder` を外してそのまま移す）。
- ファイル先頭のコメントを「upstream の lazy listing（ee40507d3）を、kaz の要件（キーの順、必要な分だけ読む、取り消し）について確かめるテスト」にする。

```bash
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -run TestKazList -skip TestS3Minio -count=1
```

Expected: `TestKazListStopsOnCancel` だけ FAIL（取り消し後もディレクトリを読み続ける）。他は PASS。

- [ ] **Step 3: ctx を渡し、取り消しで止める**

list.go の `lister` にフィールドを足す。

```go
	ctx       context.Context // the request, whose cancellation stops the walk
	started   time.Time       // when the page was started, for debug logging
```

`newLister` に `ctx context.Context` を先頭の引数で足し、`ctx: ctx, started: time.Now()` を入れる。`list()` の先頭（`bucketDirPath` の前）に足す。

```go
	// Stop before reading another directory once the client has gone.
	if err := l.ctx.Err(); err != nil {
		return err
	}
```

`listPage` の先頭の引数に `ctx context.Context` を足して `newLister(ctx, b, ...)` に渡す。ログの行を kaz.2 と同じ情報にする（本番の確認手順が `kaz list:` を探すため）。

```go
	fs.Debugf("serve s3", "Listed %d keys from %d directories (truncated=%v)", l.keys, l.dirsRead, response.IsTruncated)
	fs.Debugf(bucket, "kaz list: prefix=%q marker=%q entries=%d truncated=%v dirs=%d took=%v",
		path.Join(fdPath, name), page.Marker, l.keys, response.IsTruncated, l.dirsRead, time.Since(l.started))
```

backend.go の `ListBucket` の呼び出しを `b.listPage(ctx, _vfs, ...)` にする。upstream の list_test.go に `listPage(`／`newLister(` の直接の呼び出しがあれば、`context.Background()` を足す。import に `context`、`time`。

- [ ] **Step 4: 通ることを確かめる**

```bash
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ -skip TestS3Minio -race -count=1
```

Expected: ok（upstream の list_test.go と kaz_list_test.go の両方）。

- [ ] **Step 5: Commit**

```bash
git add cmd/serve/s3/s3.go cmd/serve/s3/list.go cmd/serve/s3/backend.go cmd/serve/s3/kaz_list_test.go cmd/serve/s3/list_test.go
git commit -m "serve s3: stop listings on cancel, log kaz list pages and keep --kaz-s3-list-by-key-order as a no-op"
```

（list_test.go を変えていなければ add から外す。）

---

### Task 7: upstream のテストを kaz モードで流す仕組みと、移植の手引き

今後の upstream の改修に強くするためのタスク。kaz の新しい経路が upstream の修正（とくにセキュリティ修正）を迂回しても、テストで分かるようにする。調査では、v1.75.2 の vfs のテスト全体を lookup モードを既定にして流すと、unsafe な名前の修正（Task 1）を入れた後で落ちるのは、次の4件だけだった（どれも lookup モードの仕様上の違い）。

| テスト | 落ちる理由 |
|---|---|
| `TestDirForgetAll`、`TestDirForgetPath` | lookup モードの Stat はディレクトリを一覧しないので、item がキャッシュに載る前提が成り立たない |
| `TestCaseSensitivity`、`TestUnicodeNormalization` | キャッシュに無い名前は NewObject で完全一致を引くので、大文字小文字・Unicode の正規化での一致を探さない（本番は JuiceFS の ASCII の key だけで影響なし） |

**Files:**
- Create: `vfs/kaz_mode_test.go`、`cmd/serve/nfs/kaz_mode_test.go`、`cmd/serve/s3/kaz_mode_test.go`
- Create: `KAZ.md`（rclone リポジトリのルート。kaz のブランチにだけ置く）
- ブランチ: `port-1.75.2/kaz-s3-list` の続きに commit する（merge の対象に含める）

- [ ] **Step 1: build tag で lookup モードを既定にするファイルを置く**

3つのパッケージに、それぞれ同じ形で置く（package 名だけ変える）。

```go
//go:build kazlookup

package vfs

import "github.com/rclone/rclone/vfs/vfscommon"

// init turns on --kaz-vfs-lookup-by-path for every VFS the upstream tests of
// this package create, so `go test -tags kazlookup` checks that the kaz
// lookup paths keep the behaviour (and the security fixes) upstream tests.
func init() { vfscommon.Opt.KazLookupByPath = true }
```

- [ ] **Step 2: 流して、落ちるテストが上の4件だけであることを確かめる**

```bash
GOTOOLCHAIN=go1.26.9 go test -tags kazlookup ./vfs/ ./cmd/serve/nfs/ ./cmd/serve/s3/ -skip TestS3Minio -count=1 2>&1 | grep -E '^(--- FAIL|ok|FAIL)'
GOTOOLCHAIN=go1.26.9 go test -tags kazlookup ./vfs/ ./cmd/serve/nfs/ ./cmd/serve/s3/ -skip 'TestS3Minio|TestDirForgetAll|TestDirForgetPath|TestCaseSensitivity|TestUnicodeNormalization' -race -count=1
```

Expected: 1つ目で FAIL は上の4件だけ。2つ目は ok。cmd/serve/s3 で別のテストが落ちたら、理由を調べ、lookup モードの仕様上の違いなら表に足し、そうでなければ直す（ユーザーに報告）。

- [ ] **Step 3: `KAZ.md`（移植の手引き）を書く**

英語ではなく日本語でよい（kaz のブランチだけのファイル）。内容:
- kaz の機能ごとに、触っている upstream の関数（フック点）と、頼っている upstream の性質。例: lookup は `Dir.stat`・`Dir.checkName`・`vfs.OpenFile` の O_TRUNC の近道・`Dir.cacheCleanup`/`ForgetAll`。persist は `tenant.storeMeta`/`getMeta`、`multipartUpload.sink()`/`close()` が最初の part で Create すること、`vfs` の `WriteFileHandle` がメタデータを Rcat に渡すこと。cancel-get は `GetObject` の本文の組み立てと `etagHash`。一覧は upstream の `lister`（ee40507d3）とその `list()` の先頭の ctx の確認。エラーは `bucketStatError`/`keyStatError` を使う Stat の呼び出し箇所の一覧（`grep -n 'StatError(' cmd/serve/s3/*.go` の結果）。
- それぞれの性質を守っているテストの名前（`TestKazLookupUnsafeName`、`TestKazPathTraversalLookup`、`TestKazPersistMultipart*`、`TestKazListStopsOnCancel`、`TestKazStatErrorsAreNot404` など）。
- 次の版への移し方: 今回の手順（新しいベースのブランチ → 機能ごとの cherry-pick → `-tags kazlookup` での upstream のテスト → merge）、upstream の差分で必ず見るもの（`git log --oneline <旧>..<新> -- cmd/serve/s3 vfs backend/drive` の GHSA 付きのコミットと、上のフック点の関数の変更）、`kazlookup` で落ちてよいテストの表。
- 新しい Stat の経路が増えたら、`gofakes3.BucketNotFound`/`KeyNotFound` を直接返していないかを `grep -n 'NotFound(' cmd/serve/s3/*.go` で見ること。

- [ ] **Step 4: Commit**

```bash
git add vfs/kaz_mode_test.go cmd/serve/nfs/kaz_mode_test.go cmd/serve/s3/kaz_mode_test.go KAZ.md
git commit -m "kaz: run upstream tests in kaz lookup mode with -tags kazlookup and document how to port"
```

---

### Task 8: 全体の確認とビルド

**Files:** なし（確認だけ）

- [ ] **Step 1: 差分の全体を見る**

```bash
git log --oneline v1.75.2..port-1.75.2/kaz-s3-list
git diff --stat v1.75.2 port-1.75.2/kaz-s3-list
git diff 1.75.1-improve-kaz port-1.75.2/kaz-s3-list -- vfs/dir_kaz_lookup.go vfs/kaz_upload_metadata.go backend/drive/kaz_properties.go cmd/serve/s3/kaz_get.go cmd/serve/s3/kaz_errors.go
```

Expected: 最後の diff は、Task 1 の unsafe な名前の修正だけ（他の kaz 独自ファイルは kaz.2 と同じ）。`cmd/serve/s3/kaz_list.go` と `pager.go` は存在しない。

- [ ] **Step 2: vet とテスト**

```bash
GOTOOLCHAIN=go1.26.9 go vet ./cmd/serve/s3/ ./cmd/serve/nfs/ ./vfs/... ./backend/drive/ ./fs/config/flags/
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ ./cmd/serve/nfs/ ./vfs/... ./backend/drive/ ./fs/config/flags/ ./lib/cache/ -skip TestS3Minio -race -count=1
```

Expected: すべて ok。Task 0 の基準から失敗が増えていない。

```bash
GOTOOLCHAIN=go1.26.9 go test -tags kazlookup ./vfs/ ./cmd/serve/nfs/ ./cmd/serve/s3/ -skip 'TestS3Minio|TestDirForgetAll|TestDirForgetPath|TestCaseSensitivity|TestUnicodeNormalization' -race -count=1
```

Expected: ok（Task 7 と同じ）。

- [ ] **Step 3: ビルドとヘルプ**

```bash
GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 go build -trimpath -ldflags "-s -X github.com/rclone/rclone/fs.Version=v1.75.2-kaz-dev.$(git rev-parse --short=8 HEAD)" -o /tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/61f1e8d5-b3c8-4c40-9d3c-2aa4c676dd40/scratchpad/rclone-kaz-1752 .
B=/tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/61f1e8d5-b3c8-4c40-9d3c-2aa4c676dd40/scratchpad/rclone-kaz-1752
$B version
$B serve s3 --help | grep -A1 -- '--kaz-'
$B help backend drive | grep -A1 kaz
sha256sum $B
```

Expected: `rclone v1.75.2-kaz-dev.…`、go1.26.9。kaz のフラグ4つ（`--kaz-s3-persist-metadata`、`--kaz-s3-cancel-get-on-disconnect`、`--kaz-s3-list-by-key-order`、`--kaz-vfs-lookup-by-path`）と `--drive-kaz-properties` が出る。

- [ ] **Step 4: 本番の起動オプションで起動できること**

本番と同じ kaz のオプションで、local の一時ディレクトリを serve して起動・停止だけを確かめる（Drive には繋がない）。

```bash
D=$(mktemp -d -p /tmp/claude-1000/-home-kwatanabe-tmp-local-juicefs-inspection/61f1e8d5-b3c8-4c40-9d3c-2aa4c676dd40/scratchpad); mkdir $D/bucket
$B serve s3 $D --addr 127.0.0.1:19090 --kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --kaz-s3-cancel-get-on-disconnect --kaz-s3-list-by-key-order --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off &
P=$!; sleep 2; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:19090/; kill $P
```

Expected: 起動エラーが無く、HTTP の応答コードが返る（403 などでよい）。

---

### Task 9: `1.75.2-improve-kaz` へ merge（ユーザーの承認の後）

- [ ] **Step 1: ユーザーに、Task 8 の結果とコミットの一覧を見せて、merge の承認を得る。**

- [ ] **Step 2: 順に merge する**

```bash
git switch 1.75.2-improve-kaz
git merge --no-ff port-1.75.2/kaz-vfs-lookup -m "Merge branch 'port-1.75.2/kaz-vfs-lookup' into 1.75.2-improve-kaz"
git merge --no-ff port-1.75.2/kaz-s3-persist-metadata -m "Merge branch 'port-1.75.2/kaz-s3-persist-metadata' into 1.75.2-improve-kaz"
git merge --no-ff port-1.75.2/kaz-s3-cancel-get -m "Merge branch 'port-1.75.2/kaz-s3-cancel-get' into 1.75.2-improve-kaz"
git merge --no-ff port-1.75.2/kaz-s3-list -m "Merge branch 'port-1.75.2/kaz-s3-list' into 1.75.2-improve-kaz"
git diff port-1.75.2/kaz-s3-list 1.75.2-improve-kaz   # 何も出ないこと
GOTOOLCHAIN=go1.26.9 go test ./cmd/serve/s3/ ./vfs/ -skip TestS3Minio -count=1
```

- [ ] **Step 3: 記録**

調査リポジトリの `agent_memo.md` と `TODO.md` に、各ブランチの先端、merge commit、元のコミットとの対応（Task 1〜6 の cherry-pick の元の SHA と、書き直した 2 つ）を書く。push（`kaz` リモートへの `1.75.2-improve-kaz`）はユーザーの確認を得てから。

---

### Task 10: 実 Drive での確認とリリースの準備（ユーザーの承認の後）

- [ ] **Step 1: 実 Drive での確認（ユーザーの承認が要る）**

前回の手順（`docs/inspection/rclone_dir_cache/2026-10-08/drive-verification-ja.md`、`phase2-drive-verification-ja.md`）と同じく、`gdrive_kwatan:/rclone-s3-test` だけを使い、Task 8 のバイナリで次を確かめる。終わったらテスト用フォルダを purge する。
- 2つの serve s3（lookup あり）で、他方が PUT した object がすぐ読める、DELETE で消える。
- persist: 一括の PUT と multipart の PUT（一時名から最終名への Rename の後）で、Drive の properties `s3m-crc32c` が残り、他方の HEAD で返る。
- 一覧: `--log-level DEBUG` で `kaz list:` の行が出て、`took=` が分かる。
- 記録は `docs/inspection/rclone_v1752_port/<日付>/drive-verification-ja.md`。

- [ ] **Step 2: リリースの準備（調査リポジトリ）**

- `release/rclone/versions.json` に足す（commit は Task 9 の `1.75.2-improve-kaz` の先端の 40 桁）。

```json
  "rclone-v1.75.2-kaz.1": {
    "repo": "tongsama/rclone",
    "commit": "<1.75.2-improve-kaz の先端の 40 桁>",
    "go_version": "1.26.9",
    "notes": "release/rclone/notes/rclone-v1.75.2-kaz.1.md"
  }
```

- `release/rclone/notes/rclone-v1.75.2-kaz.1.md` を書く。内容: ベースが公式 v1.75.2 になったこと（upstream のセキュリティ修正を含む）、Go 1.26.9、kaz.2 の機能はすべて同じオプションで使えること、一覧は upstream の方式になり `--kaz-s3-list-by-key-order` は何もしない（取り消しと `kaz list:` のログは kaz で足した）、lookup モードの unsafe な名前の拒否、CopyObject・CreateMultipartUpload のエラーの 404／500、persist の multipart が最初の part で Drive への書き込みを始めるようになったこと、本番の起動オプションは kaz.2 と同じでよいこと、確認したこと（テスト、実 Drive）。
- commit・push・タグはユーザーの確認を得てから（文書の commit とリリースの commit を分ける方針に従う）。
