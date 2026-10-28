# pack で片付けの local_error が増える理由（2026-10-28）

[観測] は試験で測ったこと、[推論] は推定、[未確認] は確かめていないこと。

- 本体: `feat/kaz-staging-pack` 145fc631（binary `~/.local/bin/kaz-test/juicefs-pack-145fc631`）。
- 試験: pack の設定（delta、trigger 30、block-ratio 0.5、`--kaz-staging-pack`）に `--debug` を付け、本番の記録（`results/20261021-trace1/trace.tsv`）の最初の 600 秒だけを流した（`jfsreplay suite --head-seconds 600`、結果は `tests/juicefs/results/20261028-070452/`）。
- 解析した log: 試験の mount の log のうちこの回の分（1,492,924 行、SHA-256 の先頭 `ee2da247c7d82825`。試験の log なので保存せず、集計だけを残す）。
- 集計の道具と出力: [busy_ana.py](busy_ana.py)、[busy_ana.txt](busy_ana.txt)。

## 1. 失敗の中身 [観測]

- この回の `local_error` は 436、`local_retire_success` は 10,828。
- debug の行（`compaction GC local retirement ... err=`）で見ると、**436 件すべてが `slice has an in-flight staging upload`**。cache や stage の unlink の失敗は 0。
- 436 件はどれも 4 MiB 未満の slice で、pack が作った slice（507 個）は 1 つも含まれない。普通の書き込みの slice が、その PUT の最中に compaction で片付けられたもの。
- 時間の関係（436 件の p10／p50／p90、秒）:
  - commit から片付けまで: 4.6／9.4／18.0
  - PUT の開始から片付けまで: 0.2／1.5／3.4
  - 片付けから PUT の終わりまで: 0.4／2.2／3.5
- 片付けの前 10 秒以内に同じ chunk で pack があったのは 45 件だけ。pack の処理（chunk を押さえる）と直接ぶつかったものは少ない。
- 436 件はどれも、PUT の終わったあと「not needed, abandoned」で 1 回 DELETE された（`finishStagingUpload`）。object は残っていない。

例: slice 172075 は 07:07:40.5 に commit、07:07:45.9 ごろ PUT を始め、07:07:47.9 に片付け（in-flight）、07:07:48.7 に PUT が終わり、07:07:49.6 に DELETE。

## 2. なぜ pack で増えるか

- [観測] 待ち行列からの PUT の待ち時間の合計（`juicefs_staging_block_delay_seconds` の増分、2026-10-27〜28 の suite）:

  | 設定 | 待ちの合計 秒 | PUT 回数 | 1 回あたり（目安） |
  |---|---:|---:|---:|
  | trigger30 | 1,491,550／1,556,762 | 6,322／6,398 | 約 240 秒 |
  | pack | 116,497／128,748 | 4,015／4,055 | 約 30 秒 |
  | skipdel | 615,056／677,891 | 6,932／6,950 | 約 90 秒 |
  | pack-skipdel | 62,366／59,018 | 5,408／5,313 | 約 11 秒 |

  （1 回あたりは PUT 回数で割った目安。PUT 回数には待ち行列を通らない即時の PUT と pack の PUT も入るので、正確な平均ではない。）
- [推論] pack は 1 回の PUT で平均約 11 個の slice を待ち行列から外すので、待ち行列が速く流れ、待ちが約 4 分から約 30 秒に縮む。すると書いたばかりの slice が数秒で PUT され始め、commit から 10 秒前後で compaction（trigger 30）に片付けられるときに、PUT の最中に当たりやすくなる。skipdel も DELETE が減って待ち行列が速く流れるので、同じ向きに少し増える（約 105 → 約 155）。pack-skipdel は待ちが最も短く、最も多い（約 940）。
- つまり local_error は pack の不具合ではなく、「待ち行列が速く流れる」ことの副作用。この 436 件の PUT と DELETE は、どちらも無駄な API 呼び出しになっている（片付ける slice を書き出して、すぐ消している）。

## 3. 影響

- 正しさ: object は漏れない [観測: 436 件とも DELETE 済み、2026-10-28 の gc で leaked 0]。片付けの判断（durable marker）は残り、後のやり直しで消える作り。
- 無駄: 失敗 1 件につき、無駄な PUT 1 回と DELETE 1 回 [観測]。durable marker のやり直しでもう 1 回 DELETE するかは [未確認]（この 600 秒の log には出ていない）。
- skipdel の取りこぼし: PUT を始めた slice は、そもそも DELETE を省けない。local_error の分は「省けたはずの DELETE を取りこぼした」のではなく、「PUT が先に始まってしまった」もの [推論]。
- 待ち行列が速く流れることは、PUT＋DELETE の回数の面では逆向きに効く（片付ける前に PUT してしまう slice が増える）。upload-delay を少し付けて待たせると、この分を減らせるかを次の試験で確かめる（[suite-delay-ja.md](suite-delay-ja.md)）。
