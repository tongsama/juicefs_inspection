# rclone serve s3：一覧をキーの順に必要な分だけ読む（`--kaz-s3-list-by-key-order`）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** rclone serve s3 の ListObjects（V1・V2）が、prefix 配下を全件読んで並べ替えてから返すのをやめ、キーの順に必要な分だけ読んで1ページずつ返すようにする。これで `juicefs gc` の `ListAll(chunks/)` が JuiceFS の応答ヘッダの待ち（30s）で切られなくなる。

**Architecture:** 新しいファイル `cmd/serve/s3/kaz_list.go` に、深さ優先でディレクトリをたどる `kazLister` を置く。各ディレクトリの中身は、ディレクトリを「名前 + `/`」として S3 のキーの順（UTF-8 のバイト順）に並べる。マーカー以下の部分木は読まずに飛ばし、MaxKeys + 1 件目を見つけた時点で止める。`ListBucket` はオプション `--kaz-s3-list-by-key-order`（既定 off）でこの方式と旧方式（`entryListR` + `pager`）を切り替える。旧方式の `pager` のマーカーは、オプションに関係なく「マーカーより大きいキーから」に直す。

**Tech Stack:** Go 1.26.0（rclone v1.75.1 の go.mod）、rclone の `cmd/serve/s3`・`vfs`、gofakes3 v0.0.9、minio-go v7（HTTP レベルのテスト）、testify。

**Spec:** [docs/superpowers/specs/2026-10-09-rclone-s3-list-by-key-order-design.md](../specs/2026-10-09-rclone-s3-list-by-key-order-design.md)

## Global Constraints

- 作業ディレクトリは本体の `rclone/`（調査リポジトリのルートから見た位置）。以下のパスと Go コマンドはすべて `rclone/` を基準にする。
- 作業ブランチ: `1.75.1-improve-kaz`（dab33da31）から `feat/kaz-s3-list-by-key-order` を切る。`feat/kaz-s3-cancel-get`（72dfdb817、本番で使用中、未 merge）からは切らない。merge は、全タスクの完了とレビューの後に、ユーザーの承認を得て行う。2つのブランチは、どちらも `s3.go` のオプション表と `Options` に1項目ずつ足すので、merge するときに小さな衝突が出る。そのときは両方の項目を残す。
- **commit は、ユーザーが許可した範囲でだけ行う。push はしない。** commit メッセージに Co-Authored-By などの attribution 行を付けない。
- オプション名は `kaz_s3_list_by_key_order`（フラグは `--kaz-s3-list-by-key-order`）。既定は false。ヘルプの先頭に `[kaz] `、`Groups: "Kaz"`。
- オプションが off のとき、一覧の結果は今と同じにする。例外は `pager` のマーカーの修正（完全に一致するキーを探す → 「マーカーより大きいキーから」）だけ。
- ディレクトリは今と同じ `getDirEntries`（VFS の `ReadDirAll`）で読む。dir cache、`--kaz-vfs-lookup-by-path`、一時 object（`tempObjectPrefix`、`legacyMultipartUploadPrefix`）を隠す条件は変えない。
- ディレクトリを読めないエラーは、そのままエラーとして返す。空の一覧にして隠さない（gc が「一覧に無い＝存在しない」と誤るのを防ぐため）。prefix のディレクトリが無いときだけ、今と同じく空の一覧にする。
- MaxKeys が 0 のときは 1000 として扱う。
- 新規・変更する関数・型には、目的が分かる英語の Go doc コメントを付ける（rclone の慣習）。
- 本番の rclone、Drive の `/rclone-s3`、JuiceFS の metadata には触らない。本番での確認（仕様書 §7）はユーザーが行う。

## Review Focus

仕様が明示していないが、本番で踏みやすい入力・状態。各行のテストは、担当するタスクに入れてある。

1. `juicefs gc --delete` が、ページの合間に object を消す。rclone の cleanup が空になった親ディレクトリも消し、マーカーのキー自体も無くなる → 次のページはマーカーの後ろから続き、重複も取りこぼしもないこと（Task 3 `TestKazListContinuesAfterDeletes`）。
2. 他のホストが Drive 上のディレクトリを消し、自ホストの VFS には親の一覧だけがキャッシュされている → そのディレクトリは空として扱い、一覧全体はエラーにならないこと（Task 3 `TestKazListDirRemovedElsewhere`）。
3. ディレクトリ `a` と、`/` より前に来る文字を含む名前（`a-`、`a.`、`a b`）が並ぶ → S3 の順（`a b` < `a-` < `a.` < `a/…` < `a_`）で返ること（Task 2 `TestKazListMatchesSortedWalk`）。
4. アップロード中の一時 object（`.rclone_temp_…`）がある → 一覧に出ず、MaxKeys の数にも入らないこと（Task 2 `TestKazListHidesTempObjects`）。
5. 実際の S3 クライアントが、V2 では continuation-token、V1 では最後のキー（delimiter が無いと NextMarker が返らない）でページを送る。空白を含むキーは URL エンコードされる → どちらでも全件が1回ずつ返ること（Task 4 `TestKazListMinioPaging`）。

## ファイル構成

| ファイル | 種別 | 責務 |
|---|---|---|
| `cmd/serve/s3/pager.go` | 変更 | 旧方式のマーカーを「マーカーより大きいキーから」にする |
| `cmd/serve/s3/pager_test.go` | 変更 | マーカーの修正のテスト |
| `cmd/serve/s3/s3.go` | 変更 | オプション `kaz_s3_list_by_key_order` と `Options.KazListByKeyOrder` |
| `cmd/serve/s3/kaz_list.go` | 新規 | `kazListBucket`、`kazLister`（キーの順の走査） |
| `cmd/serve/s3/backend.go` | 変更 | `ListBucket` でオプションにより切り替える |
| `cmd/serve/s3/kaz_test.go` | 変更 | テスト用の `failingFs` に、List の回数と、List ごとの hook を足す |
| `cmd/serve/s3/kaz_list_test.go` | 新規 | 新しい方式のテストと、テスト用の木・正解・全ページ取得の helper |

