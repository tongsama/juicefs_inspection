# v1.75.2 への移植版の実際の Drive での確認（2026-10-13）

- 対象: rclone `1.75.2-improve-kaz` 9816e18ae（v1.75.2 に kaz の改修を載せ直して merge したもの）を手元でビルドしたもの（`rclone v1.75.2-kaz-dev.9816e18a`、go1.26.9、sha256 `79817fb8004fdb253a2296f5ab6acb96435f59d5b9e82bfab82cc7fa3e8819e5`）。
- 場所: `gdrive_kwatan:/rclone-s3-test`（ユーザー承認のうえ作成し、確認後に `rclone purge` で削除した）。本番の `/rclone-s3` と本番の rclone には触れていない。
- 構成: 改修版の serve s3 を2つ（A: 127.0.0.1:19090、B: 19091）。本番と同じ kaz のオプション `--kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties --kaz-s3-cancel-get-on-disconnect --kaz-s3-list-by-key-order --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off` に、`--tpslimit 5 --log-level DEBUG --dump headers` を足した。認証キーは確認用に生成した値で、確認後に削除した。
- クライアント: `/usr/bin/rclone`（v1.75.1-kaz.2）の S3 backend（設定は環境変数で渡した）。アップロード前の HEAD をしない送り方（`--s3-no-head --s3-no-check-bucket`）。
- 生のログは scratchpad にだけ置いた。

## 結果（観測）

| 確認 | 結果 |
|---|---|
| B で新しい key `chunks/0/1/1000_0_6` を PUT（フォルダも作成）→ A で GET | A で `hello` が読めた（他のホストが作った object がすぐに見える） |
| A で `1001_0_6` を PUT → B で GET | `world` が読めた |
| A で一覧（`lsf -R`） | 正しく返った。DEBUG に `kaz list: prefix="chunks/0/1" … dirs=1 took=436ms` と `kaz list: prefix="" … dirs=4 took=1.2s` が出た |
| A で `crc32c=777` 付きの PUT（一括） | B の HEAD で `crc32c=777`。Drive の properties に `s3m-crc32c=777` |
| A で multipart の PUT（12MiB、5MiB ずつ、`crc32c=55`） | 一時名 `.rclone_temp_multipart_…` から最終名へ server-side move。その後も Drive の properties に `s3m-crc32c=55` が残り、B の HEAD で `crc32c=55`・サイズ 12582912。B の GET の中身は sha256 が一致。一時ファイルは残っていない |
| B で DELETE | Drive から消えた |
| kaz を使わず Drive に直接書いた object（メタデータ無し）を B で GET・HEAD | 中身は正しく読めた。メタデータは無し（`s3m-*` が無い） |
| ログの ERROR・CRITICAL | A・B とも0件 |
| 一覧の取り直し（`Re-reading directory`） | A・B とも0回 |

## 結論

v1.75.2 への移植版で、kaz.2 と同じ動作（ホストをまたぐ読み書き、DELETE、Drive の properties へのメタデータの保存、multipart の rename の後も properties が残ること、既存データの読み出し、一覧で `kaz list:` の行が出ること）を、実際の Drive で確かめた。cancel-get の取り消しと、JuiceFS を動かしての確認は行っていない。
