# upload-delay 1 秒を足した試験（jfsreplay suite、2026-10-28）

[観測] は試験で測ったこと、[推論] は推定、[未確認] は確かめていないこと。

- 目的: skipdel だけで PUT が 9% 増えたのは「DELETE が減って待ち行列が速く流れ、片付ける前に PUT してしまう slice が増えたから」という見立てを、upload-delay を 1 秒付けて待たせたときと比べて確かめる。
- 本体・binary・記録・種は [suite-ja.md](suite-ja.md) と同じ（145fc631、`results/20261021-trace1/trace.tsv`）。
- 設定: 前回の 3 設定に `--upload-delay 1s` を足した `trigger30-delay1`、`skipdel-delay1`、`pack-skipdel-delay1`。各 1 回（前回 2 回の差が PUT で ±1% 以内だったので、ユーザーの指示で 1 回）。
- 結果の表: [summary-20261028-072616.md](summary-20261028-072616.md)。元は `tests/juicefs/results/20261028-072616/`。
- 道具の変更: `mount_flags.txt` の `--upload-delay 0` に `[compaction]` の印を付け、settings で置き換えられるようにした。原因調べのため `debug` も settings で付けられるようにした（`EXTRA_SETTINGS_FLAGS`）。

## 1. upload-delay 1 秒の効き方（source）

- `--upload-delay` が 1 分未満だと、待ち行列を見直す間隔（`scanDelayedStaging`）もその値になる（WARN を出す）。1 秒なら 1 秒ごと。
- delay が 0 でないと、書き込みのときに空いた枠で即 PUT する道（`beginImmediateStaging`）を通らない。どのブロックも待ち行列に入り、1〜2 秒たってから PUT の順番を待つ。
- fsync・書き込みの順序・read-after-write は変わらない（staging に書いた時点で durable、という扱いは同じ）。

## 2. 結果 [観測]

| 設定 | PUT | DELETE | PUT＋DELETE | 基準との差 | local_error |
|---|---:|---:|---:|---:|---:|
| trigger30（前回 2 回の平均） | 6,360 | 12,803 | 19,163 | — | 約 105 |
| trigger30-delay1 | 6,374 | 12,848 | 19,222 | +0% | 93 |
| skipdel（前回 2 回の平均） | 6,941 | 1,825 | 8,766 | −54% | 約 155 |
| skipdel-delay1 | 6,916 | 1,904 | 8,820 | −54% | 158 |
| pack-skipdel（前回 2 回の平均） | 5,361 | 2,180 | 7,540 | −61% | 約 940 |
| pack-skipdel-delay1 | **4,848** | **1,734** | **6,582** | **−66%** | 984 |

- 待ち行列の待ちの合計（`juicefs_staging_block_delay_seconds` の増分）: trigger30-delay1 1,485,686 秒（delay なし 約 1,524,000）、skipdel-delay1 641,110（約 646,000）、pack-skipdel-delay1 58,015（約 60,700）。1 秒の delay では待ちの長さはほとんど変わらない。
- pack-skipdel-delay1 の内訳（delay なしの 2 回の値）: まとめた回数 1,008（931／975）、まとめた slice 8,771（8,344／9,138）、省いた DELETE 15,362（14,661／14,777）、skip_no_peer 1,538（1,161／1,208）。
- 待ち時間: write の p99 は skipdel-delay1 で 87.7 ms、pack-skipdel-delay1 で 68.6 ms（delay なしは 36〜48 ms）。flush と read の p99 は変わらない。1 回ずつなので、ばらつきか delay の影響かは分からない [未確認]。

## 3. 読み取れること

- **skipdel の PUT +9% は、1 秒の delay では戻らない** [観測: 6,941 → 6,916]。[推論] 増えた分の原因は、DELETE が減って待ちが約 240 秒 → 約 90 秒（PUT 1 回あたりの目安）に縮んだことで、1〜2 秒待たせても待ちの長さはほぼ変わらないため。片付けは commit から 10 秒前後（中央値、[local-error-ja.md](local-error-ja.md)）に来るので、PUT の前に片付けを間に合わせるには、少なくとも十数秒〜数十秒の delay が要ると見込む [推論、未確認]。
- **pack と組み合わせると、1 秒でも効く** [観測: pack-skipdel の PUT −10%、DELETE −20%、合計で基準比 −61% → −66%]。[推論] delay が 0 だと、枠が空いていれば書いた瞬間に即 PUT され（まとめにも DELETE の省略にも乗らない）、1 秒待たせるとそれが全部待ち行列を通るため。省いた DELETE が約 +600、まとめた回数が約 +5% 増えている。
- **local_error は delay 1 秒では変わらない** [観測]。理由は [local-error-ja.md](local-error-ja.md)（待ち行列が速く流れて、片付けの時に PUT の最中に当たる）。
- trigger30 には 1 秒の delay は効かない（PUT・DELETE とも ±1% 以内）。

## 4. pack-skipdel で delay 2 秒・5 秒（2026-10-28 追加）

ユーザーは長い遅延を好まないので、pack-skipdel で短めの delay を比べた。各 1 回。表: [summary-20261028-103939.md](summary-20261028-103939.md)（元は `tests/juicefs/results/20261028-103939/`）。最初の実行はアプリの終了で suite が途中で止まったので、残った複製を消して流し直した。

| 設定 | PUT | DELETE | PUT＋DELETE | 基準（trigger30）との差 | local_error | 省いた DELETE | まとめた回数／slice |
|---|---:|---:|---:|---:|---:|---:|---:|
| pack-skipdel（delay 0、2 回の平均） | 5,361 | 2,180 | 7,540 | −61% | 約 940 | 約 14,700 | 約 953／8,741 |
| delay 1 秒 | 4,848 | 1,734 | 6,582 | −66% | 984 | 15,362 | 1,008／8,771 |
| delay 2 秒 | 4,603 | 1,471 | 6,074 | −68% | 693 | 15,674 | 1,020／9,088 |
| delay 5 秒 | **3,899** | **858** | **4,757** | **−75%** | **238** | 16,487 | 1,045／8,453 |

