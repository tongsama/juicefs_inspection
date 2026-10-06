# Agent memo — JuiceFS VM I/O調査

更新: 2026-10-03（日本時間。文書分離と最新の評価を反映）

## 継続ルール

- やり取りは日本語。
- GitHubへのpush、gh等のリモート変更前は必ずユーザー確認。
- 最初からuntrackedなファイルを黙ってadd/commitしない。
- 大きい調査はsub-workerへ分担し、ネストはmax_depth=4以内。
- 新規/変更する関数・クラスには目的が分かるコメント。Mermaidノード改行は `<br>`。
- 方針変更は本ファイルに履歴として記録する。1000行を超える場合は入口を残し分割を検討。

## 入口

- 調査ドキュメントのGitルート: このディレクトリ。入口は `README.md` と `docs/findings.md`。
- 本体リポジトリ: `juicefs/`（独立Git、親側の `.gitignore` で除外）。
- 元の引き継ぎ: `juicefs_vm_corruption_handoff_codex.md`（同内容の添付テキストもあり）。
- 実装計画: `docs/superpowers/plans/2026-10-01-vm-io-investigation.md`
- 追加ログの使い方・制約: `docs/development/vm_io_diagnostics.md`

## 2026-10-01 調査履歴

開始HEAD: `febf149abf6b1dde730b98423b00ed796b23927f`（v1.4.1 + slice timerユーザーコミット）。v1.4.1タグは `0b90c7db5a929ae6adc5faad948d108efd2c99f9`。開始時リポジトリの未コミット/untrackedはなし。既存slice timer変更を保持。

### 確定したローカル再現

`TestVFSReadFlushError`: 実memkvとmemory object storageで"old"を書きfsync、新しい"new"のMeta.Writeだけ失敗させた。修正前はEIO/ENOSPC/EDQUOT/ENOENTの4ケースともReadがerrno=0,n=3,data="old"を返した。

ReadはFlush errnoを伝播するよう修正。失敗時n=0、buffer不変、handle op/reader lock解除を検証。修正前失敗→修正後成功を確認。これはstale read経路の修正であり、実機の5分EIO根因を修正したと主張しない。

### 引き継ぎの補正

- Redis txnにはinode key hashを使う内側lockがある。baseMeta.Writeのopenfile lockをper-chunkにするだけでは直列化解消にならない。
- background compactChunk自体はopenfile lockを取得しない。
- >=2500の同期compactionはMeta.Writeがinode lockを保持したままobject処理/既存背景compactionを待つ。
- done=true/err=0/committed=falseはVFS commit未完了を示すが、Redis RPUSH前と断定できない。backend登録済みのstat/compaction待ち、先頭sliceの順番待ちも含む。
- 後刻のchunk0 LLEN=682は、その障害時点や別chunkの>=2500を排除しない。

### 現在の修正方針

baseMeta.Write、Redis txn、compactChunk、VFS commitThreadに段階DEBUGと1秒以上のWARN集計を追加。ロック順序・flush deadline・compaction閾値・SetWriteback(false)は維持。実機ログでlock_wait/doWrite/stat/compact/Redis txLockを分離する。

upstream #6398は同様の4KiB random write/inode lock報告。今回閲覧時Open、直結修正PRは確認できず。実機の待機箇所の証明ではない。

レビューsub-workerは重大/重要問題なし。compaction queue DEBUGをbaseMeta全体mutexの外へ移す軽微指摘を反映。

### 検証（最終結果は以下を追記する）

- 回帰4errno成功。`go test -race ./pkg/vfs -run '^TestVFSReadFlushError$' -count=1 -timeout=3m` 成功。
- VFS全体とpkg/fsのテスト成功（テスト用Redis起動後）。
- 初回make test.meta.coreはRedis未起動、make test.pkgはGlusterFS開発ライブラリなし/coverディレクトリなしで失敗。
- Go 1.26.4ではpkg/chunkテストの既存mockeyがruntime.duffcopy/duffzeroリンクエラー。pkg/fuseのFstatDeleted時刻属性不一致はbaseline確認中。
- テスト用Redis 8.0.5をapt download+dpkg-deb展開で `/tmp/juicefs-redis-test-20261001` に用意（OSインストールなし）、127.0.0.1:6379で一時実行。本番接続なし。完了時停止する。
- coverは `/tmp/juicefs-cover-20261001` への一時symlink。テスト生成sqliteファイルとともに完了時削除する。
- リモート変更、commit、addなし。

### 検証結果の補足

- unchanged HEADのgit archiveからもpkg/chunkリンクエラーとFUSE `TestFUSE/FstatDeleted` 時刻不一致を再現。今回の差分由来ではない。
- Redis配置後のmake test.meta.coreは `TestRedisClient` 成功、その後 `TestLoadDump/Metadata_Engine:_tikv` がSKIP_NON_CORE=trueでも未配置TiKVへ接続してFATAL終了。フルsuite成功とはしない。
- targeted meta: Redis/SQLite/MemKV client + MemKV + quota edge/owner + canceled KV txnで成功（66.384s）。最終変更後にも再実行して結果を追記する。
- 最終ビルド: `/tmp/juicefs-vm-io-20261001`（`1.4.1+2026-10-01.febf149a-vmio-local`）。
- Git生成patch: `/tmp/juicefs-vm-io-20261001.patch`（tracked Go差分のみ。新規docs/memoは含まない）。unchanged HEAD archiveに `git apply --check` 成功。実ソースの差分が正本。
- 最終ソースでtargeted meta成功（67.504s）、VFS全体成功（9.405s）、pkg/fs成功（2.042s）、回帰race成功（1.132s）。
- 最終ビルド成功、gofmt/diff --check成功。テスト用RedisはCtrl-Cで停止、一時cover symlinkと今回生成のsqliteファイルを削除。新規docs/plan/memo以外のテスト残骸をリポジトリへ残さない。

## ユーザー提示の実機起動設定（2026-10-01）

実機実行はユーザー側で実施予定。以下は提示された設定で、こちらから実機適用・稼働確認はしていない。

```bash
JUICEFS_CUSTOM_OPTIONS="--slice-flush-wait 30s --slice-flush-idle 10s"
juicefs mount \
  --background --backup-meta 3601 --prefetch 0 --buffer-size 1024 \
  --entry-cache 1.0s --dir-entry-cache 1.0s --writeback \
  --writeback-threshold-size 0 --cache-dir="${CACHE_DIR}" --cache-size 15G \
  --cache-eviction lru --cache-expire 0s --cache-large-write \
  --verify-cache-checksum extend --free-space-ratio 0.2 --upload-delay 0 \
  --max-uploads 30 --max-stage-write 0 --max-downloads 60 \
  --get-timeout 60 --put-timeout 60 --max-deletes=-1 --io-retries 5 \
  --check-storage ${JUICEFS_CUSTOM_OPTIONS} -o allow_other \
  "${MOUNT_FROM}" "${MOUNT_TO}"
```

このRetries=5ではflush計算値は整数演算24s、最低値で5分。slice 30s/10sはwhole-file flush deadlineを延長しない。

今回の回答では、限定した再現期間に `juicefs --debug mount` と実行ごとの別 `--log` ファイルを推奨。既存ログは削除せず保持。slow WARNはdebugなしでも出るが、戻らない処理のphase確認にはdebugが有効。設定比較のため、それ以外の提示オプションはまず維持する（ユーザーの適用判断は未確認）。

## 実機障害解析（2026-10-01、前回の設定維持案を訂正）

ユーザーがVM内ファイルシステム破損を検知してVMを停止。提示ログ: `/home/kwatanabe/.juicefs/diagnostics/vm-io-20261001-183557.log`、PID989530。JuiceFS自体の停止は確認しておらずログは解析中も追記あり。

### 今回の5分EIOの直接経路は確認済み

- inode596153 chunk0背景compact: 18:46:13.311124にmetadata phase。このphaseはRedis更新だけでなく古いslice object削除を含む。
- 18:55:31.001431、slice3727383のdoWriteは42.074811msで成功、raw slices=2500。
- 18:55:31.001475から同期compactへ入り、001486に先行compact queue待ち。
- 19:00:31.024267 flush5分期限。19:00:31.033106 fsync(596153,1) EIO、300.074009秒。
- timeout stack: foregroundはbaseMeta.Write→compactChunk once=trueの10ms sleep。背景の同inode/chunkはredisMeta.doCompactChunk→deleteSlice→deleteSlice_→cachedStore.Remove→rSlice.Remove→cachedStore.delete→WithTimeoutでobject DELETE待ち。
- 19:04:28.187113背景compact終了、total18m26.307667724s、metadata18m14.875939104s。同期側のqueue waitは8m57.185735463s後に終了し、さらに次のcompaction実施。

### 設定原因と修正案

`--max-deletes=-1`は無制限並列ではない。MaxDeletes<=0では削除workersなし、MaxDeletes==0のみ削除抑止、負値はdslices=nil経由で呼び出し元の同期削除となる。旧slice数百〜1000件のDELETEを逐次待ち、m.compactingが残る。その間2500到達Writeがinode lock保持のまま同期compactを待ち、5分EIO。

前回の「その他の設定を維持」はmax-deletes負値の意味の見落とし。履歴として残し、今回訂正。次の隔離試験はCLI既定の `--max-deletes 10`へ変更を推奨。その他の設定はまず維持。ユーザーの適用・改善結果は未確認。

これは今回EIOの説明であり、ゲスト破損の全原因やsilent corruption不存在を証明しない。WAN Redisの直列化は構造上存在するが、このイベントのdoWriteは42ms、lock_wait345nsで主待機箇所ではない。

正値のworker queueは有界（MaxDeletes*10240）。十分長い期間にqueue飽和する場合は再びenqueue待ちが起こり得る。positive変更のみで永久に全I/O問題解消と主張しない。急なJuiceFS停止やcache/staging削除は実行しない。

今回はソースの動作変更を追加せず、ログと既存ソースから設定修正を特定。診断docへ時系列・意味の補正・次試験案を追記。原ログは削除/変更していない。抽出stackは `/tmp/juicefs-incident-20261001-goroutines.txt`。

## EIOとゲスト破損の因果関係の説明を厳密化（2026-10-01）

今回確定しているのはホスト側fsync EIOの発生経路まで。EIOが返っただけでゲストFS構造破損やqcow2内部破損が必ず発生する、と説明してはいけない。今回のpending sliceはRedis登録済みで同期compaction後処理待ちであり、ログ単独では欠落データ/誤ったバイト/flush順序違反を証明しない。

QEMUはI/O error policyでゲストへのreport/VM pause等に分岐する。virtio-blkではflush失敗もwrite error handlingへ渡り、reportならVIRTIO_BLK_S_IOERR。実機QEMU設定・ゲストFS種類・ゲストエラーログは未確認。提示された--writebackはJuiceFS設定で、QEMU cache設定と同一視しない。

ext4のjournalはcommit/flush順序とjournal replayでmetadata整合性を保護し、commit I/O異常ではabort/read-only等になり得る。journal abortやread-onlyは保護動作であり、それだけで保存済みFS構造破損とは限らない。通常ext4は全ユーザーファイルデータのatomic updateまで保証しないため、FS構造整合とファイル/DB内容整合は別。

今回fileWriter.flushの期限はpending commitをrollback/cancelしない。fsyncEIO後も処理が続く（実機19:04確認済み）。EIOは『何も書かれていない』の意味ではないが、遅い完了自体から順序逆転や腐敗を断定しない。

次の裏付け: ゲストの実エラーメッセージと時刻、FS種類、QEMUのcache/werror/rerror/デバイス設定。Aborting journal/I/O error、inode/checksum破損、application file破損、qcow2 container破損を区別する。

一次資料: https://www.qemu.org/docs/master/system/qemu-manpage.html 、https://github.com/qemu/qemu/blob/master/hw/block/virtio-blk.c 、https://www.kernel.org/doc/html/latest/filesystems/ext4/journal.html 、https://www.kernel.org/doc/html/latest/admin-guide/ext4.html 、https://github.com/torvalds/linux/blob/master/fs/jbd2/commit.c 。