---

### Task 0: 作業ブランチの作成

**Files:** なし（Git の操作だけ）

- [ ] **Step 1: 作業ツリーがきれいなことを確かめ、ブランチを作る**

```bash
cd rclone
git status --short
git switch 1.75.1-improve-kaz
git log --oneline -1
git switch -c feat/kaz-s3-list-by-key-order
```

Expected: `git status --short` は何も出さない（何か出たら止めて、ユーザーに確認する）。`git log` は `dab33da31 Merge branch 'feat/kaz-s3-persist-metadata' into 1.75.1-improve-kaz`。

- [ ] **Step 2: 既存のテストが通ることを確かめる（基準線）**

```bash
go test ./cmd/serve/s3/ -count=1 -skip 'TestS3Minio'
```

Expected: `ok`。`TestS3Minio` は Docker が要るので、これまでどおり除く。

---

### Task 1: 旧方式の `pager` のマーカーを「マーカーより大きいキーから」にする

**Files:**
- Modify: `cmd/serve/s3/pager.go`（`if page.HasMarker { ... }` の2つのループ）
- Test: `cmd/serve/s3/pager_test.go`

**Interfaces:**
- Consumes: なし
- Produces: `(*s3Backend).pager(list *gofakes3.ObjectList, page gofakes3.ListBucketPage) (*gofakes3.ObjectList, error)`（シグネチャは変えない）

- [ ] **Step 1: 失敗するテストを書く**

`cmd/serve/s3/pager_test.go` の末尾に追加する。

```go
// TestPagerMarkerNotInList checks a marker that is not itself a key (for
// example an S3 start-after) resumes after the marker instead of starting the
// listing again.
func TestPagerMarkerNotInList(t *testing.T) {
	list := gofakes3.NewObjectList()
	for _, key := range []string{"a", "c", "e"} {
		list.Add(&gofakes3.Content{Key: key})
	}
	list.AddPrefix("b/")
	list.AddPrefix("d/")

	got, err := (&s3Backend{}).pager(list, gofakes3.ListBucketPage{MaxKeys: 10, HasMarker: true, Marker: "bb"})
	if err != nil {
		t.Fatal(err)
	}
	var keys []string
	for _, c := range got.Contents {
		keys = append(keys, c.Key)
	}
	var prefixes []string
	for _, p := range got.CommonPrefixes {
		prefixes = append(prefixes, p.Prefix)
	}
	if strings.Join(keys, ",") != "c,e" || strings.Join(prefixes, ",") != "d/" {
		t.Fatalf("expected keys [c e] and prefixes [d/] after marker bb, got %v %v", keys, prefixes)
	}
}
```

`pager_test.go` の import に `"strings"` を足す。

- [ ] **Step 2: テストが失敗することを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestPager' -count=1`
Expected: FAIL。`expected keys [c e] and prefixes [d/] after marker bb, got [a c e] [b/ d/]`。

- [ ] **Step 3: 実装する**

`cmd/serve/s3/pager.go` の `if page.HasMarker { ... }` ブロックを、次に置き換える。

```go
	if page.HasMarker {
		// S3 returns the keys after the marker, which need not be a key
		// itself (start-after may be any string), so search by order
		// rather than for an exact match.
		i := sort.Search(len(list.Contents), func(i int) bool {
			return list.Contents[i].Key > page.Marker
		})
		list.Contents = list.Contents[i:]
		j := sort.Search(len(list.CommonPrefixes), func(j int) bool {
			return list.CommonPrefixes[j].Prefix > page.Marker
		})
		list.CommonPrefixes = list.CommonPrefixes[j:]
	}
```

関数の doc コメント `// pager splits the object list into smulitply pages.` は、次に直す。

```go
// pager sorts the whole object list and returns the page of it after
// page.Marker. It is the listing used without --kaz-s3-list-by-key-order.
```

- [ ] **Step 4: テストが通ることを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestPager' -count=1`
Expected: PASS（`TestPagerSortsContentsByKey` も通る）。

- [ ] **Step 5: commit（ユーザーが許可した場合だけ）**

```bash
git add cmd/serve/s3/pager.go cmd/serve/s3/pager_test.go
git commit -m "serve s3: resume listings after a marker that is not a key"
```

---

### Task 2: `--kaz-s3-list-by-key-order` とキーの順の一覧

**Files:**
- Modify: `cmd/serve/s3/s3.go`（`OptionsInfo` の `kaz_s3_persist_metadata` の後、`Options` の `KazPersistMetadata` の後）
- Create: `cmd/serve/s3/kaz_list.go`
- Modify: `cmd/serve/s3/backend.go`（`ListBucket`）
- Test: `cmd/serve/s3/kaz_list_test.go`