[観測]
- delay を長くするほど PUT も DELETE も減る。5 秒で PUT は基準の 6,360 から −39%、DELETE は 12,803 から −93%。
- local_error は 5 秒で 238 まで下がった（delay 0 は約 940）。片付けが PUT の前に間に合うようになった分。
- 待ちの合計（`juicefs_staging_block_delay_seconds`）は 61,481（2 秒）、64,003（5 秒）で、delay 0 の約 60,700 とほぼ同じ。
- 待ち時間: write の p99 は 42.8 ms（2 秒）、69.3 ms（5 秒）、write の max は 3.2 s／9.1 s。flush と read の p99 は変わらない（flush p99 約 2.0 s、read p99 約 114 ms）。前回の delay なしの回でも write max が 11〜18 s の回があり、1 回ずつでは delay の影響と言えない [未確認]。

[推論]
- 片付けは commit から 10 秒前後（中央値、p10 は約 5 秒）に来るので、5 秒待たせると、その前に片付く slice の PUT（と、その後の DELETE）がまるごと無くなる。2 秒ではその一部だけ。
- 待ちの合計がほぼ変わらないのは、delay で待たせる分と、PUT が減って待ち行列が空く分が打ち消し合うため。

## 5. compaction の trigger 50・100（pack、skipdel、delay 5 秒、2026-10-28 追加）

upload-delay 5 秒で効くようになったので、compaction の時期の影響を見た。trigger だけを 30 → 50／100 に変え、ほかは §4 の delay 5 秒と同じ。各 1 回。表: [summary-20261028-114745.md](summary-20261028-114745.md)（元は `tests/juicefs/results/20261028-114745/`）。

| 項目 | trigger 30 | trigger 50 | trigger 100 |
|---|---:|---:|---:|
| PUT | 3,899 | 3,824 | 3,837 |
| DELETE | 858 | 760 | 721 |
| PUT＋DELETE（基準 19,163 との差） | 4,757（−75%） | 4,584（−76%） | 4,558（−76%） |
| GET | 270 | 306 | 391 |
| local_error | 238 | 210 | 171 |
| 省いた DELETE | 16,487 | 16,427 | 16,559 |
| まとめた回数／slice | 1,045／8,453 | 1,056／9,427 | 1,067／10,011 |
| compaction 出力 slice 数 | 248 | 205 | 192 |
| delta_tier／delta_merge | 141／30 | 103／27 | 74／23 |
| compaction で消した slice（removed） | 16,670 | 14,866 | 14,147 |
| 待ちの合計 秒 | 64,003 | 65,498 | 64,581 |
| write p99／max ms | 69.3／9,123 | 34.5／3,267 | 50.3／14,846 |
| read p99／max ms | 113.6／9,151 | 110.4／1,949 | 128.0／15,144 |
| flush p99 ms | 2,036 | 1,972 | 2,046 |

[観測]
- PUT＋DELETE は trigger を 50・100 に上げても −75% → −76% で、ほとんど変わらない。PUT は ±2% 以内、DELETE は 858 → 760 → 721（−11%／−16%）。
- trigger を上げると compaction の回数（delta_tier 141 → 74）と出力（248 → 192 slice）が減る一方で、pack がまとめる slice は 8,453 → 10,011 に増える。compaction が遅いぶん、pack が先にまとめる。
- 片付けの回数（local_retire_success）は約 17,100〜17,200 でどれも同じ。
- GET は 270 → 306 → 391 に増える（trigger 100 で +45%）。
- 待ち時間: trigger 100 の回で read max 15.1 s、write max 14.8 s、予定からの遅れ最大 16.3 s。p99 は変わらない（flush 約 2.0 s、read 110〜128 ms）。delay なしの前回でも write max 11〜18 s の回があったので、1 回では trigger の影響とは言えない [未確認]。

[推論]
- pack＋skipdel＋delay 5 秒では、PUT と DELETE はもう trigger にほとんど左右されない。PUT は pack が待ち行列の中でまとめた後の数で決まり、DELETE の大半は skipdel で省かれる（約 16,500 でどれも同じ）。trigger 30 で残る DELETE の差（約 140）は、compaction の出力 slice そのもの（248 → 192）と、その後の片付けの DELETE。
- trigger を上げると、compaction の代わりに pack が slice をまとめる。ただし pack は chunk の中の slice を 1 つずつ数えるだけで、ファイルの中の断片を減らす働きは compaction ほどない。GET の増加は、断片が多いまま読まれるためと見込む [推論、未確認：GET の中身は分けていない]。
- PUT＋DELETE の点では trigger 30 と 50・100 の差は約 4%（約 200 回）で小さい。GET の増加（+36／+121 回）を差し引くと、得はさらに小さい。

## 6. 限界

- 各 1 回。前回の 2 回の差は PUT で ±1% 以内、DELETE で ±4% 以内だったので、PUT の −10% は差として読める。trigger の比べでは PUT の差（±2%）は誤差の範囲、DELETE の −11%／−16% は差として読める。write p99 の上がり方と max は 1 回では判断できない。
- delay は 1・2・5 秒だけ。もっと長い delay（10〜60 秒）は試していない（ユーザーは長い delay を好まない）。
- trigger は 30・50・100 だけ（どれも block-ratio 0.5、delay 5 秒）。長い時間の断片の増え方（read の遅さ、メタデータの大きさ）は、この 1,038 秒の記録では見えない。
