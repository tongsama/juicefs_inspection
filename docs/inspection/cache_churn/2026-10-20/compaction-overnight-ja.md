# 夜間（2026-10-19 21:38〜10-20 06:05）の compaction 書き直し量の調査

## 1. 目的

ゲストの VM はほぼアイドルで、staging への書き込みは約 5.0 GiB だった。それなのに、同じ時間帯に compaction が約 32.7 GiB を書き出した理由を調べる。具体的には次の 4 点を明らかにする。

- どの chunk か
- ゲストのどのような書き込みが原因か
- どのトリガーが同じ chunk を何度も書き直させたか
- 減らす手段と、その効果の見積もり

この文書では、以下の 4 種類を見出しや角括弧で分けて書く。

- **[観測]** ログや metrics で直接確かめた事実
- **[source]** ソースコードから読める動作の条件
- **[仮説]** 観測から推測したこと
- **[未検証]** まだ確かめていないこと

調査は読み取りだけで行った。remount、Redis・cache・staging への書き込み、元ログの変更はしていない。

## 2. データと固定 prefix／SHA

| データ | 範囲 | 備考 |
|---|---|---|
| 診断ログ `~/.juicefs/diagnostics/vm-io-20261019-213537.log` | 先頭 **3,098,639 バイト**（07:37:15 の行まで、末尾は改行）| SHA256 `240c56cb0e1349b727890d62c9ca8cc5683223a710d72565a6801e4bdb75294a`、15,121 行。`--debug` なし |
| metrics の時系列 `../2026-10-19/metrics.tsv` | 1 分ごとのサンプル | sampler は `kaz_compaction_*` を記録していない |
| `/metrics` の現在値 | 07:38 ごろ（uptime 36,148 s）と 07:49 ごろ（`kaz-compaction-metrics-snapshot.txt`）| mount（21:35）からの累積値。窓（21:38〜06:05）の外の 21:35〜21:38 と 06:05〜07:38 も含む |
| Redis の chunk の record 列 | 07:41〜07:47 に LLEN と LRANGE だけを実行 | `chunk_records_watch.py`。接続 URL は表示しない |
| ソース | `juicefs/` の 57b3de8a（1.4.1+2026-10-19.57b3de8a）| |

解析窓は 21:38:00〜06:05:00 JST（unix 1792413480〜1792443900）。uptime の差（30,431 s）が窓の長さ（30,420 s）とほぼ同じなので、窓の中で再起動はなかった。

起動時のログ [観測]:

- `kaz compaction policy: read-min=100`
- `kaz compaction mode: block (block size 4194304, hidden-ratio 0.50, max-slices 17)`

mount スクリプトの関連オプション: `--slice-flush-wait 30s --slice-flush-idle 16s --writer-reuse-window 32`、upload-delay は 0s。

QEMU のディスク設定 [観測、`ps` で確認]: raw の file を `cache.direct=false, no-flush=true`（libvirt の cache=unsafe 相当）、`discard=unmap`、virtio-blk の write-cache を on で使っている。

### スクリプトと出力（このディレクトリ）

- `compaction_overnight.py`: ログの prefix を固定して SHA を出し、slow compaction の行と metrics の差分を集計する。
  - 出力: `summary.md`、`compactions.csv`（compaction 1 回ごとの行）
- `chunk_records_watch.py`: Redis を読み取りだけで観察する。
  - 全 chunk の LLEN
  - 選んだ chunk について、20 秒ごとに新しく積まれた record
  - `offsets_report()`: 同じ位置が何回書き直されたか
  - 出力: `chunk_records_watch.out`、`chunk_offsets.out`、`chunk2810_records.out`
- `steady_state.out`: 01:00〜06:00 の chunk 別の集計

## 3. 観測

### 3.1 全体量（窓内）[観測]

| 項目 | 値 |
|---|---|
| compaction の出力（`compact_size_histogram_bytes_sum` の差分、圧縮前の論理バイト）| **32.66 GiB**（8,346 slice）|
| ログの slow compaction 行の bytes の合計 | **32.66 GiB**（1,632 行）|
| staging への書き込み | **5.01 GiB** |
| PUT のバイト数（zstd 圧縮後、staging と compaction の合計）| **7.69 GiB**（138,163 要求）|
| DELETE の要求数 | 193,226 |
| block cache への書き込み | 37.46 GiB（179,975 回）|
| kaz-2q の compaction_output_cached／skipped | 7,689 回／636 回 |

