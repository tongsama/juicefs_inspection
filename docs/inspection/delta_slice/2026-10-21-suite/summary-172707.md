# suite の結果 20261021-172707

- trace: `results/20261021-trace1/trace.tsv`、age: 0、設定: default, trigger30, trigger50, trigger200, block-ratio75

| 項目 | default | trigger30 | trigger50 | trigger200 | block-ratio75 |
|---|---|---|---|---|---|
| 結果 | 未収束 | 成功 | 未収束 | 未収束 | 失敗 |
| 設定 Meta.KazCompactionDeltaTrigger | 99 | 30 | 50 | 200  | - |
| 設定 Meta.Sid | 9 | 10 | 11 | 12  | - |
| 窓の長さ h | 0.47 | 0.42 | 0.45 | 0.45  | - |
| mount のし直し（uptime の巻き戻り）回数 | 0 | 0 | 0 | 0  | - |
| staging 書込 GiB | 4.86 | 4.86 | 4.86 | 4.86  | - |
| compaction 出力 GiB（非圧縮） | 0.46 | 0.48 | 0.47 | 0.41  | - |
| compaction 出力 slice 数 | 192 | 242 | 207 | 148  | - |
| PUT GiB（圧縮後） | 0.00 | 0.00 | 0.00 | 0.00  | - |
| PUT 回数 | 7,493 | 6,305 | 6,809 | 7,960  | - |
| GET GiB | 0.00 | 0.00 | 0.00 | 0.00  | - |
| GET 回数 | 424 | 395 | 394 | 397  | - |
| DELETE 回数 | 12,814 | 13,761 | 13,613 | 11,602  | - |
| cache 書込 GiB | 5.33 | 5.36 | 5.33 | 5.28  | - |
| FUSE ops | 185,711 | 187,542 | 187,629 | 186,996  | - |
| meta ops | 55,288 | 54,069 | 54,121 | 54,447  | - |
| compaction 出力 GiB（非圧縮） ÷ staging GiB | 0.09 | 0.10 | 0.10 | 0.08  | - |
| PUT GiB（圧縮後） ÷ staging GiB | 0.00 | 0.00 | 0.00 | 0.00  | - |
| PUT 回数 ÷ staging GiB | 1543.16 | 1298.30 | 1402.06 | 1639.08  | - |
| compaction 結果: delta_noop | 11 | 5 | 13 | 7  | - |
| compaction 結果: delta_tier | 95 | 149 | 113 | 62  | - |
| compaction 結果: delta_merge | 4 | 7 | 4 | 0  | - |
| compaction 結果: delta_fold | 0 | 0 | 0 | 1  | - |
| compaction 結果: delta_prune | 0 | 3 | 4 | 3  | - |
| compaction 量: delta_blocks_nobase | 2 | 1 | 2 | 1  | - |
| compaction 量: delta_blocks_ratio | 74 | 76 | 76 | 70  | - |
| compaction 量: delta_fold_blocks | 0 | 0 | 0 | 1  | - |
| compaction 量: blocks_written | 80 | 79 | 80 | 74  | - |
| compaction 量: delta_bytes | 130,561,024 | 168,125,440 | 137,905,152 | 100,234,240  | - |
| compaction 量: delta_merge_bytes | 21,684,224 | 38,805,504 | 20,221,952 | 0  | - |
| compaction 量: delta_records | 4,351 | 5,518 | 4,556 | 3,093  | - |
| compaction 量: removed | 14,586 | 17,128 | 15,500 | 11,907  | - |
| 片付け: hint_overflow | 0 | 0 | 0 | 0  | - |
| 片付け: local_error | 90 | 70 | 93 | 119  | - |
| 片付け: local_retire_success | 13,151 | 14,161 | 13,924 | 11,857  | - |
| 片付け: remote_deferred | 0 | 0 | 0 | 0  | - |
| 片付け: remote_enqueued | 13,151 | 14,161 | 13,924 | 11,857  | - |
| meta ops 平均 ms | 22.09 | 21.96 | 22.01 | 22.23  | - |
| transaction 回数 | 4,695 | 4,808 | 4,743 | 4,649  | - |
| transaction 平均 ms | 56.88 | 45.54 | 46.20 | 57.68  | - |
| flush p50 ms | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| flush p99 ms | 1986.24 | 1983.16 | 2396.05 | 2432.82 | 813.58 |
| flush max ms | 2990.31 | 3041.12 | 3086.45 | 15230.82 | 819.89 |
| read p50 ms | 0.29 | 0.27 | 0.26 | 0.26 | 0.21 |
| read p99 ms | 120.49 | 102.93 | 102.66 | 107.44 | 99.24 |
| read max ms | 16549.94 | 2010.40 | 1936.97 | 1881.03 | 1338.40 |
| write p50 ms | 0.16 | 0.18 | 0.15 | 0.17 | 0.21 |
| write p99 ms | 51.89 | 43.55 | 36.49 | 38.26 | 133.91 |
| write max ms | 16356.08 | 3279.24 | 3268.55 | 15575.86 | 468.84 |
| 予定からの遅れの最大 ms | 16685.78 | 3740.20 | 3741.45 | 10311.70 | 1209.26 |
| play のエラー数 | 0 | 0 | 0 | 0 | 0 |

## 失敗・注意
- block-ratio75（失敗）: 試験の metrics を 3 回続けて取得できない; 流し直しが止まった: 外から止められた

## 出どころ（各行の系列）

- 各列の区間は、その回の `<dir>/metrics.tsv` の最初のサンプルから最後のサンプルまで（`report.compare` の `first`〜`now`）。
- 「設定 …」の行: 各回の `config.json`（mount の `.config`）で、列の間で値が違う項目だけ。
- staging 書込 GiB: `juicefs_staging_write_bytes` の増分
- compaction 出力 GiB（非圧縮）: `juicefs_compact_size_histogram_bytes_sum` の増分
- compaction 出力 slice 数: `juicefs_compact_size_histogram_bytes_count` の増分
- PUT GiB（圧縮後）: `juicefs_object_request_data_bytes{method="PUT"}` の増分
- PUT 回数: `juicefs_object_request_durations_histogram_seconds_count{method="PUT"}` の増分
- GET GiB: `juicefs_object_request_data_bytes{method="GET"}` の増分
- GET 回数: `juicefs_object_request_durations_histogram_seconds_count{method="GET"}` の増分
- DELETE 回数: `juicefs_object_request_durations_histogram_seconds_count{method="DELETE"}` の増分
- cache 書込 GiB: `juicefs_blockcache_write_bytes` の増分
- FUSE ops: `juicefs_fuse_ops_durations_histogram_seconds_count` の増分
- meta ops: `juicefs_meta_ops_durations_histogram_seconds_count` の増分
- compaction 結果／量、片付け、meta ops、transaction: `juicefs_compact_*`、`juicefs_*gc*`、`juicefs_meta_ops_*`、`juicefs_transaction_*`（`measure/report.py` の compaction_counts／compare を参照）。
- ÷ staging GiB の行: 上の行を `juicefs_staging_write_bytes`（staging 書込 GiB）で割った値。
- 待ち時間の行（write p50 ms など）: 各回の `play.json` の latency（流し直しのクライアント側の測定、nearest-rank）。
- 結果の行: 各回の `run.json`／play の status と落ち着きの判定（未収束 = 上限まで待っても staging／uploading／compaction が止まらなかった）。

## 各列のディレクトリ

- default: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-172707/default`
- trigger30: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-172707/trigger30`
- trigger50: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-172707/trigger50`
- trigger200: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-172707/trigger200`
- block-ratio75: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-172707/block-ratio75`
