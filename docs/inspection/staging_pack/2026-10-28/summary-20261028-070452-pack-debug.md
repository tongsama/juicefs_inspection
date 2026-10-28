# suite の結果 20261028-070452

- trace: `results/20261021-trace1/trace.tsv`、age: 0、設定: pack-debug、--head-seconds 600（記録の最初の 600 秒だけ）

| 項目 | pack-debug |
|---|---|
| 結果 | 成功 |
| 窓の長さ h | 0.22  |
| mount のし直し（uptime の巻き戻り）回数 | 0  |
| staging 書込 GiB | 4.47  |
| compaction 出力 GiB（非圧縮） | 0.34  |
| compaction 出力 slice 数 | 152  |
| PUT GiB（圧縮後） | 0.00  |
| PUT 回数 | 2,762  |
| GET GiB | 0.00  |
| GET 回数 | 321  |
| DELETE 回数 | 6,952  |
| cache 書込 GiB | 5.23  |
| FUSE ops | 147,315  |
| meta ops | 43,595  |
| compaction 出力 GiB（非圧縮） ÷ staging GiB | 0.08  |
| PUT GiB（圧縮後） ÷ staging GiB | 0.00  |
| PUT 回数 ÷ staging GiB | 617.60  |
| compaction 結果: delta_noop | 5  |
| compaction 結果: delta_tier | 73  |
| compaction 結果: delta_merge | 8  |
| compaction 結果: delta_prune | 7  |
| compaction 量: delta_blocks_nobase | 1  |
| compaction 量: delta_blocks_ratio | 59  |
| compaction 量: blocks_written | 61  |
| compaction 量: delta_bytes | 88,854,528  |
| compaction 量: delta_merge_bytes | 31,522,816  |
| compaction 量: delta_records | 2,545  |
| compaction 量: removed | 8,492  |
| 片付け: hint_overflow | 0  |
| 片付け: local_error | 436  |
| 片付け: local_retire_success | 10,828  |
| 片付け: remote_deferred | 0  |
| 片付け: remote_enqueued | 10,828  |
| meta ops 平均 ms | 22.19  |
| transaction 回数 | 3,805  |
| transaction 平均 ms | 59.15  |
| flush p50 ms | 0.00 |
| flush p99 ms | 621.30 |
| flush max ms | 1938.86 |
| read p50 ms | 0.38 |
| read p99 ms | 126.74 |
| read max ms | 17660.04 |
| write p50 ms | 0.17 |
| write p99 ms | 45.96 |
| write max ms | 17632.11 |
| 予定からの遅れの最大 ms | 17635.98 |
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

- pack-debug: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261028-070452/pack-debug`
