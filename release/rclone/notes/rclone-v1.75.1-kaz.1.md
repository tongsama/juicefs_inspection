# rclone-v1.75.1-kaz.1

rclone v1.75.1 をベースにした改修版の最初の版です。Google Drive を JuiceFS の object storage として `rclone serve s3` で使うための改修を含みます（fork: `tongsama/rclone` の `1.75.1-improve-kaz` ブランチ、commit `dab33da31190b350cf75214080b362e0a50584d0`、Go 1.26.0 でビルド）。

## Phase 1: 名前の引き方と削除の改修

- `--kaz-vfs-lookup-by-path`（既定では無効）
  - directory cache に無い名前を、1回の NewObject で引きます。見つからなかった結果（miss）はキャッシュしません。
- `serve s3` は、path が存在しないときだけ 404 を返します。それ以外のエラーは 500 を返し、実際の失敗を「無い」に偽装しません。
- DELETE は、他のホストが作った object も削除します。
- `--no-cleanup` が実際に効くようになりました。
- 新しい key への PUT が使う Drive API の呼び出し回数は、upstream と同じです。

## Phase 2: ユーザーメタデータの Drive 保存

- `--kaz-s3-persist-metadata`（serve s3）と `--drive-kaz-properties`（drive backend。`rclone.conf` では `kaz_properties = true`）を**必ず一緒に**指定します。
- `X-Amz-Meta-*` を Drive の properties `s3m-<name>` として保存します。これにより、JuiceFS のチェックサム（crc32c）が再起動後も残り、どのホストからも見えます。
- `--vfs-cache-mode off` が必要です。

## 本番での使い方（起動オプションの例）

```
rclone serve s3 gdrive:/rclone-s3 --kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off
```

## 既知の制約

- 別のホストが同じ key を上書きした後、ホストによっては、キャッシュの期限が切れるまで古い metadata や size を返すことがあります。JuiceFS は同じ key を上書きしないため、JuiceFS の用途では影響しません。
- Drive に同名のフォルダが重複している状態は、対象範囲外です。
- Windows 版には `rclone mount`（cmount）が含まれます。使うには WinFsp が必要です。

## 詳細

- 仕様（lookup-by-path）: [2026-10-08-rclone-lookup-by-path-design.md](https://github.com/tongsama/juicefs_inspection/blob/main/docs/superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md)
- 仕様（metadata の永続化）: [2026-10-08-rclone-s3-persist-metadata-design.md](https://github.com/tongsama/juicefs_inspection/blob/main/docs/superpowers/specs/2026-10-08-rclone-s3-persist-metadata-design.md)
- 本番適用の手順書: [deploy-runbook-ja.md](https://github.com/tongsama/juicefs_inspection/blob/main/rclone_dir_cache/2026-10-08/deploy-runbook-ja.md)
