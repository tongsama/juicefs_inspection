| 項目 | vanilla | kaz-all |
|---|---:|---:|
| PUT 回数 | 17,063 | 4,057 |
| PUT 平均 ms | 3,922.2 | 2,765.8 |
| GET 回数 | 3,777 | 361 |
| GET 平均 ms | 2,579.5 | 1,102.3 |
| DELETE 回数 | 30,764 | 989 |
| DELETE 平均 ms | 1,172.9 | 1,011.6 |
| PUT＋DELETE | 47,827 | 5,046 |
| PUT＋GET＋DELETE | 51,604 | 5,407 |
| staging 書込 GiB | 4.9 | 4.9 |
| PUT 1 回あたりの staging MiB | 0.292 | 1.2 |
| 待ち行列の待ちの合計 秒 | 7,415,945.9 | 60,384.8 |
| staging の block 数 最大 | 38,362 | 1,314 |
| staging の量 最大 MiB | 3,966.8 | 1,299.6 |
| 流し終えてから staging が空になるまで 秒 | - | 36.3 |
| 落ち着くまで 秒（suite の判定） | 601.3 | 65.2 |
| キャッシュ hit 率（回数） | 0.972 | 0.996 |
| キャッシュ hit 率（バイト） | 0.885 | 0.999 |
| キャッシュ miss 回数 | 3,938 | 361 |
| キャッシュ evict 回数 | 13,583 | 0 |
| キャッシュ書込 GiB | 74.9 | 5.9 |
| FUSE ops | 272,505 | 179,982 |
| FUSE 平均 ms | 133.5 | 6.7 |
| meta ops | 164,079 | 53,254 |
| meta 平均 ms | 27.4 | 22.1 |
| transaction 回数 | 63,025 | 6,740 |
| transaction 平均 ms | 46.7 | 45.2 |
| compaction 出力 slice 数 | 884 | 262 |
| compaction 出力 GiB | 55.2 | 0.500 |
| 最後に参照されている slice 数 | 17,380 | 7,478 |
| chunk 数 | 632 | 632 |
| slice ÷ chunk | 27.5 | 11.8 |
| キャッシュの中身 full GiB | 8.4 | 15.9 |
| キャッシュの中身 partial GiB | 1.3 | 0.230 |
| キャッシュの中身 hidden GiB | 0.040 | 0.080 |
| キャッシュの中身 new GiB | 0.110 | 0.210 |
| write p50 ms | 2,428.8 | 0.173 |
| write p99 ms | 10,958.4 | 31.5 |
| write max ms | 26,060.5 | 3,227.8 |
| read p50 ms | 69.7 | 0.272 |
| read p99 ms | 5,701.9 | 106.4 |
| read max ms | 24,128.3 | 1,827.9 |
| flush p50 ms | 0.003 | 0.003 |
| flush p99 ms | 3,211.0 | 1,977.8 |
| flush max ms | 5,821.8 | 3,048.2 |
| 予定からの遅れ p99 ms | 2,417,669.6 | 202.4 |
| 予定からの遅れ max ms | 2,483,658.4 | 3,742.0 |
| play のエラー | 0 | 0 |
| staging の書込エラー | 0 | 0 |
| 片付け event="hint_overflow" | - | 0 |
| 片付け event="local_error" | - | 344 |
| 片付け event="local_retire_success" | - | 17,037 |
| 片付け event="remote_deferred" | - | 0 |
| 片付け event="remote_enqueued" | - | 17,037 |
