# Phase 2: S3 ユーザーメタデータの経路の調査（2026-10-08）

- 対象: rclone `1.75.1-improve-kaz` dd03d0243、gofakes3 v0.0.9、JuiceFS 78acd63d。ソースは読んだだけで、変更していない。
- 目的: `x-amz-meta-*`（ユーザー判断で、すべて）を Drive の object に保存し、どのホストからでも、再起動後も HEAD・GET で返す。プロセスのメモリ `b.meta` をやめる。
- 区分: 【観測】ソースで確認、【推測】未検証。

## 1. serve s3 と gofakes3【観測】

- gofakes3 の `metadataHeaders`（gofakes3.go:1318-1331）は、`X-Amz-*`・`Content-*`・`Cache-Control` の全ヘッダと `Last-Modified`（受信時刻）を map に入れる。キーは net/http の正規化形（`X-Amz-Meta-Crc32c`）。合計の上限は `metadataSizeLimit` 2000（constants.go:20）。
- そのため `b.meta` には `X-Amz-Meta-*` 以外（`X-Amz-Date`、`X-Amz-Content-Sha256`、`Content-Md5` など）も入る。保存箇所: PutObject（backend.go:427）、CompleteMultipartUpload（multipart.go:419）、CopyObject（:575、:606-619）、TouchObject（:306、どこからも呼ばれていない）。
- HEAD・GET（backend.go:174-188、255-269）は `Last-Modified`・`Content-Type` を作り、`b.meta` で上書きする。gofakes3 は全キーをレスポンスヘッダに `Set` する（gofakes3.go:565-567）。

## 2. VFS の書き込み経路【観測】

- cache-mode off: `_vfs.Create` → `WriteFileHandle.openPending` → `operations.Rcat(fh.ctx, …, meta=nil)`（vfs/write.go:94）。Rcat は meta を Put に渡せる（operations.go:1459, 1470）が、VFS にはメタデータを渡す API が無い。ctx は VFS の ctx 固定（write.go:54、file.go:74）。
- Drive がメタデータを送るのは `fs.GetMetadataOptions` を通るときだけで、`ci.Metadata`（`-M`）が false なら nil（fs/metadata.go:155-159）。
- cache-mode writes の書き戻しは `operations.Copy(ローカル cache → remote)`（vfscache/item.go:648）。`ci.Metadata` を有効にすると、ローカルの mode・uid などが Drive の properties に混ざる。
- Drive の `Object` は `fs.SetMetadataer` を実装していない（drive.go:4761-4777）。

## 3. Drive の properties【観測＋公式ドキュメント】

- 書き込み: Put・Update は `fetchAndUpdateMetadata`（metadata.go:629）→ `updateMetadata`（:511-626）。既知の名前（mtime、btime、content-type、description、starred など）以外は `Properties[k]=v`（:618-622）。作成・更新・resumable upload の本文に入るので、API の回数は増えない。
- 上限（[公式](https://developers.google.com/drive/properties)）: 1ファイルあたり public な properties は30個まで（全体で100個）。1件はキーと値を合わせて UTF-8 で124バイトまで。
- 罠1: アップロードの応答は `partialFields`（properties を含まない、drive.go:2593、4506、upload.go:57）。`ci.Metadata` が true だと、properties 無しの状態がキャッシュされ、PUT 直後の HEAD で値が取れない。
- 上書き時の古いキー【推測】: files.update は properties をマージするので、前回あって今回無いキーが残る。消すには null を送る必要がある。
- 読み出し: NewObject・一覧の fields に properties が入るのは `ci.Metadata` が true のときだけ（drive.go:1562-1578）。入っていなければ `o.Metadata(ctx)` が files.get を1回呼ぶ（drive.go:4652-4667）。罠2: その ctx でも `ci.Metadata` が false だと、空の結果をキャッシュする。
- 素のキー（例 `mtime`）を渡すと既知の名前として解釈される（`mtime` は ModifiedTime になる）。接頭辞が必要。

## 4. JuiceFS【観測】

- PUT は `Metadata{"Crc32c": 10進の uint32}`（s3.go:179-182、checksum.go:28）。全体 GET（off=0, limit=-1）だけが `resp.Metadata["crc32c"]` を読む（s3.go:145-150。SDK がキーを小文字にする）。値が無ければ検証しない。値が違えば「verify checksum failed」でエラー。
- HEAD・multipart・Copy ではメタデータを使わない。

## 5. 方式の候補

- **A: PUT のときに properties を一緒に送る。** VFS の書き込みハンドルにメタデータを渡す口を足し、Rcat に渡す。API の回数は増えない。
- **B: PUT の後に SetMetadata で付ける。** Drive の Object に SetMetadata の実装が必要で、PUT ごとに API が1回増える。本体はあるがメタデータが無い（または古い）時間ができる。
- **C: 読み出し（A・B 共通）。** properties を取るのに `ci.Metadata` を有効にする（C1、fields が大きくなり、他のメタデータも動く）か、Drive に properties だけを取る kaz オプションを足す（C2）。
