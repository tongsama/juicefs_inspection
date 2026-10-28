# vanilla 相当と kaz の全部入りの比べ（jfsreplay suite、2026-10-28）

[観測] は試験で測ったこと、[推論] は推定、[未確認] は確かめていないこと。

## 0. 要点

- 同じ本番の記録（VM のイメージへの書き込み、168,259 操作、1,038 秒）を流した。kaz の機能をフラグで外した vanilla 相当は、再生が予定から最大 41 分遅れた（流し終えるのに 3,226 秒）。kaz の全部入りは予定どおり（1,044 秒、遅れの最大 3.7 秒）だった [観測]。
- 落ち着くまでの比べ: PUT 34,521 → 4,057（−88%）。DELETE は vanilla が 14:15 の打ち切りの時点ですでに 30,764 で、kaz は 989（−97%）。GET 3,777 → 361（−90%）[観測]。
- 待ち時間（再生する側から見た値）: write の p50 は 2,429 ms → 0.17 ms、p99 は 10,958 ms → 31.5 ms。read の p50 は 69.7 ms → 0.27 ms、p99 は 5,702 ms → 106 ms [観測]。
- vanilla では、compaction が 64 MiB の chunk を丸ごと 884 回書き直した（出力 55.2 GiB、kaz は 0.50 GiB）。それがキャッシュに 75 GiB 書かれ、温めたキャッシュを 13,583 block 追い出した [観測]。これが遅れの主な原因と見込む [推論]。
- どちらも play のエラー、staging の書き込みのエラーは 0。vanilla も最後には全部アップロードした（staging 0）[観測]。

## 1. 条件

- binary: `~/.local/bin/kaz-test/juicefs-pack-145fc631`（本体 `feat/kaz-staging-pack` 145fc631）。両方とも同じ binary で、違いはフラグだけ。
- 記録と種: `tests/juicefs/results/20261021-trace1/trace.tsv`（ほかの suite と同じ）。温めたキャッシュの写しを戻してから流す。各 1 回。
- 共通（`mount_flags.txt` の印の無い行、本番の値）: `--writeback`、`--writeback-threshold-size 4194305`、`--cache-size 20480`、`--buffer-size 1024`、`--prefetch 0`、`--max-uploads 18`、`--max-deletes 10`、`--upload-delay` は下の表、`--writeback-fsync`（kaz のフラグ。ユーザーの指示で vanilla でも有効）ほか。
- **vanilla**（settings `vanilla`、結果 `tests/juicefs/results/20261028-131033/vanilla`）: kaz の機能のフラグを全部外した。
  - `--cache-eviction lru`（本家の方式。ユーザーの指示）、`--kaz-compaction-mode` なし（= upstream、chunk を丸ごと書き直す）、`--kaz-compaction-read-min` なし（= 5、本家と同じ）
  - flush の待ち 5 秒・idle 1 秒（本家の固定値）、`--writer-flush-timeout auto`（本家と同じ期限）、flush scope `file`、reuse window 4、meta write batch 0、compaction GC `legacy`、compaction scheduler `legacy`、finish-canceled-get なし、get-header-timeout なし、kaz-2q の設定なし
  - upload-delay は本番と同じ 0。
  - 実効値は mount の `.config` で確かめた（例: `KazCompactionMode upstream`、`CacheEviction lru`、`SliceFlushWait 5s`、`WriterFlushScope file`、`MetaWriteBatch 0`）。
  - フラグで切れない kaz の変更（FUSE の watchdog を止めたこと、本家の版に入っていない不具合の修正など）は残る。完全な本家の binary ではない。
- **kaz-all**（settings `kaz-all`、結果 `tests/juicefs/results/20261028-164006/kaz-all`）: 本番の kaz の設定（`mount_flags.txt` のまま）＋ delta の trigger 30、block-ratio 0.5、`--kaz-staging-pack`、`--kaz-skip-unuploaded-delete`、`--upload-delay 5s`。これまでの `pack-skipdel-delay5` と同じ値。
- 道具の変更: `mount_flags.txt` の kaz の機能の行に `[kaz]` の印を付け、settings で置き換え・削除（false）できるようにした（`replay/testvol.py` の `SETTINGS_MARKS`）。

## 2. 経過 [観測]

- vanilla: 13:12 に再生を始め、予定（1,038 秒）を大きく超えて 3,226 秒で流し終えた。suite の「落ち着くまで待つ」は上限の 600 秒で打ち切られた（14:15、staging 31,089 block、アップロード中 18）。その後、suite は複製を消し、umount を 600 秒試みて失敗し、「失敗」として止まった。
- mount は生きてアップロードを続けていたので、止めずに最後まで動かした。staging は 15:11 に 0 になった。DELETE はその後も約 85 分続き、16:35 に止まった（[vanilla-drain.tsv](vanilla-drain.tsv)、30 秒ごとの記録）。16:39 に外した。
- kaz-all: 17:00 ごろに流し終え、65 秒で落ち着いた（suite の判定）。

