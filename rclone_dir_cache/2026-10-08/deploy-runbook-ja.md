# rclone 改修版（lookup モード）の本番適用手順

- 対象: 各ホストで動いている `rclone serve s3 gdrive_kwatan:/rclone-s3 ...`（ホストごとに1つ、同じ Drive フォルダを共有）。
- 改修版: rclone `feat/kaz-vfs-lookup` 9b75066b4（v1.75.1 ベース）。仕様は [設計](../../docs/superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md)、実機検証は [記録](drive-verification-ja.md)。
- 実施はユーザーが行う。この手順書は、起動スクリプトの中身（OAuth の秘密が平文で入っている）を写していない。

## 1. バイナリの用意

Phase 3（release 構成）ができるまでは、手元でビルドする。

```bash
cd ~/tmp_local/juicefs_inspection/rclone
```

```bash
git switch feat/kaz-vfs-lookup && git log -1 --format=%h
```

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /tmp/rclone-kaz .
```

- ホストが arm64 の場合は `GOARCH=arm64` にする。
- `sha256sum /tmp/rclone-kaz` を記録し、各ホストへ配る。
- 置き場所は `/usr/local/bin/rclone-kaz` とする。**`/usr/bin/rclone` は置き換えない**（元に戻すときにそのまま使う）。
- 確認: `rclone-kaz serve s3 --help | grep -E 'kaz-vfs-lookup-by-path|no-cleanup'` で2つのフラグが出ること。

## 2. 起動オプションの変更

起動スクリプトで、実行ファイルを `/usr/local/bin/rclone-kaz` に変え、次の2つを**追加する**。既存のオプション（`--poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off --tpslimit 20 --contimeout 10s --timeout 2m --low-level-retries 1 --server-read-timeout 3m` など）は変えない。

```text
--kaz-vfs-lookup-by-path --no-cleanup
```

- **全ホストで `--no-cleanup` を指定すること。** 1台でも指定しないと、そのホストが空になったフォルダを消し、他のホストが覚えているフォルダの ID が無効になるおそれがある（仕様 §6）。
- `--vfs-cache-mode` は `off` のままにする。PUT の API を増やさない近道（仕様 §3.5）は `off` のときだけ働く。

## 3. 入れ替え

1台ずつ行う。

1. そのホストの rclone を **SIGTERM** で止める（SIGHUP は終了しない）。
2. 改修版を新しいオプションで起動する。
3. 起動ログに `Starting s3 server` が出て、JuiceFS の読み書きが通ることを確かめてから、次のホストに進む。

注意: rclone が止まっている時間が約14秒を超えると、そのホストの JuiceFS で、キャッシュに無いデータの読み込みが EIO になり得る（[findings の rclone の停止の節](../../docs/findings.md)）。止めてから起動までを短くする。staging・compaction・metadata は再試行で回復する。

## 4. 適用後に見るもの

- rclone のログ: `Re-reading directory` の頻度（key 指定の操作では出ない。JuiceFS の `gc` などの一覧でだけ出る）、`InternalError`・`Dir.Stat error`（Drive のエラーが 404 ではなくエラーとして出るようになった）、rate limit（`rateLimitExceeded`）。
- JuiceFS: 他のホストが書いたファイルの読み込みで EIO が出ないこと、`timeout awaiting response headers` が増えないこと。
- rclone のメモリ使用量（RSS）: `b.meta`（S3 のユーザーメタデータ）は、他のホストが消した key の分が残るので、PUT の件数に応じて少しずつ増える（1件あたり約 0.5〜1KB の見積もり、未測定）。増え方を見て、必要なら定期的に再起動する。根本的な対策は Phase 2。

## 5. 元に戻す

1台ずつ、SIGTERM で止めて、`/usr/bin/rclone` と元のオプション（`--kaz-vfs-lookup-by-path --no-cleanup` を外したもの）で起動し直す。Drive 上のデータの形式は変えていないので、戻してもデータの変換は要らない。

## 既知の制約

- 他のホストが消した object は、最大で `--dir-cache-time` の2倍（1h なら2時間）まで「有る」と答えることがある。JuiceFS は消えた object を読まないので問題にならない。読んだ場合はエラーになり、古いデータは返らない。
- Drive の同名フォルダの重複（複数ホストが同時に新しいフォルダを作る場合）は、今回の変更の範囲外（TODO、Phase 1b）。
- S3 のユーザーメタデータ（JuiceFS のチェックサム）は、PUT したホストのメモリにしか無い。他のホストが読むときは、JuiceFS のチェックサム検証が省略される（Phase 2）。