## ゲストdmesg確認と待機優先のユーザー方針（2026-10-01）

ユーザー提示guest dmesg: kernel7.0.14-20-pve。dm-1/ext4。uptime1229秒でwriteback/JBD2/fdatasyncタスクが122秒超待ち、1351秒で245秒超待ち、1356.383876でvda WRITE I/O error、その直後journal abort。inode3543253/3542823でdelayed allocation error30(EROFS)、Data will be lost、JBD2 journal superblock write error、ext4 superblock write error、remount read-only。

以前の『guest保護停止かは未確認』を更新: guestへの書込I/Oエラー、journal abort、dirty data保存失敗警告、read-only化まで確認。既存の保存済みmetadata構造不整合/qcow2構造破損の有無は依然未検証。提示guestログはboot相対時刻なのでhost19:00:31と正確な対応にはboot時刻等が必要。

ユーザーの明示優先方針: ファイル破壊回避を最優先、遅延やcompaction完了待ちを理由にEIOを返すくらいなら正常完了までホスト同期I/Oをwaitすることを望む。この優先方針を今後の修正判断に反映。

『ホストのファイルが読める/qcow2形式が正常』だけではguest書込の成功・flush順序・保存完了を保証しない。ゲストメモリ内のdirty dataは、ディスクI/O失敗後に保存されず失われ得る。

待機優先の修正案（まだ承認/実装前）: VFS全体flushのtimeoutを独立設定し、0sで期限によるEIOを無効化、未指定は現行Retries由来deadline維持、正のdurationは指定期限。待機中warn/進捗情報は維持、保存完了前にsuccessを返さない、実upload/meta/ENOSPC等の失敗は隠さない。現行のrequest cancel→EINTRは別経路として維持し、期限なしでもguestやQEMU側timeoutを全て防げると主張しない。

コード修正はboundedの新機能設計としてbrainstorming SKILLを確認。具体仕様の了承後にTDD。現時点ではソース実装未追加。

## 待機優先方針の承認と現在の実装（2026-10-01）

ユーザーは遅延/compaction/削除queue待ちは完了待機、実保存失敗/ENOSPC/EDQUOTは隠さず、未完了成功は禁止の3方針に同意。追加質問へ『既定も期限なし待機に変更する』と明示回答。

新WriterFlushTimeout: 0/default無期限、auto(-1 sentinel)旧Retries期限、正duration任意期限。CLI default0s、invalid/negative reject。default無期限では5分ごとWARNのみ、elapsedでEIOにしない。既存ctx cancellation猶予は維持。実f.errは別chunkがpendingでも伝播。期限超過時にcommit済みなら成功が優先。

負のMaxDeletes警告/CLI説明追加。queue容量は変えず、満杯はslot待ち。新規meta regressionで実memKV background compactのqueuewait、2500thWrite登録成功後のsynccompactwait、drain後成功・visible metadataを検証。object生成はmeta試験のcallback模擬。

reviewでauto duration overflowを発見しRED再現→最大durationへ飽和を実装。Go呼出側の無効負timeoutはwriter初期化でWARN+0へ正規化。普通の書込とwritebackの両方で回帰、実5分経過試験は実行中。

後続TODO: `TODO.md` 優先2にslice30s/idle10sでも1KB未満PUTが多い問題の調査・意図した集約挙動の修正を記録。ユーザー指示どおり現I/O待機タスク後に実施。未調査を断定しない。圧縮前rawとPUTpayloadを別計測、timer以外のfreeze・fsync・read・slice再利用条件を追う。

### 最終検証・成果物

- 新CLIとVFS/Meta新回帰の最終race 3回: pkg/vfs 3.077s、pkg/meta 1.842s、cmd 1.252s成功。
- VFS全体11.536s、pkg/fs2.089s成功。Redis/SQLite/MemKV+quota/cancel+新meta対象69.350s成功。
- 実5分越えdefault待機テストJFS_TEST_LONG_FLUSH_WAIT=1: 304.029s成功。通常/writebackとも未完了時に返らず、release後success。通常suiteでは明示環境変数なしならskip（CIで毎回5分待たせない）。
- レビュー指摘auto overflow/Go負duration検証/docs食違い/cancelgraceを対応し再レビューで重大/重要指摘なし。
- 初回mixed raceでは共有/tmp memkv設定を新queue試験が読み込んでformat不一致。新client.Reset()をInit前に追加して隔離し、race3回で成功。
- make test.cmdはsudo認証のため失敗（sudo: A terminal is required to authenticate）。今回CLI経路の個別通常/raceは成功。以前のGlusterFS不足/TiKV未配置/chunkmock Go1.26/FUSE baseline失敗の制約は継続。全suite成功としない。
- 最終ビルド `/tmp/juicefs-vm-io-wait-20261001` : 1.4.1+2026-10-01.febf149a-vmio-wait。旧 `/tmp/juicefs-vm-io-20261001` は5分期限の旧調査ビルドなので取り違えない。
- 継続TODO優先1完了、優先2小さいPUTは未着手。writebackのcloudへの非同期upload耐久性/既存retry限界は変更していない。

## PostgreSQLソース確認（2026-10-01、実テストはユーザー指示で未実施）

ユーザーが実機テストを担当、--max-deletes10設定済みと報告。結果はまだ未確認。追加依頼『metadb=PostgreSQLでも今回の変更が問題ないかソースで確認。実テスト不要』に従い、テスト/ビルド/DB起動・接続は行っていない。

確認結果: postgres登録→newSQLMeta→pgx、Name()はpostgresに戻る。dbMeta.doWrite/SQL doCompactChunkはSQLite共通。前者はinode ForUpdate、slice upsert/ref+inode属性を同一txnで更新、SQLslicecount→commonbaseWriteで2500threshold。期限なしFlush/Readerror伝播/compact待ち/MaxDeletes警告/queueは共通コードなのでPostgresにも適用。固有の不整合は今回の修正範囲では発見なし。

相違: SQLiteはtxnでinodes={1}としてclientlock一括直列化。PGはinode毎slot lock+DBrow ForUpdate。SQLdoCompactChunkはchunk/ref更新txnが完了してから参照を読み、obsoleteobject削除/queueenqueue。queue満杯はSQLtxn終了後の待機だがcommoncompacting flagやouterbaseWrite openfile lockは待機中残る。defaultno-deadlineflushはその待機をEIOへ変換しない。

実保存失敗を隠さない: pinnedXorm.Transactionはsession.Commit成功前にsuccessを返さない。dbMeta.txnは最大50試行(既存、Meta.Retriesとは別)、PGshouldRetry固有処理あり、最終driver/SQL/commiterror→errno→commitThread f.err→Flush/Readへ伝播。DB独自timeout/接続障害やretry限界は今回無期限化の対象外。Redis固有transactionログはPGに出ないがcommonwrite/compaction/slicecommitログとtxmetricsは有効。

詳細はdocs/development/vm_io_diagnostics.md のPostgreSQL source review節。runtimePostgres健全性/性能確認と混同しない。実装コードの追加変更は不要と判断、記録だけ更新。

## 派生ブランチとローカルコミット（2026-10-01）

ユーザー報告: --max-deletes10で実機稼働中、以前FSが壊れていたような場面でも今のところ問題なし。継続試験中の観測であり、完全解消の確定とは扱わない。

ユーザー明示依頼: 現ブランチから派生ブランチを作りローカルコミット。remoteは公式repoなのでpush禁止。

- 派生元: improve/flush-wait-alter @ febf149abf6b1dde730b98423b00ed796b23927f（そのまま維持）
- 現checkout: fix/vm-io-wait-policy
- ローカルcommit: 3bed0d82eecaaacc7f635f6e033f1433e2196bca
- title: fix(vfs): wait for pending writes without a default deadline
- 15files: 今回のGo修正/新回帰test/CLI docs/diagnostics docs/調査planのみ。初期untrackedはなく、新規は全て今回作成の対象を明示git addした。
- commit直前の対象VFS/Meta/CLI regression再実行成功（0.706s/0.104s/0.066s）。PostgreSQL実テストは指示どおり未実施。
- commit後リポジトリworkingtree clean。push/fetch/PR/remote変更なし。
- agent_memo.md/TODO.mdはrepoの親workspaceにあり、このcommitへは含まない。小さいPUT後続調査はTODO.md優先2、未着手。


## 小さいPUT調査の続き（2026-10-01）

開始時: fix/vm-io-wait-policy / 3bed0d82eecaaacc7f635f6e033f1433e2196bca、repo clean（初期untrackedなし）。AGENTS/memo/TODO/diagnosticsを確認、writer/source・chunk/upload・既存ログをsub-workerへ分担。remote操作、add/commitなし。元ログ/本番VM/DB/cache/staging/objectは変更しない。

### 確定したサイズ・設定

- PUT key末尾はraw block長。古いPUT logはcompressed payload長なし。新payload_bytesは圧縮後のstorage interface境界長。暗号化wrapper利用時は暗号化前なのでprovider object長と同一視しない。
- freeze/finish raw_lengthはslice全体span。stagingはraw block＋checksum(32KiB毎4bytes)＋tier footer(tier0なら無し)。slice全体とblock単位、file statサイズを区別。
- `/home/kwatanabe/mnt_juicefs` がfuse.juicefs mountであることをmountinfo確認し、root `.config`の必要fieldだけ読み取り（secret出力なし）。Version1.4.1+2026-10-01.febf149a-vmio-wait、SliceFlushWait30s/Idle10s、WriterFlushTimeout0、MaxDeletes10、zstd、block4194304、WBtrue、threshold4194305、UploadDelay0。EncryptAlgo文字ありでもEncryptKey無し、KeyEncryptedは設定secret暗号化のfieldなのでデータ暗号化の証明ではない。実process exe/現在DBdriverは独立確認していない。
- CLI→getVfsConf→NewDataWriter→writerの伝達とfebf149a変更を確認。派生元変更は固定age5s/idle1sを設定可能化しただけでslice再利用/明示flush/block upload条件は変えない。0/負timerは5s/1sへ戻す。

### 旧診断ログの集計（現在待機版のログと混同しない）

参考log先頭144,316,877bytes/953,415行、18:36:03.847499〜20:24:40.937939、SHA256 9f92d051a6e36551eaeb4b90aaedaf848f667e2767d0ab55d163aee1f2a6264c。全16,491PUTイベント、success16,456/fail35(HTTP response headers timeout)。slice IDをmetadata writeとcompaction outputへ相関、未分類0/交差0。

- 通常write: success15,719、raw4KiB9,627(61.24%)、<=64KiB94.31%、raw<1KiB11(全8bytes)、raw4MiB52。成功raw合計811,012,184bytes。
- compaction: success737、raw4MiB700(94.98%)、raw合計3,006,255,104bytes。大きいobjectは主にcompaction。compactionはSetWriteback(false)、staging経由なし。
- 通常writeはraw4KiB級が多数。本logではraw<1KiB多数とはいえない。各raw4KiBがzstdでprovider<1KiBになった可能性はあるがactual entropy/payload分布は未確認。fsync/readログはslowだけなので頻度原因の証明は不可。
- 永続成果物: `small_put_investigation/2026-10-01/{analyze_historical.py,result.json,put-events.tsv}`（親workspace、repo外）。scriptはこの旧log固定prefixを対象とし新PUTpayload構文対応の汎用parserではない。

### 集約条件と安全な局所計測