**Interfaces:**
- Consumes: Task 1 の `pager`（off のときに使う）。既存の `newKazBackend(t *testing.T, lookup bool, tweak func(*Options)) (*s3Backend, *failingFs, string)`（`kaz_test.go`。戻り値の3つ目は、`bucket` ディレクトリを含む local のルート）、`getDirEntries(prefix string, VFS *vfs.VFS) (vfs.Nodes, error)`、`bucketDirPath`、`prefixParser`、`getFileHash(node any, hashType hash.Type) string`、`tempObjectPrefix`、`legacyMultipartUploadPrefix`。
- Produces:
  - `Options.KazListByKeyOrder bool`（`config:"kaz_s3_list_by_key_order"`）
  - `func (b *s3Backend) kazListBucket(ctx context.Context, _vfs *vfs.VFS, bucket string, prefix *gofakes3.Prefix, page gofakes3.ListBucketPage) (*gofakes3.ObjectList, error)`
  - `type kazLister struct`、`func (l *kazLister) walk(dir, name string) error`、`func (l *kazLister) add(key string, node vfs.Node) error`、`var errKazPageFull`
  - テスト用 helper（Task 3・4 で使う）: `makeKazTree(t *testing.T, dir string, rng *rand.Rand, depth int)`、`kazExpected(t *testing.T, bucketDir, prefix string, delimiter bool) []string`、`kazListAll(t *testing.T, b *s3Backend, prefix string, delimiter bool, maxKeys int64) []string`、`kazListPage(t *testing.T, b *s3Backend, ctx context.Context, prefix string, delimiter bool, page gofakes3.ListBucketPage) (*gofakes3.ObjectList, error)`、`kazByKeyOrder(o *Options)`

- [ ] **Step 1: 失敗するテストを書く**

`cmd/serve/s3/kaz_list_test.go` を新しく作る。

```go
package s3

import (
	"context"
	"math/rand/v2"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/rclone/gofakes3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// kazNames mixes names that sort before "/" ("a b", "a-", "a.") and after it
// ("a_") so that a directory "a" among them checks directories sort as "a/".
var kazNames = []string{"a", "a b", "a-", "a.", "a_", "b", "0", "1", "10", "1-x"}

// makeKazTree fills dir with a random tree of files and directories (some of
// them empty) up to three levels deep, reusing entries that already exist.
func makeKazTree(t *testing.T, dir string, rng *rand.Rand, depth int) {
	for _, name := range kazNames {
		p := filepath.Join(dir, name)
		if st, err := os.Stat(p); err == nil {
			if st.IsDir() && depth < 3 {
				makeKazTree(t, p, rng, depth+1)
			}
			continue
		}
		switch r := rng.IntN(10); {
		case r < 4:
			require.NoError(t, os.WriteFile(p, []byte(name), 0666))
		case r < 7 && depth < 3:
			require.NoError(t, os.Mkdir(p, 0777))
			makeKazTree(t, p, rng, depth+1)
		}
	}
}

// kazExpected returns what an S3 listing of prefix should hold, worked out
// straight from the tree under bucketDir: the keys and, with delimiter, the
// CommonPrefixes, in key order. Directories count as CommonPrefixes even when
// empty, as serve s3 shows them.
func kazExpected(t *testing.T, bucketDir, prefix string, delimiter bool) []string {
	set := map[string]bool{}
	err := filepath.Walk(bucketDir, func(p string, info os.FileInfo, err error) error {
		require.NoError(t, err)
		rel, err := filepath.Rel(bucketDir, p)
		require.NoError(t, err)
		if rel == "." || strings.HasPrefix(info.Name(), tempObjectPrefix) {
			return nil
		}
		key := filepath.ToSlash(rel)
		if info.IsDir() {
			if !delimiter {
				return nil
			}
			key += "/"
		}
		if !strings.HasPrefix(key, prefix) {
			return nil
		}
		if delimiter {
			if i := strings.IndexByte(key[len(prefix):], '/'); i >= 0 {
				key = key[:len(prefix)+i+1]
			}
		}
		set[key] = true
		return nil
	})
	require.NoError(t, err)
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// kazListPage requests one page of the listing of "bucket" from b.
func kazListPage(t *testing.T, b *s3Backend, ctx context.Context, prefix string, delimiter bool, page gofakes3.ListBucketPage) (*gofakes3.ObjectList, error) {
	p := &gofakes3.Prefix{HasPrefix: prefix != "", Prefix: prefix}
	if delimiter {
		p.HasDelimiter, p.Delimiter = true, "/"
	}
	return b.ListBucket(ctx, "bucket", p, page)
}

// kazListAll pages through the whole listing of prefix, maxKeys entries at a
// time, and returns the keys and CommonPrefixes in the order the pages gave
// them. Every page but the last must be full and carry a NextMarker.
func kazListAll(t *testing.T, b *s3Backend, prefix string, delimiter bool, maxKeys int64) []string {
	full := int(maxKeys)
	if full == 0 {
		full = 1000
	}
	var got []string
	page := gofakes3.ListBucketPage{MaxKeys: maxKeys}
	for i := 0; ; i++ {
		require.Less(t, i, 10000, "listing does not end")
		resp, err := kazListPage(t, b, context.Background(), prefix, delimiter, page)
		require.NoError(t, err)
		var items []string
		for _, c := range resp.Contents {
			items = append(items, c.Key)
		}
		for _, cp := range resp.CommonPrefixes {
			items = append(items, cp.Prefix)
		}
		// A page is one contiguous range of the key order, so sorting
		// inside it only merges Contents and CommonPrefixes.
		sort.Strings(items)
		got = append(got, items...)
		if !resp.IsTruncated {
			return got
		}
		require.Len(t, items, full, "a truncated page must be full")
		require.NotEmpty(t, resp.NextMarker)
		page.HasMarker, page.Marker = true, resp.NextMarker
	}
}

// kazByKeyOrder turns --kaz-s3-list-by-key-order on for newKazBackend.
func kazByKeyOrder(o *Options) { o.KazListByKeyOrder = true }

// TestKazListMatchesSortedWalk pages through a random tree with many page
// sizes, prefixes and with and without a delimiter, and checks the pages
// joined together equal the sorted listing of the whole tree.
func TestKazListMatchesSortedWalk(t *testing.T) {
	for _, lookup := range []bool{false, true} {
		b, _, root := newKazBackend(t, lookup, kazByKeyOrder)
		bucketDir := filepath.Join(root, "bucket")
		require.NoError(t, os.MkdirAll(filepath.Join(bucketDir, "a", "a"), 0777))
		require.NoError(t, os.WriteFile(filepath.Join(bucketDir, "a", "a-"), []byte("x"), 0666))
		makeKazTree(t, bucketDir, rand.New(rand.NewPCG(1, 2)), 0)

		prefixes := []string{"", "a/", "a", "a/a", "a/a/", "1", "a/a-/", "zz", "nope/deep/"}
		for _, prefix := range prefixes {
			for _, delimiter := range []bool{false, true} {
				want := kazExpected(t, bucketDir, prefix, delimiter)
				for _, maxKeys := range []int64{1, 2, 3, 7, 0, 1000} {
					got := kazListAll(t, b, prefix, delimiter, maxKeys)
					assert.Equal(t, want, got, "lookup=%v prefix=%q delimiter=%v maxKeys=%d", lookup, prefix, delimiter, maxKeys)
				}
			}
		}
	}
}

// TestKazListMarkerNotAKey checks a start-after that is not a key resumes
// right after it, inside a directory and between directories.
func TestKazListMarkerNotAKey(t *testing.T) {
	b, _, root := newKazBackend(t, false, kazByKeyOrder)
	bucketDir := filepath.Join(root, "bucket")
	makeKazTree(t, bucketDir, rand.New(rand.NewPCG(5, 6)), 0)
	all := kazExpected(t, bucketDir, "", false)
	require.NotEmpty(t, all)

	for _, marker := range []string{"", "0", "a/", "a/zzz", "a0", "a~", "zzz", all[len(all)/2] + "x"} {
		resp, err := kazListPage(t, b, context.Background(), "", false, gofakes3.ListBucketPage{MaxKeys: 1000, HasMarker: true, Marker: marker})
		require.NoError(t, err)
		var got []string
		for _, c := range resp.Contents {
			got = append(got, c.Key)
		}
		var want []string
		for _, key := range all {
			if key > marker {
				want = append(want, key)
			}
		}
		assert.Equal(t, want, got, "marker=%q", marker)
	}
}

// TestKazListHidesTempObjects checks the temporary objects of uploads in
// progress are neither listed nor counted towards MaxKeys.
func TestKazListHidesTempObjects(t *testing.T) {
	b, _, root := newKazBackend(t, false, kazByKeyOrder)
	dir := filepath.Join(root, "bucket", "d")
	require.NoError(t, os.MkdirAll(dir, 0777))
	for _, name := range []string{tempObjectPrefix + "x", legacyMultipartUploadPrefix + "y", "f1", "f2"} {
		require.NoError(t, os.WriteFile(filepath.Join(dir, name), []byte("x"), 0666))
	}

	resp, err := kazListPage(t, b, context.Background(), "", false, gofakes3.ListBucketPage{MaxKeys: 2})
	require.NoError(t, err)
	require.Len(t, resp.Contents, 2)
	assert.Equal(t, "d/f1", resp.Contents[0].Key)
	assert.Equal(t, "d/f2", resp.Contents[1].Key)
	assert.False(t, resp.IsTruncated)
}
```

