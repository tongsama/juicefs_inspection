| 項目 | 前回 夜間 21:38-06:05 | 前回 01-06時 | 今回 0:48:30-07:10 | 今回 01-06時 |
|---|---|---|---|---|
| 窓の長さ h | 8.45 | 5.00 | 6.37 | 5.00 |
| uptime の差 h | 8.44 | 4.99 | 6.37 | 4.99 |
| staging 書込 GiB | 5.00 | 1.59 | 3.12 | 2.50 |
| compaction 出力 GiB（非圧縮） | 32.65 | 15.14 | 1.74 | 1.37 |
| compaction 出力 slice 数 | 8344 | 3875 | 1233 | 1007 |
| PUT GiB（圧縮後） | 7.69 | 3.16 | 1.16 | 0.88 |
| PUT 回数 | 138163 | 83027 | 96404 | 80634 |
| GET GiB | 5.48 | 0.18 | 0.24 | 0.07 |
| GET 回数 | 5796 | 373 | 474 | 199 |
| DELETE 回数 | 193226 | 112047 | 106694 | 88603 |
| cache 書込 GiB | 37.35 | 16.45 | 5.07 | 3.80 |
| FUSE ops | 2018350 | 1032077 | 2159041 | 1694074 |
| meta ops | 0 | 0 | 1021441 | 799491 |
| compaction 出力 GiB（非圧縮） ÷ staging GiB | 6.53 | 9.53 | 0.56 | 0.55 |
| PUT GiB（圧縮後） ÷ staging GiB | 1.54 | 1.99 | 0.37 | 0.35 |
| PUT 回数 ÷ staging GiB | 27618.06 | 52258.10 | 30898.76 | 32270.68 |

今回 0:48:30-07:10 の kaz compaction（差分）:
- amount_total{kind="blocks_written"}: 215
- amount_total{kind="delta_bytes"}: 963100672
- amount_total{kind="delta_records"}: 23388
- amount_total{kind="removed"}: 125445
- events_total{event="delta_hidden_full"}: 1
- events_total{event="delta_merge"}: 56
- events_total{event="delta_noop"}: 15
- events_total{event="delta_prune"}: 31
- events_total{event="delta_tier"}: 969

今回 01-06時 の kaz compaction（差分）:
- amount_total{kind="blocks_written"}: 161
- amount_total{kind="delta_bytes"}: 790577152
- amount_total{kind="delta_records"}: 19020
- amount_total{kind="removed"}: 103254
- events_total{event="delta_merge"}: 46
- events_total{event="delta_noop"}: 11
- events_total{event="delta_prune"}: 26
- events_total{event="delta_tier"}: 804
