# suite の結果 20261021-210115

- trace: `results/20261021-trace1/trace.tsv`、age: 0、設定: block

| 項目 | block |
|---|---|
| 結果 | 未収束 |
| 窓の長さ h | 0.45  |
| mount のし直し（uptime の巻き戻り）回数 | 0  |
| staging 書込 GiB | 4.86  |
| compaction 出力 GiB（非圧縮） | 2.06  |
| compaction 出力 slice 数 | 527  |
| PUT GiB（圧縮後） | 0.01  |
| PUT 回数 | 7,316  |
| GET GiB | 0.00  |
| GET 回数 | 457  |
| DELETE 回数 | 13,510  |
| cache 書込 GiB | 6.93  |
| FUSE ops | 188,518  |
| meta ops | 54,455  |
| compaction 出力 GiB（非圧縮） ÷ staging GiB | 0.42  |
| PUT GiB（圧縮後） ÷ staging GiB | 0.00  |
| PUT 回数 ÷ staging GiB | 1506.47  |
| compaction 結果: partial | 89  |
| compaction 結果: full | 0  |
| compaction 結果: noop | 8  |
| compaction 量: blocks_written | 507  |
| compaction 量: removed | 12,319  |
| 片付け: hint_overflow | 0  |
| 片付け: local_error | 97  |
| 片付け: local_retire_success | 13,844  |
| 片付け: remote_deferred | 0  |
| 片付け: remote_enqueued | 13,844  |
| meta ops 平均 ms | 21.82  |
| transaction 回数 | 5,035  |
| transaction 平均 ms | 47.01  |
| flush p50 ms | 0.00 |
| flush p99 ms | 2324.18 |
| flush max ms | 3087.43 |
| read p50 ms | 0.25 |
| read p99 ms | 109.32 |
| read max ms | 1918.33 |
| write p50 ms | 0.17 |
| write p99 ms | 73.17 |
| write max ms | 3218.04 |
| 予定からの遅れの最大 ms | 3742.92 |
| play のエラー数 | 0 |

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

- block: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-210115/block`
