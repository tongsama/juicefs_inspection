# JuiceFS 改善版の知見と評価

更新: 2026-10-08（キャッシュ cold の VM 起動の節、rclone serve s3 の dir cache の節を追加）。2026-10-07（metadata path の節を追加）。2026-10-06（rclone serve s3 の PUT 詰まりの節を追加）。2026-10-03。対象は Google Drive をバックエンドとする rclone S3 上の JuiceFS CE v1.4.1 系で、VM 仮想ディスク等の巨大ファイルの部分更新です。この文書は現在の入口として、確定した事実と未検証事項をまとめます。詳細な時系列は [agent_memo](../agent_memo.md)、測定値は [ログ解析報告](../option_effects/2026-10-03/report-ja.txt) を参照してください。

## 方針

- fsync、書き込み順序、read-after-write、必要な保存完了を優先する。
- compaction や削除 queue の遅延だけを理由に、正常進行中の処理を人工的な EIO へ変えない。
- 実際の保存失敗、容量不足、quota 超過、キャンセル等を隠さない。保存前の成功応答で PUT 数を減らさない。
- writeback のローカル staging と metadata commit、非同期 cloud upload を分けて扱う。
- 本番データへの変更はユーザーの管理下で行う。調査は読み取りと隔離再現を基本にする。

## 障害の確定した経路

最初の障害では `--max-deletes=-1` が同期 object 削除を意味していた。先行 compaction が旧 slice の DELETE を長時間待ち、raw slice 数が 2,500 に達した Write が同期 compaction を待った。従来の約5分の writer flush 期限が、ホスト側へ EIO を返す経路を確認した。別途、ゲスト ext4 の WRITE I/O error、journal abort、read-only 化の証跡も確認されている。個別イベントの正確な時刻対応と、保存済み filesystem／qcow2 構造の破損範囲は未検証。

writer を期限なし待機にしても、外側の go-fuse watchdog に固定15分の期限が残り、処理完了前に kernel へ EINTR を返す別の経路があった。元の保存・commit はその後も続き、40分以上経って成功した例を確認した。

対応した内容は以下のとおり。

- Read 前の writer flush が失敗した場合、古い metadata のデータを成功として返さず、flush errno を返す。
- ファイル全体の writer flush は既定で期限なし待機。`auto` または正の duration で明示期限を選べる。
- 実際の writer error を別 chunk の待機に隠さず伝播する。
- FUSE の固定 watchdog を無効化。実エラーと kernel の interrupt 通知は別に扱う。
- 負の max-deletes の同期削除を WARN／help／ドキュメントへ明記する。

これらは停止・失敗の経路の修正であり、既に破損したゲスト filesystem や qcow2 の修復を実施したものではない。[技術診断の詳細](development/vm_io_diagnostics.md)

## 小さい PUT のサイズと生成条件

サイズは次のように区別する。

| 観測 | 意味 |
|---|---|
| slice の `raw_length`／finish 長 | slice の圧縮前の長さ |
| object key 末尾のサイズ | その object の圧縮前 block 長 |
| `PUT payload_bytes` | chunk 層から object storage へ渡した圧縮済み buffer 長 |
| staging ファイル長 | raw data に checksum／footer 等を含む物理ファイル長 |
| `juicefs stats` の `put`／`put_c` | 表示期間の PUT bytes と request count。通常更新行では率となり、通常 write／compaction／retry が混在 |

観測 volume は zstd、block 4 MiB。耐久 run では raw slice が 1 KiB 未満のものは44件だった一方、通常成功 PUT の圧縮 payload が1 KiB未満のものは47,414件あった。「1 KiB未満の PUT」が同じサイズの raw slice を意味するわけではない。

同一 file の同一64 MiB chunk で、slice の未保存の末尾に適合する書き込みは再利用できる。隙間、新しい slice との重なり、既に保存へ回した full block 等には制約がある。別 chunk は別の slice 管理で、任意のランダム書き込みが一つにまとまる仕組みではない。

## オプションと時系列

以下は実装の既定値と、10/03 の最終確認時点の設定を分けたもの。値は恒久的な現在状態を保証するものではなく、[選択 config](../option_effects/2026-10-03/current-config-selected.json) に基づく。