- [ ] **Step 2: テストが失敗することを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestKazList' -count=1`
Expected: コンパイルエラー `o.KazListByKeyOrder undefined (type *Options has no field or method KazListByKeyOrder)`。

- [ ] **Step 3: オプションを追加する**

`cmd/serve/s3/s3.go` の `OptionsInfo` で、`kaz_s3_persist_metadata` の項目の後（`}}.` の前）に追加する。

```go
}, {
	Name:    "kaz_s3_list_by_key_order",
	Default: false,
	Help:    "[kaz] Serve ListObjects by walking the directories in S3 key order and stopping when the page is full, instead of reading and sorting everything under the prefix for every page. Directories before the marker are not read",
	Groups:  "Kaz",
```

`Options` の `KazPersistMetadata` の行の後に追加する。

```go
	KazListByKeyOrder             bool          `config:"kaz_s3_list_by_key_order"`
```

- [ ] **Step 4: テストが失敗することを確かめる（まだ旧方式）**

Run: `go test ./cmd/serve/s3/ -run 'TestKazList' -count=1`
Expected: FAIL。`TestKazListMatchesSortedWalk` は、delimiter ありで MaxKeys が小さいとき（旧方式は CommonPrefixes を先に返すので、ページの境目がキーの順とずれる）に不一致になる。`TestKazListHidesTempObjects` は通ってもよい。

- [ ] **Step 5: `kaz_list.go` を作る**

`cmd/serve/s3/kaz_list.go` を新しく作る。この段階では、マーカー以下の部分木を飛ばす処理、context の確認、途中で消えたディレクトリの扱いは入れない（Task 3 で足す）。

