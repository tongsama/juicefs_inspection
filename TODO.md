# JuiceFS 調査・修正 TODO

更新: 2026-10-03。現在の入口は [README](README.md) と [知見の要約](docs/findings.md)。`juicefs/` 本体は別 Git 管理とする。

## 現在の状態

- 本体ブランチ `fix/vm-io-wait-policy`、HEAD `3bed0d82`。後続改善の実装差分は本体に未コミットで残っている。
- ユーザーが staging 回収補正版を適用し、30s/10s で耐久試験合格と報告した。10/03 の再起動で15s/10sへ変更。15秒版は短期観測。
- 最終runtime確認はwriter/FUSE期限0、reuse16、GCdeferred、priority、max-deletes10、max-uploads18。
- 保存待機は耐久fsync335774件全成功。一方、Readfile EIOと旧clientのVFS EIO累積1207が見つかり、全I/O無エラーとは評価しない。
- ドキュメントは調査ルートへ分離した。文書と本体のcommitはユーザーが別々に行う。agentからadd/commit/pushしない。

## 次の優先作業

- [ ] **Read EIO:** shared retry counter／singleflight先頭ctx取消fanout→sticky EIOを隔離再現し、3bed baselineと比較する。実エラーは隠さず、再現回帰を先行させる。
- [ ] Read異常のcaller、sliceReader identity、取消元・理由、retry値の診断を検討する。既存counterだけでQEMU／guestへの帰属を決めない。
- [ ] 15s/10sの継続評価、負荷を合わせたreuse-window単独比較、CPU内訳を必要時に計測する。
- [ ] fallocate mode、Read前flushが対象範囲外のpendingを閉じる程度を分類し、整合性を保った改善余地を検討する。
- [ ] hint overflow／local busyの周期回復、pending-only GC index、cold workの回復保証は後段の設計課題とする。
- [ ] 元ログの保管とverbosity運用を整理する。checksum検証を維持し、正常DEBUG出力抑制を候補にする。openなproducerを再確認し、勝手に削除・圧縮・切り詰めしない。

## 完了した調査・文書化

- [x] 同期DELETE→compaction待ち→旧writer期限EIO、外側FUSE15分watchdogの別経路を特定し補正。
- [x] Read前flushのエラー伝播、既定期限なし待機、実writer error伝播を保持。
- [x] GC local retirement、upload競合保護、priority scheduler、reuse16と診断を実装・対象検証。
- [x] 固定ログのraw/payload/normal/compaction/GC、freeze origin、fsync、健全性、DEBUG容量を集計。
- [x] 明示的freezeの内訳Fsync85.2%／Fallocate11.2%／Read3.6%を確定。呼出数とslice数を区別。
- [x] 調査ドキュメントをこのルートへ移設し、README・findings・資料一覧・リンク・内容保持を確認。

## 過去の作業履歴

以下は10/01以降に追記した当時の作業記録。『現在』『未適用』『未計測』等はその時点の状態であり、現在の優先作業は上の一覧を参照する。古い未チェック項目だけを現在の未完了作業と解釈しない。

更新: 2026-10-02

## 最優先: 2026-10-02 VM filesystem障害の再発

ユーザー報告: 3VM中少なくとも2台で以前同様のguestFS障害、全3台停止済み。小PUT最適化は保留。

- [x] 失敗返却経路をログとsourceで特定。VFS期限0の外側に固定15m go-fuse watchdogが残り、05:57/06:34のfsync等をEINTRで終了させた。
- [x] GenFuseOpt Timeout0へ修正。実error/明示writer期限/kernelcancel通知保持、globalwatchdogの強制replyはoff。RED→GREEN、realFUSEpending/error/watchdog/kernelcancel、CLI伝達、既存VFS/Meta回帰とrace、ビルド確認。
- [x] positive deletionqueue backpressureをread-onlylivepprofで確認(07:43:48)。長metadata cleanup＋inodeLock＋同期compact待ちが継続。過去障害時刻stackとは区別。
- [ ] 本番への修正版適用とconfig FuseOpts.Timeout0確認（明示指示なしで実行しない）。現在稼働版はsmallput-diag-local/Timeout15mのまま。
- [ ] backend/削除queueの30分級cleanup停滞への対策。必要ならenqueue wait/queue長・cleanup producerの診断とdurablecleanup設計を検討。単にqueuewaitをerror/未保存successへ変えない。
- [ ] 隔離VMでの継続再現とguestFS状態確認。既に障害を受けたdataの復旧/repairは別作業、勝手に実行しない。

