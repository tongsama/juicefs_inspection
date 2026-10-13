# rclone v1.75.2 への kaz 改修の移植の調査（2026-10-13）

目的: `1.75.1-improve-kaz`（fe0572749、`rclone-v1.75.1-kaz.2` と同じ commit）の改修を、公式 v1.75.2（be8d6044e）に齟齬なく載せる方法を調べる。読み取りの調査と、scratchpad の worktree での試し merge・テストだけを行った。`rclone/` の作業ブランチ・リモートは変更していない。

## 0. 前提（観測）

- v1.75.2 は v1.75.1 の子孫（`v1.75-stable`）。v1.75.1..v1.75.2 は約190 commit。多くはセキュリティ修正（GHSA 付き）と serve s3 の作り直し。
- upstream は CI のビルドを go1.26.9 に上げた（2cc908837、CVE 対応）。go.mod の `go` 行は変わっていない。kaz.2 は Go 1.26.0 でビルドしている（`release/rclone/versions.json`）。
- release workflow（`.github/workflows/release-rclone.yml`）は、タグ `rclone-v<X.Y.Z>-kaz.<n>` の形だけを確かめる。ベースの版は固定していないので、`rclone-v1.75.2-kaz.N` もそのまま扱える。
- `v1.75.2` を `1.75.1-improve-kaz` に試しに merge した（scratchpad、commit なし）。テキストの衝突は次の4ファイル:
  - `cmd/serve/s3/backend.go`、`multipart.go`（内容の衝突）
  - `cmd/serve/s3/pager.go`、`pager_test.go`（kaz が変更、upstream が削除）
- `vfs/`、`backend/drive/`、`fs/config/flags/` は自動で merge できた。drive と flags には upstream の変更が無い。

## 1. 一覧（`--kaz-s3-list-by-key-order`）

観測:
- upstream の ee40507d3「list objects lazily so paging a deep hierarchy is fast」（`cmd/serve/s3/list.go`）は、kaz_list と同じ方式を**常に有効**にしたもの。ディレクトリを「名前 + `/`」のキーの順に深さ優先でたどり、ページが埋まったら止める。マーカーは「より大きい」で比べ（キーと一致しなくてよい）、マーカーより前の部分木は読まない。走査中に消えたディレクトリは飛ばし、読み込みエラーは返す。pager.go は削除された。
- kaz_list_test.go を v1.75.2 の upstream 実装に向けて移して流した（[移したテスト](kazport_list_test.go.txt)）。ランダムな木での全ページの連結、キーでないマーカー、読むディレクトリの数、エラー、削除しながらの一覧、minio での V1/V2 は**すべて PASS**。`TestKazListStopsOnCancel` だけが FAIL（upstream は要求の ctx を見ないので、クライアントが切っても走査が続く）。
- upstream の ETag は `b.s.etagHash(_vfs)`（`--etag-hash auto` と auth proxy に対応、f625ac98a）。kaz_list は古い `etagHashType` を使っているので、そのまま載せると auto のときに ETag が空になる見込み（推論、未検証）。
- upstream の Debug ログは件数・ディレクトリ数・truncated だけで、1ページの時間・prefix・marker は出ない。

推奨（案 c）: kaz_list.go と pager の修正（f8584af74）は捨て、upstream の lister に任せる。足りない点だけを薄く足す。
- `listPage`／`lister` に要求の ctx を渡し、ディレクトリを読む前に `ctx.Err()` を見る。
- `kaz list:` 相当の Debug 行（prefix、marker、dirs、took）を出す。
- `--kaz-s3-list-by-key-order` は、本番の起動オプションに入っているので、**受け付けるだけのオプション**として残す（消すと未知のフラグで起動に失敗する、推論）。ヘルプとノートに「v1.75.2 からは一覧が常にキーの順、このオプションは何もしない」と書く。
- kaz.2 のノートの「マーカーより大きいキーから返す」修正は、upstream に含まれるので維持される。
- 判断が要ること: 取り消しの確認を常に有効にするか、オプションで有効にするか。

## 2. serve s3 の backend・multipart・メタデータ

観測:
- v1.75.2 では、serve s3 の状態がユーザーごとの `tenant` に移った（10335538d）。`b.meta`（`sync.Map`）と `storeModtime` は無くなり、`tenant.meta`（LRU、上限 `--metadata-max-objects`、size・modTime が違えば捨てる）と `getMeta`／`storeMeta`／`removeMeta` になった。kaz のコードとテストの `b.meta` 参照は、このままではコンパイルできない（推論）。
- multipart は作り直された。backend へのアップロード（`vfs.Create`）は CreateMultipartUpload ではなく、最初の part（`sink()`）で始まる。part が0個の Complete は `close()` が空の object を作る（40eaa114c）。kaz は CreateMultipartUpload で Create してメタデータを付けていた。
- 自動で正しく入った部分: s3.go の kaz オプション3つ、server.go の cache-mode の検査、kaz_errors／kaz_metadata／kaz_get、PutObject の `kazSetUploadMetadata`、GetObject の直接 open の経路、`--no-cleanup`、`bucketStatError`／`keyStatError`。
- gofakes3 は v0.0.10 に上がった。kaz が使う識別子は upstream のコードでも使われており、残っている見込み（一部は未検証）。