- metrics の合計とログの合計が一致した。slow compaction の行は所要 1 秒以上のときだけ出るが、この夜は**ほぼ全部の compaction が記録された**とみなせる。
- 07:38 時点の累積値（mount から）では、block compaction の結果は partial 1,833、full 10、noop 56、error 11 で、合計 1,910 回。これは scheduler から開始した回数（scheduled_slices_count 1,910）と一致する。upstream 方式の compaction はログに 1 回だけあった（23:51、chunk 70、once=true、slices=1000）。
- 32.66 GiB は**圧縮前の論理量**で、実際にアップロードしたバイト数ではない。PUT は staging 分と compaction 分の合計でも 7.69 GiB だった。2 つの内訳はこのデータでは分けられない（[未検証]）。

### 3.2 時間帯別 [観測]

| 時間帯 | compaction GiB | 回数 | staging GiB | cache 書込 GiB | 中央 slices | 中央 MiB／回 |
|---|---|---|---|---|---|---|
| 21:38–22 | 2.56 | 97 | 0.68 | 3.32 | 116 | 24 |
| 22時 | 4.50 | 195 | 0.49 | 4.97 | 112 | 16 |
| 23時 | 4.10 | 184 | 1.77 | 6.33 | 110 | 12 |
| 0時 | 6.03 | 224 | 0.44 | 5.93 | 116 | 24 |
| 1時 | 3.39 | 193 | 0.38 | 3.76 | 102 | 12 |
| 2時 | 3.10 | 178 | 0.29 | 3.30 | 102 | 12 |
| 3時 | 2.99 | 184 | 0.30 | 3.21 | 102 | 8 |
| 4時 | 2.84 | 178 | 0.31 | 3.09 | 102 | 8 |
| 5時 | 2.89 | 183 | 0.31 | 3.17 | 102 | 8 |

1 時〜6 時の定常状態では、毎時、staging が約 0.3 GiB、compaction が約 3 GiB、compaction が約 180 回だった。staging の**約 10 倍**を書き出し続けていた。FUSE の操作は毎時約 20 万回（約 57 回／秒）で、夜の間も途切れていない。

compaction はすべて inode 634040 に対するもので、合計の所要時間は 4.13 時間（窓は 8.45 時間）だった。

### 3.3 chunk 別の上位と、同じ chunk の書き直し回数 [観測]

窓の中で compaction された chunk は 251 個あった。

- 1 回だけの chunk: 149 個
- 2 回以上の chunk: 102 個
  - その出力は 26.99 GiB（全体の 83%）
  - うち 2 回目以降の分が 23.83 GiB
- 同じ chunk を再び compaction するまでの間隔: 中央値 1.7 分、四分位 0.9 分／15.4 分

| chunk（ディスク内オフセット）| 回数 | GiB | 1 回あたり | 備考 |
|---|---|---|---|---|
| **1920**（120.0 GiB）| **279** | **11.73** | 約 43 MiB（約 11 ブロック）| 21:44–06:04 にずっと |
| **2810**（175.6 GiB）| **399** | **3.59** | 約 9 MiB（約 2 ブロック）| 23:26–06:04 |
| 1985（124.1 GiB）| 69 | 1.07 | 約 16 MiB | |
| 2807 | 105 | 0.82 | | 21:44–23:55 |
| 0（先頭）| 69 | 0.57 | 約 8 MiB | |
| 2615, 2566, 2512, 1925, 906, 1922 … | 9〜35 | 0.3〜0.5 | | slices はほぼ毎回 99〜101 |

01:00〜06:00 の定常状態（883 回、14.26 GiB、`steady_state.out`）:

| chunk | 回数／時 | 出力 | 割合 | 1 回あたり |
|---|---|---|---|---|
| 1920 | 33.4 | 7.10 GiB | **50%** | 43.5 MiB |
| 2810 | 70.0 | 2.97 GiB | **21%** | 8.7 MiB |
| 残り 71 chunk | | | 29% | |

### 3.4 slices の数（compaction 開始時の record 数）[観測]