```go
package s3

import (
	"context"
	"errors"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/rclone/gofakes3"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/vfs"
)

// errKazPageFull stops the walk when an entry is found after the page is
// already full, which proves the listing is truncated.
var errKazPageFull = errors.New("kaz: list page full")

// kazLister collects one page of a bucket listing by walking its directories
// depth first in S3 key order (UTF-8 byte order), so that only the
// directories needed for the page are read.
type kazLister struct {
	ctx       context.Context
	vfs       *vfs.VFS
	b         *s3Backend
	bucket    string
	delimiter bool   // return sub directories as CommonPrefixes
	hasMarker bool   // only entries after marker are returned
	marker    string // last key or CommonPrefix of the previous page
	maxKeys   int    // entries per page
	count     int    // entries added to resp
	dirs      int    // directories read, for the debug log
	last      string // last key or CommonPrefix added to resp
	resp      *gofakes3.ObjectList
}

// kazListEntry is a directory entry with its object key and the key it sorts
// by in S3: the object key, or for a directory the key followed by "/".
type kazListEntry struct {
	node    vfs.Node
	key     string
	sortKey string
}

// kazListBucket returns the page of the listing of bucket under prefix that
// follows page.Marker. It replaces entryListR and pager with
// --kaz-s3-list-by-key-order so that a page does not need everything under
// the prefix to be read and sorted first.
func (b *s3Backend) kazListBucket(ctx context.Context, _vfs *vfs.VFS, bucket string, prefix *gofakes3.Prefix, page gofakes3.ListBucketPage) (*gofakes3.ObjectList, error) {
	start := time.Now()
	maxKeys := int(page.MaxKeys)
	if maxKeys <= 0 {
		maxKeys = 1000
	}
	l := &kazLister{
		ctx:       ctx,
		vfs:       _vfs,
		b:         b,
		bucket:    bucket,
		delimiter: prefix.HasDelimiter,
		hasMarker: page.HasMarker,
		marker:    page.Marker,
		maxKeys:   maxKeys,
		resp:      gofakes3.NewObjectList(),
	}
	dir, name := prefixParser(prefix)
	err := l.walk(dir, name)
	switch {
	case errors.Is(err, errKazPageFull):
		l.resp.IsTruncated = true
		l.resp.NextMarker = l.last
	case errors.Is(err, gofakes3.ErrNoSuchKey):
		// The prefix directory does not exist: AWS returns an empty list.
		l.resp = gofakes3.NewObjectList()
	case err != nil:
		return nil, err
	}
	fs.Debugf(bucket, "kaz list: prefix=%q marker=%q entries=%d truncated=%v dirs=%d took=%v",
		prefix.Prefix, page.Marker, l.count, l.resp.IsTruncated, l.dirs, time.Since(start))
	return l.resp, nil
}

// walk reads dir (a path inside the bucket, "" for its root) and adds the
// entries whose name starts with name in key order, descending into sub
// directories unless they are returned as CommonPrefixes. It returns
// errKazPageFull once the page is full and one more entry exists.
func (l *kazLister) walk(dir, name string) error {
	fp, err := bucketDirPath(l.bucket, dir)
	if err != nil {
		// A listing prefix that can't be represented as a path matches nothing.
		return gofakes3.ErrNoSuchKey
	}
	nodes, err := getDirEntries(fp, l.vfs)
	if err != nil {
		return err
	}
	l.dirs++

	entries := make([]kazListEntry, 0, len(nodes))
	for _, node := range nodes {
		object := node.Name()
		// Hide the temporary objects of in-progress uploads
		if strings.HasPrefix(object, tempObjectPrefix) || strings.HasPrefix(object, legacyMultipartUploadPrefix) {
			continue
		}
		if !strings.HasPrefix(object, name) {
			continue
		}
		key := path.Join(dir, object)
		sortKey := key
		if node.IsDir() {
			sortKey += "/"
		}
		entries = append(entries, kazListEntry{node: node, key: key, sortKey: sortKey})
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].sortKey < entries[j].sortKey })

	for _, e := range entries {
		if !e.node.IsDir() || l.delimiter {
			if l.hasMarker && e.sortKey <= l.marker {
				continue
			}
			if err := l.add(e.sortKey, e.node); err != nil {
				return err
			}
			continue
		}
		if err := l.walk(e.key, ""); err != nil {
			return err
		}
	}
	return nil
}

// add appends an object, or a directory as a CommonPrefix (key ending in
// "/"), to the page. It returns errKazPageFull instead when the page already
// holds maxKeys entries.
func (l *kazLister) add(key string, node vfs.Node) error {
	if l.count >= l.maxKeys {
		return errKazPageFull
	}
	if node.IsDir() {
		l.resp.AddPrefix(key)
	} else {
		l.resp.Add(&gofakes3.Content{
			Key:          key,
			LastModified: gofakes3.NewContentTime(node.ModTime()),
			ETag:         getFileHash(node, l.b.s.etagHashType),
			Size:         node.Size(),
			StorageClass: gofakes3.StorageStandard,
		})
	}
	l.count++
	l.last = key
	return nil
}
```

- [ ] **Step 6: `ListBucket` で切り替える**

`cmd/serve/s3/backend.go` の `ListBucket` で、`prefix.HasDelimiter = false` の `if` の後、`response := gofakes3.NewObjectList()` の前に追加する。

```go
	if b.s.opt.KazListByKeyOrder {
		return b.kazListBucket(ctx, _vfs, bucket, prefix, page)
	}
```

`ListBucket` の doc コメント `// ListBucket lists the objects in the given bucket.` は、次に直す。

```go
// ListBucket lists the objects in the given bucket.
//
// With --kaz-s3-list-by-key-order only the directories needed for the page
// are read, otherwise everything under the prefix is read and sorted.
```

- [ ] **Step 7: テストが通ることを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestKazList|TestPager' -count=1 -race`
Expected: PASS。

- [ ] **Step 8: commit（ユーザーが許可した場合だけ）**

```bash
git add cmd/serve/s3/s3.go cmd/serve/s3/kaz_list.go cmd/serve/s3/kaz_list_test.go cmd/serve/s3/backend.go
git commit -m "serve s3: add --kaz-s3-list-by-key-order to list in key order page by page"
```

