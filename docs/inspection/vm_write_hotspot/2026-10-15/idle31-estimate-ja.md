# `--slice-flush-idle 31s`（`--slice-flush-wait 60s`）の机上見積もり

2026-10-15。2026-10-13 の計測（`--slice-flush-wait 30s`、`--slice-flush-idle 16s`、`--writer-reuse-window 32`、`--writer-flush-scope=range`、対象 inode 627902）のログで、idle で閉じた slice が閉じずに残っていたら、後の書き込みを吸収できたかを数えた。

## 入力 [事実]

| 証跡 | SHA256 |
|---|---|
| DEBUG ログ `~/.juicefs/diagnostics/vm-io-20261013-195837.log` の固定 prefix 226,806,428 byte | `5b9f68906f119f9a0975aec23cf763a940f88aa6988b5b7126d60f3ff97f0d0f`（2026-10-13 報告と一致） |
| `../2026-10-13/accesslog-20261013-200150.txt.gz` の展開後 | `6c079f0bcd2d2b17a9f0a118021a4fed42531bc80014885223e5e1c80b639f22`（一致） |

スクリプト `idle_extend.py`、出力 `idle_extend.out`。区間は 2026-10-13 報告と同じ（起動 20:02〜20:04、アプリ 20:04〜20:18:05）。

## モデル [ソースの条件と仮定]

- 吸収の条件は `findWritableSlice`（`pkg/vfs/writer.go:191`）: 書き込み位置が `[off + flushoff, off + slen]`（slice の末尾のブロック内）。
- 閉じる時刻は「最後の書き込み + 31s」「作成 + 60s」「同じ chunk の explicit_flush」の最も早いもの。
- 扱っていない: writable_window による追い出し、slice_pressure、延長した slice どうしの連鎖、chunk をまたぐ書き込み。idle の slice は1つずつ独立に試算した。

## 結果 [机上の推定]

| 区間 | idle で閉じた slice | 延長で育つ slice | 吸収した書き込み（うち元の最終書き込みから31秒以内） | 吸収した byte | 不要になる実 slice |
|---|---:|---:|---:|---:|---:|
| 起動 | 78 | 9（11.5%） | 13（13） | 176 KiB | 5（freeze 1,593 の 0.3%） |
| アプリ | 2,763 | 101（3.7%） | 268（169） | 3.2 MiB | 138（freeze 5,956 の 2.3%） |

- 延長した後の閉じ方（アプリ）: idle 31s 2,610、age 60s 79、explicit 74。
- 上限の目安（アプリ）: 隙間の時間に同じ chunk への書き込みがあったのは 639／2,763。位置が ±64 KiB 以内のものは 148。
- 「不要になる実 slice」とは、最初の書き込みが延長した slice に吸収される実 slice のこと。中身が全部吸収されるとは限らないので、上限寄りの数字。

## 解釈 [推論]

idle を 16s から 31s にしても、idle で閉じた slice の 96% は、閉じるのが15秒遅れるだけになる。slice と freeze の減少はアプリ区間で約2%（138件）、起動では 0.3% と見込む。書き込みが 4 KiB 単位で散っているので、末尾に続く書き込みはまれだった。PUT が減る効果は小さく、未 commit のデータが長く残る、commit が遅れるといった不利な点の方が大きい。

未検証: 実機での PUT 回数、writable_window・slice_pressure との関係、16 分を超える負荷や別のゲスト設定での結果。