## 優先1: VM向けの待機・エラー方針

- [x] 既定のflushを期限なし待機へ変更し、処理遅延・compaction待ち・削除queue待ちを経過時間だけでEIOに変換しない。
- [x] 実際の保存失敗/容量不足/quota超過は伝播し、未完了の保存を成功として返さない。
- [x] 削除queue飽和→背景compact後処理待ち→同期compact待ち→Write→Flushまでの境界を回帰テストで確認する。
- [x] 負のmax-deletesを同期削除と明示する起動WARN、CLI help、option docsを追加する。
- [x] 対象テスト/race、5分を実時間で越える長時間回帰、ビルド、差分レビューを完了する。

- [x] PostgreSQLのSQL共通・固有経路をソース確認する（実テスト不要のユーザー指示に従い未実施）。今回の待機修正の固有不整合は見つからず。

## 優先2: sliceタイマーと小さいPUTの調査（優先1の完了後）

ユーザーの意図: `--slice-flush-wait 30s --slice-flush-idle 10s` によりraw staging slice/blockがより大きく育ち、object PUT回数が減ること。実機では1KB未満のPUTが多数あるように見える。

- [ ] PUTの実payloadサイズと、圧縮前のslice/blockサイズ、stagingファイルサイズを分けて計測する。圧縮やゼロの多いデータが小さいpayloadになっているだけか確認する。
- [x] timer設定が実行中のConfigへ届いていることを確認する（現在のFUSE `.config`: 30s/10s、writer timeout=0、待機版Version）。
- [x] sliceの再利用条件を追い、連続write/ランダムwrite/上書き/複数chunkでの違いを説明する（実VFS＋mem object＋temp stagingで比較、latest data readbackあり）。
- [ ] timer以外のfreeze/flush契機（fsync、read前flush、fallocate等、full block/slice、slice数・メモリ圧迫）を計測する。
- [ ] qcow2の小さいランダムwriteとfsync頻度を再現し、timer延長でまとめられるwriteと、整合性を守るために即座の保存が必要なwriteを区別する。
- [ ] まとまることを確認する回帰/計測テストを先に作り、正常なfsync・read-after-write・writeback耐久性を保って意図した挙動に修正する。
- [x] timerだけで達成できない条件と必要な集約設計・制約を説明する。fsync成功の先送り・保存未完了の成功応答でPUT数を減らさない。

2026-10-01 続きセッションで調査と診断追加を実施。集約の動作修正は未実施。観測サイズの意味や実機のfreeze原因を仮説から断定しない。

- [x] key suffix=raw block、slice長、圧縮後payload、stage file長の意味を確定。現volumeはzstd/4MiB、実機の各payload分布は未計測。
- [x] 旧診断ログを固定範囲・SHA付きで通常write/compactionに分類（通常成功15,719件、raw4KiB=61.24%、raw<1KiB=11件。compaction成功737件、raw4MiB=700件）。
- [x] freeze reason + ID確定対応 + PUT payload_bytesの診断追加。診断テストRED→GREEN、計測テスト3回、VFS通常suite、memory経路＋従来待機/Read失敗回帰race3回、ビルド成功。
- [x] cloud PUTを止め、512 raw→516 stage bytes、fsync完了とreadback、解放後zstd19 payload bytesを確認。writebackローカルcommit待機と非同期cloud uploadを区別。
- [x] 新診断版の実機freeze理由を取得。10/02最新run: explicit55.94%、writable_window43.97%、idle0.085%。この時点では明示flush内訳が未確定だったが、10/03にorigin付きログで分類済み。
- [x] 観測元を照合。ユーザー回答は `juicefs stats --verbosity` のobject.put / put_c。通常更新行は圧縮bytes/sとAPIcall/s、比率は期間平均。normal/compaction/failedretry混在。任意データの圧縮率や全PUT<1KiBの分布は未確定。
- [ ] 実機freeze理由が確認できてから、pending read overlay / sparse range集約 / durable stage indirection等の必要性と範囲を決める。今回の直接VFS試験は実QEMU/qcow2ワークロード全体の再現ではない。

