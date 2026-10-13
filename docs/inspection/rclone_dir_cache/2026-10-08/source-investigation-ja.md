# rclone serve s3 の dir cache 問題: ソース調査（2026-10-08）

- 対象: rclone v1.75.1（`rclone/`、687d264b6）、gofakes3 v0.0.9（Go モジュールキャッシュ）。ソースは無変更。
- 構成（ユーザー回答）: 各ホストがそれぞれ `rclone serve s3 gdrive:/rclone-s3 --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off` を動かし、同じ Drive フォルダを共有する。
- 再現テスト: [vfsrepro_test.go.txt](vfsrepro_test.go.txt)（local backend。拡張子は誤ビルド防止のため .txt）。
- 区分: 【観測】は実行またはソースで確認済み、【推測】は未検証。

## 1. S3 の全操作が VFS を経由する【観測】

HEAD・GET・PUT・DELETE・List はすべて `vfs.Stat` によるパスの walk から始まる（`cmd/serve/s3/backend.go:149, 207, 342, 480`、`utils.go:19`）。gofakes3 はその前に毎回 `ensureBucketExists` → `BucketExists` → `vfs.Stat(bucket)` を呼ぶ（gofakes3.go:1261-1263）。serve s3 は Fs を直接使わない。

| S3 操作 | rclone backend | VFS／Fs |
|---|---|---|
| HEAD | `HeadObject` backend.go:135-190 | `Stat(bucket)`、`Stat(fp)`、hash は `DirEntry().Hash` |
| GET | `GetObject` backend.go:193-272 | `Stat` → `file.Open(O_RDONLY)` → `openRead`（f.o が nil なら最大5秒待つ） |
| PUT | `PutObject` backend.go:332-447 | `mkdirRecursive`（各階層を Stat）→ `vfs.Create` → `openWrite` → `operations.Rcat` → Drive `Put` |
| DELETE | `deleteObject` backend.go:475-498 | `vfs.Remove`（ENOENT は無視、491行）→ `rmdirRecursive` |
| List | `ListBucket` list.go:199 | `Dir.ReadDirAll` → `_readDir` |

## 2. パス指定の Stat が各階層の全件一覧を取り得る【観測】

- `VFS.Stat` は1階層ずつ `Dir.Stat` → `d.stat` を呼ぶ（vfs.go:489-514、dir.go:979, 861）。`d.stat` は必ず `d._readDir()` を呼び（dir.go:863）、cache が古ければ `list.DirSorted` で全件を取る（dir.go:531）。古さの判定は `age > DirCacheTime` だけ（dir.go:350-356）。
- `bucket/chunks/5/5913/<id>` の1回の Stat で、最大5ディレクトリを全件取得する。Drive は1ページ1000件（drive.go:504, 1120）。
- 取得中は `d.mu` を握るので、同じディレクトリへの並行 GET は直列化される（dir.go:862-868）。
- 再現: dir-cache-time=0 で、Stat 3回の一覧取得件数が 12 → 27（Stat ごとに3ディレクトリを全件取得）。

## 3. 「無い」がキャッシュされる仕組み【観測】

- 専用の negative cache は無い。取得済みディレクトリの `d.items` にエントリが無ければ、有効期間中は API を呼ばずに ENOENT を返す（dir.go:861-909、906行）。
- 無効化の手段（期限切れ、ChangeNotify、`vfs/forget`・`vfs/refresh`、SIGHUP）は、今回の構成ではどれも働かない。`--poll-interval 0` なので Drive の ticker 自体が作られない（drive.go:3255-3258、vfs.go:252-255）。
- 再現: dir-cache-time=1h で一度 Stat した後、外部から作ったファイルを Stat すると `file does not exist`。
- 自ホストの PUT は、`File.Open(O_CREATE)` と書き込み完了時の `addObject` で cache に即時追加される（file.go:913-917, 534-543、write.go:101）。

## 4. 付随して見つかった問題【観測】

- **エラーがすべて 404 になる:** HEAD・GET は Stat の失敗を種類によらず `KeyNotFound` で返す（backend.go:150-151, 208-209）。Drive の rate limit などによる一覧取得の失敗も 404 になり、本当の原因が隠れる。
- **DELETE が成功を返しても object が残る:** DELETE は Stat の ENOENT を成功として扱う（backend.go:491、vfs.go:722-726）。他ホストが作り、自ホストの cache に無い object は Drive に残る。【推測】JuiceFS の GC 後も object が残り、容量を消費し続ける。
- **`--no-cleanup` が効かない:** オプションは定義されているが（s3.go:38, 62）、どこからも参照されていない。DELETE のたびに `rmdirRecursive` が走り（backend.go:496、utils.go:158-171）、空かどうかはローカルの cache で判定する（dir.go:912-920）。【推測】他ホストの Drive dirCache が、削除済みのフォルダの ID を持ち続ける危険がある。

## 5. Drive の NewObject(path)【観測】

- 親ディレクトリの ID は `lib/dircache` から引く（drive.go:4224）。この cache は見つかったものだけを保持し、期限は無い。未登録の階層だけ、1階層ごとに1回 API を呼ぶ（dircache.go:198-262）。
- 対象は `'<dirID>' in parents and name='<leaf>' and trashed=false` で1回 files.list する（drive.go:4233）。親の ID が cache 済みなら、1 object あたり API 1回。
- フォルダに対する NewObject は `ErrorIsDir` を返す（drive.go:1690-1691）。
- 【推測】lib/dircache は他ホストによるフォルダの削除・再作成に追従しない。Drive の名前検索に反映の遅れがあるかは不明。

## 6. 既存オプションでは両立できない【ソース上の結論】

`--dir-cache-time` を長くすると他ホストの新規 object が見えず、短くすると毎回の全件取得になる。`--poll-interval` を有効にしても、可視化は最大で poll 間隔だけ遅れ、変更のたびに数千件を取り直す。`--vfs-refresh`、`--vfs-fast-fingerprint`、`--no-modtime`、serve s3 固有のオプションでは避けられない。ローカルの履歴にも、パス指定の Stat を一覧なしで解決する変更は無い。

## 7. 修正案の候補

- **(a) serve s3 で NewObject にフォールバック:** VFS が ENOENT のときだけ `Fs.NewObject` に問い合わせる。全件取得は減らない。
- **(b') VFS の `Dir.stat` に lookup モード（opt-in）:** miss のとき、または cache が古いとき、全件取得の代わりに `NewObject` で1件だけ確認し、見つかったものを cache に入れる。negative はキャッシュしない。List（ReadDirAll）は従来どおり全件取得。
- **(c) serve s3 の object 操作で Fs を直接使う:** 変更範囲と upstream との差が最も大きい。

## 未検証事項

- 実運用の EIO がこの経路そのものか（serve s3 の `-vv` ログで `Re-reading directory` を確認する）。
- 他ホストの `rmdirRecursive` による、Drive dirCache の古い ID の問題。
- Drive の名前検索の整合性。
