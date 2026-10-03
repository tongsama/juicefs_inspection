# 2026-10-02 14:58 run: 待機・phase相関

固定prefix385,860,940 bytes、SHA-256 `ffe72379ec8f42a8e84aef77e31ae81a01d29836118b1c465e408d5bd9493ca2`。対象 `/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-145808.log`、PID413172。14:58:15.025628から16:09:07.542061まで。原ログ変更なし。script `wait.analyze.py` と結果 `wait.results.json` を同folderへ保存。

## Writer barrier

begin/endをbarrier IDで照合した完了28,287件は全errno0。inode/origin一致もassertionで確認。未対応end0。cutoff時beginのみは2件: inode596154 vfs.Fsync(barrier28287)1.953889s、inode596152 vfs.Fallocate(barrier28289).758449s。5分still-waiting、flush interrupted、flush deadline timeout、stack dumpはこのprefixでは0件。

| origin | 完了数 | median | P99 | max |
| --- | ---: | ---: | ---: | ---: |
| vfs.Fsync | 10,907 | 41.287ms | 2.014546s | 14.210586s |
| vfs.Read preflush | 16,769 | 63.210µs | 177.892ms | 20.561719s |
| vfs.Fallocate | 593 | 30.279ms | 8.485664s | 10.873769s |
| vfs.Flush / vfs.Release / internal.close | 各6 | <1ms | <1ms | <1ms |

これはwriter preflush時間。ReadのデータGET時間や、barrier begin前のhandle lock待ちを含む全Read durationとは区別する。

## 同期compactionと同inode待ち

2500 sliceの同期routeは1回。16:02:03.071272 inode596152 chunk39 slice4209208がphase=compactへ入り、16:02:12.651730にcommit9.711997819s errno0で完了。metadata Write内compact時間9.580389976s。別chunk0 slice4209222もinode lock9.626937667s待ち、16:02:12.681769にcommit9.657054103s errno0で完了。

関連vfs.Fsync barrier26161は16:02:02.234127開始→16:02:13.272622終了(11.038469712s errno0)。slow operation FsyncとReadは同時に成功終了。以前の40分以上の同期commit停止・15分EINTRとは異なり、このprefixに同様の長commitはない。slow metadata/commit最大9.712s、1秒以上のsummaryは各19件。

cutoff直前のmetadata未doneは2件ともdoWrite開始から約12ms以下。古いlock_wait/compact phaseが残る状態ではない。

## Background compactionとRead/Write遅延

slow compaction summary481件。total中央値14.454s、P995m弱、最大22m20.924s。最大例はinode596153 chunk6、15:25:18.153345完了: object22m20.768s、metadata136.034ms、queue_wait1.237µs。他に10m23s、9m8s、5m5sのbackground例がある。

summary481件におけるmetadata最大223.438ms、P99160.622ms。queue_wait最大42.877µs。長時間phaseは以前のcleanup/metadataではなくobject callbackへ移っている。ただしobject callback時間はcloudだけのRTTとは限らず、GET/PUT retry、upload slot、memory等を含む。

全VFS slow operationは23件、最大は16:04:49.385550 inode596152 Read46.786693s。Writeも同時に46.666022sでOK、複数Readも42秒程度で同時にOK。その後16:05:06.170995同inode Fsync14.210638s OK。reader/storage/handle lockの遅延波及が疑われるが、VM再起動との因果はguest/QEMU時刻と照合が必要。

phase inventoryでphase=readのまま見えるchunkはactiveと断定不可。compactChunkは不足slice等でphase=doneを出さず早期returnする(source base.go2887/2897等)。phase=objectのcutoff未doneも、現在pendingかerror早期returnかは他ログ/stackが必要。

## 区別と不足

startup staging再送PUTとこのrunで作成・commitしたsliceを時刻だけで結合しない。ここではwriter barrier、metadata slice ID、同期compactionを相関し、新しいrequestのcommit待ちを測定した。過去stage PUTが残っていても今回のbarrier successを否定する証拠ではなく、逆にbarrier successだけで全stageのcloud upload完了は示さない。

このprefixでは長時間writer commit停滞や待機errorは見つからないが、VMのunexpected reboot、guest filesystem状態、QEMU cache mode、guest watchdog、全readデータ整合性は未検証。『再起動原因なし/FS安全』という結論は出せない。新modeのpriority/GC lifecycle違反を直接示すphase証拠はこの調査では見つからなかった。
