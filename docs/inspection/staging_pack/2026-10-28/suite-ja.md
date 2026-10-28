# staging pack と DELETE の省略の試験（jfsreplay suite、2026-10-27〜28）

[観測] は試験で測ったこと、[推論] は推定、[未確認] は確かめていないこと。

- 本体: `feat/kaz-staging-pack` 145fc631（1.4.2-improve-kaz d246b0f7 から 11 commit）。binary `~/.local/bin/kaz-test/juicefs-pack-145fc631`（sha256 `e1eeaedb…`、`1.4.2+2026-10-27.145fc631`）。
- 記録: 2026-10-21 の本番の記録（`results/20261021-trace1/trace.tsv`、SHA-256 の先頭 `0301656afd02c6fd`、168,259 操作、約 17 分）。
- 種: 2026-10-27 に作り直した（`seed --written-only --create-bucket`、316 chunk、書き出し 280 秒）。温めたキャッシュの写しも作り直した。
- 設定: どれも delta、trigger 30、block-ratio 0.5。違いは `--kaz-staging-pack` と `--kaz-skip-unuploaded-delete` だけ。各 2 回。
- 結果の表（suite の出力そのまま）: [summary-20261027-215915.md](summary-20261027-215915.md)。元のディレクトリは `tests/juicefs/results/20261027-215915/`（git には入れない）。
- 本番の VM は止め、本番の JuiceFS は外した状態で流した（rclone は本番と共有）。

## 1. 結果 [観測、各 2 回の平均]

| 設定 | PUT 回数 | 差 | DELETE 回数 | 差 | PUT＋DELETE | 差 |
|---|---:|---:|---:|---:|---:|---:|
| trigger30（基準） | 6,360 | — | 12,803 | — | 19,163 | — |
| pack | 4,035 | **−37%** | 10,197 | −20% | 14,232 | −26% |
| skipdel | 6,941 | +9% | 1,825 | **−86%** | 8,766 | −54% |
| pack-skipdel | 5,361 | −16% | 2,180 | −83% | 7,540 | **−61%** |

- 2 回の差は小さい（PUT は各設定で ±1% 以内、DELETE も ±4% 以内）。
- 全部の回が「成功」（落ち着いた）。play のエラー 0、mount のし直し 0。
- 試験の volume の `juicefs gc`（数えるだけ）: 5,919 object、参照あり 5,056、削除待ち 863（1.65 MB）、**参照の無い object 0**（[gc-dry.txt](gc-dry.txt)）。

## 2. 読み取れること

- **pack は PUT の回数を約 37% 減らした** [観測]。模擬の見込み（trigger 30 で −28%、[調査報告 §4.3](../2026-10-27/feasibility-ja.md)）より大きい。まとめ 1 回あたり平均約 11 個の slice（pack の 1 回目の途中の値: 246 回で 2,692 slice）。
- **skipdel は DELETE を約 86% 減らした** [観測]。片付けた slice への DELETE のほとんどが、一度も PUT していない object だったという見立て（調査報告 §5）と合う。
- **skipdel だけだと PUT が 9% 増えた** [観測]。[推論] DELETE が減って Drive への要求の取り合いが減り、アップロードの待ち行列が早く流れたため、アップロード前に片付けられる slice が減った（窓の長さが 0.40 h → 0.32 h と短くなり、compaction の出力 slice も 250 → 290 に増えている）。PUT と DELETE の合計では −54%。
- **pack と skipdel の両方で、PUT＋DELETE が 61% 減った** [観測]。ただし PUT だけで見ると pack 単独（−37%）より小さい（−16%）。[推論] 待ち行列が早く流れるほど、まとめる相手（待っている slice）が減るため。
- 費用 [観測]:
  - transaction の回数は pack で約 +37%（4,715 → 6,470）。まとめの置き換えの分。meta ops 平均と transaction 平均の時間はほぼ変わらない。
  - 差分の段のまとめ直し（delta_merge）が 7 → 20〜30 回、そのバイト（delta_merge_bytes）が約 2〜3 倍。設計 §5.5 の見込みどおり。compaction の出力（非圧縮）の合計は 0.49 → 0.43〜0.47 GiB で、増えていない。
  - cache 書込は pack で約 +8%（5.36 → 5.8 GiB）。まとめた slice を read cache に入れる分。
  - 待ち時間（write／read の p99）は変わらない。
- **片付けのローカルのエラー（local_error）が pack で増えた** [観測]: trigger30 は約 105、pack は約 620、pack-skipdel は約 940（skipdel だけは約 155）。理由は後で調べた（[local-error-ja.md](local-error-ja.md)。すべて PUT の最中に片付けたもの）。失敗した片付けは durable marker として残り、後でやり直す作りなので、object は漏れない（gc で leaked 0）。gc の「削除待ち 863」はこの残りと考えられる [推論]。（訂正 2026-10-28: 「省ける DELETE の一部を取りこぼしている可能性」と書いたが、local_error の slice はどれも PUT を始めていたので、もともと DELETE を省けない。）

## 3. 限界

- 各設定 2 回、記録は 1 つ（起動と apt upgrade を含む 18 分）。
- 種と書き込みの中身は作り物なので、PUT のバイト（圧縮後）は比べられない（表では 0.00）。
- 試験の rclone と Drive は本番と共有。Drive の応答の時間切れは回ごとに数件あった（warnings）。
- 前回（2026-10-21）の trigger30（v1.4.1 系 7336466c、PUT 6,305）と今回の trigger30（v1.4.2 系、PUT 6,322／6,398）はほぼ同じ。

## 4. 今回の実行で起きたこと（記録）

- 最初の実行（20:48）は、試験のバケット `juicefs-kaz-test` がユーザーの手で消されていた（前回の試験の後）のに、試験の Redis にメタデータが残っていたため、種の object がすべて 404 になった。delta compaction が全部失敗し（「not found」はログに出ない）、record が 2,500 を超えて同期の compaction（1 回 1〜2 分）が続き、書き込みが詰まった。結果は捨てた（`suite-pack-20261027-nobucket.log`）。
- `testvol destroy` はバケットが無いと `juicefs destroy` が止まるので、`--force` を付けて同じ手順を流した。
- rclone serve s3 は、Drive で手で消したフォルダの ID を持ち続け（Dir.Mkdir が 404）、format の試しの PUT が 500 になった。SIGHUP では直らず、rclone の再起動で直った（ユーザーが本番の JuiceFS を外して再起動した）。
- 道具の改善の候補: suite の前に、試験のバケットと種の object があるかを確かめる。
