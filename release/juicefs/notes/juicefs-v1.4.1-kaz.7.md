# juicefs-v1.4.1-kaz.7

juicefs-v1.4.1-kaz.6 に、ディスクキャッシュの新しい追い出し方式 **kaz-2q** を加えた版です。kaz.6 までの改修は、すべてそのまま含みます（[juicefs-v1.4.1-kaz.6](https://github.com/tongsama/juicefs_inspection/releases/tag/juicefs-v1.4.1-kaz.6) のリリースノートを参照）。

## 背景

長時間使っていると、OS やアプリの起動が遅くなる問題がありました。調べたところ、原因は無効なキャッシュの残留ではありませんでした。今までの lru では、書き込みと compaction の出力の流入（通常の使用で 2 時間に約 30 GiB）が、起動やアプリで読むブロックを押し出していました。半日使った後の VM の再起動では、起動中に GET で取り直したブロック（3.45 GiB）が、すべて当日キャッシュにあって追い出されたものでした。

## 追加したオプション（どれも既定では今までと同じ動き）

- `--cache-eviction kaz-2q`
  - キャッシュを「新入り用」と「常連用」に分けます（2Q）。新しく入ったブロック（書き込み、compaction の出力、GET で取ったもの）は新入り用に入ります。新入り用にいる間にもう一度読まれたブロックだけが、常連用に移ります。追い出しは新入り用から行います。
  - 追い出したブロックの名前を「追い出し済みリスト」に覚えておき、後でまた読まれたら、常連用に直接入れます。
  - 区分、最後に読んだ時刻、追い出し済みリストは、キャッシュのディレクトリの直下の管理ファイル `kaz-cache-state.v1`（`raw/` の外）に保存し、mount し直しても引き継ぎます。
  - compaction の読み込みは、ユーザーの読み込みとして数えません（昇格させない、最後に読んだ時刻を更新しない、GET した元のブロックをキャッシュに入れない）。`juicefs compact` も同じです。
  - メモリのキャッシュ（`--cache-dir memory`）では使えません（WARN を出して `2-random` になります）。
- `--kaz-cache-probation-ratio=<割合>`（既定 `0.4`）: 新入り用の取り分。
- `--kaz-cache-promote-delay=<時間>`（既定 `10m`）: 新入り用に入ってから、この時間より後に読まれたら常連用に移す。
- `--kaz-cache-ghost-ratio=<倍数>`（既定 `4`）: 追い出し済みリストの容量（キャッシュの容量の倍数。0 で無効）。
- `--kaz-cache-state-interval=<時間>`（既定 `5m`）: 管理ファイルの保存の間隔。umount のときにも保存します。
- `--kaz-cache-compaction-placement=inherit|probation|replace|share`（既定 `inherit`）: compaction の出力をどちらに入れるか。
  - `inherit`: 元のブロックのどれかが常連用なら常連用。
  - `probation`: 常に新入り用。
  - `replace`: 常連用の元のブロックを丸ごと写した出力だけ常連用。
  - `share`: 出力のうち常連用の元から来たデータの割合が、次のしきい値以上なら常連用。
  - どの値でも、元のブロックが一つもキャッシュに無ければ、出力はキャッシュに入れません。
- `--kaz-cache-compaction-placement-share-ratio=<割合>`（既定 `0.5`）: `share` のしきい値。
- `--kaz-cache-demote-unread-first`（既定オフ）: 常連用があふれたとき、常連用に入ってから読まれていないブロックを先に降格します。
- `--kaz-cache-compaction-hot-age=<時間>`（既定 `0` = 無効）: 元のブロックが、この時間内に作られた compaction の出力なら、常連用の元として扱いません。何度も作り直される範囲（VM のディスクの inode table など）の出力が、常連用を圧迫しないようにします。

## 計測結果（VM の再起動、半日使って流入 30 GiB の後）

rclone serve s3 → Google Drive、`--writeback`、キャッシュ 20G の構成です。各条件 1 回ずつの計測です。

| 設定 | ログインまで | アプリが使えるまで | 起動中の GET | 起動中に GET で取り直した量 |
|---|---:|---:|---:|---:|
| lru（kaz.6 と同じ） | 13.5 分 | 30.5 分 | 1,815 件 | 3.45 GiB |
| kaz-2q、probation-ratio 0.25、placement inherit | 6.2 分 | 11.4 分 | 1,097 件 | 1.03 GiB |
| 同、placement probation | 3.6 分 | 7.9 分 | 593 件 | 0.68 GiB |
| 同、placement share 0.5＋demote-unread-first | 2.5 分 | 8.7 分 | 876 件 | 0.90 GiB |

- 目標にしていた「取り直し 0.35 GiB 以下」には、どの設定も届いていません。
- 最後に加えた `--kaz-cache-compaction-hot-age` は、30 分の軽い確認だけです。常連用のうち compaction の出力の容量が増え続ける動きが止まることは確かめましたが、再起動での判定はしていません。
- 流入の大半は、block モードの compaction が、小さい書き込みの触れた 4 MiB のブロックを丸ごと書き直す分です（夜間は書いた量の約 10 倍）。この増幅そのものへの対処は、次の版以降の課題です。

詳細は [調査の報告](https://github.com/tongsama/juicefs_inspection/blob/juicefs-v1.4.1-kaz.7/docs/inspection/cache_churn/2026-10-19/report-ja.md) を参照してください。

作者の環境（VM のディスク）では、次の組み合わせで使っています。

```
--cache-eviction kaz-2q --kaz-cache-probation-ratio 0.25 \
--kaz-cache-compaction-placement share --kaz-cache-compaction-placement-share-ratio 0.5 \
--kaz-cache-demote-unread-first --kaz-cache-compaction-hot-age 30m
```

## 既定で変わる動作

- `--cache-eviction` を指定しない場合や `none`／`2-random`／`lru` の場合、動作は kaz.6 と同じです。
- kaz-2q 用の metrics（`juicefs_kaz_cache_2q_*`）の登録だけは常に行います。kaz-2q 以外では値は出ません。

## 互換と注意

- **キャッシュのファイルの形式は変えていません。** kaz-2q で使ったキャッシュのディレクトリを、`lru` や kaz の付かない JuiceFS でそのまま使えます。管理ファイルは `raw/` の外にあり、無改良の JuiceFS は読みません。
- 管理ファイルの中身の形式は version 3 です（ファイル名は `kaz-cache-state.v1` のまま）。この版より前のテスト版は version 3 を読めず、無視します。その場合は、全部が新入り用の状態で起動します（mount は失敗しません）。管理ファイルが無い、壊れている、古い場合も同じです。
- 新しく kaz-2q を使い始めた直後は、既にキャッシュにあるブロックがすべて新入り用から始まります。区分が育つまで、数回の起動がかかります。
- `juicefs gc --compact` は別のプロセスで動き、mount のキャッシュには触れません。compaction をするときは、mount の中で動く `juicefs compact <mount のパス>` を勧めます（出力の扱いと、古いブロックのキャッシュからの削除が効くため）。
- テスト: pkg/chunk・pkg/vfs・cmd の kaz のテストは通っています。`TestFinishCanceledGetSharesResultWithWaiter` は kaz.6 の時点からたまに失敗するテストで（40 回中 3 回）、この版の変更とは関係ありません。
- このほかの注意は kaz.6 と同じです。