| 設定 | 既定 | 最終確認時点 | 意味 |
|---|---:|---:|---|
| slice-flush-wait | 5s | 15s | slice 作成からの age による freeze |
| slice-flush-idle | 1s | 10s | 同 slice の最後の更新からの idle による freeze |
| writer-flush-timeout | 0s | 0s | ファイル全体の pending 保存・commit の期限なし待機 |
| writer-reuse-window | 4 | 16 | 古い非適合候補を freeze する距離。1〜64 |
| compaction-gc-mode | legacy | deferred | durable dead-ref を根拠とする非同期 local retirement |
| compaction-scheduler | legacy | priority | 既存 slice 数を使う有界の背景処理通知 |
| max-deletes | 10 | 10 | リモート削除 worker 数。負値は同期削除 |
| max-uploads | 20 | 18 | upload 枠。以前の起動設定と同一条件とは限らない |

FUSE request timeout は修正版の生成設定で0。writeback=true、runtime threshold=4,194,305 bytes、upload-delay=0、buffer=1 GiB。

**ユーザー訂正後の時系列:** 耐久テスト合格は `30s / 10s` 運転。10/03 の再起動で `15s / 10s` に変更した。15秒版は短期観測であり、同じ長期耐久合格として扱わない。

タイマーは freeze 契機で、cloud PUT 完了の期限ではない。wait の時計は書き込みで reset されず、idle は同 slice への書き込みで reset される。fsync 等が先に freeze すれば、タイマーの残り時間にかかわらず再利用できなくなる。

reuse16 は、同 chunk の pending slice list を作成順の新しい側から調べた index 0〜15 の非適合候補を、その理由だけでは freeze しない。探索は16件で打ち切らず、適合判定が freeze 距離判定より先なので、17番目以降でも未freeze・適合なら再利用できる。frozen の未commit entry も位置へ数える。16 MiB、16秒、16回の write、厳密な slice 上限や最終アクセス順の管理ではない。

## 明示的 flush の中身

耐久期間の明示的 freeze 800,560 slice は次の内訳だった。

| 呼出元 | freeze slice 数 | 明示的 freeze 内の割合 |
|---|---:|---:|
| fsync | 682,152 | 85.2% |
| fallocate | 89,607 | 11.2% |
| Read | 28,801 | 3.6% |
| close 系 | 0 | 0% |

これは呼出数ではない。writer barrier は fsync 335,774回、fallocate 56,437回、Read 167,768回等を記録した。1 barrier が複数 slice を閉じることも、新しい freeze がなくても既に frozen な pending を待つこともある。

file writer flush は同 inode の全 pending chunk／slice を対象に、freeze、Finish の保存、作成順と依存関係を守った metadata Write、pending 除去を待つ。既存 file 全体を書き直すものではない。Read 前も読み取り範囲だけの flush ではない。writeback では cloud upload 全体の完了とは別。

全3期間で age freeze は0、明示的／window freeze は全件 age10秒未満。idle freeze は8／16／14件と少ない。今回の負荷では fsync 等が主要制限で、wait30→15の直接効果は見えていない。window は全3runで16なので単独効果未分離。局所テストの window4→16で commit10→9は、実機 PUT 削減率ではない。[詳細集計](../option_effects/2026-10-03/writer_report.md)

## GC 分離・scheduler と upload の修正

初版 deferred は compaction 完了を削除 queue への投入待ちから分離したが、local staging の取消は remote worker の順番待ちに残っていた。後続補正では、1,024件の有界 hint dispatcher が local retirement を先に行い、remote queue へ非待機で送る。満杯／停止／local error では durable marker を残す。

deferred は正の max-deletes、書込可能、背景回復有効が必要。従来の約1時間の回復周期、metadata format、物理 worker を維持する。hint overflow 時は local 回収も周期回復へ延期され得る。remote garbage 量が過負荷でも有界になる保証はない。

同 cachedStore 内では、writeback の ACK 前に pending／active 状態を公開する。進行中 PUT と stage callback は完了まで追跡し、取消後の再登録や、PUT完了前に削除 marker を消すことを防ぐ。local unlink error は再試行可能な形で返す。永久 tombstone や他 client との分散 upload lease は追加していない。