- 再利用はunfrozen sliceの末尾未完block〜末尾のみ。gap、別64MiB chunk、既complete block上書きは別slice。overlapで再利用不可なら探索停止。最新から5番目以降の再利用不可sliceはfreeze。
- freeze契機: age、idle、>800sliceの選択flush、writable_window、commitThread wait*2、64MiB full slice、明示fileflush。full blockのFlushToはsliceをfreezeせずPUT開始。memory圧は直接freezeせずbackpressure、1000sliceでnew write待ち。depはfreeze契機ではなく先行growing sliceのcommit待ち。
- Read/fsync/close/fallocate/truncate/CopyFileRange等の明示flushはtimerを待たない。explicit_flushだけではcaller種別不明、accesslog等が必要。
- `pkg/vfs/small_put_test.go`: 実VFS＋mem metadata/object、30s/10s、wb両モードのsafe temp cache、latest write readback。512x2 sequential→1024 one PUT、partial overwrite→512 one、gap/chunks/fsync/read→512 two、4MiB block overwrite→4MiB+512。元の集約設計を検証する計測であり、behavior修正のRED回帰とは呼ばない。
- 128KiB zeros: none131072/lz4524/zstd22 payload bytes。4KiBzeros zstdも<1KiB。実VMデータの圧縮率の証明ではない。
- WB durability test: cloud PUTをblockして、raw512＋checksum4=stagefile516bytes、fsync metadata commit完了とreadbackを確認、cloud解放後zstdpayload19bytes。fsyncをcloud完了待ちへ変えていない。
- 初fixtureはSelfCheck未呼出でWBthreshold0となり実staging非経由。SelfCheck/AutoCreate/threshold assertions/staging_write_bytes>0を追加後の最終3回を正式結果とする。TempDirを削除する前にupload gaugeとcache UsedMemoryをdrain。
- memKVのInitは共有/tmp/juicefs.memkv.setting.jsonを書くことも確認。fixtureに既存file backup/restore（無ければ新生成分remove）追加、_FUSE_STATE_PATHも専用TempDirへ隔離。初期試験が生成したsmall-put-test設定は識別して削除。他設定は保持。

### 今回の実装範囲と未解決

新DEBUG slice freeze reason＋raw_length＋age/idle＋started_unix_ns、finishでsliceID対応。PUT paramにpayload_bytes追加。既存5箇所freezeを同じfilelock下のhelperへ同義置換。既存Read errno伝播、defaultno-deadline、実error伝播、commit順/dep、block flush、stage/cloud処理保持。集約の動作変更は未実施。

タイマーのみではgapや明示barrierを跨げない。必要ならpending read overlay、sparse-range/coalescing、fsync跨ぎdurable staging indirectionを別設計。未保存success・fsync/順序/read-afterwrite緩和でPUT削減しない。最小動作修正は実機freeze理由の比率が確認できてから判断。

ユーザーへ「1KB未満」がrclone/Drive object size、HTTP transfer、key末尾のどれかoptional質問を送信。回答未受信の時点では推定しない。本番への診断build適用や設定変更は行っていない。

### 検証と制限

- diagnostic testは追加前reason/payload欠落でRED、追加後GREEN。correctedSmallPUT tests3回成功(0.205s)、VFS全体12.236s成功（今回のみローカル6379 testRedisを起動）。
- memory経路のSmallPUT＋前Read failure/Flush/WriterFlush regressions race3回成功(3.735s)。disk writeback raceは別結果。
- 未変更HEAD3bed0d82 gitarchiveへ新計測testだけcopyしてstageFull対checkFreeSpace raceを再現。今回diagnostic由来ではない。初直接upload試験でrawFull/cache-noOp raceも検出、これらは独立baseline未検証。データ破壊原因と断定しない。
- make test.pkgはGlusterFS開発file不足、cover dirなし、既知Go1.26mockey link問題で失敗。全suite/race成功とはしない。
- sub-worker2名がsource/測定test/診断diffをreview、blocking指摘なし。fixture isolation指摘を反映。
- `/tmp/juicefs-small-put-diag-20261001` build成功、version1.4.1+2026-10-01.3bed0d82-smallput-diag-local。現在稼働待機版と区別する未適用診断build。

### 観測元のユーザー回答とstats定義（2026-10-01）

ユーザー回答: 「1KB未満」は `juicefs stats --verbosity` object欄のputとput_cから観測。圧縮後なのは理解しており、PUT回数が多い割にサイズが小さい問題を重視。rclone/Driveファイルやkey末尾の観測ではない。

cmd/stats.go179-181/printDiffにより通常更新行put=Δ圧縮後bytes/interval秒、put_c=ΔPUTduration histogram count/interval秒。比率は期間内completedstorage.Putの平均圧縮payload/call（丸め/切捨てとbinaryunitsあり）、全PUT<1KiBの分布証明ではない。cachedStore.putはAPI戻り時に成功失敗ともbytes/count加算。JuiceFS再試行は再加算、SDK内部試行数は別。byteは失敗時も渡したbuffer長でwire実転送量ではない。normal/compaction/stage reupload混在、WBなのでwrite/fsync時間帯とcloudupload完了帯もずれる。

現在`.stats`必要metricsだけ2回読み取り保存 `small_put_investigation/2026-10-01/live-stats-{first,second}.json`。累積は過去からの値で、旧log集計期間の値ではない。平均statsが小さい原因仮説: 実raw4KiB級blockの大量生成＋zstd圧縮。頻繁freezeの契機は未確認（fsync頻度等のソース上整合だけで断定しない）。新payload＋freeze診断で期間対応・normalcompaction分離する。

最終補足: 21:38:02.002532〜21:40:20.608613 JSTの現mount `.stats` deltaはPUTbytes405,661,497/calls722、平均561858.0bytes（約548.7KiB）。この約139秒窓はユーザーの1KiB未満観測窓ではなくnormal/compaction混在。平均が期間で変わることと分布が必要な点を補足。Fsync/read累積31,696/70,134は各freeze件数ではない。

修正後diskWB race3回も既存stageFull競合で失敗、結果test-writeback-race-final.logへ保存。rawFull/noOp競合は初fixtureでのみ観測。テスト＋集計証跡を上記small_put_investigationディレクトリへ保存。実機用にbinaryは生成しただけで稼働へ未適用。

完了時cleanup: 今回起動したPID1105064のtestRedisをSIGINTで停止、開始時存在しなかったpkg/vfsのSQLite試験生成fileを削除。repoには対象tracked3file＋今回作成small_put_test.goのみ。make test.pkgの今回失敗はGlusterFS/coverで先に停止し、mockeylinkは以前確認した別制約（今回はlink未到達）として区別。gofmt/diffcheck成功。原log再集計SHA/countを再確認。


## 再起動後の新診断ログ解析（2026-10-02）

ユーザー報告「再起動等があったが新しいログが割と安定状態」。最新 `vm-io-20261002-014922.log` を対象として進めた。前 `vm-io-20261001-223558.log` は起動版確認のみ、旧障害183557とは混ぜない。現repo branch/HEAD/前turn未commit差分を保持。source動作変更/remote操作/本番設定変更なし。

- 最新run: mount PID3153、version1.4.1+2026-10-01.3bed0d82-smallput-diag-local。Redis使用は起動logで確認。現FUSE .configは30s/10s、writerdeadline0、MaxDeletes10、zstd/4MiB、WBtrue/threshold4194305/uploadDelay0。親worker2名へpayload/freeze集計を分担。
- 共通固定prefix179,156,595 bytes/1,111,514行、01:49:30.457741〜02:24:09.308508 JST。SHA256 2f70019e76d958259462fff848083b8726fb7bcb3dd5701dfd9d1fbea2604788。元log追記は続くので以降は今回範囲外。元log無変更。
- 起動時Found staging key38,809個。5分以降にも旧stage再送が続く。「起動5分後なら通常write」という切分けは不成立。

### freeze契機は実機で確認できた

- 17,696freeze: explicit_flush9,900(55.94%)、writable_window7,781(43.97%)、idle15(0.085%)。age/slice_pressure/full_slice/commit_age0。
- explicit age median1.716ms/P953.632s/max7.516s、window median1.192ms/P957.949ms/max5.123s。explicit/window全件age30s/idle10s未満。主因はtimer外の条件。設定未伝達や30s経過のflushではない。
- raw_length4KiB slice11,656(65.87%)。内explicit5,620/window6,030/idle6。これはslice freezeの分布、cloud PUT原因の分布ではない。
- sliceID0の146件を含め全freezeがfinishへ対応、metadata done17,696全件errno0。成功DELETE同ID15,495。今回範囲のPUTと同IDの新sliceは0件。
- explicit_flushのRead/fsync/close等callerは現在logでは不明。runtime .config.AccessLog未設定。fsync主因と断定禁止。writable_windowは最新から5番目以降で再利用不可ならtimer前でもfreezeする既存条件。

### 実payloadの小さいPUTも確認できた

- 成功PUT11,718、失敗3、payload記録欠落0。分類normalmetadata slice17,696 / compaction output226 / startupstagekey38,809の相関、未分類0、slice集合交差0。
- 成功compaction3,381: raw14,133,121,024bytes、payload1,718,361,429bytes、payloadmedian226,094bytes、<1KiB178(5.26%)。その178はraw4MiB176＋raw3.5MiB2。大きいrawが強く圧縮される例が実機でも確定。
- 成功旧staging再送8,337: raw679,628,800bytes、payload179,215,372bytes、median844bytes、<1KiB4,576(54.89%)。tinyのraw4KiB3,709など。本logの小payload主群は旧stage再送。
- 合計payload<1KiB4,754/11,718(40.57%)。新normal sliceへ対応PUT0。5分後も旧stage6,274＋compaction3,301の計9,575successで、tiny3,687(38.51%)。今回writefreeze原因とPUTpayloadを直接結合してはいけない。
- source: rSlice.Removeはpendingとcache/stageを除去、staginguploadはpending不要ならskip、upload中不要化ならleakedobjectdelete。新sliceがstageで待つ間にcompaction/overwriteで不要化する説明と整合。具体的に全17,696のどれがcompaction/上書き/残pendingかは未確定、DELETEだけでcompaction原因断定しない。

### 安定性について確認した範囲

- ERROR/FATAL0、flush期限timeout/EIO明示0。metadata done全17,696errno0。slowoperation3はReadが12〜21sでいずれもOK。
- PUT transient fail3(compaction HTTP500×2/response-header timeout×1)はすべて同key後刻successまで確認。Redis txn retry後success19件、readSlice contextcanceledWARN263件。他のreadSlice failurewarn0。contextcancelをguestreadEIOと同一視しない。
- これは指定34分台のlog観測でありguestFS整合性検査や長期完全解消証明ではない。

成果物 `small_put_investigation/2026-10-02/`: payload_analysis.py/json、payload_put_events.csv、payload_per_minute.csv、freeze.analyze.py、freeze.results.json、freeze.correlations.jsonl、freeze.report.md、health_analysis.py/health.json、runtime-config-selected.json、retry_recovery.json。

次の最小調査はexplicit flush caller区別と、新stageのupload/obsolete lifecycle対応。次の局所改善候補はwritable_window制限と再利用条件の比較で、fsync/read-beforeflushを弱めない。timer再延長だけでは今回主因を制御できない。実機への追加log適用やsource実装変更はこのturnではしていない。


## VM filesystem障害の再発とFUSE期限の修正（2026-10-02）

ユーザー報告: 以前と全く同じguest WRITE I/O error→journal abort→delayed allocation失敗/Data will be lost→read-only化が少なくとも2VMで再発。時刻不明。3VM稼働していたが全3台をユーザーが停止。以前のguest dmesgは引継済み、再提示を必須にしない。小PUT/writable_window最適化を保留し、データ整合性・fsync失敗経路を最優先。

### 失敗返却経路は今回直接確定

