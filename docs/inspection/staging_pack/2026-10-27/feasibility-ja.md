# staging のアップロード待ちで小さい slice をまとめる案の実現性と見積もり（2026-10-27）

[事実] は観測またはソースで確認したこと、[推論] は推定、[未確認] は確かめていないこと。ソースは本体 `1.4.2-improve-kaz`（HEAD `d246b0f7`）。

## 1. 背景

- [事実] PUT の回数の約 93% は staging のアップロード（ゲストの書き込みの slice）で、compaction は約 7%（findings の 2026-10-21 の訂正）。
- [事実] trigger を 99 → 30 に下げると PUT の回数が約 16% 減った（試験 1 回、[比較](../../delta_slice/2026-10-21-suite/README-ja.md)）。アップロード前の片付け（retire）が増えたため。
- 条件: アップロードを遅らせない（upload-delay は使わない）。正しさ（fsync、read-after-write、参照数、ほかの client からの見え方、無改良版との互換）を最優先する。

## 2. 試験の metrics から分かったこと（2026-10-21 の suite、各 1 回）

| | default（trigger 99） | trigger 30 |
|---|---:|---:|
| PUT の回数（metrics） | 7,701 | 6,481 |
| PUT 1 回の平均時間 | 3.71 s | 3.81 s |
| DELETE の回数 | 13,242 | 14,234 |
| DELETE 1 回の平均時間 | 1.01 s | 0.98 s |
| staging のアップロード待ちの合計（`staging_block_delay_seconds`） | 2,620,437 s | 1,549,522 s |

- [事実] `--max-uploads 18`、PUT 1 回が平均約 3.7 秒なので、アップロードの速さは 1 秒に約 5 回が上限。
- [推論] 待ちの合計を、待ち行列を通ったアップロードの数（約 7,000）で割ると、**1 つの block がアップロードされるまで平均 4〜6 分待っている**。待ち行列は試験のほとんどの間、深く詰まっている。今の retire（compaction による片付け）が効くのは、この待ちがあるからである。
- [推論] **DELETE の回数は片付けた slice の数とほぼ同じ**（13,242 と 13,151、14,234 と 14,161）。つまり DELETE のほとんどは、一度もアップロードされていない object に対するもの（§5）。

## 3. 今のアップロードの経路（ソース）

- [事実] writeback で block が `--writeback-threshold-size` 未満なら、staging に書き（`wSlice.upload`、`cached_store.go:398-490`）、次のどちらかでアップロードする。
  - `upload-delay` が 0 で、アップロードの枠（`currentUpload`、`--max-uploads`）が空いていれば、すぐにアップロードする（`beginImmediateStaging`）。
  - 空いていなければ、`pendingKeys` に登録して `pendingCh`（容量 `100 × max-uploads`）に入れる（`addDelayedStaging`、`cached_store.go:1142`）。入りきらないものは、1 分ごとの `scanDelayedStaging` が入れる。
- [事実] 待ち行列は FIFO。`uploader` が取り出して `uploadStagingFile`（`cached_store.go:1085`）で staging のファイルを読んで PUT する。PUT の前に pending の登録を確かめ、無ければ捨てる（「Key is not needed」）。
- [事実] object の名前は `chunks/<id/1e6>/<id/1e3>/<id>_<block 番号>_<長さ>` で、slice の id から決まる。**複数の slice のデータを 1 つの object に入れるには、メタデータの record を書き換えるしかない**（無改良の client は slice の id から object の名前を作って読むため）。
- [事実] メタデータの commit は staging の後（`sliceWriter.flushData` → `Finish` → `commitSlices`、`writer_kaz.go:119`）。fsync が返る時点では、データは staging にあり、record は commit 済み。アップロードは、その後の非同期の仕事である。
- [事実] 片付け（`Retire`、`cached_store_staging_kaz.go:134`）は pending の登録を消し、staging のファイルを消す。その後 `rSlice.Remove`（`cached_store.go:191`）が、block ごとに DELETE を必ず送る。

## 4. 案 A: 取り出した時点で同じ chunk の小さい slice をまとめる（pack）

### 4.1 しくみ

uploader が小さい block を待ち行列から取り出したとき、同じ chunk に、まだアップロードが始まっていない小さい slice がほかにもあれば、それらを 1 つの slice（差分の「段」と同じ形）にまとめて 1 回だけ PUT し、メタデータの record を置き換える。