Redis CopyFileRange／Clone／BatchClone では、source chunk 読み取り前の WATCH と競合時 retry により、古い snapshot から削除済み reference が復活する競合を補正した。全書込 client にこの保護が必要。他 client の PUT／DELETE 競合は process-local guard だけでは解決しない。

priority scheduler は既存 Read／Write の raw count を用い、3固定 bucket、最大11 worker、1 inodeあたりactive1、active＋pending1,024の有界通知を使う。毎write追加DB参照や全file／chunk走査は追加しない。cold hintの取りこぼし後の厳密 eventual 保証はなく、2,500 slice 同期 compaction と手動 force の安全網は残る。inode lock 構造の変更は、今回ユーザーが対象外とした。

## ログから確認できた効果

| 観測 | 補正前30s/10s | 補正後耐久30s/10s |
|---|---:|---:|
| 通常 slice finish | 101,766 | 843,306 |
| 通常 PUT 試行 | 73,575 | 82,695 |
| 通常 PUT 試行／finish | 0.7230 | 0.0981 |
| compaction 込み PUT 試行／finish | 0.8729 | 0.2768 |
| fsync P95 | 約450 ms | 約230 ms |
| fsync P99 | 約1.42 s | 約0.79 s |
| raw2,500同期 compaction | 1件 | 0件 |

通常 PUT／finish 比率は86.4%、compaction込み比率は68.3%低い観測値。負荷・期間・手動 compaction が同一でないので、統制された機能単独の改善率とは呼ばない。

耐久期間では retired 通常 ID のうち同ログに PUT がないものが762,146、local retirement 成功 callback が880,895。不要 staging の早期回収の狙いと整合するが、ファイル削除数や反事実の必ず回避した PUT 件数とは異なる。成功 payload 量の約99%は compaction が占めるので、cloud 転送量が86.4%減ったとは言えない。

耐久 fsync335,774回と writer barrier560,104回はすべて終了 errno0、fsync最大14.756秒。通常 metadata Write非0とchecksum不一致も0。実際に freeze があった区間は約16時間半で、背景ログを含む19時間すべてをVM高負荷時間としない。ユーザーは目立つ問題なく耐久テスト合格と報告し、compaction集中時の少量CPU増は許容して耐障害性を優先する方針。

force=true slowログ3,649件は再帰callframeの完了数で、独立した手動 request 件数ではない。親 total に子孫の処理時間が入るため、最大3時間52分を metadata待ち／fsync停止と解釈しない。

## Read 側の未解決事項

**保存側の成功から、全I/O無エラーとは判断できない。**

- 10/02 20:42:48、inode596155の `read file: input/output error` を確認。直前の複数 readSlice 警告は context canceled。
- 後の旧process metricsに VFS EIO 累積1,207があった。内部GET retryだけでは増えない返却error counterだが、操作・caller・時刻labelがなく、固定prefix内件数やQEMU／guest障害1,207件には割り当てられない。
- reader.done は fileReader の sticky EIOを保持し、後続 Read／waitForIOから VFS／FUSEへ返し得る。先頭callerのctx取消がsingleflightのduplicateへ伝播し、共有retry counterでEIOになる仮説がsource上成立する。今回reader sourceは未変更で、3bed baselineも含めた再現が必要。
- 直近GET404は同exact keyが約3.63秒後に成功し、該当IDのDELETE／retirement記録がない。永久欠落とは評価しないが、原因は未特定。
- GETheader timeout後に約96.82秒のRead成功もある。Read全体の遅延と、writer preflushの待ちは別。

[Read EIO のソース経路と留保](../option_effects/2026-10-03/writer_read_error_findings.txt)。実保存失敗を隠す方向で直さず、安全な再現を先行する。

## rclone serve s3 の PUT 詰まり（2026-10-06）

詳細と証跡は [報告](../rclone_put_timeout/2026-10-06/report-ja.md) にある。