- 最新log `vm-io-20261002-014922.log` 共通prefix813,182,468bytes/5,125,719行、01:49:30.457741〜07:31:10.786546 JST、SHA256 bbc9556b9adbd20e06c63d3e85419ae52cd4f48c0498f41c8afd23033ccfa5b4。旧183557logとは区別。
- inode596153: 05:42:55.306840 slice4110001 raw2500→同期compact。05:57:55.765007 go-fuse interrupt request1316250 Opcode20 NodeId596153 after15m0.765(timeout15m)、05:57:56.768451 kernelへの強制EINTR返信、05:57:58.182179 fsync EINTR903.183971s。06:12:58/06:28:01 fallocateも15m EINTR。06:28:41.058980 Writeは45m45.792後errno0で完了。
- inode596154: 06:19:39.456960 slice4123567 raw2500→同期compact。06:34:40.088913 go-fuse interrupt request1510696 Opcode20 NodeId596154 after15m0.670、06:34:42.593855 fsync EINTR903.174922s。06:49:56.243852 Read0bytes/EINTR903.184957s。07:03:16.177091 Writeは43m36.758後errno0で完了。
- 前回VFS defaultdeadline0修正の外側に、GenFuseOptが固定15min watchdogを残していた。これは前回の調査/修正の見落とし。ユーザーへ説明済み。source pkg/fuse/fuse.go GenFuseOpt→cmd.setFuseOption→Serve opt.Timeout伝達、go-fuse fork Serve Timeout>0でcheckRequestTimeout起動。checkerがcancelchannel閉じ、さらにoperation完了前にkernel EINTR返信。今回の「15分」はQEMU timeoutの推測ではなく自クライアント内の直接証拠。
- 現稼働PID3153/.local/bin/juicefsのconfig FuseOpts.Timeout=900000000000、WriterFlushTimeout0、PutTimeout60s、MaxDeletes10、CacheChecksumextend。稼働版はまだsmallput-diag-localで、新fixは未適用。

### 長待ちの原因と別時刻のlive証拠

- 596153先行bg4100511 metadata37m39.973、同期queue11m10.411、同期自身4113831 metadata34m24.546。596154先行bg4115684 metadata33m19.677、同期queue12m40.422、同期自身4126132 metadata30m44.603。各doWrite約40msは成功、outerWrite inodeLock保持のまま長compact待ち。
- 07:43:48 JST、PID3153の既存HTTPdebugポート6062からread-only pprof goroutine?debug=2を採取（SIGQUIT等のsignal・プロセス停止なし）。10deleteworkerがobjectDELETE中、1bgcompactがdeleteSlice3053 channel send/selectでpark、他2bgcompactとcleanupSlicesが同dSliceMu3047 mutex待ち。現在の正値queuebackpressureは直接証拠。障害時刻のstackではないので過去全phase確定と混同しない。
- capacity MaxDeletes*10240=102400。queue待ちを正常waitとする方針を維持、queue容量/locking/cleanup自体は未変更。MaxDeletes10でも満杯は起こり得る。
- fullprefix ERROR/FATAL0、metadata非0error0、GET/NoSuchKey/checksum mismatch0。checksum2,858,824行比較0不一致。PUTfail42全key後success、DELETEfail64のうち57後success/7未確認。VFS EINTR5件はrealfailedoperations。ERRORlog0を健全性根拠にしてはならない。

### 今回の実装

- pkg/fuse/fuse.go GenFuseOptの固定Timeout15mを0へ変更。elapsedtime+Unique5.5M差による自動cancel/強制EINTRを止める。
- default全mount FUSErequestsに適用(getattr/readdir等も含む)。writerのauto/正duration/実errors、Read前flusherror、commit順序、WBlocalstage/clouduploadの既存モデルは維持。
- kernel explicitinterruptのdoInterrupt/cancel通知は保持。ただし旧watchdogのcancel済requestへの強制返信fallbackもoffになるので、キャンセル全挙動・即時返信が完全同一とは説明しない。ctxを確認しないoperationは長く待ち得る。macOS daemon_timeout60は別上限で未変更、全OS無期限保証ではない。
- 新pkg/fuse/request_wait_test.go: GenOpt default/auto/1hでTimeout0を要求(修正前15mで全3case RED→GREEN)。隔離realFUSE mountでpendingfsyncの早期success拒否、release後success、ENOSPC、100mswatchdogによるEINTRcalibration(実15分試験ではない)、自helperだけkillしてkernelinterrupt通知到達を検証。production VM/nodeとは無関係。
- 新cmd/fuse_wait_test.go: realmountFlags→getVfsConf→setFuseOptionのdefault/explicit1hでFUSEdeadline0確認。新CLIflag追加なし。
- docs/_common_optionsとvm_io_diagnosticsへ修正scope/incident/cancellation留保を反映。前回diag差分とinitialuntrackedsmall_put_test保持、add/commit/push/PRなし。

### QEMU伝播の一次source確認（実VM設定とは区別）

installed `/usr/bin/qemu-system-x86_64 --version`は10.2.1 Debianbuild。停止VMが実際にこのbinary/aio/cache/werrorを使っていたかは未照合。official v10.2.1 util/osdep.c qemu_fdatasyncはfdatasync/fsync戻りをそのまま返しEINTR retryなし。block/file-posix.c handle_aiocb_flushは失敗errno伝播、bufferedI/Oならpage_cache_inconsistentへ記録し以後flush失敗を維持。EINTRだから安全なretryになるとは限らない。source https://github.com/qemu/qemu/blob/v10.2.1/util/osdep.c と https://github.com/qemu/qemu/blob/v10.2.1/block/file-posix.c 。guest破壊全範囲/実QEMUbackend因果は未検証。

### 検証

- actualFUSE4cases + GenOpt3cases race3回11.714s成功、最終actualkernel4cases -v3.491s全PASS(環境skipなし)。FUSEwhole suite4.902s成功。
- CLI配置＋旧WriterFlushOption正常0.042s、race3回1.250s成功。
- metaWriteWaitsForCompactionDeleteQueue/queuebackpressure/cancel race3回1.816s成功。実objectsは既存callback模擬、metadata realmemKV。
- VFSfull10.074s、ReadFlushError+Flush/WriterFlush対象race3回3.058s成功。initialVFSfullはtestRedis未配置で85.489s失敗、今回はパッケージdownload+dpkg-deb展開だけ(システムinstallなし)でtemporaryRedis8.0.5を127.0.0.1:6379に起動し再実行成功。本番Redis56379とは別、共有memKVtestfileはbackup/restoreしてisolatedrun。
- build `/tmp/juicefs-vm-io-fuse-wait-20261002` version1.4.1+2026-10-02.3bed0d82-fuse-wait-local。未適用。レビューworker2名blocking無し、キャンセル強制返信停止scopeの留保をdocsへ反映。
- 残課題: backend削除queue停滞は残る。修正は15m自動失敗経路の除去で、破損済guestdata修復でもguestFS破壊全原因解決確定でもない。実機適用/VM起動/repair/fsck/cache削除は一切していない。
- evidence親dir incident_recurrence/2026-10-02/、originalprefixSHAとwatchdogtimeline、PPROF、log/compactionqueue分析resultsを保存。

最終補足: actualkernel4caseの最終-v実行は全部PASS(3.491s)、skipなし。FUSEwhole4.902s/VFSwhole10.074s/対象race全成功。make test.pkg再試行はGlusterFS開発file無しとcoverdir無しで失敗、広いallpkg成功とはしない。全validationログとbinarySHAをincident_recurrence/2026-10-02へ保存。prod稼働PID3153/Timeout15mは未変更。

cleanup完了: temporarytestRedis PID178419をSIGINTで正常停止。今回VFSsuiteが生成したSQLite artifactだけ削除、共有memKVtestsettingは各run前後でbackup/restore。元診断logとprodPID3153は停止/切詰め/変更していない。最終buildSHA256 6b970faa6f4f112426fc6b625036ef91bc4afd2ac2084626009b7e1f77f3ec3b。


## VM向けlatency改善の方針検討（2026-10-02、設計案のみ）

ユーザー指摘: 進行中fsyncの人工EINTRは修正する一方、通常利用で2500slice同期compactがinode全体I/Oを数十分止める構造もVM仮想disk用途では重大。無期限waitだけを最終解決にせず、この長停滞を減らすことを次の改善対象とする。方法の比較を依頼、個別案の実装・本番適用はまだ指示なし。

機序: backgroundcompactは99,199,...または>350で起動を試みるが同inode/chunkでactiveならreturn。旧cleanupのqueue投入待ちまでm.compactingが残り、その間同chunkの次compactは進まない。背景処理自体はinodewideOFlockを持たず、新slice登録は続く。raw>=2500でforegroundWriteがinodewideLock保持の同期compactへ入り停止。別chunkのbgもlen(compacting)>10のadmission制限で起動見送り。新sliceの生成側にはsmallrandom/gap/explicitflush/writable_windowがある。2500rawrecordsは現在live範囲数/uniqueobjects数とは異なる。

比較候補（提案段階）: 1)新dataPUT+metadata置換commitとobsoleteGCを分離し、GC対象をDBへdurably記録してからcleanupasync、満杯RAMqueue投入をcommit条件にしない。crash/retry/refcount安全性とRedis/SQL/KV parity必須。2)inodewideLock保持中のcompactを避け、snapshot+検証付きpublishの短criticalsection/chunk粒度設計。単にLockを外す、chunklockへ置換だけではfsyncのpendingWrite待ちは残る。3)hotchunk/rawcountに応じた背景scheduler/GCとの優先度・小さいbatch/高低watermark。backend実容量とread処理のrawlistコスト監視必須。4)新slice発生数削減(writable_window比較、Readcallerのpendingoverlay等)、fsyncを無視せずrandomrangeと重なり順序維持。5)deletion実throughput/GCpriority/metadata-ref保持を調整、MaxDeletesやqueuecapのみ増加は根本解決ではない。

優先案は1(今回metadata後処理30minの主因と整合)→3→2/4。2.5k閾値引上げのみ/無条件同期compact解除はrawlist unboundedと読みCPUmemory問題が残るので最終策としない。実装選択の合意はまだない。


## 改善案1/3/4のユーザー選択（2026-10-02）

ユーザーは1(GC分離)/3(軽量backgroundscheduler)/4(slice集約調査)に関心、2(inodeLock構造変更)は複雑・大変更・効果不明瞭として今回scopeから外す方針。1は既存挙動と別modeで切替可能を希望。3は大量files/chunksでも全件走査や件数に比例した毎eventの優先度管理を避ける。4はexplicitflushcallerとwritable_windowを継続。PUT減少はslice減少と別に実測する。

設計方向(未実装): 1は既存defaultlegacyを残し、新GC分離modeをoptinにする案。objectDELETEworkersは既に存在するので、compaction完了がRAMqueue満杯とdSliceMu待ちに依存しない受渡しを追加。専用boundedhint経路＋GCdispatcherなら既存cleanupがdSliceMuを保持中でもforeground側を阻まない構成にできる。永続deadrefmarkerの保証があるcompactionobsolete経路をまず対象にし、未検証削除経路へ一括変更しない。

確認: 3backendともmetadata置換txn内で旧ref減算markerを保存し、objectDELETE成功後にmarkerを除去。Redis/KVは内部negativeがdead(0は暗黙1参照)、SQLは実refs<=0。復帰既存collectorは約1h: RedisHSCAN全trackedrefs、KV全Kprefix、SQLrefs<=0 index query。全files/chunksの走査ではないがRedis/KVはtrackedrefs件数依存。頻度を増やさず利用するv1と、pending-only永続indexを足す後段を分ける。既存delayedSlicesはtrash保持後にrefsを減らすrecordなのでdeadjobへ無変更流用すると二重減算になる。

安全監査: 共有slice/CopyFileRangeとref再追加・compaction/GC競合を先に確認。worker直前ref確認だけはTOCTOU解決にならない。今回source上の懸念であり既存データ破壊の再現ではない。新modeへの切替/rollback、crash中の発見、DELETEretry、queue/mutex完全飽和、MaxDeletes0/負値、NoBGJob、全engineparityを仕様化してから実装。

