# rclone-v1.75.1-kaz.2

rclone-v1.75.1-kaz.1 に、`rclone serve s3` の改修を2つ加えた版です。kaz.1 までの改修は、すべてそのまま含みます（[rclone-v1.75.1-kaz.1](https://github.com/tongsama/juicefs_inspection/releases/tag/rclone-v1.75.1-kaz.1) のリリースノートを参照）。fork: `tongsama/rclone` の `1.75.1-improve-kaz` ブランチ、commit `fe05727492551ec2019857f677f0e36c62f5d437`、Go 1.26.0 でビルド。

## 新しいオプション

### `--kaz-s3-list-by-key-order`（既定では無効）

ListObjects（V1・V2）を、キーの順に必要な分だけ読んで、1ページずつ返します。

- これまでは、1ページ目を返す前に、prefix 配下の全ディレクトリを Drive から読んで並べ替えていました。`juicefs gc` の `chunks/` の一覧では1時間以上かかり、JuiceFS の応答ヘッダの待ち（30 秒）で切られて、gc が FATAL で止まっていました。
- 新しい方式は、ディレクトリを深さ優先で、S3 のキーの順（ディレクトリは「名前 + `/`」）にたどります。MaxKeys 件が埋まった時点で返します。前のページの続き（マーカー）より前のディレクトリは読みません。
- 要求が取り消されたら、次のディレクトリへ進む前に止まります。
- ディレクトリを読めないエラーは、そのまま返します。一覧から黙って抜くことはしません。走査の途中で消えたディレクトリ（他のホストの削除や、rclone の cleanup によるもの）は、空として扱います。
- 1ページごとに、debug ログ（`-vv`）に `kaz list:` の行を出します。読んだディレクトリの数（`dirs=`）と、かかった時間（`took=`）が分かります。

### `--kaz-s3-cancel-get-on-disconnect`（既定では無効）

JuiceFS が GET を諦めて接続を切ったら、Drive からのダウンロードも止めます。

- これまでは、Drive が応答する前にクライアントが切っても、Drive へのダウンロードは応答が来るまで続いていました（Drive では最長 30 秒ほど）。
- VFS が object を知っている GET は、要求の context で backend から直接開きます。chunk の設定と転送の統計は VFS と同じです。書き戻し中のファイルなどは、今までどおり VFS を通します。
- `--vfs-cache-mode off` が必要です（それ以外では起動時に拒否します）。
- 取り消しは、ERROR レベルのログ（`open file failed: ... context canceled`）として出ます。

## 既定で変わる動作

- ListObjects の続き（`start-after`、continuation-token、V1 の `marker`）を、「マーカーより大きいキーから」返すようにしました。これまでは、マーカーと完全に一致するキーを探していて、一致するキーが無ければ先頭から返し直していました。オプションに関係なく直っています。

## 本番での使い方（起動オプションの例）

```
rclone serve s3 gdrive:/rclone-s3 --kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties --kaz-s3-cancel-get-on-disconnect --kaz-s3-list-by-key-order --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off
```

## 確認したこと

- 単体テスト（`cmd/serve/s3`、race つき）。
  - ランダムなディレクトリの木で、MaxKeys・prefix・delimiter の組み合わせを変え、全ページをつなげた結果が、全件を並べ替えた正解と一致すること。
  - マーカーより前のディレクトリを読まないこと。
  - 取り消しで止まること。
  - ディレクトリの読み込みエラーが、空の一覧ではなくエラーになること。
  - ページの合間に object を削除しても、重複や取りこぼしが無いこと。
  - minio クライアントを使い、HTTP 越しに V1・V2 でページを送れること。
- `--kaz-s3-cancel-get-on-disconnect` は、kaz.1 の上で実機（2026-10-09）で使用した。GET 4,211 回で broken pipe は 0 件、取り消しは 102 回。
- `--kaz-s3-list-by-key-order` は、実機ではまだ確かめていません。

## 注意

- `--kaz-s3-list-by-key-order` の1ページの時間には、上限がありません。葉ディレクトリの件数が少ないと、1ページのために読むディレクトリが増えます。Drive ではディレクトリ1つあたり約 2.7 秒かかります。初めて使うときは、`juicefs gc` を `--delete` なしで実行してください。rclone を `-vv` で動かし、`kaz list:` の行で、1ページ目が 30 秒以内に返っていることを確かめてください。
- delimiter に `/` 以外を指定しても、`/` と同じに扱います（従来どおり）。
- このほかの注意（Drive の同名フォルダの重複は対象外、Windows 版の mount には WinFsp が必要など）は kaz.1 と同じです。

## 詳細

- 仕様（一覧をキーの順に読む）: [2026-10-09-rclone-s3-list-by-key-order-design.md](https://github.com/tongsama/juicefs_inspection/blob/main/docs/superpowers/specs/2026-10-09-rclone-s3-list-by-key-order-design.md)
- gc の一覧タイムアウトの調査: [gc-list-timeout-investigation-ja.md](https://github.com/tongsama/juicefs_inspection/blob/main/juicefs_gc/2026-10-08/gc-list-timeout-investigation-ja.md)