| slices | 件数 | GiB |
|---|---|---|
| 0–98 | 30 | 0.39 |
| **ちょうど 99** | **383** | 5.41 |
| **100–149** | **1,070** | **21.24** |
| 150–349 | 109 | 3.98 |
| 350 以上 | 40 | 1.61 |

1 回あたりの出力ブロック数の分布は 2 つの山を持つ。2 ブロックが 548 件（主に chunk 2810 など）、10〜12 ブロックが 315 件（主に chunk 1920）。full（chunk 全体の書き直し）は 10 回だけで、ほぼすべてが partial だった。

scheduler に登録された時点の slice 数の分布（mount からの累積）:

| 範囲 | 回数 |
|---|---|
| 100 以下 | 649 |
| 101〜200 | 1,161 |
| 201〜350 | 51 |
| 351 以上 | 56 |

開始までの待ち時間は、1,072 回（56%）が 10 ms 未満、平均は 94 秒だった。

### 3.5 現在の書き込みの形（07:41〜07:47、Redis の読み取りだけ）[観測]

- **chunk 1920**: 4 分間に新しい record が 230 個（57.5 個／分）。
  - ほとんどが 4〜8 KiB で、20〜30 秒ごとに十数〜三十数個ずつ届く。
  - 触れたブロックは 12 個（ブロック 0 に 70 個、1 に 36 個、3・4 に各 20 個、10 に 26 個、11 に 23 個、12 に 12 個ほか）。
  - record が約 110 個になると compaction され、13〜18 個に戻る。このとき 4 MiB の record が 11 個積まれる。約 2 分の周期で、これがそのまま夜間の 43 MiB／回の再現になっている。
  - 113 個の小さい record のうち、別々の位置は 65 個。37 の位置は 2 回以上書かれていた。
- **chunk 0**: 小さい record 89 個が、わずか 11 の位置に集中していた（1 つの位置を最大 13 回）。
  - 位置は 40.0〜40.3 MiB と 2.1 MiB。ゲストの ext4 の配置（下記）では、orphan file（block 9293〜9804）と group descriptor（block 1〜38）に当たる。
- **chunk 2810**: 観察した時間には書き込みがなかった（record は 29 個で変化なし）。
  - 最後に積まれた小さい record は、chunk 内 9.8〜16.2 MiB（ゲストの fs block 46041041〜46042675、group 1405）。14.0→15.8 MiB とほぼ順番に伸びる書き込みもあった。
- LLEN（07:41）: record がある chunk は 4,761 個、そのうち 17 個以上が 483 個、99 個以上は 6 個だった。

### 3.6 ゲストの ext4 の配置との対応 [観測と計算]

raw に変換したディスクでは、ホストのオフセットとゲストのディスクのオフセットが等しい。この前提（[未検証]）で、2026-10-13 に取ったゲストの情報（`../../vm_write_hotspot/2026-10-13/guest-ext4-info.txt`）を当てはめた。その情報は次のとおり。

- vda2 が 2 MiB から始まる
- block は 4 KiB、1 group は 32,768 block、flex_bg は 16
- group 960 の inode table は 31457312〜31465503
- orphan file は 9293〜9804

この対応で読むと次のようになる。

- **chunk 1920** の fs block は 31,456,768〜31,473,151。flex group 960 の block bitmap、inode bitmap、**16 group 分の inode table（32 MiB、chunk 内のブロック 0〜8）**がここにある。ブロック 10〜15 は group 960 のデータ領域。
  - 2026-10-13（qcow2 のとき）にも「chunk 1920 は主にグループ 960 の inode テーブル」と記録されていた。ただし当時は qcow2 の内部での位置で、偶然の一致の可能性もある。今回の raw での位置の計算のほうが直接的である。
- **chunk 0** は superblock、GDT、flex group 0 の metadata、orphan file を含む。
- **chunk 2810** は group 1404〜1405 の一般のデータ領域。どのファイルかは分からない（[未検証]）。

## 4. ソース上の条件（どのトリガーが何回）

### 4.1 commit 時のトリガー（upstream のまま）[source]

`pkg/meta/base.go` の `compactionWanted`（2189 行）と `afterWrite`（2263 行）が担当する。

- slice を commit した結果、record 数が **99・199・299 …（100 で割って 99 余る数）を通過したとき**、または **350 を超えたとき（以後は commit のたびに毎回）**に `requestBackgroundCompaction` を呼ぶ。
- この経路は kaz policy（read-min）を**通らない**。
- record 数が 2,500 以上になると、同期の upstream compaction になる（once=true）。

