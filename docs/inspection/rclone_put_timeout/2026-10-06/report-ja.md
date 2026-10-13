# rclone serve s3 への PUT が30秒でタイムアウトし、詰まった状態が続く件（2026-10-06）

## 解析対象（読み取りのみ、元ログは無変更）

| ログ | 解析時点の先頭 bytes | sha256（その prefix） |
|---|---|---|
| `~/.rclone_serve_s3-2026-10-06T09-57-58.101.log` | 104,857,569 | eae1a53a515c8703e765ac16b93b93495f75e9cdb48cf8813cd135092b7413a5 |
| `~/.rclone_serve_s3.log`（書き込み継続中） | 39,826,270 | fa4995920b14fc54445fae784cbd3abd6b47eaff4ffd46a3c091b01d2cb7a642 |
| `~/.juicefs/diagnostics/vm-io-20261006-021106.log`（書き込み継続中） | 1,355,293 | 3da8e5f9bff77f003352fcfb9ce338d4ffd74303d1b22cbab73a4dc54b15cb1e |

件数の一部は、上記より短い prefix（現行の rclone ログは 353,020 行、10:15 まで）で数えた値。稼働構成は rclone v1.75.1（`--vfs-cache-mode off --tpslimit 20 --low-level-retries 1`、`--poll-interval`・`--dir-cache-time` は既定値）、JuiceFS は PID 3080。

## 観測事実

1. JuiceFS 側のエラー文言は rclone から返ったエラーではない。
   - `exceeded maximum number of attempts, 1` は、JuiceFS が AWS SDK の再試行を1回に固定している設定（`pkg/object/s3.go:587` の `RetryMaxAttempts = 1`）による。JuiceFS は SDK の再試行を使わず、自前で再試行する。
   - `net/http: timeout awaiting response headers`（30.00秒）は、JuiceFS の HTTP クライアントに固定で入っている `ResponseHeaderTimeout: 30s`（`pkg/object/restful.go:157`）。rclone が30秒以内に応答ヘッダーを返さなかったため、JuiceFS が自分で切った。
2. rclone は PUT を受け付けてから、Drive への送信を始めるまでに大きく待たされている。
   - 例: `5913778_4`。10:10:27 に `CREATE OBJECT` と `OpenFile`、10:11:00 に `Open`、10:11:08 に `>Open`、10:11:16 に `Sending chunk`、10:11:18 に完了。Drive への送信自体は約2秒で終わっている。
3. 待ちの正体は、`chunks/5/5913` ディレクトリの全件再読み込み。
   - Drive の変更通知（`changeNotify`）が来るたびに `chunks/5/5913: invalidating directory cache` が出て、その後に `Reset virtual modtime` が約2,800行（＝ディレクトリ内の全エントリ）まとめて出る。
   - 現行ログ（09:57〜10:15）では、invalidation が126回、`chunks/5/5913` の Reset が約34万行。それ以外のディレクトリの Reset はほぼない。
   - invalidation と全件再読み込みは、約6〜8秒に1回の周期で、ほぼ休みなく繰り返されている。この間、同じディレクトリへの Open は待たされる。
4. ディレクトリが大きくなるほど、処理件数が落ちている（前のログ、10分ごと）。

   | 時間帯 | invalidation | Reset 行 | PUT 完了 |
   |---|---|---|---|
   | 07:2x | 201 | 44,538（1回あたり約220件） | 1,226 |
   | 09:2x | 124 | 157,727（約1,270件） | 942 |
   | 09:3x | 100 | 190,249（約1,900件） | 538 |
   | 09:5x | 68 | 163,194（約2,400件） | 275 |

   JuiceFS の30秒タイムアウトは 09:2x から急増（58→206→225→322→305件/10分）。
5. JuiceFS が切った PUT も、rclone 側では最後まで実行されている（同じ key が後で完了）。JuiceFS は同じ key を再送するため、重複アップロードになる。
   - 現行ログで完了した key のうち、3回完了 179件、2回 10件、1回 191件。rclone の処理能力の半分以上を重複に使っている。
   - 中断が伝わったのは `WriteFileHandle.New Rcat failed: couldn't list directory: context canceled` の数件だけ。
6. Drive のレート制限（403/429）や pacer による待機は見られない。`--low-level-retries 1` による再試行の打ち切りが原因、という証拠もない。ERROR の大半は `Dir.Remove not empty`（DELETE 後に親ディレクトリを消そうとして失敗するもので、無害）。

## 解釈（ソースは未確認。ログからの推定）

