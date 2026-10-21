# suite の結果 20261021-201742

- trace: `results/20261021-trace1/trace.tsv`、age: 0、設定: block-ratio75

| 項目 | block-ratio75 |
|---|---|
| 結果 | 失敗 |
| flush p50 ms | 0.00 |
| flush p99 ms | 1925.79 |
| flush max ms | 2946.32 |
| read p50 ms | 0.27 |
| read p99 ms | 109.91 |
| read max ms | 1842.54 |
| write p50 ms | 0.16 |
| write p99 ms | 39.40 |
| write max ms | 3231.87 |
| 予定からの遅れの最大 ms | 3741.28 |
| play のエラー数 | 0 |

## 失敗・注意
- block-ratio75（失敗）: mount を外せない: 試験の metrics の port 127.0.0.1:9568 が空かない

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

- block-ratio75: `/home/kwatanabe/tmp_local/juicefs_inspection/tests/juicefs/results/20261021-201742/block-ratio75`
