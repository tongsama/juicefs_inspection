| 項目 | default(99) | trigger30 | trigger50 | trigger200 | block-ratio75 | block |
|---|---|---|---|---|---|---|
| 設定 Chunk.KazWholeReadMax | 1048576 | 1048576 | 1048576 | 1048576 | 1048576 | 0 |
| 設定 Meta.KazCompactionDeltaBlockRatio | 0.5 | 0.5 | 0.5 | 0.5 | 0.75 | 0.5 |
| 設定 Meta.KazCompactionDeltaTrigger | 99 | 30 | 50 | 200 | 99 | 50 |
| 設定 Meta.KazCompactionMode | delta | delta | delta | delta | delta | block |
| 設定 Meta.Sid | 9 | 10 | 11 | 12 | 14 | 15 |
| 窓の長さ h | 0.47 | 0.42 | 0.45 | 0.45 | 0.45 | 0.45 |
| mount のし直し（uptime の巻き戻り）回数 | 0 | 0 | 0 | 0 | 0 | 0 |
| staging 書込 GiB | 4.86 | 4.86 | 4.86 | 4.86 | 4.86 | 4.86 |
| compaction 出力 GiB（非圧縮） | 0.46 | 0.48 | 0.47 | 0.41 | 0.42 | 2.06 |
| compaction 出力 slice 数 | 192 | 242 | 207 | 148 | 150 | 527 |
| PUT GiB（圧縮後） | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.01 |
| PUT 回数 | 7,493 | 6,305 | 6,809 | 7,960 | 7,537 | 7,316 |
| GET GiB | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| GET 回数 | 424 | 395 | 394 | 397 | 334 | 457 |
| DELETE 回数 | 12,814 | 13,761 | 13,613 | 11,602 | 12,767 | 13,510 |
| cache 書込 GiB | 5.33 | 5.36 | 5.33 | 5.28 | 5.29 | 6.93 |
| FUSE ops | 185,711 | 187,542 | 187,629 | 186,996 | 188,076 | 188,518 |
| meta ops | 55,288 | 54,069 | 54,121 | 54,447 | 54,637 | 54,455 |
| compaction 出力 GiB（非圧縮） ÷ staging GiB | 0.09 | 0.10 | 0.10 | 0.08 | 0.09 | 0.42 |
| PUT GiB（圧縮後） ÷ staging GiB | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| PUT 回数 ÷ staging GiB | 1543.16 | 1298.30 | 1402.06 | 1639.08 | 1552.00 | 1506.47 |
| compaction 結果: partial | - | - | - | - | - | 89 |
| compaction 結果: full | - | - | - | - | - | 0 |
| compaction 結果: noop | - | - | - | - | - | 8 |
| compaction 結果: delta_noop | 11 | 5 | 13 | 7 | 3 | - |
| compaction 結果: delta_tier | 95 | 149 | 113 | 62 | 92 | - |
| compaction 結果: delta_merge | 4 | 7 | 4 | 0 | 3 | - |
| compaction 結果: delta_fold | 0 | 0 | 0 | 1 | 1 | - |
| compaction 結果: delta_prune | 0 | 3 | 4 | 3 | 0 | - |
| compaction 量: delta_blocks_nobase | 2 | 1 | 2 | 1 | 2 | - |
| compaction 量: delta_blocks_ratio | 74 | 76 | 76 | 70 | 36 | - |
| compaction 量: delta_fold_blocks | 0 | 0 | 0 | 1 | 1 | - |
| compaction 量: blocks_written | 80 | 79 | 80 | 74 | 43 | 507 |
| compaction 量: delta_bytes | 130,561,024 | 168,125,440 | 137,905,152 | 100,234,240 | 249,156,608 | - |
| compaction 量: delta_merge_bytes | 21,684,224 | 38,805,504 | 20,221,952 | 0 | 40,468,480 | - |
| compaction 量: delta_records | 4,351 | 5,518 | 4,556 | 3,093 | 4,389 | - |
| compaction 量: removed | 14,586 | 17,128 | 15,500 | 11,907 | 14,261 | 12,319 |
| 片付け: hint_overflow | 0 | 0 | 0 | 0 | 0 | 0 |
| 片付け: local_error | 90 | 70 | 93 | 119 | 112 | 97 |
| 片付け: local_retire_success | 13,151 | 14,161 | 13,924 | 11,857 | 13,110 | 13,844 |
| 片付け: remote_deferred | 0 | 0 | 0 | 0 | 0 | 0 |
| 片付け: remote_enqueued | 13,151 | 14,161 | 13,924 | 11,857 | 13,110 | 13,844 |
| meta ops 平均 ms | 22.09 | 21.96 | 22.01 | 22.23 | 21.86 | 21.82 |
| transaction 回数 | 4,695 | 4,808 | 4,743 | 4,649 | 4,668 | 5,035 |
| transaction 平均 ms | 56.88 | 45.54 | 46.20 | 57.68 | 53.22 | 47.01 |
