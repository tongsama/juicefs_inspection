# 2026-10-02 freeze調査

対象は `/home/kwatanabe/.juicefs/diagnostics/vm-io-20261002-014922.log` の先頭179,156,595 bytes。末尾は完全な改行で、2026/10/02 02:24:09.308508まで。SHA-256は `2f70019e76d958259462fff848083b8726fb7bcb3dd5701dfd9d1fbea2604788`。原ログの変更なし。

`freeze.analyze.py`で同じprefixを再集計できる。集計は`freeze.results.json`、個別sliceの相関は`freeze.correlations.jsonl`。日時はログ表記のJST。percentileはnearest-rank。

## 確定したfreeze条件の分布

freezeは17,696件。最初01:54:46.525254、最後02:24:08.853201。すべて起動5分後cutoff 01:54:30以降であり、「全期間」と「起動5分後」は同じ結果。起動後5分間はfreezeログがないため、初期PUTのfreeze理由はこのログから不明。

| reason | 件数 | 割合 | age中央値 | age P95 | age最大 | idle中央値 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| explicit_flush | 9,900 | 55.9448% | 1.715677ms | 3.631669019s | 7.516422954s | 1.565697ms |
| writable_window | 7,781 | 43.9704% | 1.192480ms | 7.949085ms | 5.122677898s | 1.173068ms |
| idle | 15 | 0.0848% | 10.068657456s | 10.084276976s | 10.084276976s | 10.068624423s |
| age / slice_pressure / full_slice / commit_age | 0 | 0% | — | — | — | — |

explicit_flushは全9,900件がage30s未満かつidle10s未満。age<1sは8,309件(83.9293%)、<100msは7,790件(78.6869%)、<10msは6,796件(68.6465%)。writable_windowも全件age30s・idle10s未満。よって99.9152%のfreezeは設定timer以前に、明示Flushまたはslice再利用探索の制限で起きた。現在の条件ではtimerをさらに長くしても、この二原因は変わらない。

全freezeのraw_lengthは中央値4,096、P90 61,440、P95 196,608、P99 782,336、最大4,194,304 bytes。raw=4KiBは11,656件(65.8680%)。

| raw=4KiB freezeのreason | 件数 | 4KiB freeze内の割合 |
| --- | ---: | ---: |
| explicit_flush | 5,620 | 48.2155% |
| writable_window | 6,030 | 51.7330% |
| idle | 6 | 0.0515% |

この表はslice freezeのraw_length比率であり、cloud PUT理由の比率ではない。

## IDと後続処理の対応

freeze時ID0は146件。`(inode, chunk, started_unix_ns)`でfinishを対応させ、全17,696 freezeにfinishが見つかった。off/raw_length/既存nonzero IDの一致もassertionで確認した。対応しないfinishと重複identityは0。

全17,696件にslice commit入口とmetadata `phase=done`が見つかった。ただしdoneログはerrnoも含むため、ここではphase到達を集計し、成功errno判断・health評価は別担当へ委ねる。

成功PUTはprefix内11,718件だが、freezeから割り当てたslice IDと一致する成功PUTは0。PUT ID範囲は3,876,082–4,040,855、freeze ID範囲は4,022,999–4,040,977。範囲は重なるがIDそのものは一致しない。この結果から、freeze reasonと実測payloadの比率を結合できない。起動前からのstaging backlog、同時compaction、prefix右端の未完了uploadを考慮せず、全PUTを新freezeへ時刻だけで対応させてはならない。

15,495 freeze slice(87.5622%)に成功DELETEが同IDで見つかった。例slice4022999は01:54:46.525254 freeze→finish→metadata done(01:54:46.558918)→01:55:51.162668 DELETEであり、prefix中の同ID PUTはない。

## 仮説・不足

新しい小sliceはwriteback stagingされ、cloud upload前にcompaction/obsolete cleanupで削除された可能性が高い。ただしこの仮説を確定するには、現mountのwriteback条件とcompaction output ID、staging lifecycleを結合する必要がある。freeze→DELETEのみでは、そのDELETEの生成契機やstaging成功を単独で証明しない。

explicit_flushログには呼び出し元がない。Read、Fsync、Flush、Close、Fallocate等のどれかを識別できず、fsyncが原因とは断定できない。今回の結果は「明示barrierがtimer前にsliceを閉じた」ことまでは直接示す。

## 次の最小設計提案

まず挙動を変えず、明示Flush入口のcaller分類を追加し、inodeとbarrier単位の識別子をfreezeへ関連付ける。これでRead由来が大半なのか、fsync/close等の耐久性barrierが大半なのかを切り分けられる。staging成功/失敗とcompaction入力/出力IDの相関も、通常PUTを待たずslice寿命を確認する診断として有効。

Readが主因なら最小の挙動変更候補はread範囲に重なるpending dataのoverlayである。sliceのwrite順序、overlap、truncate、read error、同inode並行操作を保つ設計と回帰試験が必要。fsyncが主因なら単純にFlushを遅らせて成功を返す案は不可。fsyncを跨ぐ集約には耐久性とmetadata可視性を保証するstaging indirectionの設計が必要。

writable_windowはtimerと独立して約44%を閉じている。これを変える場合、単に探索個数を増やすだけではgap/complete-block rewrite/overlap制限が残る。pending範囲のメモリ量、最新writeの優先順、既にupload済みblockの不変性を維持するsparse-range/coalescing設計を別検討する。
