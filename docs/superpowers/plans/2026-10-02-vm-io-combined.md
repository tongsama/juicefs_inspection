# VM I/O 統合版の実装計画

> この文書は調査プロジェクト側へ移設した記録です。`pkg/`・`cmd/` などのソースパスと Go コマンドは、別管理の `juicefs/` リポジトリを基準にします。記載の作業状況は当時の履歴で、最新の知見は調査ルートの README と `docs/findings.md` を参照してください。

> **担当agentへの指示:** `superpowers:subagent-driven-development`を使用する。下記のファイル所有範囲を守り、統合境界では順番に反映・検証する。

**目標:** FUSE待機修正、切替可能なGC分離、切替可能な軽量scheduler、明示flush／再利用診断を、整合性を弱めずにまとめる。

**構成:** 既存metadata markerでGC intentを永続化し、有界・揮発の通知部品で実行を改善する。既定はlegacy。主担当がCLI、共有field、文書、検証を統合する。

**技術:** Go、既存go-fuse、Redis／SQL／KV metadata、隔離したローカルtest backend。

**仕様:** `docs/superpowers/specs/2026-10-02-vm-io-combined.md`

## 共通制約

やり取りとplan／specは日本語。現在のブランチ、既存FUSE／診断差分を保持する。本番書き込み、commit、push、PRは行わない。関数／型に目的コメント、新規sourceにlicense headerを付ける。PostgreSQLのruntime testは行わない。`/tmp/juicefs.memkv.setting.json`を共有するmetadata／VFSテストは`flock /tmp/juicefs-inspection-tests.lock`で直列化する。

### 作業1: compaction GC分離

**担当:** GC worker。

**ファイル:** 新規`pkg/meta/compaction_gc.go`とtests、`pkg/meta/config.go`、`base.go`のGC field／ライフサイクルだけ、redis.go／sql.go／tkv.goのobsolete cleanup呼出箇所。GC統合が安定した後、主担当がbase.goのscheduler統合部分を変更する。

- [x] 永続dead-marker／copy参照の不変条件を監査する。挙動を提供する前に未確認事項を報告する。
- [x] opt-in deferredテストで削除transportを止め、compaction metadata完了を要求する。legacyは既存どおり待つ。
- [x] legacyへ委譲するscaffoldでREDを確認後、有界dispatcherと永続marker回復を実装する。
- [x] 実metadataでoverflow／shutdown／recovery、失敗時marker保持を検証する。無制限jobを作らない。
- [x] 自己レビューし、helper／ライフサイクルの正確なsignatureを主担当へ共有する。
- [x] Redisの共有参照snapshotを読み取り前WATCHで守り、隔離した複数clientで競合時のabort／再試行を回帰検証する。

### 作業2: 独立した優先scheduler

**担当:** scheduler worker。

**ファイル:** 新しいscheduler本体とtestsだけ。base.go／config.goは編集しない。

**interface:** constructorは`newCompactionScheduler(run func(Ino,uint32,int), workers, capacity int)`、通知は`schedule(inode Ino, chunk uint32, count, tier int) bool`。`close()`はpending管理と新通知を安全に停止し、`wait()`は実行中callbackとworkerの終了をjoinする。主担当がcallbackとライフサイクルを接続する。signature変更は主担当と調整する。

- [x] 重複排除、容量overflow、公平性、urgent順序、active中のdirty再投入、closeの失敗テストを作る。
- [x] 固定bucketの有界・助言schedulerを実装し、callbackをmutex外で実行する。
- [x] 対象raceを実行する。揮発overflowの限界とsource countの扱いを報告する。

### 作業3: flush呼出元とslice再利用window

**担当:** VFS worker。

**ファイル:** 新しいtrace context helper／tests、vfs/writer.go、vfs/vfs.goのcaller注釈、pkg/fs/fs.goの注釈。vfs.goにConfig.WriterReuseWindowを追加する。cmd flagは編集しない。

