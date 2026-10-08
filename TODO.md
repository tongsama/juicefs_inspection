# JuiceFS 調査・修正 TODO

更新: 2026-10-03。現在の入口は [README](README.md) と [知見の要約](docs/findings.md)。`juicefs/` 本体は別 Git 管理とする。

## 現在の状態

- 本体ブランチ `release-1.4.1-kaz.2`（kaz の既定ブランチ）、HEAD `9268beb4`（2026-10-06 に push 済み）。旧 `fix/vm-io-wait-policy`（84f19ca4）はローカル・リモートとも削除済みで、このブランチに含まれる。
- ユーザーが staging 回収補正版を適用し、30s/10s で耐久試験合格と報告した。10/03 の再起動で15s/10sへ変更。15秒版は短期観測。
- 最終runtime確認はwriter/FUSE期限0、reuse16、GCdeferred、priority、max-deletes10、max-uploads18。
- 保存待機は耐久fsync335774件全成功。一方、Readfile EIOと旧clientのVFS EIO累積1207が見つかり、全I/O無エラーとは評価しない。
- ドキュメントは調査ルートへ分離した。文書と本体のcommitはユーザーが別々に行う。agentからadd/commit/pushしない。

## 次の優先作業

- [ ] **client-cache（CSC）の不具合調査（2026-10-08 追加、未着手）:** メタデータの変更（例: ファイルのパーミッション）が、client-cache（meta URL の `client-cache=true`）を有効にした他の client に反映されない。
  - ユーザーが確認した事実: 裏側のメタデータ DB（Redis）では、正しく変更されている。
  - 関連: 計画書 §5.4（自分の commit が local cache を消さない、BCAST の invalidation が遅れる窓）、`pkg/meta/redis_csc.go`。実機は client-cache が有効（client-cache-expire=24h）。
  - 進め方: 一時 Redis で2つの client（client-cache 有効）を使い、chmod 等が他方に反映されない状態の再現テストを先に作る。invalidation（tracking・BCAST・pubsub）の経路を確認する。本番の Redis（56379）には接続しない。