### 4.2 read 時のトリガー [source]

`base.go:2144` と `compaction_policy_kaz.go:requestFromRead` が担当する。

- Read で chunk の record 列を Redis から読み直したとき（open-file の chunk cache に無いとき）に判定する。record 数が 5 以上なら要求を出す。
- kaz policy は read-min（今回は 100）未満の要求を捨てる。
- commit や compaction のたびに chunk cache は無効になる（`InvalidateChunk`）。そのため、ゲストが書きながら読む chunk では、次の Read が読み直しになる。

### 4.3 scheduler（priority）[source]

`compaction_scheduler.go` が担当する。

- 要求は chunk ごとに 1 つの hint にまとまる。count には、まとめた要求の最大値を残す。
- 同じ inode の chunk は**同時に 1 つしか実行しない**。
- 実行中に新しい要求が来たら、終わった後にその chunk を並べ直す。

### 4.4 block compaction の計画 [source]

`compaction_block_plan_kaz.go:planKazBlocks` が担当する。

- 4 MiB のブロックに揃っていない小さい record が触れたブロックを、**すべて** 4 MiB ずつ書き直す（partial）。
- 書き直さずに残す record の数と、書き直すブロックの数の合計が max-slices（17）を超えたときや、hidden（隠れたデータ）の割合が 0.5 を超えたときは、chunk 全体を書き直す（full）。
- 出力量は「**前回の compaction 以降に小さい書き込みが触れた、別々のブロックの数 × 4 MiB**」で決まり、新しく書かれたデータの量にはよらない。

### 4.5 どのトリガーが何回か [観測と source からの推論]

| 根拠 | 内容 |
|---|---|
| policy の判定（07:38 時点の累積）| pass 8,080、read_below_min 42,181。read の要求は 100 以上のものだけが scheduler に届いた |
| scheduler からの開始 | 1,910 回 |
| ログ | slices がちょうど 99 の開始が 383 回。read の経路は 100 未満を通さないので、少なくともこの 383 回は commit の 99 によるトリガー（[source] から確実）|
| 登録時の count | 101〜200 が 1,161 回。commit の経路は 100〜198 の値では要求しないので、これらの hint のほとんどには read の要求（100 以上）がまとめられたか、待っている間に count が上がった |

ここから言えること:

- **commit の 99 と read-min 100 はほぼ同じ境目なので、どちらか早いほうが毎回この chunk を起動していた。**
- 1 回の compaction を、どちらの経路が最初に起動したかの内訳は、metrics からは出せない（[未検証]、`--debug` の phase の行が必要）。
- いずれにしても実際に効いている閾値は「約 100 record」である。**read-min を上げても commit の 99 が残るので、夜間の量はほとんど変わらない**（[source]）。

## 5. 仮説

### H1（主因）[仮説、強い根拠あり]

ゲストの ext4 は、flex group 960 の inode table（chunk 1920）、GDT と orphan file（chunk 0）、もう 1 つのデータ領域（chunk 2810）を、30 秒前後の周期で 4〜8 KiB ずつ、毎回ばらばらの位置に書いている。JuiceFS では、連続していない書き込みは 1 つずつ別の slice（record）になる。

そのため次の循環になる。

1. 約 85 個の新しい record が積まれる（chunk 1920 では約 1.5〜2 分）
2. record 数が 99〜100 に届き、compaction が起動する
3. その間に触れられたブロックを全部書き直す（chunk 1920 では約 11 ブロック、44 MiB）

新しく書かれたデータは 1 回あたり 0.5 MiB 前後だが、書き直しは 44 MiB で、**約 50〜100 倍**に増えている。

夜に「アイドル」に見えても、ゲストのカーネルによる metadata の書き出しは止まっていない（FUSE は 57 回／秒）。そのため、この循環が一晩中続いた。

### H2 [仮説]

書き込みの元は次のどれかと考えている。

- **inode のタイムスタンプ（mtime や ctime）の更新**: journald の mmap による書き込み、ログやデスクトップのサービス（tracker／localsearch、snapd、ブラウザなど）が group 960〜975 の inode を持っている
- **inode 表の書き出し周期**: ext4 の jbd2 の checkpoint やブロックデバイスの dirty page が、`dirty_expire_centisecs`（既定 30 秒）ごとに書き出される
- **orphan file の更新**: 一時ファイルの作成と削除のたびに書かれる