### 別途検出した既存課題

- [ ] disk writebackの `stageFull` 対 `checkFreeSpace` raceを別途検討。新計測テストを未変更HEAD3bed0d82へコピーしたarchiveでも再現。今回の診断変更には原因を帰属しない。フルdisk race成功とはしない。
- [ ] 初期の直接upload試験で `rawFull` とcache/noOp.Compressのraceも観測。こちらは独立baseline未検証。データ破壊との因果は未確認。
- [ ] pkg/chunk既存DATA RACE（disk_cache/disk_cache_state、未変更HEADでも11テスト失敗）。今回対象外。
- [ ] 極端な `SliceFlushWait*2` duration overflow候補（通常30sとは無関係、ソース指摘のみ未再現）。


### 2026-10-02 最新runによる進捗

- [x] raw/payloadを実logで別集計。成功PUT11,718中<1KiB4,754。旧stage再送8,337とcompaction3,381を分離、5分後も旧stage再送継続。
- [x] timer外freezeが主群と確認。17,696sliceの65.87%がraw4KiB、全metadata done errno0。
- [x] 明示flush callerをorigin付きログで分類。耐久explicit800560slice中Fsync682152(85.2%)、Fallocate89607(11.2%)、Read28801(3.6%)。close由来の新規freeze0。
- [ ] 新stage→upload/compaction/obsoleteを相関。このprefixで新freeze同IDのPUTは0、DELETE同ID15,495。残pending/obsolete内訳を断定しない。
- [ ] writable_window制限を維持/拡大した安全な局所比較を設計し、再利用効果・memory/metadata数・fsync/read-afterwriteを計測。設定timerだけを再延長しない。

最新run解析結果はsmall_put_investigation/2026-10-02/。sourceの集約動作はまだ変更していない。


### VM inode長停滞の設計候補（2026-10-02、未実装）

- [ ] 保存済みdataとmetadata commitからobsolete GCのRAMqueue待ちを分離するdurablecleanup設計を比較。参照数/再試行/crash/全metadata engine parityを前提にする。
- [ ] 同chunk背景compactionのactive状態がcleanup待ちで長く残ることを改善し、hotchunk優先schedulerとrawlist高低watermarkを検討。
- [ ] 【保留・今回対象外】inodewideLock構造変更。ユーザーは複雑性/リスクに対して効果が不明瞭として案2を今回選択せず。
- [ ] 新slice発生数低減案とdeletedata実throughput案を比較。threshold/queuecap引上げだけを解決としない。

ユーザーの追加要件: VM用filesystemとして、データ整合性と待機方針を維持しつつ通常使用時の数十分のinode全体停止を減らす。方法の比較段階、個別案の実装は未合意。


### 選択された進め方（2026-10-02、設計段階）

- [x] ユーザーは1/3/4を選択、2は今回対象外。既存挙動と新GC分離modeの切替を希望。
- [ ] 1のv1仕様: legacydefault＋optin分離mode、永続deadmarkerを根拠にbounded GC通知/dispatcherを設計。foregroundが既存dSliceMu/queue満杯を待たないことを検証。
- [ ] 1の安全監査: ref再追加/共有CopyFileRange、crash/restart/mode戻し、DELETEretry、0/負MaxDeletes、NoBGJob、全engineparity。既存markers保証なし経路へ一括拡張しない。
- [ ] pending-only永続GCindexは後段候補。既存collectorを高頻度fullrefs走査へ変更しない。
- [ ] 3は既存numSlices通知/平均O(1)更新/有界候補/bucketと公平性。全inodechunkscanと毎write追加DBlookup禁止。overflow保証と既存2500fallbackを仕様化する。
- [x] 4のexplicitflush origin+barrierID診断と実機内訳を確認。
- [ ] writablewindow単独の実機比較は継続課題。局所4→16 commits10→9は確認済み、実PUT削減率の単独帰属は未実施。

### FUSE＋1/3/4のローカル統合版（2026-10-02）

