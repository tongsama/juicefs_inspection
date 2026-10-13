# Ubuntu Desktop 26.04 インストール計測の解析（2026-10-07）

## 条件

- JuiceFS: `1.4.1+2026-10-06.9268beb4-kaz.2`、`--debug`、`--backup-meta 0`、client は1つ。Redis の Ping RTT は 9.9〜10.5ms（起動ログ）。
- QEMU: disk cache = **unsafe**。qcow2 は新規（virtual 200GiB、cluster 64KiB、compat 1.1、lazy refcounts off）。inode 613560。ISO はローカル FS に置いた（JuiceFS を経由しない）。
- 時間帯: 2026-10-07 12:52〜14:16（約85分。`--debug` 付きなので所要時間は比較の基準にしない）。
- 入力（読み取りのみ、固定 prefix）:
  - debug ログ `~/.juicefs/diagnostics/vm-io-20261007-121301.log`: 530,139,153 bytes、SHA-256 `63ff1834c163a2761acfcccafe8598097ad51edcae08ca4bee53a91cb33d5496`
  - accesslog `~/.juicefs/diagnostics/accesslog-20261007-125056.txt`: 53,528,218 bytes、SHA-256 `98cca52f0039a607905a14377a3480e297bb10027f346a7f64ffe817af9d17e0`
  - metrics `metrics-before-20261007-125046.txt` → `metrics-after-20261007-141700.txt`
- 集計: [analyze_install.py](analyze_install.py) → [result-install-20261007.json](result-install-20261007.json)

## 結論

**unsafe でのインストールでは、qcow2 への `fallocate(FALLOC_FL_ZERO_RANGE)` が最大の待ちだった。** 1回あたり平均217ms で、待ちの大部分は「同 inode の全 pending を commit し終えるまでの flush 待ち」だった。

| qcow2 への FUSE 操作 | 件数 | 合計時間 | 平均 | p95 | max |
|---|---:|---:|---:|---:|---:|
| fallocate | 15,343 | **3,330s** | 217ms | 340ms | 10.8s |
| read | 35,062 | 739s | 21ms | 99ms | 10.1s |
| getattr | 6,518 | 78s | 12ms | 14ms | 1.7s |
| write | 206,671 | 67s | 0.3ms | 0.1ms | 4.1s |

- fallocate の mode は全件 `0x10`（ZERO_RANGE）。サイズは 64KiB が10,402件、ほかも 64KiB の倍数で、offset もすべて 64KiB 境界。**QEMU が qcow2 に新しい cluster を割り当てるたびに、その cluster をゼロ化している呼び出し**と判断できる [推論：サイズ・境界・mode から]。
- 1分ごとの fallocate の合計時間は、活動中の区間で 50〜59秒/分だった。インストール中のほとんどの時間、fallocate がほぼ途切れなく実行されていた。
- unsafe なので fsync は0件（guest の flush は QEMU が捨てる）。

### fallocate 217ms の内訳 [事実：ソースとログ]

`VFS.Fallocate`（`pkg/vfs/vfs.go:877-912`）は、次の順で処理する。

1. handle の Wlock を取る。
2. **`writer.Flush(ino)`**: 範囲に関係なく、同 inode の全 pending slice を freeze し、commit 完了まで待つ。debug ログの `writer flush origin=vfs.Fallocate`: 15,343回、合計 **2,661s**、平均 173ms、p95 295ms、max 10.7s。
3. `Meta.Fallocate`: Redis の txn（WATCH／GET／MULTI… RPUSH ゼロ slice、SET inode…EXEC／UNWATCH）。metrics: 15,343回、合計 664s、**平均 43ms ≒ 4 RTT**。

つまり 217ms ≒ flush 待ち 173ms + 自身の txn 43ms。flush 待ちの中身は、同 inode の pending commit（1件 約43ms、直列）。

### commit の状況

- qcow2 の metadata Write（slice commit）: **62,317件**、183 chunk。
  - doWrite: 平均 42.9ms、p50 41.6ms、p95 51.3ms（≒ 4 RTT）。合計 2,676s で、**計測時間の52%は、この inode の commit が Redis と通信している時間**だった。
  - lock_wait（open-file lock を待った時間）: 合計 4,542s、p95 353ms、p99 713ms。**別 chunk の commit が inode lock の前で並んでいた**。