**公開adapter:** `WithWriterFlushOrigin(ctx meta.Context, origin string) meta.Context`。元contextの挙動を維持し、DEBUG時だけ有効化する。既存writer公開interfaceは変えない。

- [x] origin／barrier相関と、window 4対16の古いpartial slice再利用について失敗テストを作る。
- [x] 有界の正値正規化、既定距離4、freeze／end診断を追加する。
- [x] overlap、fsync、read-after-write、実errorを検証する。barrierを弱めない。

### 作業4: 主担当による統合

- [x] flag不足でCLIテストが失敗することを確認後、3つの検証済みflagとconfig伝達を追加する。
- [x] priority scheduler field／start／stopを接続し、通常の背景通知だけ置き換える。2,500件時と手動forceは維持する。
- [x] 各worker差分をレビューして指摘を解消し、必要なbackend／統合回帰を追加する。
- [x] 対象test／race、package suite、FUSE実kernel試験、build／version、diff／gofmt確認を実施する。
- [x] docs、memo、TODOへ実結果とmode／既定／overflow／回復の制約を反映する。統合ローカルbinaryを作り、本番へは適用しない。


## 最終検証の記録

- CLI: 新flag不足のREDを確認後、伝達／不正組合せを実装。readonly拒否を含む対象race3回は1.735秒で成功。
- Meta: Redis snapshot競合7case／WATCHエラー3case、GC各backend回復、schedulerと実Read通知・終了join、旧queue回帰を統合してrace3回2.292秒で成功。個別GC・Redis安全性は10回、schedulerは20回のraceも成功。
- I/O: VFS／CLI／実kernel FUSE対象race3回がそれぞれ3.498／1.768／11.708秒で成功。DEBUG無効のsliceログ引数12allocationをREDで再現し、条件分岐で0へ修正した。
- 通常suite: VFS9.632秒、FS2.143秒、FUSE4.788秒で成功。
- 局所benchmark: schedulerの同一key更新は容量1／1024で約28〜34ns、0allocation。実機の総合性能保証ではない。
- make test.pkgはGlusterFS開発依存とcoverage出力先不足等で失敗。広い全suite成功とはしない。PostgreSQL runtime未実施。
- 元Englishの要件を省略せずplan/specを日本語化。新しい3オプションのdocsを追加。commit／本番適用は未実施。


## 実機適用後の追加調査: GCとrawstaging滞留（2026-10-02）

ユーザーが統合版を適用した後、rawstagingが53,000件を超え、従来の高速大量DELETEが観測されなくなった。初版のmarker保持・手動recoveryテストだけでは、hourly回復下の持続的な削除遅延を検証できていなかった。新モードの性能上の問題として優先して調査する。

- [x] 実ログとstatsで未PUT/DELETE成功53,823sliceとstaging53,823件の一致を確認する。
- [x] runtime pprofとjob別metricsを取得し、GCdispatcher受信待ち、DELETE10workerのリモート待ち、upload枠30使用、cleanupSlices回収25,669を確認する。現時点でhintoverflowは証明されていない。
- [ ] 少数サンプルのrawstaging存在とRedis負refを読み取りで照合し、必要なupload待ちとobsolete回収待ちを分離する。
- [ ] 背景compaction・GC待ち・upload待ちを安全なローカル再現で組み合わせ、hourly回復・legacy/deferred差・持続的なstage残留を検証する。
- [ ] 実証した経路に対して、永続deadmarkerに基づくローカルstage回収をremoteDELETE待ちから分離する設計、または有界GC予算に基づく背景開始制御を比較する。単純なqueue拡大や全refs走査の高頻度化を解決にしない。
- [ ] 選択した補正の失敗する回帰を先に作り、inflightupload、sharedref、crash/retry、全metadata engine parityとfsync/read-after-writeを確認して実装する。

本番のVM、metadata、staging、起動設定は調査中に変更しない。ゲスト再起動の原因は別に未確定として保持する。
