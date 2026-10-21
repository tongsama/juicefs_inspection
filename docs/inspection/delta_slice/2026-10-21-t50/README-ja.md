# delta の trigger 50 の実機計測（2026-10-21 10:59〜）

目的: `--kaz-compaction-delta-trigger 50`（compaction を早める）を、前の trigger 99（既定）と比べる。前の計測は [2026-10-21](../2026-10-21/README-ja.md)。

## 条件 [観測]

- mount: 10:59:01（PID 1331980）。ログは `~/.juicefs/diagnostics/vm-io-20261021-105853.log`。
- バイナリは `~/.local/bin/juicefs`（sha256 `2b243f78…`、1.4.1+2026-10-21.7336466c）。前の計測は 67c5c96d で、違いはノブと metrics を足したことだけ。既定値のままなら、計画は同じになる（レビューで確認）。
- 起動時のログ: `kaz compaction mode: delta (tiers 8, max-records 500, gap 16384, min-file-size 1073741824, trigger 50, block-ratio 0.5)`、`kaz compaction policy: read-min=100`。
- 設定は `<mountpoint>/.config` から写した（`config.json`。接続先や鍵らしい項目は伏せた）。前の計測とは、`KazCompactionDeltaTrigger` が違うだけ（前は項目がなく、99 で固定）。
  - 前の mount の `.config` は取っていない [未確認]。前の計測の起動時のログでは、trigger と block-ratio 以外の値が同じ。

## 記録（前の計測と同じ材料）

| ファイル | 内容 | 常駐 |
|---|---|---|
| `metrics.tsv` | `/metrics` を 60 秒ごと（前と同じ `metrics_sampler.sh`） | PID 1332183 |
| `llen.tsv` | VM ディスクの全 chunk の LLEN の分布を 10 分ごと | PID 1332185 |
| `keys-snapshot-1792547973.tsv`、`keys-events.tsv` | キャッシュのファイルの出入りを 5 分ごと（`key_tracker.py`） | PID 1332187 |
| `metrics-start-105932.txt` | 開始の時点の `/metrics` の全体 | — |
| `*-start-1021105938.*` | 開始の時点のキャッシュの内訳、ファイルの一覧、kaz-2q の管理ファイル、metrics | — |
| `config.json` | `.config` の写し | — |

- 前の計測の常駐（PID 1154836、1155339、1272478）は 10:59 に止めた。
- VM を再起動する直前には `./pre_reboot_capture.sh` を実行する。起動の時刻は 4 つ記録する。
- 止めるとき: `kill 1332183 1332185 1332187`

## 比べる区間

- 前の trigger 99:
  - 昼の 07:27〜10:52（`../2026-10-21/metrics.tsv`）
  - 夜の 00:48〜07:10
- 起動:
  - mount 直後の起動は、前の 00:42 と比べる。
  - 一晩たった後の起動は、前の 07:22 と比べる。
- 比べる量は staging 1 GiB あたり: PUT の回数とバイト、片付けの回数（`local_retire_success`）、compaction の出力。ほかに、メタデータの操作。

## データファイルの圧縮（2026-10-21）

500 KB を超える記録（`metrics.tsv`、`keys-events.tsv`、`keys-snapshot-*.tsv`、`files-*.csv`、`state-*.v1`）は、commit の前に gzip で圧縮して `.gz` にした。`compare_runs.py` や `jfsmeasure compare` は `.gz` をそのまま読める。ほかの道具で使うときは、先に `gunzip -k` で展開する。
