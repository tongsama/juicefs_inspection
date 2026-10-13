# rclone-v1.75.2-kaz.1

rclone-v1.75.1-kaz.2 の改修を、公式の rclone v1.75.2 の上に載せ直した版です。fork: `tongsama/rclone` の `1.75.2-improve-kaz` ブランチ、commit `9816e18ae26e77f0f1fc52af980c5320b0e015b0`、Go 1.26.9 でビルド。

## ベースの変更（公式 v1.75.2）

- 公式 v1.75.2 には、多数のセキュリティ修正（GHSA 付き）と、`serve s3` の作り直しが含まれます。
  - `serve s3`: ユーザーごとの状態の分離、multipart のメモリ上限、`--metadata-max-objects`、一覧をキーの順に必要な分だけ読む方式（常に有効）など。
  - vfs: `..` などの危ない名前の拒否（serve nfs の path traversal の修正）。
- Go は、公式に合わせて 1.26.9 でビルドしています（Go の CVE 対応）。

## kaz の改修（kaz.2 と同じオプションで使えます）

- `--kaz-vfs-lookup-by-path`、`--no-cleanup` の配線、`serve s3` のエラーの扱い（path が無いときだけ 404、他は 500）
- `--kaz-s3-persist-metadata` と `--drive-kaz-properties`（S3 のユーザーメタデータを Drive の properties に保存）
- `--kaz-s3-cancel-get-on-disconnect`

## kaz.2 からの変更

- **一覧（`--kaz-s3-list-by-key-order`）:** 公式 v1.75.2 が同じ方式（キーの順に、必要な分だけ読む）を常に有効にしたので、kaz の実装は外し、公式の実装を使います。
  - オプションは、起動コマンドを変えなくて済むように残してあります。指定しても何もしません。
  - 公式の実装に、kaz の機能を2つ足しました。クライアントが接続を切ったら、次のディレクトリを読む前に走査を止めます。1ページごとに debug ログ（`--log-level DEBUG`）に `kaz list:` の行を出します（`dirs=`、`took=` は kaz.2 と同じ）。
  - 「マーカーより大きいキーから返す」修正は、公式の実装に含まれています。
- **lookup モードと危ない名前:** `--kaz-vfs-lookup-by-path` の経路でも、公式 v1.75.2 と同じく `..` などの危ない名前を拒否します（kaz.2 をそのまま v1.75.2 に載せると、この拒否を迂回していました）。
- **エラーの扱い:** 公式 v1.75.2 で増えた経路（CopyObject、CreateMultipartUpload）でも、path が無いときだけ 404、他は 500 を返します。
- **persist-metadata と multipart:** 公式 v1.75.2 は、multipart の Drive への書き込みを、最初の part が届いたときに始めます。メタデータは、その書き込みを作る時点で付けます（part が逆順に届いた場合、part が0個の場合も含む）。persist のときは、公式のメモリ上のメタデータ（ユーザーごとの LRU）を使いません。
- **一括の PUT:** メタデータを付けられなかったときは、アップロードを破棄し、空の object を作りません。

## 本番での使い方（起動オプションの例）

kaz.2 と同じです。

```
rclone serve s3 gdrive:/rclone-s3 --kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties --kaz-s3-cancel-get-on-disconnect --kaz-s3-list-by-key-order --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off
```

## 確認したこと

- 単体テスト（`cmd/serve/s3`、`cmd/serve/nfs`、`vfs`、`backend/drive` など、race つき）。
- 公式のテストを、lookup モードを既定にして流すテスト（`go test -tags kazlookup`）。lookup モードの仕様上の違いで落ちるテストは、fork の `KAZ.md` の表で管理しています。
- golangci-lint v2（v1.75.2 からの新しい指摘は0件）。
- 実際の Drive のテスト用フォルダで、2つの serve s3 を使った確認（2026-10-13）: ホストをまたぐ読み書き、DELETE、Drive の properties へのメタデータの保存（一括と multipart）、メタデータの無い既存データの読み出し、`kaz list:` の行。
- cancel-get の取り消しと、JuiceFS を動かしての確認は、この版ではまだ行っていません。

## 注意

- `--kaz-vfs-lookup-by-path` は、backend の NewObject がディレクトリに対して `ErrorIsDir` を返すことを前提にしています（drive と local は満たします。memory やバケット型の backend は満たしません）。
- 名前の符号化が1対1でない backend では、lookup モードが別名を引くことがあります。drive では不正な UTF-8 の名前だけが該当し、JuiceFS の key（ASCII）には影響しません。
- このほかの注意（Drive の同名フォルダの重複は対象外、Windows 版の mount には WinFsp が必要など）は kaz.1・kaz.2 と同じです。

## 詳細

- 移植の調査: [port-investigation-ja.md](https://github.com/tongsama/juicefs_inspection/blob/rclone-v1.75.2-kaz.1/docs/inspection/rclone_v1752_port/2026-10-13/port-investigation-ja.md)
- 実際の Drive での確認: [drive-verification-ja.md](https://github.com/tongsama/juicefs_inspection/blob/rclone-v1.75.2-kaz.1/docs/inspection/rclone_v1752_port/2026-10-13/drive-verification-ja.md)
- 実装計画: [2026-10-13-rclone-port-to-v1.75.2.md](https://github.com/tongsama/juicefs_inspection/blob/rclone-v1.75.2-kaz.1/docs/superpowers/plans/2026-10-13-rclone-port-to-v1.75.2.md)
- 次の版へ移すときの手引き: fork の `1.75.2-improve-kaz` ブランチの `KAZ.md`