## 3. 結果

数え方: 各回の `metrics.tsv` の最初のサンプルから最後のサンプルまでの増分。記録の途中で初めて現れる系列は、現れる前を 0 とみなす（§6 の訂正を参照）。全部の行は [compare.md](compare.md)（[compare_ana.py](compare_ana.py) の出力）。suite の表は [vanilla](summary-20261028-131033-vanilla.md)、[kaz-all](summary-20261028-164006-kaz-all.md)（直す前の数え方）。

### 3.1 object storage への要求

| 項目 | vanilla（14:15 の打ち切り） | vanilla（16:37、全部止まった後） | kaz-all | kaz-all ÷ vanilla（止まった後） |
|---|---:|---:|---:|---:|
| PUT | 17,063 | 34,521 | 4,057 | 0.12 |
| GET | 3,777 | 3,777 | 361 | 0.10 |
| DELETE | 30,764 | 138,130 | 989 | （下の注） |
| PUT 平均 ms | 3,922 | - | 2,766 | |
| GET 平均 ms | 2,580 | - | 1,102 | |
| 待ち行列の待ちの合計 秒 | 7.4 M | 58.8 M | 0.06 M | |
| 1 回の PUT あたりの待ち（目安） | 約 435 秒 | 約 1,700 秒 | 約 15 秒 | |

- 16:37 の値は mount の counter の値そのもの（mount は回ごとに新しい process で、0 から数える）。
- 注: 14:15 より後の vanilla の DELETE には、suite が消した試験用の複製（再生で書いた slice）の片付けが混ざる。kaz-all ではこの片付けは suite の後に起き、測っていない。そのため、DELETE を同じ条件で比べられるのは 14:15 の値まで（vanilla 30,764 以上、kaz-all 989）。
- PUT は複製を消しても増えない [推論: 消した slice の staging はアップロードしない]。vanilla の PUT のうち約 14,100 は compaction の出力（55.3 GiB ÷ 4 MiB）と見込む [推論]。

### 3.2 速さ（再生する側の測定）

| 項目 | vanilla | kaz-all |
|---|---:|---:|
| 流し終えるまで 秒（予定 1,038） | 3,226 | 1,044 |
| 予定からの遅れ p99／max 秒 | 2,418／2,484 | 0.2／3.7 |
| write p50／p99／max ms | 2,429／10,958／26,060 | 0.17／31.5／3,228 |
| read p50／p99／max ms | 69.7／5,702／24,128 | 0.27／106／1,828 |
| flush p99／max ms | 3,211／5,822 | 1,978／3,048 |
| FUSE の平均 ms（mount の metrics） | 133.5 | 6.7 |

### 3.3 compaction、キャッシュ、metadata

| 項目 | vanilla | kaz-all |
|---|---:|---:|
| compaction の回数（出力 slice 数） | 884 | 262 |
| compaction の出力 GiB | 55.2 | 0.50 |
| キャッシュへの書き込み GiB | 74.9 | 5.9 |
| キャッシュの追い出し（evict） | 13,583 | 0 |
| キャッシュの hit 率（回数／バイト） | 97.2%／88.5% | 99.6%／99.9% |
| 最後のキャッシュの中身 full GiB | 8.4 | 15.9 |
| staging の block 数 最大 | 38,362 | 1,314 |
| staging の量 最大 MiB | 3,967 | 1,300 |
| meta ops | 164,079 | 53,254 |
| metadata の transaction | 63,025 | 6,740 |
| 最後に参照されている slice 数（chunk 632） | 17,380 | 7,478 |
| slice ÷ chunk | 27.5 | 11.8 |
| staging pack（まとめた回数／slice） | - | 1,012／8,456 |
| 省いた DELETE | - | 16,405 |
| compaction GC の local_error | - | 344 |

- vanilla の compaction（log の `slow compaction`、884 件）: 1 回で書き直す slice 数は p10／p50／p90／max = 4／17.5／199／401、時間は 24.6／32.2／64.9／81.9 秒、合計 9.3 時間（並列）。どれも 64 MiB の chunk を丸ごと書き直した（`bytes=67108864`）。
- vanilla の log の警告: `slow compaction` 884、`cleanupZeroRef` の transaction のやり直し 124、`fail to read sliceId ... context canceled` 32、`slice has an in-flight staging upload` 9。

## 4. 読み取れること [推論]