20〜30 秒周期の波は、既定の 30 秒の expire と、JuiceFS の `--slice-flush-idle 16s` で説明できる。

### H3 [仮説]

chunk 2810 は、group 1405 にある 1 つのファイルの追記や上書きと考えている。23:26 から 06:04 まで、毎分約 100 record。ファイルの候補は journald の journal、SQLite の WAL など。

### H4 [観測と推論]

compaction の出力は kaz-2q のキャッシュにも入った（cached 7,689 回）。cache への書き込み 37.46 GiB の大部分（約 30 GiB）は compaction の出力で、同じ chunk の古い出力は次の compaction ですぐに不要になる。これは cache_churn の主な流入元の 1 つである。

### H5 [観測と推論]

前の夜の 21:38〜01:00 の量が多いのは、起動後に積み残した fragment（chunk 1920 の 860 record、record 1,000 以上の chunk 12 個など）を片付けた分と、23 時台のゲストの活動（staging 1.77 GiB）による。

## 6. 対策の候補と見積もり

見積もりは観測した比率から計算した概算である。どれも実機では計測していない（[未検証]）。

減少率は、窓全体の 32.7 GiB を基準にする。定常状態では chunk 1920 が 50%、chunk 2810 が 21% を占める。

### A. ゲスト側（JuiceFS の変更不要。効果は大きいが、効果の量に不確かさがある）

1. **dirty page の書き出し周期を延ばす**: `vm.dirty_expire_centisecs` を 3000 から 12000〜30000 に、`vm.dirty_writeback_centisecs` を 500 から 1500 程度に。必要なら ext4 の `commit=30〜60` も。
   - 同じ inode table の page を 2〜10 分に 1 回まとめて書き出すようになり、record の数が減る。
   - chunk 1920 で観測した「約 2 分で 113 個中 65 個が別々の位置」から計算すると、5 分の窓では record が約 −50〜−65%、compaction（1 回あたりの量は同じ）も同じだけ減る。
   - **全体で −35〜−50%** と見積もる。
   - 代わりに、ゲストがクラッシュしたときに失うデータの範囲が広がる。ただし、ディスクがすでに cache=unsafe（no-flush）なので、ホスト側の耐障害性はもともと弱い。
2. **`lazytime`（と `noatime`）で mount する**: タイムスタンプだけの inode の更新をメモリに留める。mmap による上書き（journald など）はサイズが変わらないので、効く可能性が高い。
   - 効果は書き手によって大きく変わるので、**−10〜−50%** と幅を持たせる（[未検証]）。
3. **書き手を特定して止めるか減らす**:
   - ゲストで `debugfs -R "icheck 46041041 46042113 46042545"`（chunk 2810）や `icheck` を実行し、group 960 の inode 番号 7,864,321〜 に対して `ncheck` をかける。あわせて `fatrace -f W` か `iotop -ao` で特定する。
   - journald は `Storage=volatile` にするか、`SyncIntervalSec` を長くする。
   - chunk 2810 の書き手を止められれば **−21%**（定常状態の割合）。

### B. JuiceFS のオプション（今のバイナリですぐ試せる）

4. **`--slice-flush-idle 60s --slice-flush-wait 120s --writer-reuse-window 64`**（今は 16s／30s／32）
   - 同じ page への再書き込みを、まだ flush していない slice に上書きで吸収させる（`findWritableSlice`。位置が flush 前の範囲にあれば上書きする [source]）。idle が 30 秒の波より短い今の設定では、波ごとに新しい record ができる。
   - 見積もり:
     - chunk 0: 11 の位置に集中しているので record は −70% 以上
     - chunk 1920: −40% 前後
     - **全体で −25〜−40%**
   - fsync したときに flush する動作は変わらない。flush しないまま持つデータの最大時間は 30 秒から 120 秒に延びる。メモリ上の保留も増える（buffer-size 1 GiB の範囲内）。
   - 前に `--slice-flush-wait 30s --slice-flush-idle 10s` を試した経緯（findings／memo）とあわせて判断が必要。