- **症状**: JuiceFS で `slow request: PUT ... exceeded maximum number of attempts, 1 ... timeout awaiting response headers`（30.00秒）が続き、転送が大きく遅れる。
- **文言の意味**: `attempts, 1` は JuiceFS 側の SDK 設定（`RetryMaxAttempts=1`。再試行は JuiceFS が自前で行う）。30秒は JuiceFS の固定値 `ResponseHeaderTimeout`（`pkg/object/restful.go`）で、`--put-timeout` では変えられない。rclone がエラーを返したわけではなく、rclone の `--low-level-retries` とも無関係。Drive のレート制限も観測されていない。
- **原因**: rclone VFS は Drive の変更通知（既定の `--poll-interval 1m`）を受けるたびに、ディレクトリのキャッシュを捨てる。次の Open では、そのディレクトリ（例: `chunks/5/5913`、約2,800件）を全件読み直し、その間は同じディレクトリへの操作が待たされる。変更通知の発生源は JuiceFS 自身の PUT／DELETE なので、自分の書き込みでキャッシュを壊し続ける。ディレクトリが大きくなるほど遅くなる（PUT 完了は10分あたり 1,226件 → 275件）。
- **悪循環**: JuiceFS が30秒で切った PUT も、rclone は最後まで実行する。JuiceFS の再送と合わせて重複アップロードになり（同じ key が3回完了したものが179件）、変更通知も増えて、さらに遅くなる。
- **対策**: rclone に `--poll-interval 0` と `--dir-cache-time 1h` を指定する。前提は、`/rclone-s3` の配下をこの rclone だけが変更すること。同じドライブの他のディレクトリは、この rclone の VFS のキャッシュ対象外なので影響しない。2026-10-06 10:40 にユーザーが適用し、その後の約20分で無効化0回、PUT タイムアウト0件、PUT 完了は1分あたり約35件 → 約340件になった。長時間の観測はまだ。
- **rclone の停止**: SIGTERM で停止する。SIGHUP は終了せず、VFS のディレクトリキャッシュを捨てるだけ。`rclone rc core/quit` は `--rc` 付きで起動したときだけ使える。rclone の停止が約14秒を超えると、キャッシュにないデータを読む VM に EIO が返る可能性がある（読み込みの再試行は10回で、待ちの合計は約13.5秒）。staging・compaction・メタデータは再試行で回復するので、不整合は起きない。

### 関連する JuiceFS の挙動（ソースで確認）

- **staging からのアップロード**: 1回の処理での上限は3回で固定。超えてもエラーは返さず、WARN も出さない。staging と pending の登録を残したまま、1分ごとの scan で無期限に再送する。成功するか、GC で不要になるまで続く。
- **compaction の書き込み**: writeback を無効にした同期アップロードで、上限は `--io-retries`＋1（既定11回）。超えたら中断して、後でやり直す。
- **キャッシュに入る書き込み**: writeback では、書き込みも staging のハードリンクとして読み取りキャッシュに入る。アップロードが終わるまでは LRU の追い出し対象外で、`--cache-size` にも数えない。完了したら LRU に加わり、最終アクセス時刻はアップロード完了時刻になる。`--cache-large-write` が効くのは、staging を通らない経路（compaction の出力と、staging に失敗したブロック）だけ。`--cache-expire` は、LRU の追い出しでは使われない。
- **圧縮の扱い**: キャッシュも staging も展開済み（生）のデータで、圧縮はアップロードのたびに行う。`--cache-size` や staging の容量は、生のサイズで消費される。zstd のレベルは `ZSTD_LEVEL = 1`（"fastest"）で固定。オプションにしない理由はソースにも履歴にもない。レベルは展開の互換性に影響しない。

## rclone serve s3 の dir cache と複数ホスト（2026-10-08）

詳細は [ソース調査](../rclone_dir_cache/2026-10-08/source-investigation-ja.md)、[実機検証](../rclone_dir_cache/2026-10-08/drive-verification-ja.md)、[設計](superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md)、[手順書](../rclone_dir_cache/2026-10-08/deploy-runbook-ja.md) にある。

