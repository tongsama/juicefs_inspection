# rclone serve s3 のユーザーメタデータを Drive に保存（Phase 2）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `rclone serve s3` が受け取った `X-Amz-Meta-*` を、メモリではなく backend の object（Drive では properties）に PUT と同時に保存し、どのホストからでも・再起動後も HEAD・GET で返す。

**Architecture:** VFS の cache-mode off の書き込みハンドルに「アップロード時のメタデータ」を渡す口を足し、`operations.Rcat` にメタデータと `ci.Metadata=true` の ctx を渡す。Drive backend には `--drive-kaz-properties` を足し、properties だけを取得・保持し、上書き時に今回のメタデータに無い既存の properties を null で消す。serve s3 は `--kaz-s3-persist-metadata` で、`X-Amz-Meta-<Name>` ↔ property `s3m-<name>` を変換して書き、HEAD・GET では object のメタデータから返す。

**Tech Stack:** Go 1.26.0（`GOTOOLCHAIN=auto`）、rclone vfs・backend/drive・backend/local（テストで xattr を使う）・cmd/serve/s3、gofakes3 v0.0.9、google.golang.org/api v0.279.0（drive/v3、`NullFields`）、testify。

**Spec:** [docs/superpowers/specs/2026-10-08-rclone-s3-persist-metadata-design.md](../specs/2026-10-08-rclone-s3-persist-metadata-design.md)

## Global Constraints

- 作業ディレクトリは `rclone/`（独立した git リポジトリ）。以下のパスと Go コマンドは `rclone/` を基準にする。
- 作業ブランチ: `1.75.1-improve-kaz`（dd03d0243）から `feat/kaz-s3-persist-metadata` を切る。merge と push はユーザーの承認を得てから。
- commit はタスクごとに行ってよい（ユーザーの許可は Phase 1 と同じ扱いで、計画の承認時に確認する）。push しない。commit メッセージに attribution 行を付けない。commit の件名は rclone の規約（`vfs: ...`、`drive: ...`、`serve s3: ...`）。
- オプション: `--kaz-s3-persist-metadata`（serve s3、config 名 `kaz_s3_persist_metadata`、既定 false、Groups に `Kaz`、ヘルプの先頭 `[kaz] `）と `--drive-kaz-properties`（Drive backend、config 名 `kaz_properties`、既定 false、Advanced、ヘルプの先頭 `[kaz] `）。
- 両方が false のとき、今の動作とまったく同じでなければならない。
- キーの対応: `X-Amz-Meta-<Name>` → property `s3m-<strings.ToLower(Name)>`。読み出しは `s3m-<name>` → `http.CanonicalHeaderKey("X-Amz-Meta-" + name)`。`s3m-` で始まらないものは返さない。`X-Amz-Meta-` で始まらないヘッダは保存しない。
- 保存の失敗・メタデータの取得の失敗を隠さない（エラーを返す）。
- コメントは rclone/AGENTS.md に従う（英語の godoc、名前で始める、現在の動作を書く、変更の経緯を書かない）。
- `cmd/serve/s3` のパッケージ全体のテストは `-skip TestS3Minio`（docker の結合テストが変更前から失敗する）。
- 本番の rclone、Drive の `/rclone-s3`、JuiceFS の metadata には触らない。実際の Drive での確認（Task 6）は、テスト用フォルダの作成・削除のたびにユーザーの確認を取る。

## Review Focus

1. PUT の途中でクライアントの送信が失敗したとき → メタデータだけが残ったり、古い object のメタデータが壊れたりしないこと（Task 4 `TestKazPersistFailedPutKeepsOld`）。
2. `X-Amz-Meta-` の大文字小文字が混ざったヘッダ（`x-amz-meta-CRC32C` など）→ `s3m-crc32c` に正規化され、読み出しで1件として返ること（Task 3 の変換テスト）。
3. メタデータ無しで PUT し直したとき → 以前のメタデータが返らないこと（Task 2 の null テスト、Task 4 の上書きテスト）。
4. 自ホストで PUT した直後の HEAD → メタデータが返ること（Task 4 `TestKazPersistAcrossHosts` の最初の確認）。
5. メタデータの無い既存の object → 取得・上書き・削除・再取得ができること（Task 4 `TestKazPersistLegacyObject`、ユーザー要望）。

## ファイル構成

| ファイル | 種別 | 責務 |
|---|---|---|
| `vfs/write.go` | 変更 | `WriteFileHandle.uploadMeta`、`SetKazUploadMetadata`、`openPending` でメタデータと ctx を渡す |
| `vfs/kaz_upload_metadata.go` | 新規 | `KazUploadMetadataSetter` インターフェース |
| `vfs/kaz_upload_metadata_test.go` | 新規 | VFS のテスト（local backend の xattr で確かめる） |
| `backend/drive/drive.go` | 変更 | オプション `kaz_properties`、`getFileFields`、`newBaseObject`、アップロードの fields、`Object.Update` |
| `backend/drive/kaz_properties.go` | 新規 | `kazUploadFields`、`kazUserMetadata`、`kazNullStaleProperties` |
| `backend/drive/kaz_properties_test.go` | 新規 | Drive のオフラインの単体テスト |
| `cmd/serve/s3/s3.go` | 変更 | オプション `kaz_s3_persist_metadata` |
| `cmd/serve/s3/server.go` | 変更 | 起動時の cache-mode の確認 |
| `cmd/serve/s3/kaz_metadata.go` | 新規 | キーの変換、書き込みハンドルへの設定、object からの読み出し |
| `cmd/serve/s3/backend.go`、`multipart.go` | 変更 | PUT・multipart・HEAD・GET・Copy での使い分け |
| `cmd/serve/s3/kaz_metadata_test.go` | 新規 | serve s3 のテスト |

---

### Task 0: 作業ブランチと基準のテスト

- [ ] **Step 1:**

```bash
cd rclone
git switch 1.75.1-improve-kaz && git status --short && git log --oneline -1
git switch -c feat/kaz-s3-persist-metadata
```

Expected: `git status` が空、HEAD は dd03d0243。

- [ ] **Step 2: 基準のテスト**

Run: `go test ./vfs/... ./backend/drive/ ./backend/local/ 2>&1 | grep -v "no test files" | tail; go test ./cmd/serve/s3/ -skip TestS3Minio 2>&1 | tail -2`
Expected: すべて ok（`backend/drive` のテストは remote が無いとオフラインのものだけ走る。失敗するものがあれば変更前からの失敗として記録する）。

- [ ] **Step 3: xattr の確認**（Task 1・4 のテストの前提）

