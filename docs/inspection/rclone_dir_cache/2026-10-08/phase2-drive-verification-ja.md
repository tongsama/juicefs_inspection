# Phase 2（ユーザーメタデータの Drive 保存）の実際の Drive での確認（2026-10-08）

- 対象: rclone `feat/kaz-s3-persist-metadata` e1509bad8 を手元でビルドしたもの（sha256 `4b6992a572c0c4ccbe0cc7305b2bd87c539ed9d71c92e9b1fcd67e28b7e80092`）。
- 場所: `gdrive_kwatan:/rclone-s3-test`（ユーザー承認のうえ作成し、確認後に `rclone purge` で削除した）。本番の `/rclone-s3` と本番の rclone には触れていない。
- 構成: 改修版の serve s3 を2つ（A: 127.0.0.1:19090、B: 19091）、`--kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off --tpslimit 5 --dump headers -vv`。再起動は SIGTERM のあと同じオプションで起動し直した。
- クライアント: `/usr/bin/rclone`（v1.75.1）の S3 backend。メタデータ付きの PUT は `-M --metadata-set crc32c=...`。このクライアントは `-M` のとき、ローカルファイルの情報（atime、uid、mode など）も `X-Amz-Meta-*` として送るので、それらも `s3m-*` として保存される（JuiceFS は crc32c だけを送る）。
- Drive 側の properties は `/usr/bin/rclone lsjson -M gdrive_kwatan:...` で確かめた。生のログは scratchpad にだけ置いた。

## 結果（観測）

| 確認 | 結果 |
|---|---|
| A で `crc32c=777` 付きで PUT → A・B・再起動後の A の HEAD | すべて `crc32c=777` を返した。B の GET の中身も正しい。Drive の properties に `s3m-crc32c=777` |
| 上書き（1MiB、分割アップロードの経路。JuiceFS の chunk と同じ） | `crc32c=1, old=x` → B が `crc32c=2` で上書きすると `s3m-old` が消え `s3m-crc32c=2`。メタデータ無しで上書きすると `s3m-*` は `s3m-mtime`（クライアントが常に送る更新時刻）だけになった |
| 上書き（6バイト、一括アップロードの経路） | 同じ結果。同名のファイルは増えていない |
| 値が124バイトを超えるメタデータの PUT（6バイト、1MiB） | Drive が 403 `propertyLengthLimitExceeded` を返し、PUT は失敗（クライアントには 500）。object は作られなかった |
| 30個を超えるメタデータの PUT | Drive が 403 `propertyCountLimitExceeded`、PUT は失敗。object は作られなかった |
| 既存の object を上限超えのメタデータで上書き | PUT は失敗し、既存の中身と properties は変わらなかった |
| multipart（12MiB、5MiB ずつ） | 開始・3パート・完了のあと、一時的な名前からの移動（Drive の `addParents` の PATCH）を経ても `s3m-crc32c=55` が残り、B の HEAD で返った。一時ファイルは残っていない |
| **既存データとの互換**（改修前の `/usr/bin/rclone` で直接書いた object） | (1) キャッシュの無い B で HEAD・GET できた（メタデータ無し、中身は正しい）。(2) B がメタデータ付きで上書きすると、B と再起動後の A から `crc32c=98` と新しい中身が返った。(3) A の DELETE で Drive から消えた。(4) その後の HEAD は「無い」 |
| API の回数（フォルダはキャッシュ済み、アップロード前の HEAD をしない送り方） | PUT: 名前の検索1回＋アップロード1回（＋クライアントが更新時刻を送るときの PATCH 1回）。自ホストの HEAD 0回、他ホストの HEAD 1回、GET はダウンロード1回。Phase 1 と同じで、properties の保存・読み出しによる増加は無い |
| 一覧の取り直し（`Re-reading directory`） | A・B とも0回 |

## 確認中に分かったこと

- 他のホストが同じ key を上書きした後、自ホストのキャッシュに残っている古い object の情報（古いサイズとメタデータ）が返ることがある（B が上書きした後の A の HEAD で `crc32c=1` が返った）。Phase 1 の lookup モードの仕様どおりの動作で、キャッシュの期限（最大 `--dir-cache-time` の2倍）で解消する。JuiceFS は同じ key を違う中身で書き直さないので、実運用では起きない。
- 上限を超えたときは 500 になる（S3 の 400 `MetadataTooLarge` ではない）。JuiceFS は crc32c しか送らないので、上限には当たらない。

## 結論

仕様 §7.2 の確認項目（ホストをまたぐ往復、上書き時の置き換え、上限、multipart、既存データとの互換、API の回数）は、実際の Drive ですべて期待どおりだった。JuiceFS を動かしての確認（チェックサムの検証が効くこと）は行っていない。
