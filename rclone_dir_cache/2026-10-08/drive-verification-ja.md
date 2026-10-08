# 実際の Drive での検証（Phase 1、2026-10-08）

- 対象: rclone `feat/kaz-vfs-lookup` 078531045 を手元でビルドしたもの（`rclone v1.75.1-DEV`、sha256 `3c6e9fb288e6b090d68b3de76c7479137713381901e52d8f8b51f1f95ce860eb`）。
- 場所: `gdrive_kwatan:/rclone-s3-test`（ユーザー承認のうえ作成し、検証後に `rclone purge` で削除した）。本番の `/rclone-s3` と、本番の rclone には触れていない。
- 構成: 改修版の serve s3 を手元で3つ起動した。
  - A（127.0.0.1:19090）と B（19091）: `--kaz-vfs-lookup-by-path --no-cleanup --poll-interval 0 --dir-cache-time 1h --vfs-cache-mode off --tpslimit 5 --dump headers -vv`。2台のホストに見立てた。
  - C（19092）: 比較用。`--kaz-vfs-lookup-by-path` を付けない以外は A・B と同じ。
- クライアント: `/usr/bin/rclone`（v1.75.1）の S3 backend を接続文字列で指定した。認証キーは検証用に生成した値で、検証後に削除した。
- 回数の数え方: 各操作の前後で serve のログの行数を記録し、その間の `HTTP REQUEST`（Drive API への要求）と `Re-reading directory` を数えた。生のログはリポジトリに置いていない（scratchpad のみ）。

## ホストをまたぐ動作（観測）

| 操作 | 結果 | Drive API（A / B） | 一覧の取り直し |
|---|---|---|---|
| B で PUT `chunks/0/1/1000_0_6`（フォルダも作成） | 成功 | 0 / 20 | 0 |
| A・B で一覧（キャッシュさせる） | `1000_0_6` | 9 / 1 | 0 |
| A で PUT `1001_0_6` | 成功 | 6 / 0 | 0 |
| **B で `1001_0_6` を読む（A が後から作ったもの）** | **`hello`（読めた）** | 0 / 2 | 0 |
| B でもう一度読む | `hello` | 0 / 1 | 0 |
| A で PUT `1002_0_6` → **B で DELETE** | 成功。**Drive から消えた**（`lsf` で確認） | 6 / 2 | 0 |
| A で `1002_0_6` を読む（B が消したが、A のキャッシュに残る） | エラー（古いデータは返らない） | 20（クライアントの再試行10回） / 0 | 0 |
| A で DELETE `1001_0_6` → B で同じ key を DELETE（すでに無い） | どちらも成功。Drive から消えた | 1 / 2 | 0 |

- `Re-reading directory` は、A・B・C のどのログでも0回だった。
- A が消えた object を読んだとき、serve は 200 とヘッダーを先に返し、その後 Drive が 404 を返したため本文を送れなかった（ログ: `open file failed: googleapi: Error 404`、`http: wrote more than the declared Content-Length`）。クライアントはこれを壊れた応答としてエラーにした。仕様 §3.3 の訂正のとおり。

## PUT 1回あたりの Drive API（観測）

JuiceFS に近い形にするため、アップロード前の HEAD をしない送り方（`rclone rcat --s3-no-head --s3-no-check-bucket`）で、フォルダがキャッシュ済みの状態から新しい key を2件ずつ PUT した。

| serve | 名前の検索（files.list） | アップロード（POST） | 更新時刻（PATCH） | 合計 |
|---|---|---|---|---|
| A（lookup モード） | **3** | 1 | 1 | 5 |
| C（lookup モードなし） | 1 | 1 | 1 | 3 |

- 増えた2回の出どころ（ソースで確認）: `vfs.OpenFile` の `O_CREATE` は、まず `vfs.Stat` で存在を確かめ（vfs/vfs.go:578）、続けて `Dir.Create` の `d.stat` でもう一度確かめる（vfs/dir.go:1043）。lookup モードは「無い」をキャッシュしないので、どちらも Drive に問い合わせる。3回目は Drive backend の Put がアップロード前に行う `NewObject`（lookup モードなしでも行われる）。
- PATCH は、テストに使った rclone クライアントが更新時刻のメタデータ（`X-Amz-Meta-Mtime`）を送るために起きる。JuiceFS はこのメタデータを送らないので、PATCH は出ないと推測する（未確認）。
- 推測: JuiceFS の PUT 1回あたりの API は、lookup モードなしの2回から4回に増える。2026-10-06 の観測（PUT 完了は1分あたり約340件、約5.7件/秒）に当てはめると、PUT だけで約11回/秒から約23回/秒になり、`--tpslimit 20` を超えて PUT が律速される可能性がある。

## 追加の修正後の再計測（9b75066b4、sha256 `5f43b032dd591de4fd7109423f6f4f12c470fa020e7e7d82a8acb410d9117e9f`）

ユーザー承認の追加修正（lookup モードかつ `--vfs-cache-mode off` で `O_CREATE|O_TRUNC`（`O_EXCL` なし）で開くときは、VFS がファイル自体の存在を Drive に問い合わせない）を入れて、同じ手順で測り直した（テスト用フォルダは再作成し、検証後に削除した）。

| 操作 | 結果 |
|---|---|
| A で新しい key を PUT（3件、フォルダはキャッシュ済み） | 各 PUT とも `files.list`（名前の検索）1回 ＋ POST ＋ PATCH。lookup モードなしと同じ回数 |
| B で A の `2003_0_6` を読む（B は初回アクセス） | `hello`。API 11回（root からフォルダの ID を解決する初回分を含む） |
| B から、B のキャッシュに無い既存の `2001_0_6` を上書き | Drive の同名ファイルは増えず（4件のまま）、中身が `world` に置き換わった |
| 一覧の取り直し | A・B とも0回 |

## 結論

- 目的の動作（他のホストが作った object がすぐに見える、DELETE が実際に消す、key 指定の操作で一覧を取らない）は、実際の Drive で確認できた。
- 新しい key の PUT で Drive API が2回増える問題は、追加の修正（9b75066b4）で解消し、lookup モードなしと同じ回数になった。