1. 取り出した slice の id から、inode と chunk の番号を引く。vfs の writer が commit したときに覚えておく（`commitSlices` の成功の後。覚えていない slice は今のままアップロードする）。
2. 同じ chunk の、待っている小さい slice（block 1 つだけで、まだ PUT が始まっていないもの）を集め、pending の item を「まとめ中」として押さえる。ほかの uploader はそれを取らない。（訂正: 列に入った item は `uploading` が既に true なので、`uploading` の CAS では押さえられない。設計書 §5.3 で別の状態を足した。）
3. メタデータの chunk の record を読み、集めた slice のうち record に見えている部分だけを、位置順に連結して 1 つの新しい slice を作る（**隙間は埋めない**ので、元のデータを読まない。読むのは staging のローカルのファイルだけで、GET は出ない）。
4. 新しい slice を PUT する（今の compaction と同じく、PUT が終わってから次へ進む）。
5. 差分方式の置き換え（`doKazReplaceChunk`、参照の数 = record の数）で、集めた slice の record を新しい slice の record（`off`、`len` で部分を指す）に置き換える。
6. 置き換えが成功したら、古い slice は参照が 0 になり、今の経路で片付けられる（staging を消す）。失敗したら、押さえた item を戻して、すぐ待ち行列に入れ直す。

### 4.2 正しさ

- **fsync:** 変わらない。fsync が返る時点の条件（staging と record の commit）には触れない。まとめるのは、その後の非同期の部分だけ。
- **read-after-write:** 置き換えの前後で見え方が同じ（差分方式の不変条件 I1〜I4 と同じ）。新しい slice の出力はキャッシュに入る（今の `vfs.Compact` の経路）。置き換えは chunk の先頭の比較で、その間に来た書き込みは上に残る。
- **参照の数:** 差分方式と同じ（新しい slice は record の数、古い slice は record ごとに 1 つ減らす）。
- **ほかの client からの見え方:** 今は、アップロード前の slice の record をほかの client が読むと、object が無いので再試行になる。まとめた後は、PUT 済みの slice を指すので、**見えるのが早くなる**。
- **無改良版との互換:** `off`、`len` で 1 つの slice の部分を指す record は、もとの形式（CopyFileRange も作る）。新しい種類の key は作らない。差分方式と同じく TKV では使わない。
- **失敗:** PUT の失敗や置き換えの失敗では、元の slice が残り、そのままアップロードされる。容量不足や quota のエラーは今の経路のまま返る。

### 4.3 効果の見積もり（模擬、[推論]）

`pack_sim.py`（入力は 2026-10-21 の記録 `trace.tsv`、SHA-256 の先頭 `0301656afd02c6fd`、出力は `pack_sim.txt`）。write から slice（idle 16 s／wait 30 s）と 4 MiB の block を作り、18 の worker が FIFO で 1 回 3.7 s かけて PUT する。compaction は chunk ごとに trigger 個の新しい slice で、待っている小さい item を片付ける。

| | 今の模擬 PUT | 片付け | pack（まとめる上限 4 MiB）の PUT | 差 |
|---|---:|---:|---:|---:|
| trigger 99 | 7,553 | 9,658 | 3,841 | **−49%** |
| trigger 30 | 5,290 | 11,921 | 3,790 | **−28%** |
| trigger 30、PUT 2.0 s | 7,791 | 9,420 | 4,802 | −38% |
| trigger 30、worker 36 | 8,170 | 9,041 | 5,416 | −34% |

- 模擬の今の数は、試験の実測と同じくらい（trigger 99: 模擬 7,553 と実測 7,493、trigger 30: 5,290 と 6,305。実測には compaction の約 7% を含む）。
- まとめる上限を 1 MiB／16 MiB にしても、差は 1〜2 ポイントしか変わらない。
- 待ち行列が詰まっていないとき（すぐアップロードできるとき）は、まとめる相手がいないので何も変わらない（害もない）。

### 4.4 費用と心配