Run: `cd $(mktemp -d) && touch f && setfattr -n user.k -v v f && getfattr -n user.k f; echo rc=$?`（`setfattr` が無ければ `python3 -c "import os;open('f','w');os.setxattr('f','user.k',b'v');print(os.getxattr('f','user.k'))"`）
Expected: 値 `v` が読める。読めなければユーザーに報告する（Task 1・4 のテストは xattr が無い環境では `t.Skip` する）。

---

### Task 1: VFS の書き込みハンドルにアップロード時のメタデータを渡す

**Files:** Create `vfs/kaz_upload_metadata.go`、`vfs/kaz_upload_metadata_test.go`。Modify `vfs/write.go`（`WriteFileHandle` 構造体、`openPending`）。

**Interfaces:**
- Produces: `type KazUploadMetadataSetter interface { SetKazUploadMetadata(meta fs.Metadata) error }`（`vfs` パッケージ、exported）。`*WriteFileHandle` が実装する。最初の書き込み（`openPending`）の後に呼ぶと `ErrKazUploadStarted` を返す。

- [ ] **Step 1: 失敗するテスト** `vfs/kaz_upload_metadata_test.go`:

```go
package vfs

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fstest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// requireUserXattrs skips the test when the test remote is not local or the
// filesystem behind it cannot store user xattrs, which the local backend uses
// for user metadata.
func requireUserXattrs(t *testing.T, r *fstest.Run) {
	t.Helper()
	if r.Fremote.Features().UserMetadata == false {
		t.Skip("remote has no user metadata support")
	}
	ctx := context.Background()
	obj := r.WriteObject(ctx, "kaz-xattr-probe", "x", t1)
	o, err := r.Fremote.NewObject(ctx, obj.Path)
	require.NoError(t, err)
	setter, ok := o.(fs.SetMetadataer)
	if !ok {
		t.Skip("object cannot set metadata")
	}
	if err := setter.SetMetadata(ctx, fs.Metadata{"kaz-probe": "1"}); err != nil {
		t.Skipf("user xattrs not supported here: %v", err)
	}
	m, err := fs.GetMetadata(ctx, o)
	require.NoError(t, err)
	if m["kaz-probe"] != "1" {
		t.Skip("user xattrs not read back here")
	}
	require.NoError(t, o.Remove(ctx))
}

// TestKazUploadMetadataReachesBackend checks metadata set on a write handle
// before the first write is stored with the uploaded object.
func TestKazUploadMetadataReachesBackend(t *testing.T) {
	r, v := newTestVFS(t)
	requireUserXattrs(t, r)
	ctx := context.Background()

	fd, err := v.OpenFile("meta.txt", os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
	require.NoError(t, err)
	setter, ok := fd.(KazUploadMetadataSetter)
	require.True(t, ok, "cache-mode off write handle must accept upload metadata")
	require.NoError(t, setter.SetKazUploadMetadata(fs.Metadata{"s3m-crc32c": "12345"}))
	_, err = fd.Write([]byte("hello"))
	require.NoError(t, err)
	require.NoError(t, fd.Close())

	o, err := r.Fremote.NewObject(ctx, "meta.txt")
	require.NoError(t, err)
	m, err := fs.GetMetadata(ctx, o)
	require.NoError(t, err)
	assert.Equal(t, "12345", m["s3m-crc32c"])
}

// TestKazUploadMetadataUnsetUnchanged checks an upload without metadata does
// not store any kaz user metadata.
func TestKazUploadMetadataUnsetUnchanged(t *testing.T) {
	r, v := newTestVFS(t)
	requireUserXattrs(t, r)
	ctx := context.Background()

	fd, err := v.OpenFile("plain.txt", os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
	require.NoError(t, err)
	_, err = fd.Write([]byte("hello"))
	require.NoError(t, err)
	require.NoError(t, fd.Close())

	o, err := r.Fremote.NewObject(ctx, "plain.txt")
	require.NoError(t, err)
	m, err := fs.GetMetadata(ctx, o)
	require.NoError(t, err)
	_, found := m["s3m-crc32c"]
	assert.False(t, found)
}

// TestKazUploadMetadataTooLate checks setting metadata after the upload has
// started is refused rather than silently ignored.
func TestKazUploadMetadataTooLate(t *testing.T) {
	_, v := newTestVFS(t)
	fd, err := v.OpenFile("late.txt", os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0666)
	require.NoError(t, err)
	_, err = fd.Write([]byte("x"))
	require.NoError(t, err)
	err = fd.(KazUploadMetadataSetter).SetKazUploadMetadata(fs.Metadata{"s3m-a": "b"})
	assert.True(t, errors.Is(err, ErrKazUploadStarted), "got %v", err)
	require.NoError(t, fd.Close())
}
```

注意: `Features().UserMetadata` の正確なフィールド名は `fs/features.go` で確かめる（無ければ `WriteMetadata` などで代える）。xattr の probe は、テスト用 remote が local 以外（`-remote` 指定時）や xattr 非対応の環境で `t.Skip` するためのもの。

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./vfs/ -run 'TestKazUploadMetadata' -v 2>&1 | tail -20`
Expected: コンパイルエラー（`KazUploadMetadataSetter`、`ErrKazUploadStarted` が無い）。

- [ ] **Step 3: 実装**

`vfs/kaz_upload_metadata.go`:

```go
package vfs

import (
	"errors"

	"github.com/rclone/rclone/fs"
)

// ErrKazUploadStarted is returned by SetKazUploadMetadata once the upload of
// the handle has started, as the metadata can then no longer be sent with it.
var ErrKazUploadStarted = errors.New("vfs: upload already started, metadata must be set before the first write")

// KazUploadMetadataSetter is implemented by write handles which can send
// metadata with the upload of the file (cache mode off only).
//
// The metadata is passed to the backend as if --metadata were set for this
// upload only. It must be set before the first write.
type KazUploadMetadataSetter interface {
	SetKazUploadMetadata(meta fs.Metadata) error
}

// check interface
var _ KazUploadMetadataSetter = (*WriteFileHandle)(nil)