3案: existingdoWrite返却numSlicesを使うkey(inode,chunk)通知、boundedmap＋固定bucket、平均O(1)更新、worker処理I/O中schedulerLockを保持しない。active時generation/dirtyで追加通知を保持、readyinode roundrobinでhotfile占有を抑える。priorityはlocalderivedhintで非永続、backendCASが正しさの根拠。毎write追加DBlookup/全inodechunkscan不要。boundedvolatilehintでoverflowdropはcoldchunkの永久取りこぼしを防げないので、まずopportunistic改善＋既存2500fallback維持を候補にし、厳密eventual保証はdurableworkkey案として別比較。NoBGJobの現compaction扱いとmanualforce経路を勝手に変更しない。

4: caller origin/barrierIDをprivatecontextでVFS/highlevelFS→flush→freezelogへ対応、既存publicwriterinterfaceを変えず診断追加。開始/終了errnoの対応も追う。writable_window保持候補を限定増加させる比較test、探索回数/memory/reuse/metadata数/actualPUT+compaction/deletesを計測。gapやfullblockoverwriteを一つへまとめる保証はしない。

次は1の小さいmode仕様・安全監査と、4のcaller診断を先行。3の新たな永続scheduler/全scanは導入しない。source新挙動の実装とprod変更はこのturnで行っていない。

ユーザーが「先のFUSE件は？」と進行順を確認。1/3/4の新設計検討をいったん止め、既存FUSE修正の状態を明確化。再確認時、localbin /tmp/juicefs-vm-io-fuse-wait-20261002 は存在/version正しい、稼働.configはsmallput-diag-localかつFuseTimeout900sのまま。FUSE修正はsource/test/buildまで完了、未commit/本番未適用。先にFUSE修正版の適用・稼働確認を区切る優先順を維持。本番変更の明示指示はこの質問だけでは受けていない。

ユーザー追記: plan/specは日本語、現在の英語内容も漏らさず保持。今後のplan/specも日本語に統一する永続方針。既存2026-10-01計画の英語headingも日本語化、統合spec/plan全文を日本語化し元全要件を維持、追加したRediscopy安全性条件も追記。統合作業は継続中。

## 統合版実装の進捗（2026-10-02）

ユーザー「fuseも混ぜて一緒に片付け」承認でローカル統合版を実装。新flags: compaction-gc-mode legacy/deferred(defaultlegacy)、compaction-scheduler legacy/priority(defaultlegacy)、writer-reuse-window 1..64(default4)。本番変更/commitなし。案2inodeLock構造変更なし。

GC: persisteddeadref後のobsolete経路だけboundedhint1024＋1dispatcherに切替、foreground atomicload+nonblockingselectでolddeletionchannel/mutexを待たない。旧worker10等は維持。MaxDeletes>0/writable/NoBGJobfalse必須、Goinvalidfallbacklegacy、CLIreject。losthintは既存marker1h回復、schema/scaninterval変更なし。legacyのblocking回帰維持、MemKV/SQLite/isolatedRedisのmarker/crash-likehandoff/error/sharedref/stop/fullmutex tests PASS。

Redis共有安全性: copy/clone/batchclone LRANGE後、他client compact+oldrefdelete→古snapshotでref復活する競合を3経路でRED再現。読み取り前sourcechunkWATCHを追加、EXECabort/re-readを7cases、WATCHerror EIOで未ack3cases、10回race成功。自然にコピー対象chunks件数依存、全volume走査/metadataformat/案2locks変更なし。旧writerclientは修正を守らないため同volume全writer更新が必要。既存KV各engine完全atomicityは未保証・今回独立race再現なし、PostgreSQLruntime未実施。

priority: fixed3buckets (<100/100..999/>=1000)、weighted4:2:1、readyinodeRR、perinodeactive1、11workers/active+pending1024。hints advisory/drop時cold eventual保証なし、旧2500sync/manual安全網保持。count/tier既存返値利用で追加perwriteDBlookup/globalfilechunksscanなし。CloseSessioncancel→close/waitjoin beforemetaconnshutdown、NoBGJob ordinaryrequestscheduler意味保持。独立10tests×20race成功＋realMetaRead→compact→shutdownjoin integration PASS。notify benchmark capacity1/1024 ~28..34ns/op、0alloc、局所参考値で実機性能保証ではない。

4: DEBUG時origin/barrier begin/freeze/end errno、VFS/highlevelfsのcaller注釈、publicwriterinterfaces変更なし。window4vs16 metadata10→9、データ/overlap/gap/fullblock/read/fsync保護回帰PASS、実cloudPUT減とは未主張。reviewP2でfreeze/finishログのINFO時12allocをRED計測し、debuggateで0allocへ修正。PUTpayloadformatもfastrequestDEBUGoffは作らずslowWARNは保持。

検証途中結果: combinedmeta race1.695s/IOvfs3.498s CLI1.768s FUSE11.708s（各count3）。full VFS9.632s/FS2.143s/FUSE4.788s PASS。追加CLIreadOnlycrossvalidation3回とfinalmeta再検証中。レビューmeta/IOnonblocking、P2allocは修正、docsnew3flags記載。plans/specs全3files日本語、元English全要件保持。

統合版最終結果: CLIcrosssettings/readOnly含むrace3回1.735s、Meta全new+旧queue+Rediscopyguard race3回2.292s成功。make test.pkgはGlusterFS開発依存とcoverage先不足等で失敗、allpkg成功扱いしない。build最終再生成中。統合evidenceは親combined_vm_io/2026-10-02。既存3bedbranch/HEAD保持、初期untracked含めstage/commit無し、本番未適用。plans/spec日本語化3filesは元全要件保持。レビューP2診断allocはRED12→GREEN0、再レビュー依頼。

統合版build最終成功/version1.4.1+2026-10-02.3bed0d82-vm-io-combined-local、新3flagsのmount--help表示確認。IO限定再レビューP2解消確認。parentcombined_vm_io/2026-10-02/build.jsonにSHA保存。実機適用はしていない。

## 統合版の実機開始（2026-10-02）

ユーザー報告: 修正版でVM3台を再稼働して監視開始。root read-only `.config`確認でversion3bed0d82-vm-io-combined-local、WriterFlushTimeout0、FuseTimeout0、GCdeferred、schedulerpriority、reuse16が実際に有効。最新診断log vm-io-20261002-145808.log、PID413172、origin/barrier/payload DEBUGあり。現新runの健全性全体解析は未実施。GC/scheduler自身のenqueue/dispatch専用ログは現在存在しないのでfilterだけで候補数/dispatcherqueue長を直接観測できるとは説明しない。設定は.config、効果はcompactionphase/slowduration/flushorigin/error/payloadで間接追跡する。


## 統合版稼働中の再起動・staging滞留調査（2026-10-02）

ユーザー報告: proxmox01ゲストが再起動、QEMUプロセス再起動ではない。vol1(root) inode596152、vol2 inode596155。journalctl -b -1に怪しい記録なし。rootFS readonly等で永続journalが残らなかった可能性は否定できず、今回readonly/データ破壊を確定しない。libvirtにitco watchdog action resetあり、guestagent未接続、SSH未認証でゲスト内部の原因未確認。16:04にvol1のRead/Writeが42〜46.79s遅れて成功、対応Read前flushは数十〜数百µs、同時fsync/PUT/DELETE遅延あり。現run固定prefixでwriter完了errno0、QMPfailedread/write/flush0だがguestFS健全性証明ではない。

ユーザーは旧del_c高速大量burstが消え、rawstagingが通常2000〜3000→53000超へ増加と報告。del_cは新GCでも従来のobject DELETE histogram countを使い、成功・失敗完了を計上するので『経路変更で表示されないだけ』ではない。RemoveはremovePending+local cache/stage除去の後remoteDELETE、upload成功もstage除去。

新run vm-io-20261002-145808.log固定534837717B/3254271行、14:58:15〜16:42:15、SHA6daa85eb928399999f2922854a9bfcc12925099f8e399b398c4b876ee5a82f8f。normalmetadata77831distinct、PUT成功21410distinct、DELETE成功17060distinct、両成功14462distinct。未PUT/DELETE成功集合53823 = runtime stage53823と一致。stagebytes1203396608、uploading30/30、stageerror0。新DELETE25321(成功25310/失敗11)、平均2.422s、最大550/min、毎分median<10msなし。旧07:03/04は25801/29199/min、median0.615/0.586ms。期間/負荷差があるため合計差を新mode原因と即断しない。

16:47 livepprofでGCdispatcherはcompaction_gc.go39のouterselect（hint受信待ち）、10削除workerはremoteDELETE待ち、upload枠30使用。cleanupSlices既に1回完了、job別metrics回収25669、次hourlysleep。FormatTrashDays0。現在queue飽和/hintoverflowを実機で証明していない。最古未処理4176869(15:16生成raw65536)のPUTは16:42:48成功=約86min滞留。全stageがobsoleteとは未確認。

設計上の限界: boundedhint1024overflowは通知を捨て、durabledeadmarkerは保持するが回復は初回57〜63min・54min分散gate。既存テストは手動cleanupで安全性を検証しただけで、hourly運用下のsteady-state滞留を検証していない。legacyのqueuebackpressureをdeferredが外したため、削除能力を超えるobsolete生成・stage滞留を起こし得る。現場でどこまで該当するかsample負ref照合が必要。単なるqueue増加やfullHSCAN頻度増加、未確認stage手動削除は採らない。候補はdeadstageローカル回収とremoteDELETEの分離、bounded GC予算とbackground admission、drop/queue/backlog可視化。整合性・inflightupload・sharedrefs・persistentretryを設計/回帰先行で扱う。調査成果gc_backlog/2026-10-02とrestart_check/2026-10-02。本番への変更なし、source修正もこの追加調査では未実施。

追加観測:16:47 stage54139/1.257GB、16:52:35 stage54040/1.254GBへ99減。増加が永続単調とは断定せず、処理が進むが大きなbacklogが残るとする。CSV層化28IDのexact rawstaging pathをstat、最古15:16〜15:18の10IDは既に不存在、後の18ID中17存在。例raw4096→stage4100、raw12288→stage12292、raw290816→stage290852（圧縮payloadとは別のlocal形式）。負ref照合は未実施。process argv/envに接続URL無し、mountinfo sourceJuiceFSのみでDB/prefixを確定できず、接続先を推測せずユーザーへ起動script/configパスをasync質問。秘密をチャットへ求めない。GCローカルstage除去自体がremoteDELETEと同workerFIFOで遅延する構造はsource確認、全stage obsoleteとは未確定。


## staging回収補正の承認と実装（2026-10-02、検証中）

ユーザー「改善して」で、直前説明したlocal staging回収をremoteDELETE待ちから分離する補正を承認。既存checkout/branch/3bedHEADを維持し、本番適用・stage/delete/DB変更やcommitは行わない。日本語spec/planを2026-10-02-vm-io-gc-retirement.mdとして追加。

meta: optional RetireSlice1009 callback、bounded1024dispatcherがlocalcallback後にnonblockingdslices送信。localerror/busy・transportfullではpersisteddeadmarkerを保持。fixed5eventmetric compaction_gc_events_total（mountprefixjuicefs_）とDEBUGlocalsuccess/error/remotedeferredログを追加。legacy/copyguards/periodic recovery/worker数維持。remotequeuefullの後続callback停滞をRED→GREEN、MemKV/SQLite/isolatedRedis16380、queue/recovery/error/sharedref/legacy/copyguards race3/10回成功。sharedrefsはsentinelFIFO通過でfalsepass回避、意図wrongsharedhintのcompiler overlayで3backendFAIL感度検証後overlay撤去、実source20/3回PASS。

chunk: optional cachedStore.Retire local-only、Remove共通localhelperとunlinkerrors返却。ACK前pending/active公開、同storeactualPUT+abandonedcleanup進行中はmarker消去をbusyerrorで保留。actualstagecallbacksもtimeout後cleanup完了まで追跡し、旧stageFailed/path/完了共有状態をmutex保護。unlink前後pendingcancel+activecheck、scanner stat→pending登録をsame mutexで直列化（ENOENTだけreject、他staterrorはpending保持）。失敗取消PUTをpendingへ復活しない。永久tombstone/新unboundedgoroutine/分散leaseなし。activePUT数はexistinguploadslots、stage trackingは既存actualcallbacks数で終了時消える。