- **構成**: 各ホストがそれぞれ `rclone serve s3` を動かし、同じ Drive フォルダを共有する。
- **原因（ソースと再現で確認）**: serve s3 の操作はすべて VFS のパスの walk を通る。VFS は一覧済みのディレクトリに名前が無ければ、期限内は Drive に問い合わせずに「無い」を返す。`--poll-interval 0` では他のホストの PUT が反映されないので、他のホストが作った object を最大 `--dir-cache-time` の間「無い」（404）と返し、JuiceFS の Read が EIO になる。期限を短くすると、key 指定の操作でも各階層のディレクトリを全件一覧する。
- **付随していた問題**: Stat のどのエラーも 404 になる（rate limit も「無い」に見える）。DELETE が自ホストのキャッシュに無い object を消さずに成功を返す。`--no-cleanup` が配線されていない。S3 のユーザーメタデータ（JuiceFS の crc32c）はプロセスのメモリにしか無く、DELETE でも消えない。
- **対策（改修版 rclone、`rclone-v1.75.1-kaz.1` に含めて公開し、本番に適用済み）**: `--kaz-vfs-lookup-by-path` で、キャッシュに無い名前を Drive に1件だけ問い合わせ、「無い」をキャッシュしない。404 は「無い」ときだけにし、他は 500。DELETE は他ホストの object も実際に消し、すでに無ければ成功。`--no-cleanup` を配線。PUT（`O_CREATE|O_TRUNC`、cache-mode off）では存在を問い合わせないので、PUT の API 回数は改修前と同じ。
- **実機で確認したこと**: 2026-10-08、ユーザーが公開版 `rclone-v1.75.1-kaz.1` で2台構成を試し、公式 v1.75.1 で起きていた問題が解消したことを確認。テスト用フォルダでは、他のホストが作った object がすぐ読める。DELETE で Drive から消える。key 指定の操作で一覧の取り直しが0回。既存 object の上書きで同名ファイルが増えない。
- **JuiceFS 側**: GET・PUT では 404・500・503 を区別せずに再試行する。404 を特別に扱うのは Head と Delete だけ（[記録](../rclone_dir_cache/2026-10-08/juicefs-error-handling-ja.md)）。
- **Phase 2（2026-10-08、`rclone-v1.75.1-kaz.1` に含めて公開し、本番に適用済み）**: `--kaz-s3-persist-metadata` と `--drive-kaz-properties`（必ず一緒に使う）で、`X-Amz-Meta-*` を Drive の properties `s3m-*` に PUT と同時に保存し、どのホストからでも・再起動後も返す。`b.meta` は使わない（メモリが増え続ける問題も解消）。API の回数は増えない。上書きはメタデータを丸ごと置き換える。上限（1件124バイト、30個）を超えると PUT は失敗し、object は作られない。改修前に書かれた object は「メタデータ無し」として扱われ、JuiceFS は検証を省略する（[実機の確認](../rclone_dir_cache/2026-10-08/phase2-drive-verification-ja.md)）。
- **本番への適用**: 2026-10-08、まず単体で適用し（新しい chunk に `s3m-crc32c` が付くことを確認）、続いて公開版 `rclone-v1.75.1-kaz.1` を2台構成に入れて、公式 v1.75.1 で起きていた問題が解消したことをユーザーが確認した。起動オプションは [手順書](../rclone_dir_cache/2026-10-08/deploy-runbook-ja.md) のとおり（`--kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties`）。
- **残る制約**: 改修前に書かれた object にはチェックサムが無く、検証されない。他のホストが同じ key を上書きした後は、キャッシュの期限まで古い情報が返ることがある（JuiceFS は同じ key を書き直さない）。Drive の同名フォルダの重複は範囲外（2026-10-08 時点で0件）。

## キャッシュ cold の VM 起動（2026-10-08）

[解析報告](../vm_boot_cold_read/2026-10-08/report-ja.md)。VM `ubuntu26-test`、qcow2、約26分。