5. `--kaz-compaction-read-min` を上げる: **単独では効果がほぼない**（commit の 99 が残るので [source]）。
6. `--kaz-compaction-mode upstream` に戻す: chunk 1920 では 1 回 64 MiB（今は 44 MiB）、chunk 2810 では 64 MiB（今は 9 MiB）になり、**増える**ので不可。

### C. kaz のコード変更（設計と承認が必要）

7. **block mode での commit トリガーの閾値を設定可能にする**（例: `--kaz-compaction-write-min`。read-min と組で 300 程度）
   - 1 回の compaction で片付ける新しい record が約 85 個から約 285 個になり、回数は 1/3.3 になる。触れるブロック数は chunk 1920 ではほぼ頭打ち（11 から最大 12〜16）。
   - **全体で −60〜−70%** と見積もる。
   - 代わりに、Read が 1 chunk で最大 300 record を合成する負担と、小さい object の GET が増える。また、350 を超えると commit のたびに要求する upstream の条件にも合わせる必要がある。
   - 2026-10-14 の記録では、compaction を遅らせる cooldown は「upload 前の小さい slice の片付け（retire）を減らし、PUT の総数を増やした」ので削除された。今回の窓では、PUT 138k 要求に対し compaction のブロックは約 8.3k で、小さい slice の PUT が大部分を占める。閾値を上げると小さい slice の PUT がさらに増える恐れがあるので、PUT の総数（rclone → Google Drive の要求数）での確認が必須である。
8. **書き直す価値の低いブロックを先送りする**（block の計画で、小さい record が k 個未満のブロックは今回書き直さない）
   - chunk 1920 の観測では、10 個以上の record を持つ 7 ブロックが全 record の 90% を持っていた。
   - 1 回あたりの出力は 11 ブロックから約 6〜7 ブロックに減る。**chunk 1920 で −35〜−40%、全体で −20〜−25%**。
   - record の減り方は少し弱くなる。
9. **compaction の出力をキャッシュに入れない**（kaz-2q）: 何度も compaction される chunk（または compaction 由来のブロックの置き換え）の出力をキャッシュしない。
   - object storage への量は変わらない。ローカルの cache への書き込み約 30 GiB／夜（SSD の消耗）と、追い出し（churn）が減る。

### 上位 3 つ（窓全体に対する見積もり）

| 順位 | 対策 | 見積もり | 備考 |
|---|---|---|---|
| 1 | 7: commit の閾値を設定可能にする（約 300）| −60〜−70% | コード変更。PUT の総数の確認が条件 |
| 2 | 1＋2: ゲストの dirty_expire を延ばし、lazytime | −35〜−55% | 設定だけ。ゲストのクラッシュ時に失う範囲が広がる |
| 3 | 4: slice-flush-idle／wait と reuse-window を延ばす | −25〜−40% | 今のバイナリで可能 |

2 と 3 は同じ「再書き込みの吸収」を別の層でするので、効果は足し算にならない。

## 7. 未検証

- 32.66 GiB のうち、実際にアップロードした（圧縮後の）量。PUT の 7.69 GiB を staging と compaction に分けるには、`--debug` の block-object の行か、PUT ごとの記録が必要。
- 1 回の compaction を、commit（99）と read（100 以上）のどちらが最初に起動したかの内訳。`kaz_compaction_*` の時系列は sampler が記録していない（終わりの累積値だけ）。
- chunk 1920 のブロック 10〜15、chunk 2810、chunk 1985 の具体的なファイル。ゲストでの `debugfs icheck／ncheck` が必要。
- raw のディスクでホストとゲストのオフセットが等しいこと（パーティションが 2 MiB から始まり、変換前と同じ配置であること）。ゲストの `lsblk` を変換後に取り直してはいない。
- 07:41〜07:47 の観察は朝の時点の状態であり、夜（特に chunk 2810 が書かれていた 23:26〜06:04）の形と同じとは限らない。
- 6 章の見積もり（周期を延ばしたときに record がどれだけ減るか、ブロック数が頭打ちになるか）は、観察した数分のデータからの外挿である。
- 23 時台の readSlice の「context canceled」3,252 行（slice 436 種類）の原因と、`compaction_gc local_error` 6,245 の中身は、今回は調べていない。
- 6 章の対策 4（flush の待ちを延ばす）の副作用（メモリ、遅延、PUT の大きさの変化）は、計測していない。