- [x] FUSEwatchdog修正を統合。実kernel・error・cancel回帰保持。
- [x] GC legacy/deferred切替、永続marker＋有界独立dispatcher。新schema/scan増加なし。
- [x] priority opt-in scheduler、通知平均O(1)、固定bucket・有界1024・inode公平性。old2500/manual安全網保持。
- [x] explicitcaller/barrierDEBUG診断、writer-reuse-window4既定/1..64、fsync/readデータ回帰。
- [x] Rediscopy/clone source snapshot競合をRED→WATCH修正→retry/Error回帰。newmode評価は全writer更新条件。
- [x] plan/spec全3file日本語化、英語内容の要件欠落なし。
- [x] 対象race・fullVFS/FS/FUSE・CLI・build確認。allpkgは環境依存失敗。
- [ ] 実機configの独立確認とVM継続試験・PUT/latency/memory総合計測。2026-10-02にユーザーが補正版へ更新したと報告（下記参照）。
- [ ] pending-only永続GC indexとcoldjob eventual保証は後段、初版には含めない。


### 統合版実機での再起動とGC滞留（2026-10-02、優先調査）

- [x] ユーザー適用のcombined版/configをread-only確認。FUSE0・writer0・deferred・priority・reuse16有効。旧『未適用』は過去の状態。
- [x] proxmox01 vol1/inode596152の42〜46.79s成功I/O遅延、itco reset設定を確認。ゲスト再起動原因/readonlyは未確定、前bootjournal異常なしというユーザー観測を保存。
- [x] del_c集計経路継続をsource確認、新旧高速DELETE burst差を実log比較。53823未PUT/DELETE成功slice集合がruntime staging53823と一致。
- [x] livepprof/Prometheus取得、GC通知受信待ち・DELETE10workerリモート待ち・upload30枠占有・cleanupSlices回収25669を確認。実機hintoverflowは未証明。
- [ ] 少数staging sampleの存在/rawsizeとmetadata負refを照合し、live upload待ち/obsolete回収待ちを分離する。
- [ ] GC modeのhourly recovery・backpressure除去のsteady-state回帰を先に作成し、bounded local-retirement/remoteDELETE分離やbackground admissionの補正設計を比較。FIFO remote backlogとstaging取消遅延、inflightupload整合性を対象にする。
- [ ] GC hintdrop/queue/localstage age・dead backlogの可視化。全file/chunk走査やperwrite追加DB参照を導入しない。
- [ ] 本番変更はユーザー所有。元ログ/staging/VM/DBは削除・変更しない。


### ユーザー承認のstaging回収補正（2026-10-02）

- [x] 「改善して」を受け、日本語spec/planとTDDでlocal retirementをremoteDELETE待ちから分離。
- [x] RetireSlice callback/nonblockinghandoff/marker保持/固定metrics/DEBUGを実装、3backendのshared/error/recovery/legacy安全性を検証。
- [x] optionalcachedStore.Retireと同storeupload/stagecallback busyguard、ACK前pending、scanner取消競合、localunlinkerror/retryを実装・race検証。
- [x] cmd接続/customstore互換とerror伝播、独立review、cachedStore通常試験を確認。
- [x] fullVFSの診断capture競合をcontrolledRED→testhookで解消、fullVFS/FS/FUSE再検証PASS。
- [x] 別名binary build/version/SHA、docs/memo/testevidence最終化。ownisolatedRedis停止結果はmemoへ記録。
- [x] 統合版＋補正をローカルコミット 84f19ca4（旧69fd077b、fix/vm-io-wait-policy、author tongsama、2026-10-03）。push無し。
- [ ] 実機適用後にlocal_retire_success/hint_overflow/remote_deferredとstagingage/PUTを定量照合。ユーザーによる更新・低staging・安定という初期報告あり。agentからの本番変更は行わない。

fullpkgchunkはGo1.25.11(goenv)で通常49PASS（TMPDIRはルートFS上）。raceはHEADにも既存の11失敗あり、新retire testsはrace PASS。hourlyhintoverflow回復・別clientのinflightuploadlease不在は残る。guest再起動解消を未実証のまま確定扱いしない。


### 補正版のユーザー実機試験（2026-10-02、継続中）

- [x] ユーザー報告の適用手順を記録: 全VM停止→新規書き込み停止→rawstaging/deletionの全排出を待機→JuiceFS更新→VM3台再起動。
- [x] 初期観測を記録: 高いdisk I/O負荷でも安定、rawstagingは常に低水準、PUT減少とゲストI/O速度向上の印象。これらはユーザー観測で定量比較前。
- [x] ユーザー実施の継続負荷・耐久試験では問題なく、2026-10-03に耐久テストクリアと報告。試験条件を越えた一般保証には広げない。
- [ ] stage件数/bytes/age・PUT率・I/O/fsync待ちの定量評価と、compaction時CPU負荷の内訳は未計測。