actualdisk/CRC+objectmemによる9対象tests race3回16.280s、rootstore全対象explicitfile13.131sPASS。fullpkgchunkは既存mockeyのGo1.26 runtime.duffcopy/duffzero linkererrorで未実行。既存Mockeytestfileを除いたproductionGoFiles全+cached_store_test/retire_testで検証、fullsuite成功扱いしない。

cmd: optionalRetire typeassert接続、customstore未対応no-opで旧Removecleanup維持。新callbackregistrationRED2件→GREEN3回、combinedoptions/FUSEconfig込みrace3回2.023sPASS。独立meta/IOreview重大なし、既存他clientinflightPUTとcacheindex一時再挿入制約維持。

fullVFS/FS/FUSE通常試験でFS2.070s/FUSE5.010sPASS、VFSは診断captureTestWriterFlushTraceCorrelationだけFAIL（phase=end自体stdoutに実在、背景backupProgressがglobalOutputを変える疑い）。prodFlushを変更せずtestcapturehookで修正/再検証中。build別名/tmp/juicefs-vm-io-gc-retire-20261002を作成中。hourlyhintoverflow/localbusyretry残存、全旧garbage即時drain/remotegarbagebounded保証なし。実機53kすべてdead・guest再起動解消は未確認。

isolatedtestRedisはroot所有のloopback16380と6379、pidfile/directoryはgc_backlog/2026-10-02/improvement/redis-test*.jsonに記録し検証後自分のPIDだけ停止する。productionRedisには接続していない。最終成果とlimitsは同improvement配下へ保存する。

補正最終検証: fullIO再実行VFS10.785s/FS2.054s/FUSE5.275sPASS。diagnosticcapture失敗は背景Progress.Doneのutils.SetOutput(os.Stderr)をcontrolledtestでRED再現、writer_trace_testだけDEBUGhookへ変更し関連3tests normal/race3回+fullVFS成功。stage/Flush productionerror処理を触らず修正。全productionchunkGoFiles+cached_store_test/retire_test通常13.131sPASS、fullpkgchunk linkerlimitは維持。補正binary version1.4.1+2026-10-02.3bed0d82-vm-io-gc-retire-local、path/tmp/juicefs-vm-io-gc-retire-20261002、SHA1a022a7e186eda8b88593842d16dc90316b2698b15c1aee254346f48fa70073f。diffcheckPASS、HEAD3bed/branch維持。新modeのlocalretireはdeferredだけ、共通chunkのACK/activeguard/localunlinkerror伝播はlegacyにも適用する（legacyqueue待ち方針は維持）。実機未適用。既存53k即時解消・PUT総減少・guest再起動解消は未主張。

最終cleanup: ownisolatedRedis16380 PID485375/6379 PID501474をexe+port照合の上SIGTERM停止、両方停止確認。今回通常VFS試験が作ったSQLite署名付きartifact pkg/vfs/?_journal=WAL&_timeout=5000&cache=sharedだけ除去（turn開始時untrackedには無かった）。元logs/initialuntracked/prodは無変更。最終result.json/binary.json/cleanup.jsonと各testlogsをgc_backlog/2026-10-02/improvementへ保存。

## pkg/chunk フルテストのGo版制約の解消（2026-10-02）

原因: mockey v1.2.14 が `//go:linkname` で runtime.duffcopy/duffzero を参照し、Go1.26(amd64)にはそのシンボルが無いためlinkerror。upstream CI(verify.yml)はGo1.25。ユーザーがgoenvで1.25.11を導入。以後pkg/chunkのフルテストは `GOENV_VERSION=1.25.11` で実行する（go.mod/mockeyは変更しない）。

- 通常: 49PASS/0FAIL 47.7s。ただし `TMPDIR` をルートFS上に置くこと。WSLの/tmpはtmpfsのため、既存の `TestInRootVolume`（t.TempDir()がルートボリューム上にある前提）が環境依存で失敗する。未変更ファイル。
- race: 11件失敗/DATA RACE 44件（disk_cache.go/disk_cache_state.go等）。未変更HEAD 3bed0d82のgit archiveでも同じ11件+TestSingleFlight（不安定）/48件を再現したので既存問題、今回の差分由来ではない。pkg/chunk全体race成功とはしない。
- 今回追加したretire_testの9件はrace count3でPASS（16.3s）、DATA RACE 0件。
- ログ: gc_backlog/2026-10-02/improvement/go1.25_chunk/。一時TMPDIRとbaseline archiveは削除済み、repoのuntrackedは変化なし。


## staging回収補正版の実機適用と初期観測（2026-10-02、継続試験中）

ユーザー報告: 全VMを停止し、JuiceFSに新たな書き込みが発生しない状態にした。rawstagingとdeletionの両方がすべて掃けるまで待ってからJuiceFSを更新。その後VM3台を再起動し、かなりのdisk I/Oを発生させて継続試験している。

更新後は今のところかなり安定し、rawstagingが常に低い値を保つ。PUT回数も以前より減ったように見え、ゲストのI/O速度も少し向上したように見える、とユーザーが報告。ユーザーはもう少し様子を見る方針。agentが本番の停止/排出/更新を実行したのではない。実行中binary version/起動引数、観測時間、定量的なPUT率/latencyの独立確認はこの報告時点では行っていない。

評価: 不要stagingのlocal回収をremoteDELETE待ちから分離し、未開始のobsolete uploadを取消する変更の意図と観測は整合する。ただしPUT減少量・I/O向上の因果/量は同等負荷の定量比較前なので仮説として扱う。以前のVM filesystem破壊/再起動の完全解消や長期安定性は未確定。起動前に滞留を排出したため、旧backlogを引き継がない初期状態での観測として記録する。

継続方針: 現設定を維持してユーザーの継続試験を尊重し、今の報告だけで追加修正・本番変更・自動監視を開始しない。後続評価ではstage低水準が持続するか、長いI/O/fsync待ちやguest異常の再発がないか、比較可能な負荷でPUT率/latencyがどう変化したかを区別して確認する。


## 耐久テスト結果と現設定の評価（2026-10-03）

ユーザー報告: 補正版を継続稼働させ問題なし、耐久テストクリアと判定。compactionが激しく動く場面ではCPU負荷が少し上がったが、耐障害性を優先し現状許容する方針。今回提示設定はslice-flush-wait15s/idle10s、writer-flush-timeout0s、writer-reuse-window16。以前のwait30sとは区別する。実行中version/引数やtest継続時間・負荷量をこの報告では独立確認していない。『ユーザーが実施した条件の耐久試験合格』として記録し、全条件の無障害保証には一般化しない。

source再確認: waitはslice作成startedからのage、idleは同slice最後のlastModからの非活動時間。flushAllが約100ms間隔でage>15sまたはidle>10sかつage>10sをfreeze。連続更新でもageはresetされず約15s、単発は約10s。fsync/read等explicitbarrier、fullblock/slice、slicepressure/window/overlap等が先行し得る。commitThreadの未freeze先頭にはwait*2=30sの補助freezeもある。これらはcloudPUT完了deadlineではない。

writer-flush-timeout0はfilewriter全pending保存/metadatacommitの期限なし待機で、実error/cancel等は別。WBは従来local staging+metadatacommitとcloudasyncuploadを区別。FUSEwatchdog0の別修正とセットで外側15min人工EINTRを防ぐ。timers15/10とは異なる役割。

writer-reuse-window16は同file同64MiBchunkのpending slice listを作成順の新しい側から探索したindex i>=16の非適合・未freeze候補をwritable_windowでfreezeする距離。0..15の16位置を、この理由ではfreezeしない。frozen未commitentriesもindexへ数える。適合判定はfreeze距離判定より先で、探索は16で打ち切らず、17番目以降でもその時点で未freeze/適合なら再利用可能。途中の適合return/overlapreturnで古い候補へ達しないこともある。厳密slice上限/MRUアクセス順/16MB/16秒/16chunk/16write回ではない。fullblock先頭・gap・新しいsliceとのoverlap・explicitflush制約は保持。値1..64/既定4。

効果評価: VMで旧partial領域へ戻る書込みではwindow16が早期freeze/newslice/metadata増加を抑え得る。局所回帰は4→16でcommits10→9、readback確認で、実機PUT削減率を表さない。保持候補とbuffer/探索CPUの負担は増え得る。15/10timerのみでVMの強いexplicitflushを越えて任意randomwriteを一つにまとめる保証なし。過去freeze集計ではexplicit/writablewindowが大部分、timerage/idleは少数。最新PUT減少・低stage/速度改善はGC localretirement等との総合結果で四flags単独の効果と切り分けていない。CPU増加はcompaction read/decompress/merge/compress/PUT等が原因候補、profile未計測につき確定せず。現設定を維持し、追加変更/本番操作/自動監視を開始しない。


## タイマー変更の時系列訂正とログ効果解析（2026-10-03、解析中）

ユーザー訂正: 『逆です。いままで30s/10sだったのをさっきの再起動で15s/10sに変えました』。従って耐久合格は30s/10sでの結果、15s/10sは10/03再起動後の短期観測。前節の提示値15s/10sを耐久試験の設定と結び付けない。最初の追加発言『30s/10sに変更』はユーザー自身が撤回した。起動script /home/kwatanabe/utilities/cloudfs/juicefs_mount.shをread-only確認、現在literal15s/10s/timeout0/reuse16、GCdeferred/priority、maxuploads18、maxdeletes10。実mount.config PID1156153/versiongc-retire-localも15s/10s/timeout0/reuse16/FUSE0で一致。

固定prefix manifest option_effects/2026-10-03/manifest.json: combined145808 794809736B、補正耐久201718 5976434018B、再起動142854 267671574B。元log削除・切詰め・変更なし。3workerへfreeze/barrier、PUT/GC/rawpayload、health/SHA/log肥大を分担。source/gitHEAD3bed/branch保持。

旧耐久PID683253(201718)と新PID1156153(142854)が両方残り、それぞれlogfdを持つ。旧耐久freeze最後12:51:24、その後15:33cutoffまで背景ログは続く。全19hをactiveVM負荷時間と呼ばない。oldlogはproducerがまだopenなので閉鎖済みとして削除圧縮の対象にしない。現在Prometheus旧9567/new39219でread-onlysnapshot取得、oldstage0/oldPUTactive0、newstage904≈45.7MB/PUTactive18、oldhintoverflow0/remotedeferred0/localerror2156/localsuccess880895。GC localerrorはbusy等再試行を含みguestwritefailureとは別に分類する。CPU過去時系列はログに無くpeakcompactionCPUの因果は確定不可。

ログ効果解析完了（option_effects/2026-10-03/report-ja.txt）。固定prefix総7,038,915,328Bを3workerがstreaming解析、各SHA/completeboundary/独立linecounts/カテゴリbytes一致検証。timersは訂正通り耐久30/10・直前再起動15/10。元log変更/圧縮/削除/移動なし、source/本番変更なし。

主要結果: 補正前normalfinish101766/normalPUT試行73575(0.723)、補正後耐久843306/82695(0.0981)、最新22713/8670(0.3817)。前→耐久のnormalPUT/finish86.4%低下、comp含む全PUT/finish0.8729→0.2768で68.3%低下。ただし負荷・期間・forcecompaction混在の非統制観測で単独因果改善率ではない。retirednormalIDでPUT未観測762146、localretire成功880895、busy2156は全inflight、既存normal小PUT抑制の設計と整合。成功payload量はcompactionが98.9〜99.2%を占める。耐久normalrawslice<1KiB44件とsmallpayload47414件を区別。