衝突の解消の方針:

| 場所 | 方針 |
|---|---|
| HeadObject・GetObject | persist なら `kazObjectUserMetadata`、そうでなければ `tenant.getMeta` |
| storeModtime | upstream の削除を採る。kaz 用に `kazStoreMeta` を作る（persist なら mtime の反映だけで LRU に入れない、そうでなければ `storeMeta`）。PutObject・CompleteMultipart で使う |
| deleteObject | upstream の `removeMeta`（19068b660）を採り、kaz の `b.meta.Delete` は落とす。`--no-cleanup` は残す（upstream は今も見ていない） |
| CopyObject | upstream の「先に Stat、無ければ NoSuchKey」（96d263383）の後に、persist で同じ key なら 501。Stat のエラーは `keyStatError` にする（upstream はどのエラーでも NoSuchKey） |
| CreateMultipartUpload | kaz の先に Create する処理は捨てる（upstream の「part が無い upload は handle を持たない」と `--multipart-max-uploads` の前提が崩れるため）。`sink()` と `close()` の両方の `vfs.Create` を、メタデータを付ける `createSink` に置き換える |
| CompleteMultipartUpload | その upload の tenant で `kazStoreMeta` |

意味の衝突（推論を含む）:
- persist では、メタデータは object 側にあり LRU を通らない。1dcc246e0（古いメタデータ）と `--metadata-max-objects` の影響を受けない。
- persist off では、LRU から追い出された key の `X-Amz-Meta-Crc32c` が返らなくなる。JuiceFS はそのときチェックサムの検証を黙って省く（再起動の後と同じ）。
- 「ENOENT のときだけ 404」と食い違う箇所: upstream の新しい CopyObject（上記）と、既存の CreateMultipartUpload の Stat（どのエラーでも NoSuchBucket、v1.75.1 からの取りこぼし）。
- multipart のメタデータは `up.meta` に保持され、最初の Write より前に付くので、経路は成り立つ（推論）。逆順の part（pump）も `sink()` を通る。
- cancel-get: upstream の GetObject の変更は etagHash の1行だけ。直接 open の経路は自動で入った。

## 3. vfs・drive・flags

観測:
- kaz の vfs／drive／flags の差分を v1.75.2 に当てた結果は、試し merge の結果とバイト単位で同じ。build・vet ok、`go test -race ./vfs/ ./vfs/vfscommon/ ./backend/drive/ ./fs/config/flags/ ./lib/cache/` はすべて ok。
- **要修正: kaz の lookup モードが、upstream のセキュリティ修正 2f43b99ab（GHSA-44pm-2q5p-fm63、serve nfs の path traversal、unsafe な名前の拒否）を迂回する。** upstream は `Dir.checkName`（`sanitize.Leaf`）を Create・Mkdir・Rename に入れ、Stat 側は「一覧に `..` が無い」ことで守っている。kaz には次の2つの経路がある。
  - `Dir.lookup`（dir_kaz_lookup.go）: 名前を確かめずに `NewObject` を引き、`d.items` に載せる。
  - `kazCreateNoLookup`（O_CREATE|O_TRUNC の近道）: `Dir.Create` を通らない。
- 再現（[プローブ](zz_probe_kaz_unsafe_test.go.txt)）: upstream の nfs の TestPathTraversal を `KazLookupByPath=true`、cache off で回すと、memory backend で FAIL。`..` の作成が成功し、公開した root の外に object が書かれた。local backend は `..` を全角に変えるので外へは出ない。Drive は未検証（`..` を普通の名前として扱うので親には届かないと推測）。
- 修正案（[パッチ](kaz-unsafe-name-fix.patch)）: `lookup` の先頭で unsafe な名前に ENOENT、`kazCreateNoLookup` の先頭で `checkName`（EINVAL）。これを入れると、プローブと vfs・nfs の全テスト（-race）が PASS。
- 残る小さな差: `OpenFile("")` が kaz の cache off で EINVAL、upstream で EPERM。近道で `leaf==""` のときは通常の経路に回すのを推奨。
- 衝突なしと判断したもの: AddVirtual の isDir（13ae25b56）、statMetadata（af92ea943）、Hold／VFS の共有（オプションが違う VFS は共有しない）、Busy（DirCacheTime×2 の掃除の後も書き込み中は true のままとプローブで確認）、SetCanExpire、50d2430b4、cd6941ed9。
- 既存の制約（v1.75.2 と無関係、未検証）: `--links` を併用すると、lookup("foo") は `foo.rclonelink` を見つけない。

## 4. 移植の進め方（推奨）

