# VM I/O staging回収の実装計画

> この文書は調査プロジェクト側へ移設した記録です。`pkg/`・`cmd/` などのソースパスと Go コマンドは、別管理の `juicefs/` リポジトリを基準にします。記載の作業状況は当時の履歴で、最新の知見は調査ルートの README と `docs/findings.md` を参照してください。

> 実装担当はTDDとsubagent-driven-developmentで以下を実行する。ユーザーの既存指示に従いcommit/add/pushと本番変更は行わない。

**目的:** remote DELETEのFIFO待ちから、不要sliceのlocal staging回収を分離する。
**構成:** metaの有界GCdispatcher→local-only RetireSlice callback→非待機remotequeue。chunk側はpending/active upload協調によりlocal取消とmarker保持を両立する。
**技術:** Go、既存MsgCallback、Prometheus、isolated metadata/store。
**仕様:** ../specs/2026-10-02-vm-io-gc-retirement.md

## 全体制約

legacydefault/新modeoptinを維持、FUSE/watchdogとwriterの既存修正を保持。fsync/read-after-write/実error伝播を維持。newpublicChunkStoremethod要求やschema追加、全files/chunkscan、unboundedqueue/workerを導入しない。本番VM/cache/stage/DBを変更しない。関数commentとGoApacheheaderを追加する。PostgreSQL実試験を行わない。

## 作業1: metadata dispatcher

対象pkg/meta/compaction_gc.go、interface.go、base.go、compaction_gc_test.go。callback型はfunc(uint64,uint32) error、message RetireSlice=1009。既存3引数newCompactionGC呼出との互換を保持する。

- [x] remote出力満杯でも後続sliceのlocalcallback完了を待てるRED testを追加し、現在のblocking dispatcherで失敗を確認する。
- [x] localcallback後nonblockingremotehandoff、失敗/満杯時marker保持を実装。eventcounterを追加し専用loggingを有界化する。
- [x] localerror/sharedref/overflow/stop/recoveryとMemKV・SQLite・isolatedRedisを検証する。

## 作業2: local retirementとupload競合

対象pkg/chunk/cached_store.go、必要最小のdiskcache helper、retirement tests。optionalAPI Retire(id uint64,length int) error。

- [x] remoteDeleteを止めたAと独立のBのstage取消、live読戻しを実cacheで検証するRED testを作る。
- [x] Removeのlocal前半をerror-return helperへ分離、stage取消errorを返す。
- [x] stageack前pending登録、即時/queuedupload開始とRetireの協調、進行中PUT時marker消去保留のRED testを先に追加する。
- [x] 進行中PUT成功後abandonedDELETE失敗、再回収成功、queued取消時noPUT、正常WB耐久性を確認する。

## 作業3: cmd接続・レビュー・提供

対象cmd/mount.go、cmd/retirement_callback_test.go、調査側 docs/development/vm_io_diagnostics.md、docs/en/reference/_common_options.mdx。

- [x] RetireSlice登録・id/size・localerror伝播・customstore互換のRED callbacktestを作る。
- [x] optionalinterfaceのcallback登録を実装、型が非対応なら明示no-opで従来Removeを使う。
- [x] 独立IOreview、対象raceと既存FUSE/Writer/Read/Redis回帰を実行。必要範囲の通常suite、diffcheck、build/version/SHAを確認する。
- [x] 日本語memo/TODOとdocsへ仕様・観測event・残るhourlyoverflow制約を反映し、新binaryを別名で提供する。旧binary/本番適用は変更しない。


## 最終検証結果

- meta: remotequeuefull時の後続localretire RED→GREEN。MemKV/SQLite/隔離Redis16380でmarkererror/sharedref/legacy/copyguard race3/10回成功。sharedhintの誤投入をcompiler overlayだけで再現し全3backendFAIL後、実source20/3回成功。
- chunk: 実diskstage/CRC/objectmemを使う9tests race3回16.280s成功。全Linux productionGoFiles＋cached_store_test/retire_testの通常実行13.131s成功。fullpkgchunkは既存mockey/Go1.26 runtime.duffcopy/duffzero linker不整合で実行できず、全suite成功扱いしない。
- cmd: 新callback2cases RED→GREEN、combinedoptions/FUSEconfig込みrace3回2.023s成功。
- fullIO最終: VFS10.785s、FS2.054s、FUSE5.275s成功。実kernelFUSE試験と既存writererror/read-beforeflush回帰を含む。途中のdiagnostic capture失敗は背景Progress.DoneによるglobaloutputresetをcontrolledREDで確認、testhookだけ修正して再試験。productionFlushには追加変更なし。
- GoFormatとdiffcheck成功。別名binary /tmp/juicefs-vm-io-gc-retire-20261002 build/version確認、SHA1a022a7e186eda8b88593842d16dc90316b2698b15c1aee254346f48fa70073f。
- FUSE/writer既存修正保持、branch/HEAD変更なし。add/commit/pushなし。元診断log/本番VM/DB/cache/staging/起動設定変更なし。
- 残る制約: hintoverflow/localbusyのhourly回復、remotegarbageの有界量保証なし、他clientinflightuploadleaseは未導入。実機53k全部dead・今回guest再起動解消は未実証。起動script/configパス待ちのmetadata少数照合は今後の観測課題。