freeze explicit78.87/94.93/86.03%、window21.12/5.07/13.91%、idle8/16/14。age全0、explicit/window全age10s未満。wait30→15の直接効果は現ログでは見えず、window全run16で単独効果も未分離。耐久Fsync335774全errno0、p50≈41ms/p95≈230ms/p99≈792ms/max14.756s、全writerbarrier560104errno0。通常metadataWrite非0全0、checksum不一致0。raw2500同期compは補正前1件、補正後耐久/最新0。background(oncefalse/forcefalse)耐久6234slow完了、metadata中央値81.59ms/p95168.66ms/max5.217s。force3649は再帰callframe数で独立manualrequests数ではなく、total最大3h52には子孫全処理が入りmetadata待ちと誤解しない。

重要残課題: 10/02 20:42:48 readfile596155 EIO1行と、その旧PIDの後の実metrics EIO累積1207を発見。counterはlogitがVFS返却errorを加算するもので、単なる内部GETretryではない。errnoだけのlabelsなので操作/actor/時刻や1207独立VM障害を特定できない。直前12readSlice4238443失敗はcontextcanceled。reader.doneはf.err stickyEIO、Read/waitForIO→VFS/FUSEへ返せる。sharedfileReader.tried＋singleflight先頭ctx取消fanoutがcancel→stickyEIOになる仮説はsource上成立するが未再現、sourceは今回未変更・3bed同一。ownslice.dropはBREAKを正常処理するためそれだけを原因としない。guest耐久合格報告はユーザー観測として保持しつつ全I/O無エラーという評価は撤回/禁止。

その他: GETheader timeout3件後に約96.82秒のRead成功（fsync待ちとは別）。10/03 14:36:19GET4913906_3_4MiB404→3.63秒後同key成功、両logに同IDDELETE/retire無し、永久欠落とは評価せず。CompactwalkrootEINTR02:33はfsyncwatchdogと別。最新版metricsEINTR1/EIOseries無し（counter期間とlogprefixを混同しない）。

DEBUG容量約99.7〜99.9%、正常checksumDEBUG行だけ全byte60.1/63.4/72.0%（耐久3.787GB・22786514行）。ログ肥大は検証自体ではなく出力であり、checksum検証維持＋通常INFOや正常DEBUG抑制が今後の候補。元logsは全保持し、特にEIO証跡は優先保管。old201718/new142854の両producerがまだopenで、旧名前だけで閉鎖済みと決めない。現在stage904/45.7MB、旧PIDstage0を補助snapshotとして保存（継続推移は未測）。主報告・各JSON/CSV・例外縮約・SHAmanifest・loginventoryを保存、解析script py_compile成功。


## 明示flushの中身・呼出元の確定（2026-10-03）

ユーザーが本題のflush理由/explicitflush中身の分析状況を確認。耐久201718ではexplicitfreeze800560件、Fsync682152(85.2%)、Fallocate89607(11.2%)、Read28801(3.6%)。close/Flush/Release由来の新規freezeは0。ただしwriterbarrier呼出はFsync335774/Fallocate56437/Read167768/Flush41/Release41/internal.close41/Truncate2であり、freeze数は呼出数と異なる。1barrierが複数sliceを閉じたり、新しいfreezeがなくても既にfrozenなpendingcommitを待つため、freeze0をno-opとは呼ばない。

当初『explicitFlush内訳未確定/Read主因か』から、主因Fsync・次点Fallocate・Readは少数と確定した。全3期間のexplicit内Fsync比率85〜89%で傾向は共通。耐久全freeze843306中explicit94.9%、window5.1%、age0/idle16のみ。タイマー時間到達前の整合性barrierが主要な制限。

sourceconfirmed: VFS.Readはreader.Read前にwriter.Flush(同inode)を必ず呼ぶ。fileWriter.flushはそのfileの全pendingchunk/sliceをfreezeし、chunkWriter.Finish保存→作成順meta.Write→reader.Invalidate→pending除去→完了まで待つ。fsync/Readともflushで全file既存dataを再書込みするのではなくpendingが対象。WBではローカルstage保存とmetadata登録を待ち、全cloudupload完了は別。新しいblockstage/PUTがFlushToで先に始まる場合もあり、fsync1回=PUT1回ではない。

改善後は明示flush割合自体は減らず(補正前78.9→耐久94.9、workload異なる)、要求barrierは維持したまま後段obsolete localretirementで不要PUTを減らした。FsyncP95≈450→230ms、P99≈1.42→0.79sは観測変化で単独因果評価ではない。Readのpreflush成功とreader側EIOは別境界なのでwriterend成功から全Read成功を主張しない。

今後必要ならFallocateのmode(通常allocation/punchhole/zeroRange等)、Read前全fileflushのうち対象range外のpendingを閉じる程度を診断する。ただしFsyncが主因なのでReadflushだけを減らして全体が劇的改善すると期待しない。fsyncを跨ぐ集約には別のdurablepending/visibility設計が必要で、タイマー/window変更だけで行わない。sourceや本番への変更なし。


## ドキュメントと本体の別管理（2026-10-03、ユーザー指示）

主題はJuiceFSの個人的な改善版を作ること。今回のscopeはGoogleDrive backendのrclone S3最適化、とくにVM仮想disk等の部分更新が多い巨大fileの正常更新と最適化。プロジェクトルートjuicefs_inspectionを調査docs/evidenceの独立Gitにし、juicefs/は後から置ける独立本体repoとしてignoreする。submoduleにしない。

ユーザーが先に文書をcommitし、本体は後で扱う。agentはadd/commit/pushしない。初期untrackedにもindex操作なし。

移設: juicefs/docs/superpowers/→docs/superpowers/、juicefs/docs/en/development/vm_io_diagnostics.md→docs/development/vm_io_diagnostics.md。全ファイル（既存swap含む）を移動時SHA一致で内容保持確認、docs/documentation-migration.jsonに記録。swapとPythoncacheはGitignoreで除外。計画/specは日本語を保持し、当時の状態が最新状態と誤読されないよう注記、参照パス更新。CLIreference等の本体製品docsは本体側に残す。移設元trackeddocsが本体Gitで削除状態になるのは分離の結果であり、実装コードの変更/復元はしない。

README.mdに主題/scope/別Git管理/資料入口/成果と留保/配置/検証方針を記載。docs/findings.mdに確定原因、4options、明示flush、GC/upload/copyguard/scheduler、ログ効果、ReadEIO、検証範囲を日本語で集約。AGENTS.mdをrootにも追加し管理ルールを明文化。TODOは現在のstatus/優先課題/完了を冒頭に整理し、旧追記を歴史的作業記録として保持する。

原log/metadata/cache/stage/VM/稼働設定に変更なし。本体Goソース・testsに変更なし（文書移設のみ）。履歴の検証データ/JSON/CSVは書換せず、元のsnapshotとして保存する。

文書整理の最終確認: README/AGENTS/TODO/memo/移設docs等12Markdownのローカルリンク30件を検査し、broken link0。移設時SHA一致7ファイル（本文6＋既存swap1）を確認。source配置先がparentGitignore対象、Pythoncache/swapも除外を確認。parent/childともstaged paths0、diffcheck成功。独立文書reviewでhostEIOとguestFS障害の個別時刻因果を言い切る表現を修正し、両証跡と未検証の対応範囲を分けた。検証記録はdocs/documentation-verification.json。文書移設後も本体HEAD3bed/branch保持、Go/test本体変更なし。

## 本体repoのauthor変更とローカルコミット（2026-10-03）

ユーザー指示: juicefs本体のローカルブランチのauthorを、ルートrepoの既定（`.git/config` の tongsama <getpow2@gmail.com>）へ変更し、現在の変更をローカルコミットしてよい。

- juicefs/.git/config に user.name=tongsama / user.email=getpow2@gmail.com を設定した（global の kwatanabe 設定は変更していない）。以後このrepoのcommitはtongsamaになる。
- 統合版＋staging回収補正を1コミットにした。新規ファイル（compaction_gc/scheduler/writer_trace/各test）と、ルートの `docs/` へ移動済みの本体docs 2件の削除を含む。コミット前に Go1.25.11 で build/vet が成功。
- 両ブランチともremoteに含まれていないことを確認したうえで、0b90c7db 以降をrebaseし、author/committerをtongsamaへ書き換えた。author日時・commit日時・treeは同一。
  - improve/flush-wait-alter: febf149a → **396d8f6a**
  - fix/vm-io-wait-policy: 3bed0d82 → **2ae17f94**、その上に統合コミット **69fd077b**（現checkout）
- 旧hash（febf149a/3bed0d82）はreflogにだけ残る。過去のmemo/証跡/binary version文字列（`3bed0d82-...`）は旧hashのまま。内容は 2ae17f94 と同一。push/remote変更なし。

## Co-Authored-By削除とローカルmain作成（2026-10-03）

- ユーザー指摘: commitにClaudeのCo-Authored-Byを勝手に入れない（永続方針）。統合commitからtrailerを削除し、69fd077b → **84f19ca4**（treeは同一）。本memo上の69fd077bは84f19ca4と読み替える。
- 「fix(vfs)がmainにある」件を確認: ローカルmainはもともと存在しなかった（cloneしてv1.4.1をdetachedでcheckoutしてから派生）。履歴はv1.4.1から一直線で、mainには入っていない。
- ユーザー意図（A案）: main → v1.4.1 → 派生ブランチの形に戻したいだけで、rebaseはしない。`git fetch origin`（origin/mainはadcca1ccのまま）のあと、ローカル `main` を作成してorigin/mainを追跡させた。続けてローカル `release-1.4`（origin/release-1.4 = 0b90c7db = v1.4.1）も追跡ブランチとして作成した。さらにユーザー指示で、ローカルmain（adcca1cc）から `develop_kaz` を作成した（upstream未設定、checkoutはfix/vm-io-wait-policyのまま）。v1.4.1（0b90c7db）はrelease-1.4上にありmainの祖先ではない。派生ブランチ2本は無変更。push無し。
- juicefs本体のpush先（2026-10-03）: ユーザーが追加したremote `kaz` = git@github.com:tongsama/juicefs.git。`develop_kaz` と `fix/vm-io-wait-policy` に branch.<b>.pushRemote=kaz を設定。upstreamが無くsimpleだとpush先が解決しないため、repo-localで push.default=current を設定した（同名ブランチへpush）。main/release-1.4はpush先がorigin（公式）のままなので、pushしないこと。push自体は未実行で、実行前には必ずユーザーに確認する。
- ユーザー指示で、ローカルブランチ `improve/flush-wait-alter` を削除した（`git branch -d`）。commit 396d8f6a は fix/vm-io-wait-policy の履歴に含まれている。

## JuiceFS バイナリサイズの比較（2026-10-03）

ユーザー質問に基づき、変更せずに以下を読み取り比較した。

- `/tmp/juicefs-vm-io-gc-retire-20261002` と `/home/kwatanabe/.local/bin/juicefs` は同じファイル内容。両方177,381,752 bytes、SHA-256 `1a022a7e186eda8b88593842d16dc90316b2698b15c1aee254346f48fa70073f`、version `1.4.1+2026-10-02.3bed0d82-vm-io-gc-retire-local`。Go1.26.4、VCS revision 3bed0d82、`vcs.modified=true`、ELFはnot strippedでdebug_infoあり。
- 現 checkout `/home/kwatanabe/tmp_local/juicefs_inspection/juicefs/juicefs` は125,961,416 bytes、SHA-256 `9eaf9c055cff8083f2fc07aeb84af15db49b7896e4dea58822a695dacf037d28`、version `1.4.1+2026-10-03.84f19ca4`。Go1.25.11、VCS revision84f19ca、`vcs.modified=false`、`-s -w`付きでstripped。旧バイナリより51,420,336 bytes（約29.0%）小さい。
- `mount --help` ではslice timers、writer flush timeout/reuse window、compaction GC/scheduler各flagを3つとも出力。現HEAD84f19caに該当実装が存在。サイズ差には新HEAD、Go compiler版、debug情報stripの差が関係する。SHA/build metadataから同一ELFとは言えない。旧バイナリはdirty worktreeから生成されVCS metadataに全dirty source tree hashが保存されていないため、厳密なソースtree一致はbuild-infoだけでは証明できない。
- 本比較でPATH上のコマンド選択、稼働中mount、バイナリ置換は行っていない。root docs memo/TODO以外のプログラムやbinaryに変更なし。base branch HEADは84f19ca、push済み状態をユーザーが報告。