1. `v1.75.2` から新しいベースのブランチ（例 `1.75.2-improve-kaz`）を切る。
2. 機能ブランチ（vfs-lookup、persist-metadata、cancel-get）を `rebase --onto v1.75.2` で載せ直し、v1.75.1 のときと同じ順で `--no-ff` で merge する。意味の変更が merge commit に埋もれないので、試し merge の解消より見直しやすい。
   - 書き直す commit: c5ae06e24（メタデータ削除を外す、テストを tenant に）、116f16f51（`kazStoreMeta`、`createSink`、HEAD/GET の分岐、テスト）。他は文脈のずれだけ。
   - list-by-key-order は、案 c で新しく書く（ctx の確認、ログ、受け付けるだけのオプション、kaz_list_test を upstream の lister のテストとして残す）。
3. 新しい修正 commit: lookup の unsafe な名前の拒否（必須）、CopyObject・CreateMultipartUpload の Stat エラーを 500 に。
4. 追加するテスト: lookup の unsafe な名前（cache off／writes）、nfs の TestPathTraversal を kaz on・cache off で、persist の multipart（逆順の part、part 0 個、Create 直後は handle が無い）、persist で LRU が空のまま、Copy の 501 と NoSuchKey、Stat エラーが 500、ListObjects の取り消し。
5. ビルドは Go 1.26.9 以上（upstream の CVE 対応に合わせる）。`versions.json` の `go_version` を変える。
6. 実 Drive での確認（ユーザーの確認が要る）: 2台での lookup、persist の properties（multipart の一時名から最終名への Rename の後に残るか、仕様で未検証の項目）、gc の一覧。

## 5. 作業の痕跡

- scratchpad に worktree を3つ作った（`wt-merge`：試し merge の途中、`wt-list`：v1.75.2 ＋移したテスト、`wt-vfs`：v1.75.2 ＋kaz の vfs 差分）。`rclone/` の `git worktree list` に出るので、要らなくなったら `git worktree remove` する。
- ビルド・テストで Go の module（gofakes3 v0.0.10 など）を取得した。

## 6. 今後の upstream の改修への強さ（同日の追加調査）

観測:
- upstream の master（4f19e9cd6、v1.76 の開発中）と v1.75.2 を、kaz が触る upstream のファイルで比べた。差があるのは `backend/drive/upload.go` だけ（チャンクのバッファの作り直し、393544b11）。kaz の1行（`fields` を `f.kazUploadFields()` にする）とは別の場所。serve s3 と vfs の v1.75.2 の変更は、master にあるものを stable に持ってきたもので、master にだけある変更は無い（`git log --cherry-pick --right-only v1.75.2...origin/master` で確認）。
- kaz の vfs・drive・flags の差分は、master にもそのまま当たる（`git apply -3 --check` が成功）。
- 近い次の版での衝突は小さいと見込む（推論。v1.76 の serve s3 にこれから入る変更は分からない）。
- kaz.2 の、upstream のファイルへの変更量（v1.75.1 から）: `cmd/serve/s3/backend.go` +103/−31、`vfs/dir.go` +53/−22、`backend/drive/drive.go` +32/−2、`vfs/vfs.go` +21、`cmd/serve/s3/server.go` +20、`multipart.go` +19、`s3.go` +18、`vfs/file.go` +14、`vfs/write.go` +10、他は数行。v1.75.2 への移植で `pager.go` の分は無くなる。
- v1.75.2 の vfs と nfs のテスト全体を、lookup モードを既定にして流した（build tag で `vfscommon.Opt.KazLookupByPath = true`）。unsafe な名前の修正を入れると、落ちるのは `TestDirForgetAll`、`TestDirForgetPath`、`TestCaseSensitivity`、`TestUnicodeNormalization` の4件だけ（lookup モードの仕様上の違い）。修正を入れないと `TestDirUnsafeName` も落ちる。つまり、この方法で今回の迂回は見つけられた。

計画に入れたこと（`docs/superpowers/plans/2026-10-13-rclone-port-to-v1.75.2.md`）:
- upstream のファイルへの変更は、kaz_*.go の関数を呼ぶ数行のフックにとどめる。例: HEAD/GET のメタデータは `kazUserMeta` を呼ぶ1か所だけを差し替える。upstream の関数の形が変われば、コンパイルエラーで分かる。
- `-tags kazlookup` で、upstream のテスト全体を lookup モードで流す仕組み（Task 7）。次の版で upstream がセキュリティのテストを足せば、kaz の経路で迂回していないかが、そのまま分かる。落ちてよいテストは表で管理する。
- 移植の手引き `KAZ.md`（rclone の kaz のブランチ）: 機能ごとのフック点、頼っている upstream の性質、それを守るテスト、次の版への手順。

計画に入れていない候補:
- upstream への PR で kaz の差分を減らす案は、ユーザーの方針（個人的な改修なので PR は考えない、2026-10-13）により採らない。
- cancel-get の GetObject は、upstream の本文を組み替えている（約40行）。フックの形にすると、upstream の本文をそのまま else 側に包むことになり、字下げで差分が大きくなるので、今回は組み替えのままにした。
- `vfs/dir.go` の `statCachedNormalized` は、upstream の正規化のループを関数に切り出している。upstream がこのループを直すと、衝突として出る（黙って失われることはない）ので、そのままにした。
