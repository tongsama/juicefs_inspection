# VM I/O ローカル統合版の仕様 — 合意した方向と実装条件

> この文書は調査プロジェクト側へ移設した記録です。`pkg/`・`cmd/` などのソースパスと Go コマンドは、別管理の `juicefs/` リポジトリを基準にします。記載の作業状況は当時の履歴で、最新の知見は調査ルートの README と `docs/findings.md` を参照してください。

ユーザーはGC分離（案1）、軽量なcompaction実行制御（案3）、flush／slice再利用の継続調査（案4）を選択し、実装済みのFUSE watchdog修正も一緒にまとめるよう明示的に依頼した。ローカルでレビュー可能な統合版を作る。本番への適用、VM再起動、データ修復は含めない。指定された現在の作業ディレクトリとブランチで、既存の未コミット変更を保持して継続する。開始時から未追跡のファイルをstage／commitしない。

## 完了待機とエラーの方針

FUSE timeout=0の修正、writerの既定期限=0、明示的なwriter期限、kernelキャンセル通知、実際のstorage／metadataエラー、read-after-write、commit順序を維持する。必要なデータ／metadata処理が終わる前に成功を返さない。inode全体のロック構造変更（案2）は対象外。

## GC分離

`meta.Config.CompactionGCMode`は文字列とし、CLIは`--compaction-gc-mode legacy|deferred`、既定はlegacy（Go設定の空文字もlegacy）とする。参照数減算が永続化済みのcompaction obsolete sliceだけを新経路へ渡す。他の削除呼び出し元の意味は維持する。deferredは正のMaxDeletes、sessionの背景回復処理が有効、書き込み可能であることを要求し、不正なCLI組合せはmount前に拒否する。Goからの不正設定も、安全に拒否または既存モードへ戻し、回復条件がないまま新モードを有効にしない。

既存の永続dead-reference markerと回復周期を再利用する。新しいディスク上のmetadata形式や、全ref走査の増加は導入しない。既存DELETE workerが物理削除を行う。独立した有界通知channelとdispatcherがobsolete仕事を既存workerへ渡す。foreground compactionは既存削除channelの空きも、そのmutexも待たない。通知が満杯／停止の場合は、無制限goroutineを作らず、既存回復処理が使う永続intentを残す。object削除成功までmarkerを保持する。停止時に通知が失われてもよいのは、永続intentが残る場合だけである。legacyの動作は変えない。新モードを準備完了と判断する前に、copy／共有参照とcrash境界を監査し、未確認の「dead参照は絶対に復活しない」保証を主張しない。pendingだけの永続index追加は初版に含めない。

## 優先scheduler

`meta.Config.CompactionScheduler`は文字列、CLIは`--compaction-scheduler legacy|priority`、既定はlegacyとする。doWriteが既に返すcount／tierと、cache missで既に読み取るslice情報を使う。毎writeの追加DB照会や、全file／chunk走査は行わない。独立部品が`(inode, chunk, count, tier)`を受け、callbackを実行する。固定個数の優先bucketと有界重複排除mapを使い、実行中に受けた通知も保持する。ready inodeのround-robin等による有界な公平制御で、一つのhot fileが全jobを独占しないようにする。scheduler lockは管理情報の操作中だけ保持し、storage I/O中は保持しない。優先度通知は揮発的な助言情報として扱う。満杯時は既存の受付制限と同様に背景仕事を延期できるが、既存の2,500件での同期処理と手動force経路を安全網として残す。本版は、以後アクセスされない全cold chunkが必ず整理されることを保証しない。NoBGJobでも通常Read／Write起因compactionが利用できる現在の意味を保ち、ライフサイクル終了時は通知を安全に止める。

## flush／再利用の診断

`vfs.Config.WriterReuseWindow`は整数、CLIは`--writer-reuse-window`、既定4、正値で最大64とする。Go設定のzeroは4へ正規化する。gap／overlap／完成済みblockの制約は維持し、古い候補をfreezeする距離だけ変える。context wrapperで呼出元originとprocess内のbarrier識別子を渡し、FileWriter／DataWriter interfaceは変更しない。VFSと高位fsのFlush呼出元へ注釈を付ける。DEBUGでbegin／freeze／endとerrnoを対応付け、debug無効時の呼出ごとのallocation／counter更新は避ける。テストは古いpartial sliceの再利用と、fsync／Read barrier後の最終データを比較する。slice数と実cloud PUT数を同一視しない。

## 検証

機能ごとにRED／GREENを確認する。GCでは既存legacy queue待機回帰を残し、飽和queue／mutex下でもdeferredが可視metadataをcommitすること、通知overflow／停止後の永続markerと回復、削除失敗時の保持、参照安全性、無効／負設定、backend parityを検証する。Redis／SQLite／MemKVを実行し、PostgreSQLは先のユーザー指示に従いsource確認のみとする。schedulerでは容量上限、重複排除、urgent／公平dispatch、generation再投入、close／cancel、lock外callback、通知に正しさが依存しないことを確認する。flushではcaller／barrier対応、既定の動作一致、大きいwindowでの再利用とreadback／fsyncを確認する。FUSE実kernel試験と既存writer／error回帰を再実行する。広いsuiteの依存不足を正確に記録する。本版で本番適用は行わない。

## 安全監査から追加した条件

RedisのCopyFileRange／単一clone／batch cloneで、source chunk snapshotと参照追加の競合を検出するため、chunk読み取り前のWATCHと再試行を追加検証する。これは案2のinodeロック構造変更ではなく、共有参照を回収するために必要な局所的transaction検証である。作業量はコピー対象chunk数に限定し、全volume走査は増やさない。旧clientは新しい検証を守らないため、新モードの評価時は同volumeの書込clientを更新版へ揃える条件を明記する。KV全種類の既存transaction原子性を、未実施の実テストまで保証したとは表現しない。
