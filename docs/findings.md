# JuiceFS 改善版の知見と評価

更新: 2026-10-03。対象は Google Drive をバックエンドとする rclone S3 上の JuiceFS CE v1.4.1 系で、VM 仮想ディスク等の巨大ファイルの部分更新です。この文書は現在の入口として、確定した事実と未検証事項をまとめます。詳細な時系列は [agent_memo](../agent_memo.md)、測定値は [ログ解析報告](../option_effects/2026-10-03/report-ja.txt) を参照してください。

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

## 検証範囲

対象の Redis／SQLite／MemKV、queue飽和、共有reference、marker回復、upload取消、fsync／read-after-write、実kernel FUSE等を隔離して検証した。PostgreSQLはユーザー指示でsource reviewのみ、runtime未実施。他KV engineの全runtime atomicityまで保証しない。

Go1.26では既存mockeyのruntime.duffcopy／duffzero link不整合がある。Go1.25.11では、`TMPDIR`をroot filesystem上に置いて `pkg/chunk` 通常49PASSの記録がある。全体raceは今回版11FAIL／44DATA RACE、未変更3bed archiveでも同じ11件等／48DATA RACEを再現した既存問題で、全suite race-cleanとはしない。今回追加のretire9testsはrace3回成功。[Go1.25検証記録](../gc_backlog/2026-10-02/improvement/go1.25_chunk/)

writebackの既存stage処理はfile／directory fsyncによるhost power-loss耐久性を追加保証していない。プロセス・I/O遅延下の試験と、停電／host crash／全cloud保存の保証は分ける。

## 次の調査とログ保管

優先は Read の取消／共有retry／sticky EIOの再現とcaller診断。ほかに fallocate mode、Read前flushの範囲、window16単独比較、CPU内訳、永続pending-only GC／cold work回復などが残る。現在の作業は [TODO](../TODO.md) を参照する。

DEBUGはログ容量の約99.7〜99.9%、正常checksum DEBUGだけで60〜72%を占めた。checksum検証維持と成功ログ出力抑制は別に判断する。元ログは変更せず、固定prefix合計7,038,915,328 bytesのSHA・集計・例外証跡を保存した。

旧／新clientが採取時点でともにlogfdを持っていたため、古い名前だけでclosedと決めない。[ログ一覧](../option_effects/2026-10-03/log-inventory.json)は当時のsnapshotで、圧縮・移動・削除前にはproducer状態を再確認する。