---

### Task 3: マーカー以下を読まない・取り消しで止める・途中で消えたディレクトリ

**Files:**
- Modify: `cmd/serve/s3/kaz_test.go`（`failingFs`）
- Modify: `cmd/serve/s3/kaz_list.go`（`walk`）
- Test: `cmd/serve/s3/kaz_list_test.go`

**Interfaces:**
- Consumes: Task 2 の `kazLister.walk`、`kazListPage`、`kazListAll`、`kazExpected`、`kazByKeyOrder`、`newKazBackend`、`errKaz`（`kaz_test.go`）。
- Produces: `failingFs.lists atomic.Int64`（`List` が呼ばれた回数）、`failingFs.onList func(dir string) error`（nil でなければ `List` の前に呼び、エラーならそれを返す）。

- [ ] **Step 1: テスト用の `failingFs` に、回数と hook を足す**

`cmd/serve/s3/kaz_test.go` の `failingFs` と `List` を、次に置き換える。

```go
// failingFs wraps an fs.Fs and, while fail is set, makes List and NewObject
// return errKaz like a remote rate limit would. It also counts the List calls
// and, when onList is set, calls it before each List and returns its error.
// onList must be set before the backend is used.
type failingFs struct {
	fs.Fs
	fail   atomic.Bool
	lists  atomic.Int64
	onList func(dir string) error
}

var errKaz = errors.New("kaz: simulated remote failure")

// List fails while fail is set or onList fails, otherwise delegates.
func (f *failingFs) List(ctx context.Context, dir string) (fs.DirEntries, error) {
	f.lists.Add(1)
	if f.fail.Load() {
		return nil, errKaz
	}
	if f.onList != nil {
		if err := f.onList(dir); err != nil {
			return nil, err
		}
	}
	return f.Fs.List(ctx, dir)
}
```

Run: `go test ./cmd/serve/s3/ -run 'TestKaz' -count=1`
Expected: PASS（既存のテストは変わらない）。

- [ ] **Step 2: 失敗するテストを書く**

`cmd/serve/s3/kaz_list_test.go` に追加する。import に `"fmt"` と `"slices"` を足す。

