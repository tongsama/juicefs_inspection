# 4 option効果: writer/freeze担当集計

3本をmanifest指定prefixから各1回streaming走査。元ログ無変更。SHAは別担当へ委ねる。最新ユーザー訂正に基づき、旧combined/長期runは30s/10s、直前再起動runは15s/10sとして扱う。15→30という途中説明は撤回し、比較方向は30→15。実runtime `.config` のPID1156153も15s/10sと整合。

## 範囲と精度

| file | PID | 固定bytes | 行数 | log時刻範囲 |
| --- | ---: | ---: | ---: | --- |
| 145808旧combined | 413172 | 794,809,736 | 4,907,086 | 10/02 14:58:15–20:14:08 |
| 201718 retirement耐久 | 683253 | 5,976,434,018 | 36,536,314 | 10/02 20:17:23–10/03 15:33:19 |
| 142854再起動短期 | 1156153 | 267,671,574 | 1,603,566 | 10/03 14:29:02–15:33:19 |

全prefixは完全改行で終了し、不完全行除外bytes0。全eventはそれぞれ表のPIDのみ。個別record全件保存は避け、durationはlog2 histogram(倍増ごと64bin、約1.1%幅)のnearest-rank interval。件数、min/max、threshold件数、raw長percentileは正確。p50/95/99の小数を正確値として扱わない。

時間別CSVのcreation列は`started_unix_ns`による「今回freezeを観測できたsliceの生成hour cohort」。全NewSlice allocationではなく、compaction生成objectやprefix末尾の未freeze sliceを含まない。quiet hourも0行として補完した。

## Freeze内訳と小slice制限

| run | freeze | explicit_flush | writable_window | idle | raw=4KiB slice割合 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 旧combined | 101,766 | 80,265 (78.872%) | 21,493 (21.120%) | 8 | 65.033% |
| retirement耐久30s | 843,306 | 800,560 (94.931%) | 42,730 (5.067%) | 16 | 41.034% |
| 再起動短期15s | 22,713 | 19,540 (86.030%) | 3,159 (13.908%) | 14 | 62.805% |

age/slice_pressure/commit_age/full_sliceは全3runで0。explicitとwindowは全件age10s未満。idleは約10.05–10.10sであり、idle10sと整合する。age15s/30s理由がないので、freeze ageからwait15/30を識別・効果推定することはできない。wait設定はユーザー訂正/runtime設定による根拠であり、age発火ログによる確認ではない。

raw=4KiB表はVFS slice raw_lengthであり、cloud PUTの圧縮payloadでも、成功PUT key suffix比率でもない。

| explicit origin | 旧combined | 耐久30s | 短期15s |
| --- | ---: | ---: | ---: |
| vfs.Fsync | 69,402 | 682,152 | 17,366 |
| vfs.Read | 5,835 | 28,801 | 1,638 |
| vfs.Fallocate | 5,028 | 89,607 | 536 |

explicit内Fsync比率はそれぞれ86.466%、85.209%、88.874%。readだけの問題ではなく、fsyncを尊重したfreezeが主な小slice生成制限。wait30→15を変えてもこのbarrierを越える集約は現在設計では起きない。

reuse16の効果としてwindow freezeの割合は耐久runで5.067%まで低い。ただし3runはいずれもreuse16を含む構成で、workloadが大幅に変わる。旧21.120%→耐久5.067%という差をreuse設定単独の効果とは断定不可。同じ耐久run内でもwindowは10/02 20時台22.108%から、10/03 08時台0.443%へ変わり、負荷・アクセスpatternの寄与が大きい。

## barrier完了/errnoと待機分布

| run | barrier完了 | fsync完了 | fsync median概数 | P95概数 | P99概数 | 最大 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 旧combined | 47,696 | 24,471 | 42.8–43.3ms | 448.7–453.6ms | 1.414–1.430s | 14.210586s |
| retirement耐久30s | 560,104 | 335,774 | 40.5–41.0ms | 229.3–231.7ms | 788.0–796.6ms | 14.756335s |
| 再起動短期15s | 82,234 | 7,912 | 41.0–41.4ms | 269.7–272.6ms | 850.1–859.3ms | 14.191824s |

全barrier end errno0。各run begin/end対応missing0、cutoff outstanding0、boundedmap eviction0。fsync<15秒は全件。この観測範囲では以前の15分自動EINTRや数十分のcommit barrier停止に相当する結果はなく、FUSE/Writer timeout0を含むcombined構成の継続安定を支持する。ただし無期限設定の実際の発火を測るには遅いpending状態が必要であり、0期限そのものがこの改善の単独原因とするものではない。

耐久runのRead writer preflush最大10.131715s、短期最大12.906119s。Read全体のGET/reader/handle lock時間とは別。barrier errno0だけでguest整合性、VM再起動なし、全cloudstage upload成功を示さない。

freeze時ID0は旧337/耐久2558/短期35件、すべてfinishの`(PID,inode,chunk,started_unix_ns)`へ対応し未完了0。finishはmetadata/physicalPUT成功の証明ではない。

## 耐久時間・負荷epoch

耐久logは約19時間続くが、freeze最終は10/03 12:51:24.616034、window最終12:51:12.110787。それ以降15:33までslice生成freeze/barrierは0で、旧processが残りbackground処理ログが続いている区間。log全長をそのままactiveVM耐久19時間と表現しない。active freeze区間は最初から12:51まで約16時間半。

耐久runのfreezeは21–00時台に11.4万–15.1万/h、02–11時台は約1.85万–1.95万/h。短期restart後14時台12,888、15時台9,825(15:33まで)で、期間・workload・startup read/stage replayが同等でない。30/10耐久と15/10短期のP99やraw比率を単純比較してtimer変更の因果としない。

## 結論の範囲

combined導入後のwriter側は大量のfsync/read/fallocate barrierをerrno0で完了し、旧問題の長barrier/EINTRはこの3prefixで見えない。GC局所退役/deferred durability/schedulerがbackend停滞を除いたかはhealth/payload/phase担当の結果と結合する。writer側から各option単独の改善率を割り当てることはできない。

依然としてfsync由来explicit flushがsliceを早期に閉じ、小raw sliceの主要原因。age timer15/30は実際の閉鎖契機でないので、今回の変更によるPUT件数低減を期待値として示す根拠はない。fsyncを越える集約にはdurable staging/visibility設計が必要。

## Artifacts

`writer_analyze.py`:1pass streaming再現script。`writer_vm-io-*.json`:PID/reason/origin分布、duration interval、bounded correlation。`writer_vm-io-*_hours.csv`:PID別hour生成cohort/finish/freeze/begins/endsと割合。`writer_summary.csv`:比較表。`writer_progress.log`:走査経過。原ログ変更なし、sourceコード/本番操作なし。

## 追加例外: writer成功は全Read成功を意味しない

追加source/metrics調査で、耐久runの20:42:48にinode596155のreader sticky EIOと、旧process snapshotのVFS errnoEIO累積1,207が確認された。これはwriter barrier表と矛盾しない。VFS.Readはpreflush後にreader.Readを行い、reader EIOをkernelへ返し得る。速いエラーはmetricsへ加算された後、個別logが省略される。従って「全barrier errno0」の事実を「全IO error無し/耐久完全合格」と読むことは禁止する。

latest GET404は同key後続成功が確認されたが、内部retryであって全request成功まで自動的に示すものではない。1207はmetrics取得時点のprocess累積で、固定prefix内の時刻/operation/QEMU actorへ割り当てられない。詳しい確定経路、shared retryとsingleflight cancel fanout仮説、必要な最小診断は`writer_read_error_findings.txt`に記録した。
