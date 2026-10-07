# range flush 版での Ubuntu インストール計測（2026-10-07、qcow2）

## 条件

- JuiceFS: `1.4.1+2026-10-07.18e641b8-rangeflush`（本体 `feat/range-flush` 18e641b8、exe の SHA-256 `c94da572…c572e`）。
- 前回の qcow2 の回（[install-report-ja.md](install-report-ja.md)、`9268beb4-kaz.2`）との違いは、`--writer-flush-scope=range` を足したことだけ（ユーザーが確認）。ほかは同じ条件。
  - `--debug`、`--backup-meta 0`、writeback、slice-flush-wait 30s／idle 16s、writer-reuse-window 32、writer-flush-timeout 0s。
  - 18e641b8 には mtime の修正（commit の直列化）も入っており、これは scope に関係なく効く。
- QEMU: cache=unsafe。qcow2 は新規作成（preallocation=off）`ubuntu-install-test-rangeflush.qcow2`、inode 614589。
- 時間帯: 17:34〜18:27（約53分）。前回は 12:52〜14:16（約84分）。どちらも `--debug` 付き。
- 入力（読み取りのみ、固定 prefix）:
  - debug ログ `~/.juicefs/diagnostics/vm-io-20261007-172643.log`: 388,871,282 bytes、SHA-256 `fb299d1d5e0f0113dd8adacfaa72584ec5a3a921acdef6e652d1214f018cc8b2`
  - accesslog `~/.juicefs/diagnostics/accesslog-20261007-173235.txt`: 46,548,451 bytes、SHA-256 `e8eeb0b1368dbce6d96658fb5699e8c3b457dfb601b8ec4d7088c68a5932c6eb`
  - metrics `metrics-before-20261007-173235.txt` → `metrics-after-20261007-182833.txt`
- 集計: [analyze_install.py](analyze_install.py)（window 17:34:00〜18:28:00）→ [result-install-range-20261007.json](result-install-range-20261007.json)

## 結果（前回の qcow2 の回との比較）

| 項目 | 前回（file） | 今回（range） | 変化 |
|---|---:|---:|---:|
| インストール時間 | 約84分 | **約53分** | −37% |
| fallocate（accesslog） | 15,343回、3,330s、max 10.8s | 12,876回、**1,887s**、max 3.96s | −43% |
| Fallocate 前の flush | 2,661s（平均173ms） | prelock 1,264s（平均98ms）＋ロック内 0.3s | −53% |
| read（accesslog） | 35,062回、739s、max 10.1s | 38,138回、659s、max 8.99s | −11% |
| Read 前の flush | 360s | prelock 352s ＋ロック内 10.9s | ほぼ同じ |
| write（accesslog） | 206,671回、67.4s、max 4.1s | 210,258回、**13.4s**、max 1.55s | −80% |
| slice commit | 62,317件 | **35,006件** | −44% |
| slice の平均長 | 244 KB | 481 KB | ×2.0 |
| 明示的な freeze | 61,346 | 27,032 | −56% |
| 通常の PUT／compaction の PUT | 15,561／9,846 | 11,187／4,404 | −28%／−55% |
| compaction | 587回 | 257回 | −56% |
| accesslog のエラー | 0 | 0 | — |

## 解釈

- **Fallocate**: 範囲外の pending を待たなくなったので、Fallocate 前の待ちは半分以下になった。強制 freeze が減ったため slice が大きくなり、commit・PUT・compaction もまとめて減った。
- **Fallocate に残る待ち**（1,264s、平均98ms）: qcow2 は cluster をファイルの末尾に順に割り当てる。そのため、Fallocate の範囲は直前の書き込みと同じ chunk に入ることが多く、その chunk の未 commit 分（1件 約41ms）を待つ。範囲を限定しても、この待ちは消えない。
  - 「Fallocate による freeze の slice 数 ÷ 呼び出し数」は約1.9 だった。
  - Meta.Fallocate 自体は、fallocate 全体から flush を引いた 約620s（平均約48ms ≒ 4 RTT）。
- **Read**: ほとんど変わらなかった。インストール中の Read は、直前に書いた chunk を読むことが多いため、範囲を限定しても待つ commit が減らなかったと推定する（未検証）。
- **Write**: 1秒を超える write が減り、合計時間は1/5になった。2段階 preflush で同じ handle の Write が止まらなくなった効果と、flush が短くなった効果が重なっている（どちらの寄与かは分離していない）。
- **commit の lock 待ち**: meta 層で計測した lock 待ちは 4,542s → 5s に減った。ただし、今回入れた `commitMu` が、client 側で commit を meta に入る前に直列化しているので、待ちがそこへ移っただけで計測に出ていない分がある。「lock 待ちが消えた」とは評価しない。commit 本体（doWrite）は1件 約41ms で、前回と変わらない。

## 付随の観測

- **rawstaging**: 終了時点で 2,182 block（1.80GB）。前回の終了時点は qcow2 が 422 block（0.42GB）、raw が 567 block（0.89GB）。同じ量を短い時間で書いたので、Drive へのアップロードが追いついていない。
  - 減り方: 18:29:58 に 1,585件 → 18:30:38 に 1,357件 → 18:32:41 に 781件。順調に減っており、滞留はしていない。
- **PUT のエラー**: `slow request ... exceeded maximum number of attempts, 1`（rclone の30秒タイムアウト）が3件あった。再送で成功している（例: `6238194_0_4096` は 17:46:15 に成功）。
- **compaction GC の local_error**: 152件（前回 487件）。既存の別件の課題。

## 次の候補

- 残る大きな待ちは、Fallocate 前の in-range の commit 待ち（1,264s）、Meta.Fallocate（約620s）、Read 前の待ち（352s）。いずれも「commit 1件 約41ms が直列」に起因する。
  - そのため、次は Phase 2（group commit。同じ chunk の連続する slice を1つの transaction で commit する）が効く見込み。
  - Meta.Fallocate を commit と同じ transaction にまとめられるかも検討する。
- raw でも、range 版の計測を行う。commitMu の影響を確認するため、range を付けない回も行うかどうかはユーザーが判断する。
