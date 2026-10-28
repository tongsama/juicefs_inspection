# juicefs-v1.4.2-kaz.2

juicefs-v1.4.2-kaz.1 に、object storage への **PUT と DELETE の回数を減らす 2 つのオプション**を足した版です。どちらも既定では無効で、指定しなければ kaz.1 と同じ動きです。ビルド元は `tongsama/juicefs` の `1.4.2-improve-kaz` ブランチです。

## 追加したオプション

### `--kaz-staging-pack`（既定 false）

- writeback の upload が列に並んでいるときに働きます。同じ chunk で列に並んでいる小さな staging の slice（1 block 未満のもの）を、1 つの object にまとめて upload します。そのうえで、それらの slice の record を、まとめた object の中を指すように書き換えます。
- まとめるのは、すでに列で待っている slice だけです。まとめるために upload を遅らせることはありません。
- 動く条件:
  - `--kaz-compaction-mode delta` が必要です。それ以外のモードでは、起動時に警告を出して無効にします。
  - metadata のエンジンは Redis か SQL に限ります。TKV（TiKV、etcd、Badger など）では行いません。
  - trash が有効なとき（`--trash-days` が 0 でないとき）は行いません。
- 書き換えた record は、本家と同じ形です。新しい種類の key も作りません。改造していない本家の client も、この chunk を普通に読み書きできます。

### `--kaz-skip-unuploaded-delete`（既定 false）

- slice を消すとき、このプロセスが一度も upload を始めていない staging の block には DELETE を送りません。object がまだ存在しないためです。
- 次の block には、これまでどおり DELETE を送ります。
  - 再起動の前に staging された block
  - 一度でも upload を試みた block（試みたときに object ができている可能性があるため）
  - staging を経ない直接の upload
- object を消し忘れることはありません（試験の後の `juicefs gc` で leaked 0）。

## 試験の結果

本番の書き込みの記録（約 17 分）を試験用の volume で再生して比べました（各 1〜2 回）。

- **設定:** `--kaz-compaction-mode delta`、trigger 30、block-ratio 0.5 を基準にしました。
- **pack と skip を足し、`--upload-delay 5s` にした場合:** PUT＋DELETE は 19,163 回から 4,757 回に減りました（−75%）。内訳は PUT −39%、DELETE −93% です。
- **`--upload-delay` が 0 の場合:** −61% でした。少し待たせると、compaction で片付く slice の PUT とその後の DELETE が省けます。
- **待ち時間:** flush と read の p99 は変わりませんでした。
- **compaction の trigger:** 50 や 100 に上げても、PUT＋DELETE の差は 1% 程度でした。上げると GET が増えました。
- **kaz の機能を外した本家相当の設定との比較:** 全部入りの設定は、同じ記録の再生で PUT が約 1/8、GET が約 1/10 でした。write の p50 は 2.4 s から 0.17 ms に縮みました。

詳細: [suite-delay-ja.md](https://github.com/tongsama/juicefs_inspection/blob/main/docs/inspection/staging_pack/2026-10-28/suite-delay-ja.md)、[vanilla 相当との比較](https://github.com/tongsama/juicefs_inspection/blob/main/docs/inspection/vanilla_compare/2026-10-28/report-ja.md)。

作者の本番で使っている設定（kaz.1 からの変更点だけ。`--kaz-compaction-mode delta` は前から指定済み）:

```bash
--kaz-staging-pack
--kaz-skip-unuploaded-delete
--upload-delay 5s                     # 5 秒待たせると、すぐ片付く slice の PUT が省ける
--kaz-compaction-delta-trigger 50     # 30 から変更（PUT＋DELETE の差は 1% 程度）
```

## 互換と注意

- **reflink のコピー:** `--kaz-staging-pack` が動いている volume で、改造していない client から reflink のコピー（`cp --reflink`、`copy_file_range`）をしないでください。
  - これは pack の形式の非互換ではありません。本家 v1.4.2 の Redis の `CopyFileRange` に、もともと競合があります。コピー元の chunk の key を WATCH しないまま読むため、その間に compaction が chunk を書き換えると、消えた slice を指す record をコピー先に作ることがあります。
  - pack も chunk の record を置き換えるので、この競合に当たる機会が増えます。
  - kaz の client は、コピー元の chunk を WATCH するように直してあります。client が全部 kaz の版なら、この競合は起きません。
  - 確かめたのは Redis の経路だけです。
- **compaction GC の local_error:** pack を有効にすると、片付けの `local_error` が増えます。これは、PUT の最中の slice を片付けようとして後回しにしたものです。object は PUT の後に消え、残りません。`--upload-delay` を数秒にすると減ります。
- **メタデータの形式:** 変えていません。kaz.1 や本家 v1.4.2 の client と同じ volume を使えます。
- **`.config`:** キーが 2 つ増えます（`KazStagingPack` と、`Chunk` の中の `KazSkipUnuploadedDelete`）。
- **テスト:** Go 1.25.11 で、次が通っています。
  - pkg/chunk、pkg/vfs（Redis を使用）、pkg/compress、pkg/utils
  - pkg/meta の kaz のテストと、Redis・SQLite・MemKV・Badger のテスト（Redis Cluster のテストは環境がないので除く）
  - cmd（環境に依存する `TestIntegration`、`TestJFS`、`TestRestore` は除く）
  - 作者の環境での実機テスト
- **Windows:** ビルドと起動の確認までで、mount の動作は確かめていません（これまでと同じ）。
