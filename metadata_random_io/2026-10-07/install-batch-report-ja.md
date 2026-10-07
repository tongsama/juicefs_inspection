# `--meta-write-batch` 版での Ubuntu インストール計測（2026-10-07、qcow2）

## 条件

- JuiceFS: `1.4.1+2026-10-07.f9308391-metabatch`（本体 `feat/meta-write-batch` f9308391。exe の SHA-256 `236f2cdf…a7161e066`）。
- range 版の回（[install-range-report-ja.md](install-range-report-ja.md)）との違いは、`--meta-write-batch=64` を足したことだけ。
  - `.config` は WriterFlushScope=range、MetaWriteBatch=64、reuse 32、slice-flush-wait 30s／idle 16s、writer-flush-timeout 0、writeback。
  - `--debug`、`--backup-meta 0`。
- QEMU: cache=unsafe。qcow2 は新規作成（preallocation=off）`ubuntu-install-test.qcow2`、inode 614590。
- 時間帯: 21:19〜22:02（約43分）。比較対象は、range 版 17:34〜18:27（約53分）と、変更前 12:52〜14:16（約84分）。いずれも `--debug` 付き。
- 入力（読み取りのみ、固定 prefix）:
  - debug ログ `~/.juicefs/diagnostics/vm-io-20261007-211557.log`: 349,139,233 bytes、SHA-256 `424b55135cd3a17f67b3e3080b24adea13d6f73b20d1871cdb0143810d1db17f`
  - accesslog `~/.juicefs/diagnostics/accesslog-20261007-211753.txt`: 43,899,343 bytes、SHA-256 `dd06b4ebdd7663b37b20a98727ce5fd3b813db1014c8f8beca3fe95bd12b2ad5`
  - metrics `metrics-before-20261007-211753.txt` → `metrics-after-20261007-220635.txt`
- 集計（window 21:19:00〜22:03:00）:
  - [analyze_install.py](analyze_install.py) → [result-install-batch-20261007.json](result-install-batch-20261007.json)
  - [analyze_batch.py](analyze_batch.py)（batch の commit の集計。新規）→ [result-install-batch-commits-20261007.json](result-install-batch-commits-20261007.json)

## 結果（3回の比較）

| 項目 | 変更前（file） | range | **range＋batch64** |
|---|---:|---:|---:|
| インストール時間 | 約84分 | 約53分 | **約43分** |
| metadata transaction（slice の commit） | 62,317 | 35,006 | **20,654**（単発 14,235 ＋ batch 6,419） |
| commit した slice | 62,317 | 35,006 | 35,057 |
| fallocate の合計（accesslog） | 3,330s（max 10.8s） | 1,887s（max 3.96s） | **1,495s**（max 1.57s） |
| Fallocate 前の待ち（prelock） | 2,661s（ロックの中） | 1,264s（平均98ms） | **905s**（平均68ms、max 0.95s） |
| read の合計（accesslog） | 739s（max 10.1s） | 659s（max 8.99s） | **332s**（max 0.95s） |
| Read 前の待ち（prelock） | 360s（ロックの中） | 352s | **66s**（max 0.39s） |
| write の合計（accesslog） | 67.4s（max 4.1s） | 13.4s（max 1.55s） | 12.7s（max 0.88s） |
| 通常の PUT／compaction の PUT | 15,561／9,846 | 11,187／4,404 | 8,114／4,957 |
| compaction | 587回 | 257回 | 305回 |
| 終了時の staging | 422 block | 2,182 block | 1,419 block |
| accesslog のエラー | 0 | 0 | 0 |

**batch の内訳**:
- batch の commit 6,419回で、20,822 slice を commit した（平均3.2件）。
- 大きさの分布は、2件が 4,205回、3件が 1,236回、4件が 415回、16〜31件が 108回、32〜63件が 36回、64件が1回。
- batch 1回の doWrite は平均 41.45ms で、単発の commit（約41ms）と同じ。
- batch のエラーは0件。

## 解釈

- **transaction 数が −41%**（35,006 → 20,654）。batch の doWrite は1件の commit と同じ約41ms なので、まとめた分の commit 時間がそのまま減った。設計時の見込み（−40〜60%）の範囲に入っている。
- **Read 前の待ちが −81%**（352s → 66s）。最大も 8.9s → 0.39s になった。range だけでは Read の待ちは減らなかった。原因は、直前に書いた chunk の commit を待っていたことで、batch がその commit をまとめて速くした。
- **Fallocate 前の待ちは −28%**（1,264s → 905s）。平均 68ms は、commit 約1.6件分にあたる。
- **残る大きな待ち**:
  - Fallocate 前の commit 待ち（905s）
  - Meta.Fallocate 自体（fallocate 全体 1,495s − 待ち 905s ≈ 590s、平均約44ms ≒ 4 RTT）
  - 単発の commit 14,235件（合計593s）
  - どれも「Redis の transaction 1回 ≒ 41ms が inode 単位で直列」に起因する。
- 通常の PUT は −27%（11,187 → 8,114）。一方、compaction の PUT は少し増えた（4,404 → 4,957）。slice の平均の大きさはほぼ同じ（481KB → 470KB）なので、PUT の減少を batch の効果と言える根拠は弱い。負荷のばらつきの範囲として扱う。
- `--debug` 付きの計測なので、所要時間は3回の比較にだけ使う（絶対値の基準にはしない）。

## 次の候補

- **Fallocate を、範囲内の pending の commit と同じ transaction にまとめる**: 待ち（905s）と Meta.Fallocate（約590s）の一部を重ねられる可能性がある。ただし、meta の API と3 engine に関わる、大きめの変更になる。
- **chunk をまたぐまとめ（案A）**: 単発の commit が 14,235件残っている。そのうち、他の chunk の commit とまとめられる割合を、ログから先に見積もる。
- **Redis の transaction の往復の削減（計画書 C7）**: WATCH・GET・MULTI/EXEC を pipeline や Lua にまとめて、1回の commit を 4 RTT から 1〜2 RTT にする。commit とFallocate の両方に効く。
- 終了時の staging（1,419 block）は、アップロードの追いつき待ち。range 版と同じく、減っていることを確認すればよい。