// SetKazUploadMetadata sets the metadata sent with the upload of this handle.
//
// It returns ErrKazUploadStarted if the upload has already started.
func (fh *WriteFileHandle) SetKazUploadMetadata(meta fs.Metadata) error {
	fh.mu.Lock()
	defer fh.mu.Unlock()
	if fh.opened {
		return ErrKazUploadStarted
	}
	fh.uploadMeta = meta
	return nil
}
```

`vfs/write.go` の `WriteFileHandle` 構造体に追加:

```go
	truncated   bool
	uploadMeta  fs.Metadata // metadata sent with the upload, see SetKazUploadMetadata
}
```

`openPending` の Rcat の呼び出しを次のようにする:

```go
		// NB Rcat deals with Stats.Transferring, etc.
		ctx := fh.ctx
		if fh.uploadMeta != nil {
			// The backend only sends metadata when --metadata is set, so
			// enable it for this upload alone.
			var ci *fs.ConfigInfo
			ctx, ci = fs.AddConfig(ctx)
			ci.Metadata = true
		}
		o, err = operations.Rcat(ctx, fh.file.Fs(), fh.remote, pipeReader, time.Now(), fh.uploadMeta)
```

注意: `openPending` は lock を持って呼ばれ、goroutine は `fh.uploadMeta` を読むだけ（goroutine 開始前に値が決まっている）。

- [ ] **Step 4: 通ることを確かめる**

Run: `go test -race ./vfs/ -run 'TestKazUploadMetadata' -v 2>&1 | tail -20; go test ./vfs/... 2>&1 | grep -v "no test files" | tail -5`
Expected: 3件 PASS（xattr が使えない環境では2件 SKIP）。他も ok。

- [ ] **Step 5: commit**

```bash
git add vfs/kaz_upload_metadata.go vfs/kaz_upload_metadata_test.go vfs/write.go
git commit -m "vfs: allow sending metadata with an upload from a write handle"
```

---

### Task 2: Drive `--drive-kaz-properties`

**Files:** Create `backend/drive/kaz_properties.go`、`backend/drive/kaz_properties_test.go`。Modify `backend/drive/drive.go`（オプション一覧の末尾 `env_auth` の後、`Options` 構造体、`baseObject` 構造体、`newBaseObject`、`getFileFields`、`PutUnchecked` の `Fields(partialFields)`、`baseObject.update` の `Fields(partialFields)`、`Object.Update`）、`backend/drive/upload.go`（`"fields": {partialFields}`）。

**Interfaces:**
- Produces: `Options.KazProperties bool`（`config:"kaz_properties"`）。`baseObject.kazProperties map[string]string`（object 生成時の properties）。`func (f *Fs) kazUploadFields() string`、`func kazUserMetadata(info *drive.File) *fs.Metadata`、`func kazNullStaleProperties(updateInfo *drive.File, existing map[string]string)`。

- [ ] **Step 1: 失敗するテスト** `backend/drive/kaz_properties_test.go`（remote 不要のオフラインのテスト）:

```go
package drive

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/rclone/rclone/fs"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	drive "google.golang.org/api/drive/v3"
)

// TestKazFieldsIncludeProperties checks properties are requested for listings
// and uploads only when kaz_properties is set.
func TestKazFieldsIncludeProperties(t *testing.T) {
	ctx := context.Background()
	f := &Fs{}
	assert.NotContains(t, string(f.getFileFields(ctx)), "properties")
	assert.Equal(t, partialFields, f.kazUploadFields())

	f.opt.KazProperties = true
	assert.Contains(t, strings.Split(string(f.getFileFields(ctx)), ","), "properties")
	assert.Contains(t, strings.Split(f.kazUploadFields(), ","), "properties")
}

// TestKazNewBaseObjectKeepsProperties checks an object made from a listing
// with properties answers Metadata from them without an API call.
func TestKazNewBaseObjectKeepsProperties(t *testing.T) {
	ctx := context.Background()
	f := &Fs{}
	f.opt.KazProperties = true
	info := &drive.File{Id: "id1", Name: "a", Properties: map[string]string{"s3m-crc32c": "42"}}
	o, err := f.newBaseObject(ctx, "a", info)
	require.NoError(t, err)
	m, err := o.Metadata(ctx) // must not call the API (f.svc is nil)
	require.NoError(t, err)
	assert.Equal(t, "42", m["s3m-crc32c"])
	assert.Equal(t, map[string]string{"s3m-crc32c": "42"}, o.kazProperties)

	// No properties: empty metadata, still no API call.
	o, err = f.newBaseObject(ctx, "b", &drive.File{Id: "id2", Name: "b"})
	require.NoError(t, err)
	m, err = o.Metadata(ctx)
	require.NoError(t, err)
	assert.Empty(t, m)
}

// TestKazNullStaleProperties checks an update sends null for existing
// properties which the new metadata no longer has, and keeps the rest.
func TestKazNullStaleProperties(t *testing.T) {
	updateInfo := &drive.File{Properties: map[string]string{"s3m-new": "1"}}
	kazNullStaleProperties(updateInfo, map[string]string{"s3m-old": "x", "s3m-new": "0"})
	body, err := json.Marshal(updateInfo)
	require.NoError(t, err)
	var got struct {
		Properties map[string]*string `json:"properties"`
	}
	require.NoError(t, json.Unmarshal(body, &got))
	require.Contains(t, got.Properties, "s3m-old")
	assert.Nil(t, got.Properties["s3m-old"], "stale key must be sent as null")
	require.NotNil(t, got.Properties["s3m-new"])
	assert.Equal(t, "1", *got.Properties["s3m-new"])

	// Nothing new at all: every existing key is nulled and properties is sent.
	updateInfo = &drive.File{}
	kazNullStaleProperties(updateInfo, map[string]string{"s3m-old": "x"})
	body, err = json.Marshal(updateInfo)
	require.NoError(t, err)
	assert.Contains(t, string(body), `"properties":{"s3m-old":null}`)
}