ユーザーはもう少し様子を見る方針。現時点では追加の設定変更/実装/自動監視を始めず、今の設定での継続観測を優先する。


### 耐久合格後の設定評価（2026-10-03）

- [x] ユーザー方針を記録: compaction集中時の少量CPU増は許容、耐障害性を優先。
- [x] 提示値wait15s/idle10s/timeout0s/reuse16をsourceで再確認し、timerfreezeと保存待機を区別。
- [x] reuse16は同chunk pendinglistの非適合候補freeze距離であり、探索数/保持数の厳密上限ではないこと、適合判定が先行・frozen entriesも位置へ数えることを確認。
- [ ] 定量性能比較/CPUprofileは別途必要時のみ。今はユーザーが許容した現設定を維持し、本番変更は行わない。


### オプション効果の固定ログ解析（2026-10-03）

- [x] タイマー時系列を訂正: 耐久合格30s/10s→直前再起動で15s/10s。起動scriptと現在configの15s/10s一致をread-only確認。
- [x] 3logの固定completeprefix、sourceinventory、runtime/config/metricsを保存。元logの変更なし。
- [x] freeze/origin/barrier・PUTraw/payload/normal/compaction/GC・health/longwait・DEBUG容量内訳を集計し、日本語report-ja.txtとSHA/JSON/CSV/例外証跡を保存。
- [x] 実測と未分離の因果を区別し、loginventoryにproduceropen状況を保存。旧耐久logもopenなので終了済み扱いにしない。元log変更なし。


### ログ解析で判明したRead側の残課題（2026-10-03、優先）

- [x] 保存側は耐久Fsync335774全成功・writerbarrier560104全errno0と確認。一方、Readfile EIO1行と旧PIDのVFS EIO累積1207を確認し、『全I/O無エラー』と分けて記録。
- [ ] reader共有retrycounter・singleflight先頭ctx取消fanout→stickyEIOの安全な再現、3bed baseline比較、呼出元/取消理由の診断を検討。実保存失敗を隠さず、再現回帰先行で対応。今回source修正なし。
- [x] 最新GET404はsamekey3.63秒後成功・同ID削除記録無し、永久欠落とは評価しない。
- [ ] CPU時系列・window16単独効果は未分離。タイマー30→15のagefreezeは全0で直接効果未観測。
- [ ] 元ログ保管/verbosity運用は別途実行する。checksum検証を維持し、正常DEBUGログ出力の抑制を候補とする。openな旧/現producerのlogは勝手に整理しない。


### 明示flushの中身の分析結果（2026-10-03）

- [x] 呼出元は耐久期間Fsync85.2%/Fallocate11.2%/Read3.6%（新しくfreezeしたslice数の比率）。writerbarrier呼出回数335774/56437/167768とは区別する。
- [x] filewriter.Flushは同inode全pendingchunksのsliceをfreezeし、Finishの保存と作成順metadata commitの完了を待つ。Read前も範囲限定ではなく同file全pendingを対象。WBではcloud全upload完了と別。
- [ ] Fallocate mode(通常予約/punch-hole/zero-range等)と、Read時のどの範囲のpendingがflushされたかの実機内訳は未分類。整合性維持の上で必要性を検討する際の次の診断対象。


### 改修版バイナリの配布（2026-10-04）

- [x] 設計の合意（Actions、3対象、draft Release、install.sh/ps1）。armv7は対象外。
- [x] 仕様 docs/superpowers/specs/2026-10-04-release-distribution.md のユーザーレビュー
- [x] 仕様の承認と実装計画の作成（docs/superpowers/plans/2026-10-04-release-distribution.md）。
- [x] Task1〜4の実装と全体レビュー（未コミット）。install.shのテストは13/13合格。
- [x] shellcheck / actionlint 実行、指摘0（2026-10-04）
- [ ] ユーザーのcommitとpush → Task5（workflow_dispatchでの試行。Windowsビルドが改修コードで通るかをここで確認）→ Task6（タグのpushとdraft Release、公開はユーザー）
- [ ] 保留したMinor 10件（ledgerを参照）