- freeze の理由: explicit_flush 61,346 件のうち、**origin が `vfs.Fallocate` のものが 55,536 件（89%）**、`vfs.Read` が 5,810 件。timer（idle）による freeze は 66 件。
  - fallocate の flush のたびに slice が強制的に閉じられ、slice が小さいまま commit される（raw 長の中央値 40KiB）。**fallocate が commit 数そのものを増やしている**。
- commit 時の chunk の slice 数: 平均 109、p95 270、max 490。2,500 に達した同期 compaction は0件。
- compaction: 588回、合計 35.96GB を再書き込み（FUSE の書き込みは 15.23GB）。PUT 25,459件、DELETE 66,718件。

### Read

- qcow2 への read: 35,062件、合計 739s。このうち Read 前の writer flush（`origin=vfs.Read`）が 35,023回、合計 360s、p99 168ms、max 8.5s。
- Meta.Read（cache miss）は 4,589回、合計 59s。

### その他

- `statfs` が全体で 23,811回、合計 519s（1回 約22ms ≒ 2 RTT）。呼び出し元は uid 0 の pid 565／567／569（各約4,900回、約1秒周期）。このセッションからはプロセスが見えず、正体は不明（WSL 側の監視の可能性）。qcow2 の I/O と並行しており、直接の待ちではないが、Redis への負荷にはなる。
- QEMU の setlk（ファイルロック）は21回、1回 約44ms。影響は小さい。
- compaction GC の `local_error` が487件（metrics の差分）。今回の主題ではないが、別途確認する価値がある。
- `slow metadata write`（1秒以上）は128件。I/O エラーは0件（accesslog の errno、`metadata_write` の errno）。

## 改善への示唆

1. **最優先: Fallocate の flush を範囲に限定する**（計画書 C4 を Fallocate に拡張）。
   - ZERO_RANGE の前に commit が必要なのは、ゼロ化する範囲と重なる pending だけ（ゼロの記録が、それより前の書き込みに上書きされないようにするため）。範囲外の pending は後から commit しても、結果は変わらない [推論、要テスト]。
   - QEMU は新しい cluster をゼロ化してからデータを書くので、ゼロ化範囲と重なる pending はほとんど無いはず [推論]。
   - 期待値: fallocate 1回が約217ms から約43ms（自身の txn だけ）になる。15,343回で約2,650秒（約44分）分の FUSE 時間が減る。さらに、強制 freeze の89%が無くなり、slice が大きくなって commit 数と compaction も減る。
2. **group commit（計画書 C3）**: commit の Redis 時間が計測時間の52%を占め、lock 待ちも大きい。1 が入った後も残る主要な待ち。
3. **Read 前の flush の範囲限定（C4）**: 360s 分。
4. Meta.Fallocate 自身の 4 RTT は、commit の batch に合流させる（C3 の拡張）か、Lua で減らせる余地がある。
5. QEMU 側だけで試せる回避策（コード変更なし）: qcow2 を `preallocation=metadata` で作る（cluster の割り当て自体を事前に済ませ、ZERO_RANGE を出させない）、または cluster_size を大きくする（2MiB なら割り当ての回数は約1/32）。どちらも、本当に ZERO_RANGE が減るかは実測が必要 [未確認]。

## 留保

- `--debug` 付きで、ログ出力のぶん遅くなっている。所要時間の比較には使わない。
- accesslog の内部バッファ（10,240行）があふれた場合、行が欠ける。今回の read／write／fallocate の件数は metrics（`fuse_read_size_bytes_count` 35,062、`fuse_written_size_bytes_count` 206,671、Fallocate 15,343）と一致したので、欠落は無かったと判断した。
- writeback（QEMU の cache）では、これに fsync の待ちが加わる。今回は測っていない。

---

# 追加計測: raw イメージ（同日 14:49〜15:25）

## 条件