- **メタデータ:** まとめるたびに chunk の読み込みと置き換えの transaction が 1 回ずつ（Redis で 50 ms 前後 [推論]）。PUT 1 回 3.7 s に比べれば小さい。
- **段が増える:** まとめた slice は、差分方式から見ると「差分の段」になる。段の上限（8）を早く超え、差分のまとめ直し（1 回の PUT、バイトは差分の合計）が増える。compaction のバイトが少し増える [推論、未見積もり]。
- **record の数は減らない:** k 個の record が k 個の record（同じ slice を指す）になるだけ。
- **DELETE が増える:** 古い slice の片付けで、一度もアップロードしていない object への DELETE が出る（今の retire と同じ）。§5 の案 B と組み合わせると無くなる。
- **再起動の後:** 覚えた対応（slice → inode、chunk）は消えるので、それより前の staging は今のままアップロードする。

## 5. 案 B: 一度もアップロードしていない object の DELETE を送らない

- [推論] DELETE の回数は片付けた slice の数とほぼ同じで、PUT の約 2 倍ある（§2）。1 回 約 1 秒。
- [事実] `rSlice.Remove` は `Retire` の後、block ごとに DELETE を必ず送る。
- [推論] この process が staging して pending に登録し、PUT を一度も始めていない block なら、object は存在しない。DELETE を省いても参照の無い object は残らない。
  - 省いてはいけないもの: PUT を始めたもの（時間切れの PUT が後で成功していることがある）、再起動の後の scan で登録したもの（前の process が PUT した後、staging を消す前に落ちたことがある）、staging に失敗して直接アップロードしたもの。
- [未確認] rclone serve s3 が、存在しない key の DELETE で Drive の API をどれだけ呼ぶか。

案 B は PUT の回数を減らさないが、バックエンドとのやり取りの回数を大きく減らす。案 A とは独立に入れられる。

## 6. ほかに考えて外した案

- **object の中に複数の slice を詰める（独自の索引）:** 無改良の client が slice の id から object の名前を作るので、互換が壊れる。外す。
- **commit の前に writer の中でまとめる:** fsync のたびに slice を staging と commit まで済ませる必要があり、まとめる余地が無い。fsync を弱める形になる。外す。
- **アップロードを遅らせる（upload-delay）:** ユーザーの条件に合わない。外す。
- **trigger をさらに下げる:** 効くが（16%）、compaction の回数とメタデータの読み込みが増え、chunk 全体の計画を何度も作る。案 A の方が狙いが絞れている [推論]。

## 7. 限界

- 模擬は単純化している（slice の作り方、PUT の時間が一定、compaction の時間 0、compaction 自身の PUT を数えない、まとめた後の段のまとめ直しを数えない）。数は目安で、向きと大きさの見当として使う。
- 確かめは `tests/juicefs/`（jfsreplay suite）で、同じ記録を流し直して比べる。

## 8. upload-delay と組み合わせたとき（追記、2026-10-27）

`delay_sim.py`（出力 `delay_sim.txt`、入力は同じ `trace.tsv`）。trigger 30、18 worker、PUT 1 回 3.7 s。item は freeze から delay 秒たってから列に並ぶ（delay > 0 では、すぐ PUT する経路を使わない。`cached_store.go:462`）。

| delay | pack なし | pack（delay を過ぎたものだけ取る） | pack（delay 前の若いものも取る） |
|---:|---:|---:|---:|
| 0 s | 5,290 | 3,790（−28%） | 3,790（−28%） |
| 10 s | 5,130 | 3,644（−29%） | 3,019（−41%） |
| 30 s | 4,910 | 3,551（−28%） | 2,578（−47%） |
| 60 s | 4,661 | 3,395（−27%） | 2,086（−55%） |
| 120 s | 4,342 | 3,160（−27%） | 1,732（−60%） |

- [推論] delay を足すと、取り出したときに同じ chunk で待っている相手が増え、まとめの効果が大きくなる。delay 0 の pack なし（5,290）に比べて、delay 60 s と pack（若いものも取る）で約 −61%。
- 「若いものも取る」は、delay を待たずに一緒に送る形。後から来た item は delay より早くアップロードされるが、正しさには関係しない（早く送るだけ）。片付け（retire）の機会は少し減るが、模擬ではまとめの効果の方が大きい。
- 模擬の delay は compaction の片付けの起動の時刻を変えない（freeze の時点で数える）。1 回の記録の単純な模擬で、数は目安。