- **律速は GET の往復の遅延**: 4MiB の GET 1回が中央値1.9s で、同時数が1本でも8本でも変わらない。同時数は1〜2本が時間の72%。GET は1,351回、約5.3GiB で、QEMU の read（約1.6GiB）の約3.5倍。compaction と Read 前の flush（合計21s）は主因ではない。
- **`--prefetch` は zstd の volume では効かない**: prefetch のキューへの登録は Range GET（`loadRange`）の成功時だけ（cached_store.go:768）。Range GET は圧縮なしのときだけ使う（`seekable`、:867・:155）。ファイルの readahead は、順番に読むときだけ（reader.go:419-435）。
- **ゲストの I/O エラーと read-only 化の原因は、30秒前後かかった read／write**: ゲストの SATA コマンドタイムアウト（既定30s）の3回が、ホストの約27〜32s の操作と時刻で一致した。ホストの JuiceFS はすべて OK を返していた（EIO なし）。VM の disk は virtio のつもりが `bus='sata'` だった。virtio-blk ならゲスト側のコマンドタイムアウトは基本的に無い。ゲストの ext4 は不整合の可能性があり、fsck が必要。
- **`--get-timeout` と `--kaz-get-header-timeout` の違い（2026-10-09）**: `--get-timeout`（既定 60s）は GET 全体（応答を待つ時間と本体の受け取りの合計）の上限。`--kaz-get-header-timeout`（改修版、既定 0＝無効）は、ブロックの GET で応答ヘッダが返るまでの待ちだけの上限。遅いが流れている本体は切らない。共通の HTTP client の `ResponseHeaderTimeout: 30s` は、PUT・DELETE・LIST と全部の object storage に効くので、そのまま残す。
- **LRU の優先度**: 稼働中はメモリ上にある。再起動したときは block ファイルの atime を初期値にする（disk_cache.go:1060）。JuiceFS は atime を書き戻さず、キャッシュディレクトリは relatime なので粗い。
- **GET が25〜30s かかるのは、Drive のダウンロードの応答待ち**: 同じ時刻の他の GET は約1s で終わっており、tpslimit の待ちではないと推測。30s で切れるのは JuiceFS の `ResponseHeaderTimeout: 30s`（restful.go:157）で、`--get-timeout` より先に効く。再試行は1.2〜8.7s で成功した。tpslimit の上限（20/s）には21:33〜21:40 に張り付き、主に PUT と DELETE の分だった。

## 巨大ファイル random I/O の metadata path（2026-10-07、ソース調査）

詳細と実装計画は [調査結果と実装計画](superpowers/specs/2026-10-07-metadata-random-io-optimization-plan.md)。実測はまだで、以下はソースで確認した構造。

- FUSE write は memory copy だけで返り、metadata RTT は 0。commit は非同期だが、fsync・close・Read は同 inode の全 pending commit を待つ。
- Redis の Meta.Write は 1 slice あたり 4 RTT（WATCH／GET／MULTI…EXEC／UNWATCH）。同 inode の commit は open-file lock と Redis txLock で client 内完全直列（upstream #6398 に該当）。1 inode の commit 上限は約 1/(4×RTT) slice/s。
- NewSlice は既に 4096 個単位で予約しており、RTT は 4096 回に 1 回。
- 自分の commit 後は chunk cache を invalidate し、次の GetAttr の mtime 差で全 chunk cache も消える。Read 前 flush は読む範囲に関係なく全 pending が対象。
- Redis client-cache（meta URL の `client-cache`）は attr と entry のみで、chunk は対象外。有効時に doWrite が古い attr を読む経路がソース上成立する（未再現）。
- **実測（2026-10-07、unsafe、Ubuntu インストール）**: 最大の待ちは qcow2 の cluster 割り当てに伴う `fallocate(ZERO_RANGE)` で、15,343回・合計 3,330s・平均 217ms。うち 173ms は、範囲に関係なく同 inode の全 pending commit を待つ flush。強制 freeze の89%がこの flush によるもの。commit は平均 43ms（≒ 4 RTT、RTT 約10ms）で62,317件、計測時間の52%を占めた。[解析報告](../metadata_random_io/2026-10-07/install-report-ja.md)
- **同条件の raw では約36分（qcow2 は約84分、どちらも debug 付き）**。fallocate は0回。raw で残る最大の待ちは Read 前 flush（合計799s、最大24.8s）で、flush 中は同 inode の Write も止まる。
- **Phase 1（`--writer-flush-scope=range`、本体 18e641b8）の qcow2 計測（2026-10-07）: 約84分 → 約53分**。Read と Fallocate 前の flush を、触る chunk の pending と、その依存に限定した。待ちは handle のロックの外で行い、ロックの中でもう一度確認する。fallocate の合計は 3,330s → 1,887s、commit は −44%、write の合計は −80%。Read 前の待ちは変わらなかった。残る待ちは in-range の commit（約41ms/件）と Meta.Fallocate で、次は group commit。mtime が古い slice の commit で巻き戻る upstream 由来の問題も、同じ commit で修正した（scope に関係なく有効）。fsync・close は全体の flush のまま。[報告](../metadata_random_io/2026-10-07/install-range-report-ja.md)
- **Phase 2（`--meta-write-batch=64`、本体 f9308391）の qcow2 計測（2026-10-07）: 約53分 → 約43分**（変更前の約84分からは −49%）。同じ chunk の連続した slice を1回の transaction でまとめ、transaction 数は 35,006 → 20,654（−41%）、batch は平均3.2件。batch 1回の時間は単発と同じ約41ms。Read 前の待ちは 352s → 66s（−81%）、Fallocate 前の待ちは 1,264s → 905s（−28%）。残る主な待ちは、Fallocate 前の commit 待ち、Meta.Fallocate（約44ms/回）、単発の commit で、どれも Redis の transaction 1回 ≒ 41ms の直列に起因する。[報告](../metadata_random_io/2026-10-07/install-batch-report-ja.md)