## 改修版バイナリ配布の設計（2026-10-04、仕様レビュー待ち）

ユーザー方針: 本体repoは公式の形を保ち、配布物（install.sh/install.ps1、Actions、Releases、版の対応表）はinspection repo（tongsama/juicefs_inspection、公開）に置く。本体はSHA指定でcheckoutするだけ。
- 対象: linux-amd64 / linux-arm64（=aarch64）/ windows-amd64。armv7は、groupcache/tikvの32bit intオーバーフローでビルド不可かつ整合性未検証のため、ユーザー合意で対象外（後で追加できる構成にする）。darwinも対象外。
- ビルド: GitHub Actions（方式1）。arm64はubuntu-24.04-armのネイティブrunner、windowsはmingw+hack/winfsp_headers。draft Releaseまで作り、公開はユーザーが行う。workflow_dispatchでビルドだけ試すモードを用意する。
- install: 既定のインストール名はjuicefs（公式版を置き換える）。WindowsはPATHへ追加せず、WinFspは警告のみ。
- 仕様: docs/superpowers/specs/2026-10-04-release-distribution.md。ユーザーのレビュー待ちで、未コミット（文書のcommitはユーザーが行う）。
- 2026-10-04 仕様の訂正: 本体のpkg/versionは `-X` で上書きできる文字列が revision/revisionDate だけ（1.4.1は固定値）。版表示は `juicefs version 1.4.1+<本体commit日付>.<sha8>-kaz.<n>` とする（pre-releaseに入れないので、metadataのクライアント版比較にも影響しない）。install.ps1はirm|iexで実行するため、JFS_INSTALL_DIRを追加し、exitを使わずthrowで失敗を返す。配布スクリプトはASCIIのみで書く。同じタグのReleaseの有無は、draftも見えるように、release job（contents: write）で作成直前に確認する。
- 実装計画: docs/superpowers/plans/2026-10-04-release-distribution.md（Task1 install.sh+テスト、Task2 install.ps1、Task3 versions.json+リリースノート、Task4 workflow、Task5 手動実行での試行、Task6 draft Release）。commit/push/タグ/Release作成はユーザーが行う。ユーザーのレビュー待ち。
- 2026-10-04 実装（Task1〜4、未コミット）: release/install.sh、release/tests/test_install.sh（13ケース全て合格）、release/install.ps1、release/versions.json（v1.4.1-kaz.1 → 84f19ca4）、docs/release-notes/v1.4.1-kaz.1.md、.github/workflows/release.yml。resolveを手元で実行して期待どおり。ldflagsの版文字列も手元のビルドで一致を確認。
- 全体レビュー（opus subagent）: Critical なし。Important「再mountまで旧版のまま」は誤りと判明した。juicefsのmount supervisorは、起動時に取得した実行ファイルのパスで子プロセスを再起動する（cmd/mount_unix.go:984-1006）ので、置き換え後に子プロセスが異常終了すると新版で動き出す。install.shの文言、仕様、リリースノートを修正した。リリースノートには slice-flush-wait/idle と max-deletes の説明、Windows版のmount動作が未検証であることも追記した。Minor 10件は保留（ledger .superpowers/sdd/2026-10-04-release-distribution/progress.md）。
- 2026-10-04 shellcheck（install.sh・test_install.sh）とactionlint（release.yml）はユーザーの許可を得て実行し、指摘0。
- 残り: ユーザーのcommitとpush、Task5（手動実行での試行）、Task6（draft Release）。
- 2026-10-04 Task5: ユーザーの許可を得て workflow_dispatch（run 37189012217）を実行し、全job成功（Releaseは作らない）。linux-amd64/arm64は静的リンク、windows-amd64は改修コードのままmingwでビルドできた。WinFspが無いwindows-latestでも install.ps1 と `juicefs.exe version` が動き、checksumの不一致は既存exeを変えずに失敗した。手元でもダウンロードしたamd64版の version が `1.4.1+2026-10-03.84f19ca4-kaz.1` で一致。残りはTask6（タグのpushとdraft Release、公開はユーザー）。
- 2026-10-04 Task6: ユーザーの許可を得てタグ v1.4.1-kaz.1（4f69f00）をinspection repoへpush。run 37190020196 が全job成功し、draft Release（6ファイル）ができた。手元でダウンロードし、checksumsは全OK、配布されたスクリプトはrepoと一致、amd64のversionも一致。公開はユーザーが行う。公開後に、実URLから scratchpad へのインストールを確認する。
- 2026-10-04 公開完了: ユーザーが v1.4.1-kaz.1 を公開（https://github.com/tongsama/juicefs_inspection/releases/tag/v1.4.1-kaz.1）。実URLの `curl -fsSL .../releases/latest/download/install.sh | sh -s <dir>` で scratchpad にインストールして version一致を確認。JFS_VERSIONでの版の固定と、存在しない版での404停止も確認。配布の計画（Task1〜6）は完了。保留中のMinor 10件は ledger（.superpowers/sdd/2026-10-04-release-distribution/progress.md）を参照。
- 次の版を出す手順: 本体で修正してkazにpush → release/versions.json に新しいタグ（v1.4.1-kaz.2 など）と40桁のSHAを追加し、docs/release-notes/<tag>.md を書いてcommit・push → 必要なら workflow_dispatch で試す → タグをpush → draftを確認して公開。

## rclone serve s3 の PUT 30秒タイムアウトの原因調査（2026-10-06）

証跡: `rclone_put_timeout/2026-10-06/report-ja.md`（元ログの prefix と SHA を記録。元ログは無変更）。
- JuiceFS の `timeout awaiting response headers` は、JuiceFS 自身の `restful.go` に固定で入っている `ResponseHeaderTimeout` 30s。`exceeded maximum number of attempts, 1` は JuiceFS 側の SDK 設定 `RetryMaxAttempts=1`。どちらも rclone の `--low-level-retries` とは無関係。
- rclone 側では、Drive の changeNotify ごとに `chunks/5/5913` のディレクトリキャッシュが無効になり、Open のたびに約2,800件を全件再読み込みしていた（約6〜8秒周期）。ディレクトリが大きくなるほど処理件数が落ちる（07:2x の 1,226件/10分 → 09:5x の 275件）。Drive のレート制限は観測されていない。
- JuiceFS が切った PUT も rclone は最後まで実行していた。再送と合わせて、同じ key が最大3回完了している（179 key）。
- 対策候補: rclone `--poll-interval 0` と、長めの `--dir-cache-time`（単一書き込みが前提）。JuiceFS 側の30s固定値の扱い。いずれも未検証で、本番設定は未変更。
- 追記（同日）: ユーザーが 10:40 に rclone を再起動し、`--poll-interval 0 --dir-cache-time 1h --rc --rc-addr localhost:5572` を適用した（PID 100624。rc の認証はなし）。再起動後の約20分で changeNotify・invalidation 0回、PUT 完了は1分あたり約35件 → 約340件、PUT の30秒タイムアウトは 10:41 以降0件。長時間の観測は未実施。
- 同日に source で確認した関連事項（`docs/findings.md` の同名の節にまとめた）: staging からのアップロードは1回の処理で3回まで、超えると1分ごとの scan で無期限に再送する（WARN なし）。compaction は io-retries＋1 回。writeback の書き込みは staging のハードリンクとして読み取りキャッシュに入り、アップロード完了で LRU に加わる（atime は完了時刻）。cache-large-write は同期経路だけに効く。キャッシュ・staging は展開済みのデータ。zstd レベルは1で固定（理由の記述なし）。rclone は SIGTERM で停止し、SIGHUP はディレクトリキャッシュを捨てるだけ。rclone の停止が約14秒を超えると、キャッシュにない Read が EIO になり得る。
- 注意: 起動スクリプト `~/utilities/cloudfs/rclone_s3_start.sh` に OAuth の client_secret と token が平文で入っている。調査中に agent が伏せ忘れてツールの出力に表示してしまった（外部への送信や保存はしていない）ので、ユーザーに通知済み。今後このスクリプトを読むときは、`client_secret`／`token`／`*_KEY`／`*_SECRET` を伏せて表示すること。

## staging の0バイト化と `--writeback-fsync`（2026-10-06）

- 事象: WSL のクラッシュ（13:38:34）後、`rawstaging` に0バイトの正式名ファイルが16件残り、`uploadStagingFile` が `invalid file size 0` を出した。mtime はクラッシュ直前の約30秒。原因は、staging の書き込み（`flushPage`）が tmp→rename だけで fsync していないこと。新しいバグではない。
- 調査メモ: エラーが出るかどうか（`isPendingValid`）は、メモリ上の `pendingKeys` を見ているだけで、metadata を参照しているかとは無関係（最初の回答で誤って説明し、訂正済み）。Redis の chunk list（`c{inode}_{indx}`、24バイト/slice、big endian）を SCAN/LRANGE で逆引きするツールを scratchpad に作った（可視バイト数の計算つき）。結果は全件無害。trash はユーザー設定で無効。
- 実装（本体ブランチ `fix/staging-fsync` の e6ab89b8。親は `fix/vm-io-wait-policy` の 84f19ca4）:
  - `chunk.Config.StagingNoSync`（ゼロ値で同期ありにして、gateway/sdk など他の呼び出し元も安全側にした）と mount フラグ `--writeback-fsync`（既定 true）。docs の en/zh_cn `_common_options.mdx` にも追記。
  - `flushPage(..., durable)`: データ書き込み後に fdatasync（linux は `unix.Fdatasync`、darwin/windows は `f.Sync`）→ close → rename → 親ディレクトリを fsync → 祖先ディレクトリを親で fsync。`durableDirs` には、祖先まで全部 sync し終えたディレクトリだけを記録する（同時に書く側が親の sync を飛ばさないため）。staging scan が空ディレクトリを消したときは記録から外す。ディレクトリの sync に失敗したら正式名を消し、stage を失敗させて直接アップロードに切り替える。
  - テスト: `pkg/chunk/staging_sync_test.go`（8件。順序・NoSync・read キャッシュは対象外・file/dir の sync 失敗・既存ディレクトリ・再作成）と `cmd/staging_sync_flag_test.go`。pkg/chunk の失敗は既知の環境依存の `TestInRootVolume` だけ。vfs は Redis が必要な4件を除いて ok。fs と cmd の対象テストも ok。
  - 実バイナリ: `juicefs webdav --writeback`（FUSE は sandbox の setuid/ptrace 制限で strace 不可）で、20ブロックに対し既定は fdatasync 20回・fsync 24回、`=false` では 0/0。
  - 既存の問題: `-race` で `stageFull` の読み書き競合（`checkFreeSpace` と `stage`）が検出される。今回の変更とは無関係。
- 2026-10-06 ユーザーの指示で e6ab89b8 を commit し、`release-1.4.1-kaz.2`（84f19ca4 から作成）へ --no-ff で merge した（9268beb4）。中身は release-1.4（0b90c7db）＋ kaz の3 commit ＋ staging fsync。develop_kaz/main は upstream main のミラーで、kaz 独自の commit はない。push はしていない。
- 2026-10-06 ユーザーの許可を得て、`release-1.4.1-kaz.2` を kaz へ push（9268beb4）。kaz の既定ブランチをこのブランチへ変更し、リモートの `fix/vm-io-wait-policy` を削除した。ローカルの `fix/vm-io-wait-policy` と `fix/staging-fsync` も削除。kaz に残っているのは develop_kaz・main（upstream の写し）と release-1.4.1-kaz.2。84f19ca4（kaz.1 の versions.json が参照）は release ブランチの祖先なので、まだ到達できる。
