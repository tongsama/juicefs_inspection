# juicefs-v1.4.1-kaz.8

juicefs-v1.4.1-kaz.7 に、compaction の新しい方式 **delta**（差分 slice 方式）を加えた版です。kaz.7 までの改修は、すべてそのまま含みます（[juicefs-v1.4.1-kaz.7](https://github.com/tongsama/juicefs_inspection/releases/tag/juicefs-v1.4.1-kaz.7) のリリースノートを参照）。

## 背景

VM のディスクのような巨大なファイルでは、ゲストの 4〜8 KiB の書き込みが chunk のあちこちに散らばって届きます。block モードの compaction は、小さい書き込みが 1 つでも触れた 4 MiB のブロックを丸ごと書き直していました。書いた量の約 6〜10 倍を compaction が書き出すことがありました。この書き出しが、Drive への転送量と、kaz-2q のキャッシュの押し流し（kaz.7 の判定で主な損失）の原因でした。

## 追加したオプション（どれも既定では今までと同じ動き）

- `--kaz-compaction-mode delta`
  - 元の 4 MiB のブロックは書き直しません。前回の compaction の後に書かれた小さい書き込みを、見えている範囲だけ 1 つの小さい slice（差分の「段」）に詰めて書きます。chunk の record（同じ slice を指す複数の record、それぞれ別の位置と長さ）で読み分けます。
  - ブロックごとに安い方を選びます。差分がブロックの大きさの一定の割合に達したブロック、下に元のデータが無いブロックは、今までどおり 4 MiB で書き直します。1 回の compaction の書き出しは、block モードで同じ回に書き出す量を超えません。
  - 段が増えたらまとめ直し、record が増えたらブロック単位で畳み込みます。
  - 差分の slice の参照の数は、その slice を指す record の数に合わせて、置き換えと同じトランザクションで設定します（Redis／SQL／TKV で同じテストを通しています）。
  - TKV のエンジン（TiKV、etcd、BadgerDB、FoundationDB）では、自動で block モードに戻ります（WARN を出します）。TKV の単一ファイルの clone は、同じ slice を指す record が複数あると参照を少なく数える既存の問題（upstream 由来）があるためです。
  - `juicefs compact`／`gc --compact` は、今までの block モードと同じく、全部をブロックの形に戻します。
  - delta のとき、1 MiB 以下の object は、キャッシュに入れる読み込みに限って、range GET ではなく丸ごと取ってキャッシュに入れます（差分の小さい object を何度も取り直さないため）。
- `--kaz-compaction-delta-tiers=<数>`（既定 `8`、1〜16）: 差分の段の上限。超えたら 1 段にまとめ直します。
- `--kaz-compaction-delta-max-records=<数>`（既定 `500`、50〜800）: chunk の record の上限。超えたら、差分の多いブロックから書き直して減らします。
- `--kaz-compaction-delta-gap=<大きさ>`（既定 `16K`、0〜1M）: この幅以下の隙間は、元のデータごと差分に入れて record を減らします。
- `--kaz-compaction-delta-min-file-size=<大きさ>`（既定 `0`）: この長さ未満のファイルは block モードで compaction します。
- `--kaz-compaction-delta-trigger=<数>`（既定 `99`、10〜400）: 前回の compaction の後に record がこの数たまったら compaction します。小さいほど早く動き、アップロード前の小さい slice をより多く片付けられる可能性があります。`max-records + trigger + 50` は 1000 以下にする必要があります。
- `--kaz-compaction-delta-block-ratio=<割合>`（既定 `0.5`）: ブロックの中の差分が、ブロックの大きさのこの割合に達したら、4 MiB で書き直します。
- metrics: `juicefs_kaz_compaction_block_events_total{event}` に delta の結果（`delta_tier`、`delta_merge`、`delta_fold`、`delta_prune`、`delta_noop`、`delta_hidden_full`、`delta_block_fallback`、`fallback_block`）、`juicefs_kaz_compaction_block_amount_total{kind}` に量と、書き直したブロックの理由（`delta_bytes`、`delta_records`、`delta_merge_bytes`、`delta_blocks_ratio`、`delta_blocks_nobase`、`delta_fold_blocks`、`delta_blocks_fallback`、`delta_blocks_whole`）を足しました。

## 計測結果

rclone serve s3 → Google Drive、`--writeback`、キャッシュ 20G、kaz-2q の構成で、block モードと delta（trigger 99）を比べました。

| | block | delta |
|---|---:|---:|
| staging 1 GiB あたりの compaction の出力（非圧縮） | 6.53 | **0.56** |
| staging 1 GiB あたりの PUT のバイト（圧縮後） | 1.54 | **0.37** |
| staging 1 GiB あたりのキャッシュへの書き込み | 7.5 GiB | **1.6 GiB** |

同じ書き込みを、設定を変えて流した比較（各設定 1 回）:

| 設定 | PUT の回数 | compaction の出力 |
|---|---:|---:|
| block モード | 7,316 | 2.06 GiB |
| delta、trigger 99 | 7,493 | 0.46 GiB |
| delta、trigger 50 | 6,809 | 0.47 GiB |
| delta、trigger 30 | 6,305 | 0.48 GiB |

- **delta が減らすのは、compaction の書き出しのバイトと回数です。PUT の回数全体はほとんど変わりません。** PUT の回数の約 9 割は、ゲストの小さい書き込みをそのままアップロードする PUT で、compaction の分は 1 割未満のためです。
- trigger を下げると、アップロード前に compaction が片付ける slice が増え、PUT の回数が減りました（trigger 30 で 99 より約 16% 少ない）。1 回ずつの試験なので、ばらつきは確かめていません。
- record の数は増えます（record が 100 以上の chunk が 35 個、最大 478）。メタデータの操作は約 1.3 倍で、平均の所要時間は変わりませんでした。
- キャッシュへの書き込みが減ったので、compaction の出力に押し流されて読み直す量も減りました。

作者の環境（VM のディスク）では、次の組み合わせで使っています。

```
--kaz-compaction-mode delta
--kaz-compaction-delta-min-file-size 1G
--kaz-compaction-delta-trigger 50
```

## 既定で変わる動作

- `--kaz-compaction-mode` を指定しない場合や `upstream`／`block` の場合、compaction の動作は kaz.7 と同じです。
- delta 用の metrics の項目（上記）は、delta のときだけ値が出ます。

## 互換と注意

- **メタデータの形式は変えていません。** record の形も参照の数の key も upstream のままです。delta で compaction した volume を、kaz の付かない JuiceFS や、block／upstream モードの kaz で読み書きしても、データは正しく扱われます。ただしそれらの client は、差分の record を小さい書き込みとして扱い、触れたブロックや chunk 全体を書き直します（費用だけの問題です）。また、record が 350 を超えた chunk に書くと、書き込みのたびに compaction を要求します。
- TKV のエンジンでは delta は使えません（自動で block モード）。改造していない client で、TKV の volume の `juicefs clone` をする場合の既存の問題は、delta を使わなければ従来どおりです。
- 改造していない client で `cp --reflink`（copy_file_range）を compaction と同時に行うと、upstream の Redis の実装の既存の競合が起きえます（kaz では直してあります）。
- テスト: pkg/meta（Redis／SQLite／memkv、`-race`）、pkg/vfs、pkg/chunk、cmd の kaz のテストは通っています。pkg/chunk の disk cache のテストの一部は、`-race` を付けるとたまに失敗します（upstream 由来の race、この版の変更とは関係ありません）。
- このほかの注意は kaz.7 と同じです。