## 検証範囲

対象の Redis／SQLite／MemKV、queue飽和、共有reference、marker回復、upload取消、fsync／read-after-write、実kernel FUSE等を隔離して検証した。PostgreSQLはユーザー指示でsource reviewのみ、runtime未実施。他KV engineの全runtime atomicityまで保証しない。

Go1.26では既存mockeyのruntime.duffcopy／duffzero link不整合がある。Go1.25.11では、`TMPDIR`をroot filesystem上に置いて `pkg/chunk` 通常49PASSの記録がある。全体raceは今回版11FAIL／44DATA RACE、未変更3bed archiveでも同じ11件等／48DATA RACEを再現した既存問題で、全suite race-cleanとはしない。今回追加のretire9testsはrace3回成功。[Go1.25検証記録](../gc_backlog/2026-10-02/improvement/go1.25_chunk/)

writebackの既存stage処理はfile／directory fsyncによるhost power-loss耐久性を追加保証していない。プロセス・I/O遅延下の試験と、停電／host crash／全cloud保存の保証は分ける。

**実例（2026-10-06）**: 13:38:34 に WSL（juicefs を動かす OS）がクラッシュし、再起動後の staging scan（19:06）で `invalid file size 0` が16件出た。0バイトの staging は mtime 13:38:04〜13:38:24、つまりクラッシュ直前約30秒（dirty writeback の既定 30s）に書かれたもの。tmp→rename の後でもデータが未 flush のまま rename だけ journal に残った典型。通常稼働で0バイトの正式名が残る経路は source 上にない。Redis で逆引きすると、16件中13件は inode 603330 の chunk 225 内で後続 slice に完全に上書き済み（可視0B）、3件は chunk から参照なしで、実害はなかった。ユーザーが該当ファイルを手動で削除済み。
**対策（本体 e6ab89b8、`release-1.4.1-kaz.2` に merge 済み）**: `--writeback-fsync`（既定 true）を追加。staged block を rename 前に fdatasync し、rename 後に親ディレクトリと、この process でまだ永続化していない祖先ディレクトリを fsync する。失敗すると stage は失敗扱いになり、既存の直接アップロードに切り替わる。`=false` で従来どおり（OS クラッシュで失われ得る）。read キャッシュの書き込みは対象外。

## 次の調査とログ保管

優先は Read の取消／共有retry／sticky EIOの再現とcaller診断。ほかに fallocate mode、Read前flushの範囲、window16単独比較、CPU内訳、永続pending-only GC／cold work回復などが残る。現在の作業は [TODO](../TODO.md) を参照する。

DEBUGはログ容量の約99.7〜99.9%、正常checksum DEBUGだけで60〜72%を占めた。checksum検証維持と成功ログ出力抑制は別に判断する。元ログは変更せず、固定prefix合計7,038,915,328 bytesのSHA・集計・例外証跡を保存した。

旧／新clientが採取時点でともにlogfdを持っていたため、古い名前だけでclosedと決めない。[ログ一覧](../option_effects/2026-10-03/log-inventory.json)は当時のsnapshotで、圧縮・移動・削除前にはproducer状態を再確認する。