// keep fs imported for helpers used in later assertions
var _ = fs.Metadata{}
```

（最後の `var _` は、実装でテストに `fs` が不要になれば import ごと消してよい。）

- [ ] **Step 2: 失敗を確かめる**

Run: `go test ./backend/drive/ -run 'TestKaz' -v 2>&1 | tail -20`
Expected: コンパイルエラー（`KazProperties`、`kazUploadFields` などが無い）。

- [ ] **Step 3: 実装**

オプション（`drive.go` の `env_auth` の要素の後、`}}...),` の前）:

```go
		}, {
			Name: "kaz_properties",
			Help: `[kaz] Read and keep the user properties of files without --metadata.

Properties are fetched with listings, lookups and uploads at no extra API
cost and returned as the object's metadata. When an upload is made with
metadata, existing properties missing from it are deleted so the
properties match the new upload (as an S3 PUT replaces user metadata).`,
			Default:  false,
			Advanced: true,
```

`Options` に `KazProperties bool \`config:"kaz_properties"\`` を加える（`EnvAuth` の後）。`baseObject` に `kazProperties map[string]string // user properties when kaz_properties is set` を加える。

`backend/drive/kaz_properties.go`:

```go
package drive

import (
	"maps"

	"github.com/rclone/rclone/fs"
	drive "google.golang.org/api/drive/v3"
)

// kazUploadFields returns the fields requested from upload responses, adding
// properties when kaz_properties is set so a new object knows its metadata.
func (f *Fs) kazUploadFields() string {
	if f.opt.KazProperties {
		return partialFields + ",properties"
	}
	return partialFields
}

// kazUserMetadata returns the user properties of info as metadata, empty but
// non-nil when there are none so Metadata does not fetch them again.
func kazUserMetadata(info *drive.File) *fs.Metadata {
	m := make(fs.Metadata, len(info.Properties))
	maps.Copy(m, info.Properties)
	return &m
}

// kazNullStaleProperties makes updateInfo delete the existing properties
// which it does not set, so the properties match the new upload.
func kazNullStaleProperties(updateInfo *drive.File, existing map[string]string) {
	for k := range existing {
		if _, ok := updateInfo.Properties[k]; ok {
			continue
		}
		updateInfo.NullFields = append(updateInfo.NullFields, "Properties."+k)
	}
	if len(updateInfo.NullFields) > 0 {
		// An empty map is otherwise left out of the request with its nulls.
		updateInfo.ForceSendFields = append(updateInfo.ForceSendFields, "Properties")
	}
}
```

`getFileFields` の末尾（`ci.Metadata` の分岐の後）:

```go
	if fs.GetConfig(ctx).Metadata {
		fields += "," + metadataFields
	} else if f.opt.KazProperties {
		fields += ",properties"
	}
	return fields
```

`newBaseObject` の末尾を次のようにする:

```go
	err = nil
	if f.opt.KazProperties {
		o.kazProperties = info.Properties
	}
	if fs.GetConfig(ctx).Metadata {
		err = o.parseMetadata(ctx, info)
	} else if f.opt.KazProperties {
		o.metadata = kazUserMetadata(info)
	}
	return o, err
```

アップロードの fields: `PutUnchecked` の `Files.Create(...).Fields(partialFields)` と `baseObject.update` の `Files.Update(...).Fields(partialFields)` を `.Fields(googleapi.Field(f.kazUploadFields()))`（update は `o.fs.kazUploadFields()`）にする。`upload.go` の `"fields": {partialFields}` を `"fields": {f.kazUploadFields()}` にする。

`Object.Update` の `fetchAndUpdateMetadata` の直後に:

```go
	if o.fs.opt.KazProperties && fs.GetConfig(ctx).Metadata {
		// Replace the user properties as a whole, as an S3 PUT does.
		kazNullStaleProperties(updateInfo, o.kazProperties)
	}
```

注意: `NullFields` の項目名は Go のフィールド名 `Properties` を使う（google.golang.org/api の gensupport/json.go:31-42）。`Fields(...)` の引数の型（`googleapi.Field` か string か）は呼び出し側に合わせる。

- [ ] **Step 4: 通ることを確かめる**

Run: `go test -race ./backend/drive/ -run 'TestKaz' -v 2>&1 | tail -20; go test ./backend/drive/ 2>&1 | tail -3; go vet ./backend/drive/`
Expected: PASS、既存も ok。

- [ ] **Step 5: commit**

```bash
git add backend/drive/kaz_properties.go backend/drive/kaz_properties_test.go backend/drive/drive.go backend/drive/upload.go
git commit -m "drive: add --drive-kaz-properties to keep user properties as metadata"
```

---

### Task 3: serve s3 のオプション、起動時の確認、キーの変換

**Files:** Create `cmd/serve/s3/kaz_metadata.go`、`cmd/serve/s3/kaz_metadata_test.go`。Modify `cmd/serve/s3/s3.go`（OptionsInfo の末尾、Options）、`cmd/serve/s3/server.go`（`newServer`）。

**Interfaces:**
- Produces: `Options.KazPersistMetadata bool`（`config:"kaz_s3_persist_metadata"`）。`func kazToProperties(meta map[string]string) fs.Metadata`、`func kazFromProperties(m fs.Metadata) map[string]string`、`func kazObjectUserMetadata(ctx context.Context, node vfs.Node) (map[string]string, error)`、`func kazSetUploadMetadata(h vfs.Handle, meta map[string]string) error`。

- [ ] **Step 1: 失敗するテスト** `cmd/serve/s3/kaz_metadata_test.go`:

```go
package s3

import (
	"context"
	"errors"
	"testing"

	"github.com/rclone/rclone/cmd/serve/proxy"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/vfs/vfscommon"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestKazToProperties checks only X-Amz-Meta-* headers are kept, prefixed
// and lower cased.
func TestKazToProperties(t *testing.T) {
	got := kazToProperties(map[string]string{
		"X-Amz-Meta-Crc32c":    "123",
		"x-amz-meta-Mixed-Key": "v",
		"X-Amz-Date":           "20261008T000000Z",
		"Content-Type":         "text/plain",
		"Last-Modified":        "Thu, 08 Oct 2026 00:00:00 GMT",
		"mtime":                "1.5",
	})
	assert.Equal(t, fs.Metadata{"s3m-crc32c": "123", "s3m-mixed-key": "v"}, got)
	assert.Nil(t, kazToProperties(map[string]string{"X-Amz-Date": "x"}), "no user metadata → nil")
}

// TestKazFromProperties checks only s3m-* entries come back as canonical
// X-Amz-Meta-* headers.
func TestKazFromProperties(t *testing.T) {
	got := kazFromProperties(fs.Metadata{"s3m-crc32c": "123", "s3m-mixed-key": "v", "mtime": "x", "owner": "y"})
	assert.Equal(t, map[string]string{"X-Amz-Meta-Crc32c": "123", "X-Amz-Meta-Mixed-Key": "v"}, got)
}

// TestKazPersistNeedsCacheModeOff checks the server refuses to start with
// --kaz-s3-persist-metadata unless the VFS cache mode is off.
func TestKazPersistNeedsCacheModeOff(t *testing.T) {
	ctx := context.Background()
	f, err := fs.NewFs(ctx, t.TempDir())
	require.NoError(t, err)
	opt := Opt
	opt.HTTP.ListenAddr = []string{endpoint}
	opt.KazPersistMetadata = true
	vfsOpt := vfscommon.Opt
	vfsOpt.CacheMode = vfscommon.CacheModeWrites
	_, err = newServer(ctx, f, &opt, &vfsOpt, &proxy.Opt)
	assert.Error(t, err)

	vfsOpt.CacheMode = vfscommon.CacheModeOff
	w, err := newServer(ctx, f, &opt, &vfsOpt, &proxy.Opt)
	require.NoError(t, err)
	_ = w.Shutdown()
}

var errKazMeta = errors.New("kaz: metadata read failed")

// kazFakeNode is a vfs.Node whose DirEntry is a fixed object; no other method
// is used by kazObjectUserMetadata.
type kazFakeNode struct {
	vfs.Node
	o fs.Object
}

// DirEntry returns the fixed object.
func (n kazFakeNode) DirEntry() fs.DirEntry {
	if n.o == nil {
		return nil
	}
	return n.o
}

// kazFailingMetaObject is an object whose metadata cannot be read.
type kazFailingMetaObject struct{ fs.Object }

// Metadata always fails.
func (kazFailingMetaObject) Metadata(context.Context) (fs.Metadata, error) { return nil, errKazMeta }

// TestKazObjectUserMetadataErrors checks a failure to read metadata is
// returned (HEAD/GET then answer 500) and a node without an object gives none.
func TestKazObjectUserMetadataErrors(t *testing.T) {
	ctx := context.Background()
	_, err := kazObjectUserMetadata(ctx, kazFakeNode{o: kazFailingMetaObject{}})
	assert.True(t, errors.Is(err, errKazMeta), "got %v", err)
	m, err := kazObjectUserMetadata(ctx, kazFakeNode{})
	require.NoError(t, err)
	assert.Empty(t, m)
}
```

（import に `github.com/rclone/rclone/vfs` を加える。）

- [ ] **Step 2: 失敗を確かめる** — Run: `go test ./cmd/serve/s3/ -run 'TestKazToProperties|TestKazFromProperties|TestKazPersistNeedsCacheModeOff|TestKazObjectUserMetadataErrors' -v 2>&1 | tail`。Expected: コンパイルエラー。

- [ ] **Step 3: 実装**

`s3.go` の OptionsInfo の `multipart_expiry` の後に:

```go
}, {
	Name:    "kaz_s3_persist_metadata",
	Default: false,
	Help:    "[kaz] Store X-Amz-Meta-* user metadata with the object in the backend (as s3m-* metadata) instead of in memory. Needs --vfs-cache-mode off; use --drive-kaz-properties on drive",
	Groups:  "Kaz",
}}.
```

`Options` に `KazPersistMetadata bool \`config:"kaz_s3_persist_metadata"\`` を加える。注意: serve s3 の OptionsInfo の他の項目に Groups が無いので、`Groups: "Kaz"` で `installFlag` が分類を探すときに問題が出ないか（serve s3 のフラグは command 用の FlagSet なので分類は使われない見込み）を `go run . serve s3 --help` で確かめる。

`server.go` の `newServer` の、`w.opt` を設定した後の早い位置に:

```go
	if opt.KazPersistMetadata && vfsOpt.CacheMode != vfscommon.CacheModeOff {
		// Cached uploads are written back later without the metadata.
		return nil, errors.New("--kaz-s3-persist-metadata needs --vfs-cache-mode off")
	}
```

（`errors` と `vfscommon` の import を確かめる。`newServer` の `vfsOpt` が nil のことがあるなら nil を `vfscommon.Opt` として扱う。）

`kaz_metadata.go`:

```go
package s3

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/vfs"
)

const (
	// kazAmzMetaPrefix is the canonical prefix of S3 user metadata headers.
	kazAmzMetaPrefix = "X-Amz-Meta-"
	// kazPropertyPrefix prefixes user metadata stored in the backend so it
	// never collides with metadata names the backend interprets itself.
	kazPropertyPrefix = "s3m-"
)

// kazToProperties returns the X-Amz-Meta-* entries of meta as backend
// metadata named s3m-<lower case name>, or nil if there are none.
func kazToProperties(meta map[string]string) fs.Metadata {
	var out fs.Metadata
	for k, v := range meta {
		if len(k) <= len(kazAmzMetaPrefix) || !strings.EqualFold(k[:len(kazAmzMetaPrefix)], kazAmzMetaPrefix) {
			continue
		}
		if out == nil {
			out = fs.Metadata{}
		}
		out[kazPropertyPrefix+strings.ToLower(k[len(kazAmzMetaPrefix):])] = v
	}
	return out
}

// kazFromProperties returns the s3m-* entries of m as X-Amz-Meta-* headers.
func kazFromProperties(m fs.Metadata) map[string]string {
	out := map[string]string{}
	for k, v := range m {
		name, ok := strings.CutPrefix(k, kazPropertyPrefix)
		if !ok || name == "" {
			continue
		}
		out[http.CanonicalHeaderKey(kazAmzMetaPrefix+name)] = v
	}
	return out
}

// kazSetUploadMetadata sends the user metadata of meta with the upload made
// through h. It fails if the handle cannot carry metadata, so metadata is
// never dropped silently.
func kazSetUploadMetadata(h vfs.Handle, meta map[string]string) error {
	setter, ok := h.(vfs.KazUploadMetadataSetter)
	if !ok {
		return errors.New("serve s3: this upload cannot store metadata (needs --vfs-cache-mode off)")
	}
	props := kazToProperties(meta)
	if props == nil {
		// Still mark the upload as carrying metadata so a backend which
		// replaces properties on update drops stale ones.
		props = fs.Metadata{}
	}
	return setter.SetKazUploadMetadata(props)
}

// kazObjectUserMetadata returns the X-Amz-Meta-* headers stored with the
// object behind node. A node still uploading or a backend without metadata
// support gives none; a failure to read the metadata is returned.
func kazObjectUserMetadata(ctx context.Context, node vfs.Node) (map[string]string, error) {
	o, ok := node.DirEntry().(fs.Object)
	if !ok || o == nil {
		return map[string]string{}, nil
	}
	// Ask with --metadata set so a backend fetching it on demand does not
	// cache an empty result.
	ctx, ci := fs.AddConfig(ctx)
	ci.Metadata = true
	m, err := fs.GetMetadata(ctx, o)
	if err != nil {
		return nil, err
	}
	return kazFromProperties(m), nil
}
```

注意: `fs.GetMetadata` の正確な名前とシグネチャを fs/metadata.go で確かめる。`SetKazUploadMetadata(fs.Metadata{})`（空でも非 nil）で `ci.Metadata=true` の上書きになり、Drive は §4.3 のとおり既存の properties をすべて null にする — これが「メタデータ無しで PUT し直すと以前のメタデータが消える」（Review Focus 3）を満たす。

- [ ] **Step 4: 通ることを確かめる** — Run: `go test -race ./cmd/serve/s3/ -run 'TestKazToProperties|TestKazFromProperties|TestKazPersistNeedsCacheModeOff|TestKazObjectUserMetadataErrors' -v 2>&1 | tail; go run . serve s3 --help 2>&1 | grep -n "kaz-s3-persist-metadata"`。Expected: PASS、フラグが出る。

- [ ] **Step 5: commit** — `git add cmd/serve/s3/kaz_metadata.go cmd/serve/s3/kaz_metadata_test.go cmd/serve/s3/s3.go cmd/serve/s3/server.go && git commit -m "serve s3: add --kaz-s3-persist-metadata option and metadata mapping"`

---

### Task 4: serve s3 の書き込みと読み出しを切り替える

**Files:** Modify `cmd/serve/s3/backend.go`（PutObject、HeadObject、GetObject、CopyObject、storeModtime）、`cmd/serve/s3/multipart.go`（CreateMultipartUpload、CompleteMultipartUpload）。Test: `cmd/serve/s3/kaz_metadata_test.go`（追記）。

**Interfaces:**
- Consumes: Task 1 の `vfs.KazUploadMetadataSetter`、Task 3 の `kazSetUploadMetadata`、`kazObjectUserMetadata`、`Options.KazPersistMetadata`。

- [ ] **Step 1: 失敗するテスト**（追記。import に `os`、`path/filepath`、`strings`、`io`、`time`、`gofakes3`、`fstest` を加える）:

```go
// newKazPersistBackend serves root (a local directory containing "bucket")
// with --kaz-s3-persist-metadata. Each call makes a VFS of its own (distinct
// dirCacheTime), so two backends on one root act as two hosts, or as one
// host before and after a restart.
func newKazPersistBackend(t *testing.T, root string, dirCacheTime time.Duration) *s3Backend {
	t.Helper()
	fstest.Initialise()
	ctx := context.Background()
	f, err := fs.NewFs(ctx, root)
	require.NoError(t, err)
	vfsOpt := vfscommon.Opt
	vfsOpt.CacheMode = vfscommon.CacheModeOff
	vfsOpt.KazLookupByPath = true
	vfsOpt.DirCacheTime = fs.Duration(dirCacheTime)
	vfsOpt.PollInterval = 0
	opt := Opt
	opt.HTTP.ListenAddr = []string{endpoint}
	opt.KazPersistMetadata = true
	w, err := newServer(ctx, f, &opt, &vfsOpt, &proxy.Opt)
	require.NoError(t, err)
	t.Cleanup(func() { _ = w.Shutdown() })
	return newBackend(w)
}

// kazRequireXattrs skips when the temp filesystem cannot hold user xattrs.
func kazRequireXattrs(t *testing.T, dir string) {
	t.Helper()
	p := filepath.Join(dir, "kaz-probe")
	require.NoError(t, os.WriteFile(p, []byte("x"), 0666))
	defer func() { _ = os.Remove(p) }()
	if err := kazSetXattr(p); err != nil {
		t.Skipf("user xattrs not supported: %v", err)
	}
}

// TestKazPersistAcrossHosts checks metadata PUT on host A is returned by
// HEAD and GET on A, on host B, and on A after a restart, and that the
// in-memory store stays empty.
func TestKazPersistAcrossHosts(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket"), 0777))
	a := newKazPersistBackend(t, root, time.Hour)
	b := newKazPersistBackend(t, root, time.Hour+time.Second)

	_, err := a.PutObject(ctx, "bucket", "chunks/0/1/1_0_3", map[string]string{"X-Amz-Meta-Crc32c": "777", "X-Amz-Date": "d"}, strings.NewReader("abc"), 3)
	require.NoError(t, err)

	for name, be := range map[string]*s3Backend{"A": a, "B": b, "A-restarted": newKazPersistBackend(t, root, time.Hour+2*time.Second)} {
		obj, err := be.HeadObject(ctx, "bucket", "chunks/0/1/1_0_3")
		require.NoError(t, err, name)
		assert.Equal(t, "777", obj.Metadata["X-Amz-Meta-Crc32c"], name)
		_, hasDate := obj.Metadata["X-Amz-Date"]
		assert.False(t, hasDate, name+": request headers must not be stored")
		obj, err = be.GetObject(ctx, "bucket", "chunks/0/1/1_0_3", nil)
		require.NoError(t, err, name)
		_ = obj.Contents.Close()
		assert.Equal(t, "777", obj.Metadata["X-Amz-Meta-Crc32c"], name)
	}
	n := 0
	a.meta.Range(func(any, any) bool { n++; return true })
	assert.Zero(t, n, "b.meta must stay empty")
}

// TestKazPersistOverwriteReplaces checks a new PUT replaces the metadata:
// keys it does not send are gone.
func TestKazPersistOverwriteReplaces(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket"), 0777))
	a := newKazPersistBackend(t, root, time.Hour)
	_, err := a.PutObject(ctx, "bucket", "k", map[string]string{"X-Amz-Meta-Old": "1", "X-Amz-Meta-Crc32c": "1"}, strings.NewReader("one"), 3)
	require.NoError(t, err)
	_, err = a.PutObject(ctx, "bucket", "k", map[string]string{"X-Amz-Meta-Crc32c": "2"}, strings.NewReader("two"), 3)
	require.NoError(t, err)
	obj, err := newKazPersistBackend(t, root, time.Hour+time.Second).HeadObject(ctx, "bucket", "k")
	require.NoError(t, err)
	assert.Equal(t, "2", obj.Metadata["X-Amz-Meta-Crc32c"])
	_, hasOld := obj.Metadata["X-Amz-Meta-Old"]
	assert.False(t, hasOld)

	// A PUT without user metadata leaves none behind.
	_, err = a.PutObject(ctx, "bucket", "k", map[string]string{}, strings.NewReader("three"), 5)
	require.NoError(t, err)
	obj, err = newKazPersistBackend(t, root, time.Hour+2*time.Second).HeadObject(ctx, "bucket", "k")
	require.NoError(t, err)
	_, hasCrc := obj.Metadata["X-Amz-Meta-Crc32c"]
	assert.False(t, hasCrc)
}

// TestKazPersistLegacyObject checks an object stored without metadata (as by
// an older rclone) can be read, overwritten with metadata, deleted and is
// then gone.
func TestKazPersistLegacyObject(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket", "d"), 0777))
	require.NoError(t, os.WriteFile(filepath.Join(root, "bucket", "d", "old"), []byte("legacy"), 0666))
	a := newKazPersistBackend(t, root, time.Hour)

	obj, err := a.HeadObject(ctx, "bucket", "d/old")
	require.NoError(t, err)
	assert.Equal(t, int64(6), obj.Size)
	_, has := obj.Metadata["X-Amz-Meta-Crc32c"]
	assert.False(t, has)
	obj, err = a.GetObject(ctx, "bucket", "d/old", nil)
	require.NoError(t, err)
	data, err := io.ReadAll(obj.Contents)
	_ = obj.Contents.Close()
	require.NoError(t, err)
	assert.Equal(t, "legacy", string(data))

	_, err = a.PutObject(ctx, "bucket", "d/old", map[string]string{"X-Amz-Meta-Crc32c": "9"}, strings.NewReader("new!"), 4)
	require.NoError(t, err)
	obj, err = a.GetObject(ctx, "bucket", "d/old", nil)
	require.NoError(t, err)
	data, err = io.ReadAll(obj.Contents)
	_ = obj.Contents.Close()
	require.NoError(t, err)
	assert.Equal(t, "new!", string(data))
	assert.Equal(t, "9", obj.Metadata["X-Amz-Meta-Crc32c"])

	require.NoError(t, a.deleteObject(ctx, "bucket", "d/old"))
	_, err = a.HeadObject(ctx, "bucket", "d/old")
	assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey))
	_, err = a.GetObject(ctx, "bucket", "d/old", nil)
	assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNoSuchKey))
}

// TestKazPersistFailedPutKeepsOld checks a PUT whose body fails part way
// leaves the previous object and its metadata in place.
func TestKazPersistFailedPutKeepsOld(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket"), 0777))
	a := newKazPersistBackend(t, root, time.Hour)
	_, err := a.PutObject(ctx, "bucket", "k", map[string]string{"X-Amz-Meta-Crc32c": "1"}, strings.NewReader("one"), 3)
	require.NoError(t, err)
	_, err = a.PutObject(ctx, "bucket", "k", map[string]string{"X-Amz-Meta-Crc32c": "2"}, &errorReader{data: []byte("tw"), err: errBoom}, 3)
	require.Error(t, err)
	obj, err := newKazPersistBackend(t, root, time.Hour+time.Second).GetObject(ctx, "bucket", "k", nil)
	require.NoError(t, err)
	data, _ := io.ReadAll(obj.Contents)
	_ = obj.Contents.Close()
	assert.Equal(t, "one", string(data))
	assert.Equal(t, "1", obj.Metadata["X-Amz-Meta-Crc32c"])
}

// TestKazPersistMultipart checks metadata given when a multipart upload is
// created is stored with the completed object, also after it is moved from
// its temporary name.
func TestKazPersistMultipart(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	vfsOpt := vfscommon.Opt
	vfsOpt.CacheMode = vfscommon.CacheModeOff
	core, _, bucket := newMultipartTestServerVFS(t, root, false, func(o *Options) { o.KazPersistMetadata = true }, &vfsOpt)
	uploadID, err := core.NewMultipartUpload(ctx, bucket, "mp", minio.PutObjectOptions{UserMetadata: map[string]string{"crc32c": "55"}})
	require.NoError(t, err)
	data := []byte(random.String(1024))
	p, err := core.PutObjectPart(ctx, bucket, "mp", uploadID, 1, bytes.NewReader(data), int64(len(data)), minio.PutObjectPartOptions{})
	require.NoError(t, err)
	_, err = core.CompleteMultipartUpload(ctx, bucket, "mp", uploadID, []minio.CompletePart{{PartNumber: 1, ETag: p.ETag}}, minio.PutObjectOptions{})
	require.NoError(t, err)

	obj, err := newKazPersistBackend(t, root, time.Hour+3*time.Second).HeadObject(ctx, bucket, "mp")
	require.NoError(t, err)
	assert.Equal(t, "55", obj.Metadata["X-Amz-Meta-Crc32c"])
}

// TestKazPersistCopySameKeyNotImplemented checks a metadata-only copy onto
// the same key is refused instead of being silently ignored.
func TestKazPersistCopySameKeyNotImplemented(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	kazRequireXattrs(t, root)
	require.NoError(t, os.MkdirAll(filepath.Join(root, "bucket"), 0777))
	a := newKazPersistBackend(t, root, time.Hour)
	_, err := a.PutObject(ctx, "bucket", "k", map[string]string{}, strings.NewReader("one"), 3)
	require.NoError(t, err)
	_, err = a.CopyObject(ctx, "bucket", "k", "bucket", "k", map[string]string{"X-Amz-Meta-A": "b"})
	assert.True(t, gofakes3.HasErrorCode(err, gofakes3.ErrNotImplemented), "got %v", err)
}
```

import には `bytes`、`github.com/minio/minio-go/v7`（`minio`）、`github.com/rclone/rclone/lib/random` も加える（multipart_test.go と同じもの）。`kazSetXattr(path string) error` はテスト用の小さな関数として同じファイルに書く（`golang.org/x/sys/unix.Setxattr` か `github.com/pkg/xattr` の `xattr.Set(path, "user.kaz-probe", []byte("1"))`。後者は rclone の依存に既にある）。

- [ ] **Step 2: 失敗を確かめる** — Run: `go test ./cmd/serve/s3/ -run 'TestKazPersist' -v 2>&1 | tail -40`。Expected: ほとんどが FAIL（メタデータが Drive／local に保存されない、`b.meta` に入る、Copy が 501 にならない）。

- [ ] **Step 3: 実装**

PutObject（backend.go）: `f, err := _vfs.Create(tmpFp)` の直後に:

```go
	if b.s.opt.KazPersistMetadata {
		if err := kazSetUploadMetadata(f, meta); err != nil {
			_ = f.Close()
			cleanup()
			return result, err
		}
	}
```

（`f.Close()` が空のアップロードを確定してしまわないかを確かめる。確定してしまうなら、`CloseWithError` が使えればそれで中止する — 既存の失敗時の処理と同じ形にする。）

同じ関数の `b.meta.Store(fp, meta)` を `if !b.s.opt.KazPersistMetadata { b.meta.Store(fp, meta) }` にする。`storeModtime` の先頭に:

```go
	if b.s.opt.KazPersistMetadata {
		// User metadata is stored with the object, not in memory.
		return
	}
```

（Chtimes の呼び出しはそのまま残す。）

CreateMultipartUpload（multipart.go）: `fh, err := _vfs.Create(streamFp)` の直後に、PutObject と同じく `kazSetUploadMetadata(fh, meta)`（失敗したら fh を中止して返す）。CompleteMultipartUpload の `b.meta.Store(up.fp, up.meta)` も `!b.s.opt.KazPersistMetadata` のときだけにする。

HeadObject と GetObject: `if val, ok := b.meta.Load(fp); ok { ... maps.Copy(meta, metaMap) }` を次のようにする:

```go
	if b.s.opt.KazPersistMetadata {
		user, err := kazObjectUserMetadata(ctx, node)
		if err != nil {
			return nil, err
		}
		maps.Copy(meta, user)
	} else if val, ok := b.meta.Load(fp); ok {
		metaMap := val.(map[string]string)
		maps.Copy(meta, metaMap)
	}
```

CopyObject: 同じ key の分岐の先頭に:

```go
	if srcBucket == dstBucket && srcKey == dstKey {
		if b.s.opt.KazPersistMetadata {
			// Replacing only the metadata of a stored object is not supported.
			return result, gofakes3.ErrNotImplemented
		}
		b.meta.Store(fp, meta)
```

- [ ] **Step 4: 通ることを確かめる** — Run: `go test -race ./cmd/serve/s3/ -run 'TestKaz|TestNoCleanup' -v 2>&1 | tail -40; go test ./cmd/serve/s3/ -skip TestS3Minio 2>&1 | tail -2`。Expected: すべて PASS（xattr 非対応の環境では SKIP）。既存のテストも ok。

- [ ] **Step 5: commit** — `git add cmd/serve/s3/ && git commit -m "serve s3: store user metadata with the object when --kaz-s3-persist-metadata is set"`

---

### Task 5: 全体のテストとビルド

- [ ] **Step 1:** `go test -race ./vfs/... ./backend/drive/ ./backend/local/ ./fs/config/flags/ 2>&1 | grep -v "no test files" | tail -10` と `go test -race ./cmd/serve/s3/ -skip TestS3Minio 2>&1 | tail -3`。Expected: すべて ok。
- [ ] **Step 2:** `go vet ./vfs/ ./backend/drive/ ./cmd/serve/s3/` と `gofmt -l vfs backend/drive cmd/serve/s3`。Expected: 指摘なし。
- [ ] **Step 3:** 検証用のバイナリをビルドする（scratchpad へ）: `go build -o <scratchpad>/rclone-kaz . && <scratchpad>/rclone-kaz serve s3 --help | grep -E 'kaz-s3-persist-metadata|kaz-vfs-lookup-by-path' && <scratchpad>/rclone-kaz help backend drive | grep -n kaz-properties`。sha256 を記録する。

---

### Task 6: 実際の Drive での確認（ユーザー確認を取りながら）

各ステップの前に、実行するコマンドをユーザーに見せて確認を取る。認証キーは検証用に生成し、表示しない。本番の rclone と `/rclone-s3` には触らない。結果は `rclone_dir_cache/<実施日>/phase2-drive-verification-ja.md` に書く。

- [ ] **Step 1:** テスト用フォルダ `gdrive_kwatan:/rclone-s3-test/bucket` を作る。
- [ ] **Step 2: 既存データの準備** — 改修前の `/usr/bin/rclone` で `gdrive_kwatan:/rclone-s3-test/bucket/chunks/0/1/legacy_0_6` に object を直接書く（メタデータ無し）。
- [ ] **Step 3:** 改修版の serve s3 を2つ（A: 19090、B: 19091）、`--kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off --tpslimit 5 --dump headers -vv` で立てる。
- [ ] **Step 4: メタデータの往復** — S3 クライアントでメタデータ付きの PUT をする（rclone の S3 backend は `--metadata-set crc32c=777 -M` で送れるかを確かめる。難しければ `aws` CLI が手元にあるかを確かめ、無ければ Go の小さなクライアント（aws-sdk-go-v2 は rclone の依存にある）を scratchpad に書く）。A で PUT → A・B で HEAD・GET（`X-Amz-Meta-Crc32c` が返る）→ A を再起動して HEAD。Drive 側で `rclone backend query` などで properties `s3m-crc32c` を確かめる。
- [ ] **Step 5: 上書き** — 違うメタデータで上書きし、古いキーが Drive の properties から消えたことを確かめる。メタデータ無しで上書きしたときも、すべて消えることを確かめる。
- [ ] **Step 6: 上限** — 124バイトを超える値、または31個以上のメタデータで PUT し、失敗することと、object が作られない（または既存の中身が変わらない）ことを確かめる。作られてしまう場合は、ユーザーに報告し、serve s3 側で事前に数えて 400 `MetadataTooLarge` を返す追加の修正を提案する。
- [ ] **Step 7: multipart** — メタデータ付きの multipart upload で、完了後（一時的な名前からの移動の後）も properties が残ることを確かめる。
- [ ] **Step 8: 既存データとの互換** — Step 2 の object を、改修版で (1) HEAD・GET（メタデータ無し、中身は正しい）、(2) メタデータ付きで上書き → HEAD・GET でメタデータと新しい中身、(3) DELETE、(4) HEAD・GET が 404。
- [ ] **Step 9: API の回数** — 新しい key の PUT、HEAD、GET の Drive API の回数が Phase 1 の計測（PUT は名前の検索1回＋アップロード）と同じことを、`--dump headers` のログで数える。
- [ ] **Step 10:** serve を止め、テスト用フォルダを `rclone purge` で消す（ユーザー確認の後）。
- [ ] **Step 11（余力があれば、ユーザーに相談してから）:** テスト用フォルダの上で、scratchpad の sqlite を metadata にした JuiceFS を A・B の上に置き、A で書いたファイルを B で読むときにチェックサムの検証が行われることを確かめる（JuiceFS のログ、または中身を壊した object で `verify checksum failed` になること）。

---

### Task 7: 手順書と記録の更新

- [ ] **Step 1:** `rclone_dir_cache/2026-10-08/deploy-runbook-ja.md` に Phase 2 の項を加える: `--kaz-s3-persist-metadata` と、`rclone.conf` の `[gdrive_kwatan]` への `kaz_properties = true`（または `--drive-kaz-properties`）。1台ずつ入れ替えてよいこと（仕様 §6）。適用後に見るもの（HEAD・GET のメタデータ、RSS が増え続けないこと、Drive の properties）。元に戻す方法（オプションを外せばメモリの方式に戻る。Drive の properties は残るが害は無い）。
- [ ] **Step 2:** `TODO.md`、`agent_memo.md`、`docs/findings.md`（rclone の節の「残る制約」を更新）、`docs/superpowers/README.md` の一覧表を更新する。commit・push はユーザーの許可を得てから。