- rclone の VFS は、Drive の変更通知を受けると、該当ディレクトリのキャッシュを無効にする。次にそのディレクトリの中のファイルを Open すると、Drive から全件を取り直す（ページングするので、件数に比例して時間がかかる）。その間、同じディレクトリへの操作は直列化される。
- 変更通知の発生源は、主に JuiceFS 自身の PUT／DELETE。自分の書き込みが、自分のディレクトリキャッシュを壊し続けるループになっている。
- JuiceFS の slice ID は1,000個ごとに1つのディレクトリ（`chunks/5/5913/` など）にまとまる。1 slice は最大16 block あるので、1ディレクトリに数千件のオブジェクトがたまる。ディレクトリが埋まるほど再読み込みが重くなり、30秒を超え始める。
- 30秒を超えると、JuiceFS の再送で重複 PUT が増え、変更通知も増え、さらに遅くなる、という正のフィードバックがかかる。いったんこの状態に入ると抜けにくい理由はこれと考えられる。slice ID が次のディレクトリ（5914）に移ると、一時的に軽くなるはず（未確認）。

## 対策候補（未検証）

- rclone `--poll-interval 0`: Drive の変更通知を止める。対象の Drive フォルダーに書き込むのがこの rclone serve s3 だけであることが前提。
- rclone `--dir-cache-time` を長くする（例: 1h 以上）: 期限切れによる全件再読み込みの頻度を下げる。単一書き込みの前提は上と同じ。
- JuiceFS 側: `ResponseHeaderTimeout` 30s は固定値で、`--put-timeout` では変えられない。改修版で設定可能にするか延長すれば重複 PUT は減るが、根本原因（rclone の再読み込み）は残る。

## 補足: JuiceFS の再試行上限を超えたときの挙動（source 確認、2026-10-06）

- staging（writeback）経由のアップロード（`cached_store.go` の `uploadStagingFile` と即時経路）: 1回の処理での上限は3回（待ちは0、1、4秒＋各PUTの30秒）。3回とも失敗すると、エラーはどこにも返さず、WARN も出さない（各試行は DEBUG だけ）。`pendingKeys` の登録と staging ファイルは残り、`scanDelayedStaging`（upload-delay=0 のとき1分ごと）が再び queue に積んで、また3回試す。成功するか、GC で不要になるまで無期限に繰り返す。アプリ側への書き込み成功は staging への保存時点で返しているので、この失敗はアプリに伝わらない。
- compaction（`pkg/vfs/compact.go` で `SetWriteback(false)`）: 同期アップロードで上限は `--io-retries`＋1 = 11回（待ちは try² 秒）。超えると compaction は中断され、WARN `compact ... (max tries) upload block ...` が出る。今回のログでは 09:41:43 に1件（`5913904_0`）。

## 対策を適用した後の観測（2026-10-06 10:40 以降、ユーザーが rclone を再起動）

- rclone は PID 100624（起動 10:40:29）。`--poll-interval 0`、`--dir-cache-time 1h`、`--rc --rc-addr localhost:5572` が追加されている。`--rc-user`／`--rc-pass` は指定なし。
- 解析対象（読み取りのみ）:

  | ログ | 先頭 bytes | sha256（その prefix） |
  |---|---|---|
  | `~/.rclone_serve_s3-2026-10-06T10-59-10.176.log` | 104,857,520 | 18498a79d9abf6a4d7cae0250e8b18dc955beded7b6c4348fa62b21ead27738b |
  | `~/.rclone_serve_s3.log`（書き込み継続中） | 1,357,426 | 51fb18bcb71217b1df83fa664059b2173491ba44e76cbb102cb4dd4aa7609006 |
  | `~/.juicefs/diagnostics/vm-io-20261006-021106.log`（書き込み継続中） | 1,828,740 | 6ed697d50976baf3a2cd268cf6c64c4fde33a85522e863b1a4c0e2246e9f70c9 |

- 10:40:30〜11:00 の rclone: `changeNotify` 0回、`invalidating directory cache` 0回、`Reset virtual modtime` 56行（以前は1分あたり約2万行）。PUT 完了は1分あたり約340件（10:41〜10:45）。対策前は約35件。
- 同じ期間の JuiceFS: `slow request: PUT` は 10:40 の32件だけ（再起動をまたいで処理中だったもの）。10:41 以降、PUT の30秒タイムアウトは0件。compaction は1チャンク（64MiB）あたり6〜13秒で完了している（`slow compaction` の WARN は出るが成功）。
- 観測は再起動後の約20分だけ。長時間の安定性、ディレクトリの切り替わり、1時間ごとのキャッシュ期限切れのときの挙動は、まだ確認していない。