```go
// makeKazFlatTree creates n directories d00, d01, ... in the bucket, each
// with the files f0, f1 and f2.
func makeKazFlatTree(t *testing.T, root string, n int) {
	for i := range n {
		dir := filepath.Join(root, "bucket", fmt.Sprintf("d%02d", i))
		require.NoError(t, os.MkdirAll(dir, 0777))
		for _, name := range []string{"f0", "f1", "f2"} {
			require.NoError(t, os.WriteFile(filepath.Join(dir, name), []byte(name), 0666))
		}
	}
}

// TestKazListReadsOnlyNeededDirs checks the first page reads only the
// directories it needs, and that a page after a marker deep in the bucket
// does not read the directories before the marker.
func TestKazListReadsOnlyNeededDirs(t *testing.T) {
	ctx := context.Background()

	b, ff, root := newKazBackend(t, false, kazByKeyOrder)
	makeKazFlatTree(t, root, 50)
	resp, err := kazListPage(t, b, ctx, "", false, gofakes3.ListBucketPage{MaxKeys: 2})
	require.NoError(t, err)
	assert.True(t, resp.IsTruncated)
	assert.Equal(t, "d00/f1", resp.NextMarker)
	assert.LessOrEqual(t, ff.lists.Load(), int64(5), "first page must not read all 50 directories")

	// A fresh backend has nothing cached, as after a restart.
	b, ff, root = newKazBackend(t, false, kazByKeyOrder)
	makeKazFlatTree(t, root, 50)
	resp, err = kazListPage(t, b, ctx, "", false, gofakes3.ListBucketPage{MaxKeys: 2, HasMarker: true, Marker: "d40/f2"})
	require.NoError(t, err)
	require.Len(t, resp.Contents, 2)
	assert.Equal(t, "d41/f0", resp.Contents[0].Key)
	assert.LessOrEqual(t, ff.lists.Load(), int64(6), "directories before the marker must not be read")
}

// TestKazListStopsOnCancel checks the walk stops between directories with
// context.Canceled once the request is canceled.
func TestKazListStopsOnCancel(t *testing.T) {
	b, ff, root := newKazBackend(t, false, kazByKeyOrder)
	makeKazFlatTree(t, root, 10)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var listed []string
	ff.onList = func(dir string) error {
		listed = append(listed, dir)
		if dir == "bucket/d01" {
			cancel()
		}
		return nil
	}

	_, err := kazListPage(t, b, ctx, "", false, gofakes3.ListBucketPage{MaxKeys: 1000})
	assert.ErrorIs(t, err, context.Canceled)
	assert.False(t, slices.Contains(listed, "bucket/d02"), "walk must stop after the cancel: %v", listed)
}

// TestKazListDirErrorIsNotEmpty checks a directory that can't be read fails
// the listing instead of being left out of it.
func TestKazListDirErrorIsNotEmpty(t *testing.T) {
	b, ff, root := newKazBackend(t, false, kazByKeyOrder)
	makeKazFlatTree(t, root, 5)
	ff.onList = func(dir string) error {
		if dir == "bucket/d03" {
			return errKaz
		}
		return nil
	}

	_, err := kazListPage(t, b, context.Background(), "", false, gofakes3.ListBucketPage{MaxKeys: 1000})
	assert.ErrorIs(t, err, errKaz)
}

// TestKazListContinuesAfterDeletes deletes objects through serve s3 between
// pages, as juicefs gc --delete does, so that emptied directories and the
// marker key itself disappear, and checks the listing still returns every
// remaining key exactly once.
func TestKazListContinuesAfterDeletes(t *testing.T) {
	ctx := context.Background()
	b, _, root := newKazBackend(t, false, kazByKeyOrder)
	makeKazFlatTree(t, root, 5)

	var got []string
	page := gofakes3.ListBucketPage{MaxKeys: 2}
	for i := 0; ; i++ {
		require.Less(t, i, 100)
		resp, err := kazListPage(t, b, ctx, "", false, page)
		require.NoError(t, err)
		for _, c := range resp.Contents {
			got = append(got, c.Key)
			// Delete every key of d01 and d02 as soon as it is listed.
			if strings.HasPrefix(c.Key, "d01/") || strings.HasPrefix(c.Key, "d02/") {
				require.NoError(t, b.deleteObject(ctx, "bucket", c.Key))
			}
		}
		if !resp.IsTruncated {
			break
		}
		page.HasMarker, page.Marker = true, resp.NextMarker
	}
	assert.Equal(t, kazExpectedFlat(5), got)
	_, err := os.Stat(filepath.Join(root, "bucket", "d01"))
	assert.True(t, os.IsNotExist(err), "the emptied directory must have been cleaned up")
}

// TestKazListDirRemovedElsewhere removes a directory on the remote after its
// parent was listed but before the walk reaches it, as another host sharing
// the remote can, and checks it is treated as empty.
func TestKazListDirRemovedElsewhere(t *testing.T) {
	ctx := context.Background()
	b, _, root := newKazBackend(t, false, kazByKeyOrder)
	makeKazFlatTree(t, root, 5)

	resp, err := kazListPage(t, b, ctx, "", false, gofakes3.ListBucketPage{MaxKeys: 2})
	require.NoError(t, err)
	require.True(t, resp.IsTruncated)
	require.NoError(t, os.RemoveAll(filepath.Join(root, "bucket", "d02")))

	got := []string{resp.Contents[0].Key, resp.Contents[1].Key}
	page := gofakes3.ListBucketPage{MaxKeys: 2, HasMarker: true, Marker: resp.NextMarker}
	for i := 0; ; i++ {
		require.Less(t, i, 100)
		resp, err = kazListPage(t, b, ctx, "", false, page)
		require.NoError(t, err)
		for _, c := range resp.Contents {
			got = append(got, c.Key)
		}
		if !resp.IsTruncated {
			break
		}
		page.HasMarker, page.Marker = true, resp.NextMarker
	}
	var want []string
	for _, key := range kazExpectedFlat(5) {
		if !strings.HasPrefix(key, "d02/") {
			want = append(want, key)
		}
	}
	assert.Equal(t, want, got)
}

// kazExpectedFlat returns the keys makeKazFlatTree creates for n directories.
func kazExpectedFlat(n int) []string {
	var keys []string
	for i := range n {
		for _, name := range []string{"f0", "f1", "f2"} {
			keys = append(keys, fmt.Sprintf("d%02d/%s", i, name))
		}
	}
	return keys
}

// TestKazListOffKeepsOldListing checks that without the option the old
// listing still returns the whole tree when the result fits in one page, and
// pages correctly without a delimiter now that the marker is compared by
// order.
func TestKazListOffKeepsOldListing(t *testing.T) {
	b, _, root := newKazBackend(t, false, nil)
	bucketDir := filepath.Join(root, "bucket")
	makeKazTree(t, bucketDir, rand.New(rand.NewPCG(1, 2)), 0)
	for _, delimiter := range []bool{false, true} {
		assert.Equal(t, kazExpected(t, bucketDir, "", delimiter), kazListAll(t, b, "", delimiter, 1000), "delimiter=%v", delimiter)
	}
	for _, maxKeys := range []int64{1, 3} {
		assert.Equal(t, kazExpected(t, bucketDir, "", false), kazListAll(t, b, "", false, maxKeys), "maxKeys=%d", maxKeys)
	}
}
```

- [ ] **Step 3: テストが失敗することを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestKazList' -count=1`
Expected:
- `TestKazListReadsOnlyNeededDirs` が FAIL（2つ目の確認。d00〜d39 も読むので、List の回数が 6 を超える）。
- `TestKazListStopsOnCancel` が FAIL（エラーが nil。d02 以降も読む）。
- `TestKazListDirErrorIsNotEmpty`、`TestKazListContinuesAfterDeletes`、`TestKazListDirRemovedElsewhere`、`TestKazListOffKeepsOldListing` は、この時点で通ってもよい（退行を防ぐためのテスト。VFS は Drive 上に無いディレクトリを空として読む）。

- [ ] **Step 4: 実装する**

`cmd/serve/s3/kaz_list.go` の `walk` で、先頭（`fp, err := bucketDirPath(...)` の前）に追加する。

```go
	if err := l.ctx.Err(); err != nil {
		return err
	}
```

`walk` の `for _, e := range entries { ... }` の中の、`if err := l.walk(e.key, ""); err != nil { return err }` を、次に置き換える。

```go
		// Every key below e starts with e.sortKey, so when that is before
		// the marker and the marker is not inside e, the whole subtree is
		// at or before the marker and need not be read.
		if l.hasMarker && e.sortKey < l.marker && !strings.HasPrefix(l.marker, e.sortKey) {
			continue
		}
		err := l.walk(e.key, "")
		if errors.Is(err, gofakes3.ErrNoSuchKey) {
			// Gone since its parent was read: it holds no keys any more.
			continue
		}
		if err != nil {
			return err
		}
```

`walk` の doc コメントの最後に、次の2文を足す。

```go
// Sub directories wholly at or before the marker are skipped unread, and the
// walk stops with the context's error once the request is canceled.
```