- **vanilla の遅れの主因は compaction の書き直しの量**: 本家の compaction は、読み込みで slice が 5 個以上の chunk（`read-min 5`）や、書き込みで slice が増えた chunk を、64 MiB 丸ごと書き直す。VM のイメージのように同じ chunk へ小さい書き込みが散らばると、17 slice 程度でも 64 MiB を読み直して書き出す。これが 884 回で 55 GiB の出力になり、キャッシュへの 75 GiB の書き込みでディスクを取り合い、キャッシュを 13,583 block 追い出した（最後の full は 15.9 → 8.4 GiB）。その結果、GET（平均 2.6 秒）と read の待ちが増え、write も buffer が詰まって待った、と見込む。
- **read の待ち**: flush scope `file` では、read の前にそのファイルの未 commit の書き込みを全部 commit するまで待つ。イメージ 1 つに書き込みと読み込みが混ざるので、read の p50 が 70 ms になった一因と見込む。
- **metadata**: flush の待ちが 5 秒・idle 1 秒と短く、まとめての commit も無いので、transaction が 9 倍になった。
- **アップロードの待ち**: staging の滞留が最大 38,000 block、1 回の PUT あたり約 28 分（全体の平均）待った。writeback でも fsync しているので、データは staging（ディスク）に残る。ただ、その間に object storage へ届いていない量（最大約 4 GiB）は kaz-all（約 1.3 GiB）の 3 倍だった。
- どの kaz の機能がどれだけ効いたかは、この 2 回では分けられない（全部をまとめて外した）[未確認]。一つずつ戻す試験で分けられる。

## 5. 限界

- 各 1 回。vanilla は再生が大きく遅れたので、時間あたりの負荷の形が kaz-all と違う（同じ操作を 3 倍の時間に引き延ばした）。
- vanilla の DELETE は、複製の片付けを含むので、全部止まった後の値は kaz-all と比べられない（§3.1 の注）。
- フラグで切れない kaz の変更が残る binary で、本家の v1.4.2 そのものではない。
- 温めたキャッシュは kaz-2q で作ったものを lru で読み込んでいる（キャッシュの中身は同じ）。
- PUT のバイト数は、流し直しのデータがよく圧縮されるためほぼ 0 になり、比べに使っていない。

## 6. 訂正: suite の表の PUT／DELETE などが少なく出ていた

- [観測] label 付きの counter（`juicefs_object_request_durations_histogram_seconds_count{method="PUT"}` など）は、最初の要求のときに初めて metrics に現れる。試験の記録では、最初のサンプルから 120〜240 秒後に現れる。`lib/metrics_io.delta` は窓の中の最初のサンプルを起点にしていたので、現れた時点の値（最初の 1〜2 分の回数）が抜けていた。
- 抜けていた量（記録の途中で現れた時点の値）: PUT 145〜382（2〜5%）、DELETE 82〜440（1〜13%）、GET 1〜109。
- 直した: 記録の最初のサンプルより後に初めて現れた系列は、現れる前を 0 とみなす（`delta(..., born_after=...)`、`measure/report.py` の `_D`／`_Dsum`、テスト `test_series_born_in_window_counts_from_zero`）。これまでの `summary.md` は直す前の数のまま。
- 直した数え方での主な値（[compare_ana.py](compare_ana.py) で出し直し）:

  | 設定 | PUT | DELETE | PUT＋DELETE | 基準との差 |
  |---|---:|---:|---:|---:|
  | trigger30（2 回の平均、基準） | 6,574 | 13,247 | 19,821 | — |
  | pack（2 回） | 4,235 | 10,691 | 14,926 | −25% |
  | skipdel（2 回） | 7,220 | 2,035 | 9,255 | −53% |
  | pack-skipdel（2 回） | 5,618 | 2,354 | 7,971 | −60% |
  | trigger30-delay1 | 6,573 | 13,343 | 19,916 | +0% |
  | skipdel-delay1 | 7,182 | 2,080 | 9,262 | −53% |
  | pack-skipdel-delay1 | 5,085 | 1,901 | 6,986 | −65% |
  | pack-skipdel-delay2 | 4,807 | 1,611 | 6,418 | −68% |
  | pack-skipdel-delay5 | 4,044 | 940 | 4,984 | −75% |
  | t50-pack-skipdel-delay5 | 3,992 | 873 | 4,865 | −75% |
  | t100-pack-skipdel-delay5 | 3,965 | 785 | 4,750 | −76% |
  | kaz-all（= pack-skipdel-delay5） | 4,057 | 989 | 5,046 | −75% |

- 結論は変わらない（基準との差は ±1% 以内）。細かい数の違い: pack の PUT は −37% → −36%、skipdel の PUT は +9% → +10%、trigger 50／100 の DELETE は trigger 30 より −7%／−16%（前は −11%／−16%）。