- qcow2 の回と同じ JuiceFS（`--debug`、`--backup-meta 0`）、QEMU の cache は unsafe、ISO はローカル FS に置いた。discard は未設定（QEMU 既定の ignore）。
- ディスクは `qemu-img create -f raw … 200G` で作ったスパースな raw（inode 613567）。
- 入力（読み取りのみ、固定 prefix）:
  - debug ログ（同じファイル）: 799,409,244 bytes、SHA-256 `79496c404ab03d08d53c1917becb65e0395830fb2f3f57a88a61963ef4f24a97`
  - accesslog `~/.juicefs/diagnostics/accesslog-20261007-144812.txt`: 41,568,387 bytes、SHA-256 `f888f947fbbba86b476041f9f45b20bc1884304987bffd012de5a0d82b53b915`
  - metrics: before は `metrics-before-20261007-144804.txt`。after はユーザーの保存が無かったため、agent が 15:25:58 に取得した [metrics-after-agent-20261007-152558.txt](metrics-after-agent-20261007-152558.txt) を使った（終了から1分未満のずれ）。その後ユーザーが 15:28:46 に `metrics-after-20261007-152846.txt` を保存した。両者で Meta.Write 件数（79,638）と FUSE 書き込み件数（433,315）は一致し、read だけ114件多い（終了後の読み取り）。終了時刻に近い agent 取得分を採用した。
- 集計: [result-install-raw-20261007.json](result-install-raw-20261007.json)

## qcow2 との比較

| 項目 | qcow2（prealloc=off） | raw |
|---|---:|---:|
| 所要時間（debug 付き） | 約84分 | **約36分** |
| fallocate | 15,343回、3,330s | **0回** |
| read | 35,062回、739s | 32,232回、**1,035s**（max 24.8s） |
| └ Read 前の writer flush | 35,023回、360s | 32,205回、**799s**（p99 219ms、max 24.8s） |
| write | 206,671回、67s | 226,626回、171s（1秒超が31回、計121s、max 23.6s） |
| metadata commit（Meta.Write） | 62,317件 | **17,312件** |
| └ doWrite 平均 | 42.9ms | 41.5ms |
| └ open-file lock 待ち 合計／平均／p95 | 4,542s／73ms／353ms | 2,768s／160ms／630ms |
| └ doWrite 合計 ÷ 計測時間 | 52% | 32% |
| slice の raw 長（中央値／平均） | 40KiB／238KiB | 16KiB／1.02MiB |
| freeze の主因 | Fallocate 55,536／Read 5,810 | Read 12,744／window・idle 等 4,568 |
| commit 時の slice 数（平均／max） | 109／490 | 59／321 |
| compaction 回数／再書き込み | 588回／35.96GB | 250回／13.07GB |
| FUSE 書き込み量 | 15.23GB | 18.55GB |
| PUT 回数 | 25,459 | 11,249 |

## 読み取れること

- raw では fallocate が出ない（guest の write-zeroes も来なかった）。qcow2 の cluster 割り当てに伴う ZERO_RANGE が、qcow2 の回の最大の差だったことが裏付けられた [事実：件数] [推論：時間差の主因]。
- **raw で残る最大の待ちは、Read 前の writer flush（799s、計測時間の36%相当）**。Read のたびに、同 inode の全 pending を commit し終えるまで待つ。commit は1件約41ms で直列なので、pending が多いと1回の Read が秒単位で止まる（max 24.8s）。
- Read の flush 中は同 inode への新しい Write も止まる（`flushwaiting`）。write の 1秒超31回（計121s、max 23.6s）は、これに当たったものと推論する。
- 15:07 台の1分間に read の待ちが合計 235s あった（並行する read の待ちの合計）。この区間で guest がほぼ止まっていた可能性がある。
- commit の lock 待ち（平均160ms）は qcow2 より長い。slice が大きく chunk が多い（303）ので、並行する chunk の commit が inode lock の前に並ぶ。group commit の効果が出やすい形 [推論]。

## 施策の優先順位（raw の結果を含めて）

1. **Read 前の flush の範囲限定（C4）**: raw では最大の待ち。qcow2 にも効く（360s）。
2. **Fallocate 前の flush の範囲限定**: qcow2（prealloc=off）では最大の待ち。raw では出ない。
3. **group commit（C3）**: 両方で、commit の直列化と lock 待ちが残る。1・2 で flush 待ちが減ると、次の律速になる。
4. 運用上の回避策: raw（または qcow2 の preallocation=metadata）で ZERO_RANGE を避けられる。今回、所要時間が半分以下になった。