- [ ] **rclone serve s3 の修正の検討（2026-10-08 追加。次の会話で継続。引き継ぎは agent_memo の「引き継ぎ（2026-10-08、次の会話へ）」）:**
  - 症状（ユーザーの観察）:
    - JuiceFS は object のパスを直接指定するので、ディレクトリのリストは要らない。それなのに、パスを直接取得しようとすると、rclone は毎回ディレクトリのリストを取りに行く。
    - dir cache を使うと、今度はファイルの有無までキャッシュしてしまい、存在するはずのファイルを「無い」と返す。
    - 背景: 2026-10-06 の調査（`rclone_put_timeout/2026-10-06/report-ja.md`）。現在は `--poll-interval 0 --dir-cache-time 1h` で運用中。
  - 方針（やる場合）: JuiceFS と同じ形にする。
    - rclone のソースを、このリポジトリの中の Git ignore したディレクトリ（例: `/rclone`）に clone して修正する。
    - このリポジトリで release を作る。release のディレクトリ構成（`release/`、`versions.json`、workflow、install スクリプト）を、JuiceFS と rclone の2つの成果物に対応する形へ変える必要があるかもしれない。
  - 先にやること:
    - rclone VFS の、パス指定での取得（HEAD・GET）がディレクトリのリストを必要とする経路を、ソースで確認する。
    - 「存在するはずのファイルを無いと返す」negative cache の条件（dir cache の有効期間、自分の PUT がキャッシュに反映されるか）を、ソースで確認する。
    - 修正案と upstream への報告の要否を比較する。実装はユーザーの承認後。
  - 補足（ユーザー、2026-10-08）: 複数台から mount するので深刻。dir cache が有効だと、他の client が新しく作ったファイルは metadata では見えるのに、rclone が「無い」をキャッシュしていて input/output error になる。
  - 構成（ユーザー回答、2026-10-08）: (A) 各ホストがそれぞれ rclone serve s3 を動かし、同じ Drive フォルダ（`/rclone-s3`）を見ている。
  - 2026-10-08 ソース調査: [報告](rclone_dir_cache/2026-10-08/source-investigation-ja.md)。方式 (b') VFS の lookup モードを軸にすることをユーザーが承認。
  - Phase 分け（ユーザー承認、2026-10-08）:
    - [ ] Phase 1（コード実装済み、実機検証と本番適用が残り）: (b') lookup モード（`--vfs-lookup-by-path`）、HEAD/GET のエラーを 404 にしない、`--no-cleanup` の配線、DELETE 時に `b.meta` を消す（メモリが増え続ける問題）。[仕様](docs/superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md) はユーザー承認済み（2026-10-08）。[実装計画](docs/superpowers/plans/2026-10-08-rclone-lookup-by-path.md) を作成（ユーザーのレビュー待ち）。
      - 2026-10-08 実装: rclone `feat/kaz-vfs-lookup`（1.75.1-improve-kaz から分岐、c27d12d03..9b75066b4 の11 commit、push なし、未 merge）。タスクごとのレビューと最終レビューを通過。race つきテスト ok（`cmd/serve/s3` は docker が要る TestS3Minio を除く。変更前から失敗）。
      - 追加の変更（レビューでの判断）: ListBucket・PutObject・DeleteBucket の bucket 確認も「無い」ときだけ 404 にした。lookup 中にディレクトリ名が変わった場合は1回やり直す。
      - JuiceFS は Get/Put で 404・500・503 を区別せず再試行する → 500 で十分（[記録](rclone_dir_cache/2026-10-08/juicefs-error-handling-ja.md)）。
      - 残る注意: `b.meta` は他ホストが消した key の分が残る（Phase 2 まで定期再起動で抑える）。key 指定の新規 PUT ごとに Drive API が1回増える（実機で測る）。
      - [x] 実際の Drive での検証（2026-10-08、[記録](rclone_dir_cache/2026-10-08/drive-verification-ja.md)）: 他ホストの新規 object がすぐ読める、DELETE で実際に消える、一覧の取り直し0回を確認。テスト用フォルダは削除済み。
      - [x] 新しい key の PUT で Drive API が2回増える問題（名前の検索が1→3回）: ユーザー承認の追加修正（lookup モードかつ cache-mode off の `O_CREATE|O_TRUNC` では VFS が存在を問い合わせない、0e050fab1・9b75066b4）で、lookup モードなしと同じ1回に戻した。実機で再計測済み。
      - [x] 本番適用の手順書（2026-10-08、[手順書](rclone_dir_cache/2026-10-08/deploy-runbook-ja.md)）
      - [ ] 本番への適用（ユーザー）。全ホストで `--no-cleanup` を指定すること。適用後は RSS（`b.meta`）と rate limit を観測する。
      - [x] 2026-10-08 ユーザーの指示で `feat/kaz-vfs-lookup` を `1.75.1-improve-kaz` へ `--no-ff` で merge（dd03d0243、merge 後もテスト ok）。ユーザーが作成した `tongsama/rclone` を `kaz` リモートとして追加し、`1.75.1-improve-kaz` を push（既定ブランチ）。`feat/kaz-vfs-lookup` はローカルに残してある。
    - [ ] Phase 2: S3 のユーザーメタデータ（JuiceFS の `x-amz-meta-crc32c`）を Drive の properties に保存し、どのホストからも・再起動後も返す。現状はプロセスのメモリ（`b.meta`）にだけあり、他ホストの PUT や再起動後は JuiceFS のチェックサム検証が黙って省略される。
      - 2026-10-08 着手（brainstorming、architectural）。ユーザー判断: 保存するのは `x-amz-meta-*` すべて（A）。既存の object への後付けは範囲外（前提、未確認）。
      - 方式 A＋C2（PUT 時に properties を同梱、Drive に properties だけを取るオプション）をユーザーが承認。オプション名は `--kaz-s3-persist-metadata`（serve s3）と `--drive-kaz-properties`（Drive backend。backend のオプションは `--<backend>-kaz-<内容>` とする規則の補足をユーザーが承認）。[調査](rclone_dir_cache/2026-10-08/phase2-metadata-investigation-ja.md)、[仕様](docs/superpowers/specs/2026-10-08-rclone-s3-persist-metadata-design.md)（ユーザー承認済み）。既存データ（メタデータ無し）に対する取得・上書き・削除・再取得のテストをユーザーが要望し、仕様に入れた。
    - 独自オプションの命名（ユーザー承認、2026-10-08）: 接頭辞 `--kaz-<対象>-<内容>`。ただし backend のオプションは rclone が backend 名を先頭に付けるので `--<backend>-kaz-<内容>`（例 `--drive-kaz-properties`）（例 `--kaz-vfs-lookup-by-path`）、ヘルプ先頭に `[kaz]`、可能なら flag グループ「Kaz」。既存オプションの不具合修正（`--no-cleanup` 等）は upstream の名前のまま。
    - [ ] 範囲外の既知のリスク: Drive の同名フォルダの重複（複数ホストが同時に新しい chunks フォルダへ最初の PUT をすると、それぞれ作成し得る。lib/dircache の FindLeaf→CreateDir に、ホスト間の排他が無い）。今も同じリスクがある。2026-10-08 に読み取りのみの問い合わせで確認し、`rclone-s3` 配下（1,442 フォルダ）に重複は 0（[記録](rclone_dir_cache/2026-10-08/dup-folders-ja.md)）。回避策の候補（全ホストで同じ規則で正のフォルダを選び、作成直後に検索し直して寄せる、Drive backend の opt-in オプション）は Phase 1b として別の仕様にする。object の key は metadata DB の slice ID で一意だが、フォルダ（ID 1000 ごと）は 4096 個単位の払い出しの境界で2ホストが共有し得る。発生の幅は狭いので、ユーザー判断で優先度を下げ、TODO に残すだけにする（2026-10-08）。
    - [ ] Phase 3: release 構成を JuiceFS と rclone の2成果物に対応させる（別の spec）。
  - 2026-10-08 rclone v1.75.1 を `rclone/`（Git ignore、独立リポジトリ、ブランチ `1.75.1-improve-kaz`）に clone した。fork `tongsama/rclone` は未作成。

- [ ] **JuiceFS 改修版の独自オプションに `kaz` 名前空間を付ける（2026-10-08、ユーザー要望、いずれ）:** rclone と同じ規則（`--kaz-...`、ヘルプ先頭 `[kaz]`）に揃える。対象例 `--writer-flush-scope`、`--meta-write-batch`、`--writeback-fsync` など。既存の設定・起動スクリプトとの互換（旧名を別名として残すか）を検討する。

- [ ] **metadata path 最適化（2026-10-07。Phase 1・2 は実装済みで `1.4.1-improve-kaz` に merge 済み、残りは次の機会）:** [調査結果と実装計画](docs/superpowers/specs/2026-10-07-metadata-random-io-optimization-plan.md)。主指標は qcow2 への Ubuntu Desktop 26.04 インストール時間（現状 約1時間）。ユーザーの計画承認後に着手する。
  - [x] 2026-10-07 ユーザー実施の計測（unsafe、debug付き、12:52〜14:16）を解析した: [報告](metadata_random_io/2026-10-07/install-report-ja.md)。最大の待ちは fallocate(ZERO_RANGE) の全 inode flush（3,330s／173ms が flush 待ち）。commit は 43ms×62,317件。
  - [x] 同日 raw でも計測した: 約36分（qcow2 約84分）、fallocate 0回、最大の待ちは Read 前 flush 799s。
  - [x] 計画書を実測に合わせて改訂（§7 の順番、C4 の詳細設計・テスト・見込み）。
  - [ ] **最優先（実測で変更、Phase 1）:** Read 前と Fallocate 前の writer flush を、対象範囲と重なる chunk の pending に限定する（計画書 C4）。その後 group commit。writeback（QEMU）条件での計測は未実施。
    - [x] 2026-10-07 実装・検証済み（本体 `feat/range-flush` 18e641b8。`1.4.1-improve-kaz` から作成。2026-10-07 に merge 済み）。`--writer-flush-scope=range` で有効。詳細・懸念は agent_memo の「Phase 1 C4 実装」。計測用バイナリ `~/tmp_local/juicefs-builds/juicefs-rangeflush-20261007`。
    - [x] qcow2 の range 計測（2026-10-07 17:34〜18:27）: **約84分 → 約53分**。fallocate 3,330s → 1,887s、commit −44%、write 合計 −80%。Read 前の待ちは変わらず。[報告](metadata_random_io/2026-10-07/install-range-report-ja.md)
    - [x] raw の range 計測は、ユーザーの判断で行わない（ほとんど変わらない見込みのため、2026-10-07）。
    - [x] 本体の commit: `feat/range-flush` 18e641b8（2026-10-07、未 push）。バイナリ `~/tmp_local/juicefs-builds/juicefs-rangeflush-18e641b8`。
    - [x] 同じ handle の Write が preflush 中に待つ件 → 2段階 preflush（Read・Fallocate、range のみ）で対応した（2026-10-07）。
    - [x] mtime の巻き戻り → commit の順序と mtime の下限で対応した（file にも有効）（2026-10-07）。
    - [ ] 要判断: range では、範囲外の追記の quota エラーが Fallocate ではなく後続の I/O で返る。
    - [ ] `TestSmallPUTDiagnostics` が全体実行で1回だけ FAIL した（payload サイズの照合）。再現せず、原因は未特定。
  - [ ] 実機で client-cache が有効なので、stale attr の再現テストを先に行う。statfs を約1秒周期で呼ぶ uid0 の pid 565/567/569 の正体（WSL 側？）は未確認。compaction GC local_error 487件も別途確認。
  - [ ] Phase 0: metadata metrics（op 別 p95/p99、open-file／txn lock 待ち、Redis cmd RTT、flush 時 pending 数、slices/chunk、compaction 回数）と、Redis RTT・インストール時の baseline 計測。
  - [ ] Phase 1: 同期 compaction（≥2500）を open-file lock の外へ。実機 meta URL の `client-cache` 有無を確認し、doWrite の stale attr（ファイル長後退）の可能性を再現テストで確認。
  - [x] Phase 2: 同 chunk の slice commit をまとめる（案C、3 engine、`--meta-write-batch` 既定0）。本体 `feat/meta-write-batch` f9308391（push なし）。[設計](docs/superpowers/specs/2026-10-07-meta-write-batch-design.md)・[計画](docs/superpowers/plans/2026-10-07-meta-write-batch.md)。最終レビューの Critical 2件・Important 3件を修正済み。
    - [x] qcow2 の計測（2026-10-07 21:19〜22:02）: **約53分 → 約43分**。transaction −41%、Read 前の待ち −81%、Fallocate 前の待ち −28%。[報告](metadata_random_io/2026-10-07/install-batch-report-ja.md)
    - [x] 2026-10-07 `1.4.1-improve-kaz` へ `--no-ff` で merge した（78acd63d）。2026-10-08 にユーザーが push。feat/range-flush・feat/meta-write-batch のブランチは残してある。
    - [x] 2026-10-08 **v1.4.1-kaz.3 を公開した**（本体 78acd63d、調査リポジトリのタグ 852b8f5）。ユーザーが install の動作を確認済み。
  - **次の機会に回す改善（2026-10-07 ユーザー方針）**。根拠は [batch 版の報告](metadata_random_io/2026-10-07/install-batch-report-ja.md) の「次の候補」:
    - [ ] Fallocate の metadata 更新を、範囲内の pending の commit と同じ transaction にまとめる（Fallocate 前の待ち 905s ＋ Meta.Fallocate 約590s が対象。meta API と 3 engine に関わる）。
    - [ ] chunk をまたぐまとめ（案A）。まず、単発の commit 14,235件のうち、他の chunk とまとめられる割合をログから見積もる。
    - [ ] Redis の transaction の RTT 削減（計画書 C7。WATCH／GET／MULTI-EXEC／UNWATCH を pipeline や Lua で 1〜2 RTT に）。
    - [ ] 計画書の残り: C2（同期 compaction を lock の外へ）、C5・C6（chunk cache の更新、空 chunk の cache）、client-cache の stale attr の再現テスト、`--large-file-mode`。
    - [ ] Phase 2 で先送りにした Minor 6件（`.superpowers/sdd/2026-10-07-meta-write-batch/progress.md`）。加えて、`TestWriteSlicesRedis` は Redis がないと skip せずに失敗する（`newRedisMeta` が接続エラーを返さないため。既存の TestRedisClient と同じ挙動）。
    - [ ] writeback（QEMU cache=writeback）条件での計測（fsync の待ちへの batch の効果）。
    - [ ] 先送りにした Minor 6件（ledger `.superpowers/sdd/2026-10-07-meta-write-batch/progress.md`）。chunk をまたぐまとめ（案A）と `--large-file-mode` は計測の後に判断する。
  - [ ] Phase 3: Read 前 flush の範囲限定、自分の commit 結果で chunk cache 更新、空 chunk の cache。
  - [ ] Phase 4: Redis txn の RTT 削減（WATCH+GET pipeline、Lua）を実測次第で判断。
  - 前提: fio・redis-server は未導入、RTT 注入（netem／toxiproxy／docker image）はユーザーの承認が必要。

- [ ] **staging fsync（2026-10-06）:** `--writeback-fsync` は e6ab89b8 として commit 済み、`release-1.4.1-kaz.2`（9268beb4）へ merge 済み（未 push）。実 VM 負荷での書き込みレイテンシ・PUT への影響を計測し、必要なら次の配布版（v1.4.1-kaz.2 など）に含める。

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
- [x] rclone serve s3 の PUT 30s timeout（2026-10-06）: 原因は、Drive の変更通知による VFS ディレクトリの全件読み直し。ユーザーが `--poll-interval 0`／`--dir-cache-time 1h` を 10:40 に適用し、短期の観測で解消を確認した。詳細は `rclone_put_timeout/2026-10-06/report-ja.md`。
- [ ] 上記対策の長時間観測: chunks ディレクトリの切り替わり、1時間ごとのキャッシュ期限切れのとき、`/rclone-s3` を変更するのがこの rclone だけであることの維持。
- [ ] 任意: JuiceFS の `ResponseHeaderTimeout` 30s 固定の扱い（設定可能にするか）。staging 再送が上限に達したときに WARN を出すか。zstd レベル 1 と 3 の圧縮率・CPU 比較（どれも未着手、実装はユーザーの指示があってから）。
- [ ] 任意: rclone の `--rc` は localhost のみで、`--rc-user`／`--rc-pass` がない。同じホストの他ユーザーから操作できるので、必要なら認証を付ける。

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
- [ ] 【保留・今回対象外】inodewideLock構造変更。ユーザーは複雑性/リスクに対して効果が不明瞭として案2を今回選択せず。→ 2026-10-07 の依頼で再び対象。計画では lock 分割ではなく group commit を推奨（上記 metadata path 最適化）。
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
- [x] commitとpush（085a43b、4f69f00）、Task5の手動実行 run 37189012217 が全job成功（2026-10-04）
- [x] Task6: タグ v1.4.1-kaz.1 をpushし、draft Releaseを作成（run 37190020196、2026-10-04）
- [x] v1.4.1-kaz.1 を公開し、実URLからのインストールを確認（2026-10-04）
- [ ] 保留したMinor 10件（ledgerを参照）