- [ ] **Step 5: テストが通ることを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestKazList|TestPager' -count=1 -race`
Expected: PASS。

- [ ] **Step 6: commit（ユーザーが許可した場合だけ）**

```bash
git add cmd/serve/s3/kaz_list.go cmd/serve/s3/kaz_list_test.go cmd/serve/s3/kaz_test.go
git commit -m "serve s3: skip directories before the marker and stop listing on cancel"
```

---

### Task 4: 実際の S3 クライアントでページを送る（HTTP）

**Files:**
- Test: `cmd/serve/s3/kaz_list_test.go`

**Interfaces:**
- Consumes: Task 2 の `makeKazTree`、`kazExpected`、`Options.KazListByKeyOrder`。既存の `newServer(ctx, f, &opt, &vfsOpt, &proxy.Opt) (*Server, error)`、`(*Server).Serve()`、`(*Server).Shutdown()`、`w.server.URLs()`、`endpoint`（`s3_test.go`）。
- Produces: なし

- [ ] **Step 1: テストを書く**

`cmd/serve/s3/kaz_list_test.go` に追加する。import に `"net/url"`、`"github.com/minio/minio-go/v7"`、`"github.com/minio/minio-go/v7/pkg/credentials"`、`"github.com/rclone/rclone/cmd/serve/proxy"`、`"github.com/rclone/rclone/fs"`、`"github.com/rclone/rclone/fstest"`、`"github.com/rclone/rclone/lib/random"`、`"github.com/rclone/rclone/vfs/vfscommon"` を足す。

```go
// TestKazListMinioPaging pages through a listing over HTTP with a real S3
// client, using ListObjects V2 (continuation-token) and V1 (the last key, as
// no NextMarker is sent without a delimiter), and checks every entry comes
// back exactly once in key order.
func TestKazListMinioPaging(t *testing.T) {
	fstest.Initialise()
	ctx := context.Background()
	root := t.TempDir()
	bucketDir := filepath.Join(root, "bucket")
	require.NoError(t, os.MkdirAll(bucketDir, 0777))
	makeKazTree(t, bucketDir, rand.New(rand.NewPCG(3, 4)), 0)
	f, err := fs.NewFs(ctx, root)
	require.NoError(t, err)

	keyid, keysec := random.String(16), random.String(16)
	opt := Opt
	opt.AuthKey = []string{keyid + "," + keysec}
	opt.HTTP.ListenAddr = []string{endpoint}
	opt.KazListByKeyOrder = true
	w, err := newServer(ctx, f, &opt, &vfscommon.Opt, &proxy.Opt)
	require.NoError(t, err)
	go func() { _ = w.Serve() }()
	t.Cleanup(func() { _ = w.Shutdown() })
	u, err := url.Parse(w.server.URLs()[0])
	require.NoError(t, err)
	client, err := minio.New(u.Host, &minio.Options{Creds: credentials.NewStaticV4(keyid, keysec, "")})
	require.NoError(t, err)

	for _, v1 := range []bool{false, true} {
		for _, delimiter := range []bool{false, true} {
			for _, maxKeys := range []int{1, 3} {
				var got []string
				for obj := range client.ListObjects(ctx, "bucket", minio.ListObjectsOptions{Recursive: !delimiter, MaxKeys: maxKeys, UseV1: v1}) {
					require.NoError(t, obj.Err)
					got = append(got, obj.Key)
				}
				want := kazExpected(t, bucketDir, "", delimiter)
				if delimiter {
					// The client sends a page's Contents before its
					// CommonPrefixes, so only the set can be compared.
					sort.Strings(got)
				}
				assert.Equal(t, want, got, "v1=%v delimiter=%v maxKeys=%d", v1, delimiter, maxKeys)
			}
		}
	}
}
```

- [ ] **Step 2: テストが通ることを確かめる**

Run: `go test ./cmd/serve/s3/ -run 'TestKazListMinioPaging' -count=1 -race -v`
Expected: PASS。Task 2・3 の実装だけで通るはず。失敗したら、gofakes3 の `listBucket`（`gofakes3.go:246-345`）のマーカーの扱いと照らして原因を調べる。テストを実装に合わせて弱めない。

- [ ] **Step 3: commit（ユーザーが許可した場合だけ）**

```bash
git add cmd/serve/s3/kaz_list_test.go
git commit -m "serve s3: test --kaz-s3-list-by-key-order paging with a real S3 client"
```

---

### Task 5: 全体の確認

**Files:** なし（確認だけ）

- [ ] **Step 1: パッケージのテストを race つきで全部流す**

```bash
go test ./cmd/serve/s3/ -count=1 -race -skip 'TestS3Minio'
```

Expected: `ok`。

- [ ] **Step 2: vet と、関連パッケージのビルド**

```bash
go vet ./cmd/serve/s3/
go build ./...
```

Expected: 出力なし（成功）。

- [ ] **Step 3: フラグがヘルプに出ることを確かめる**

```bash
go run . serve s3 --help 2>&1 | grep -A1 'kaz-s3-list-by-key-order'
```

Expected: `--kaz-s3-list-by-key-order` と `[kaz] Serve ListObjects by walking ...` が表示される。

- [ ] **Step 4: 結果をまとめる**

ブランチの commit 一覧（`git log --oneline 1.75.1-improve-kaz..`）と、テストの結果を、調査リポジトリの `agent_memo.md` と `TODO.md` に記録する（調査リポジトリ側の commit はユーザーが行う）。merge、release、本番での確認（仕様書 §7。最初は `juicefs gc` を `--delete` なしで、rclone は `-vv` で `kaz list:` の行を見る）は、ユーザーの判断を待つ。
