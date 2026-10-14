# juicefs-v1.4.1-kaz.6

juicefs-v1.4.1-kaz.5 に、VM の仮想ディスクのように一部だけを書き換え続けるファイルで、compaction の PUT を減らす改修を加えた版です。kaz.5 までの改修は、すべてそのまま含みます（[juicefs-v1.4.1-kaz.5](https://github.com/tongsama/juicefs_inspection/releases/tag/juicefs-v1.4.1-kaz.5) のリリースノートを参照）。

## 追加したオプション（どれも既定では今までと同じ動き）

- `--kaz-compaction-read-min=<個数>`（既定 `5` = upstream と同じ、範囲 5〜2499）
  - Read で chunk（64 MiB）の slice の一覧をメタデータから読み直したとき（開いたファイルの chunk キャッシュのミス）、その chunk の slice の記録がこの個数**以上**なら、バックグラウンドの compaction を要求します。
  - upstream は 5 個で要求するため、slice の少ない chunk でも 64 MiB を書き直していました。
  - 書き込みの側の compaction（99、199、… 個目、または 350 個超）は変わりません。
- `--kaz-compaction-mode=upstream|block`（既定 `upstream`）
  - `block` にすると、compaction は chunk 全体ではなく、前回から変化した 4 MiB のブロックだけを、ブロックごとの新しい slice として書きます。
  - 書き込みが数 KiB でも 64 MiB（PUT 約 16 件）を書き直していたのが、変化したブロックの数（実測で平均 4〜6）だけになります。
  - `juicefs compact` と `juicefs gc --compact` も、block のときはブロック単位で行います。どの chunk も「ブロックごとの slice、隠れたデータ 0」になるまで繰り返し、既にその形の chunk には何もしません。
  - 2,500 個での同期 compaction は upstream のままです。
  - 対応するメタデータのエンジンは Redis、SQL、TKV です。使えない場合（ブロックサイズ、etcd のトランザクションの上限など）は、mount 時に WARN を出して upstream の方式で動きます。
- `--kaz-compaction-block-hidden-ratio=<割合>`（既定 `0.5`、0 より大きく 1 以下）
  - block のとき、残す slice のデータのうち新しいデータに隠れた割合がこれを超えたら、chunk 全体をブロックごとに書き直します。上書きされた古いデータの容量を、いつまでも残さないためです。
- `--kaz-compaction-block-max-slices=<個数>`（既定 `0` = chunk のブロック数＋1、4 MiB なら 17）
  - block のとき、compaction の後に残る slice の記録がこの個数を超えるなら、chunk 全体をブロックごとに書き直します。
  - ブロック数＋1 より小さい値は、mount 時に WARN を出します（ブロックごとの形の chunk でも、書き込みのある compaction のたびに全体を書き直すことになるため）。

VM イメージの用途では、`--kaz-compaction-read-min 100 --kaz-compaction-mode block` を想定しています（`--upload-delay` は付けない）。

## 計測結果（同じ日・同じ手順、VM の起動とアプリの起動）

rclone serve s3 → Google Drive、`--writeback` の構成です。各条件 1 回ずつの計測で、同じ設定どうしでも PUT の数は 10〜15% ばらつきます。

| | upstream（read-min 100） | block（read-min 100、2 回） |
|---|---:|---:|
| compaction 1 回あたりの PUT | 13.8 | 4.6〜4.9 |
| compaction の PUT（ゲストの書き込み 1 MiB あたり） | 6.9 | 1.9〜2.2 |
| PUT の合計（ゲストの書き込み 1 MiB あたり） | 26.2 | 19.0〜21.1 |
| アップロード前に compaction で片付けられた書き込みの割合 | 66% | 70〜74% |
| ゲストの read の p99 | 2,984 ms | 106〜137 ms |
| アプリの起動にかかった時間 | 約 7 分 | 約 1.5〜3 分 |

- PUT の大半は書き込みのアップロードで、block が減らせるのは compaction の分です。PUT の合計は 2〜3 割の減少でした。
- read の待ちが大きく減ったのは、64 MiB の書き直しのアップロードが、書き込みのアップロードと枠を取り合わなくなったためと見ています（推論）。
- read の下限を 30・50 に下げたり、`--upload-delay 1s` を足したりしても、PUT の合計は減りませんでした（50＋1s では read の p99 が 319 ms に増えた）。
- `--kaz-compaction-read-min 100` だけ（block なし）でも、policy なしに比べて PUT の合計は約半分になりました（別の日の計測）。

詳細は [調査の報告](https://github.com/tongsama/juicefs_inspection/blob/juicefs-v1.4.1-kaz.6/docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)（§6〜11）を参照してください。

## 既定で変わる動作

- mount 時に、compaction の設定を INFO で 1 行ずつ出します（`kaz compaction policy: off (upstream compaction triggers)`、`kaz compaction mode: upstream`）。
- 新しい metrics を出します: `juicefs_kaz_compaction_policy_events_total`、`juicefs_kaz_compaction_queue_wait_seconds`、`juicefs_kaz_compaction_scheduled_slices`、`juicefs_kaz_compaction_block_events_total`、`juicefs_kaz_compaction_block_amount_total`。
- それ以外の動作は、オプションを付けなければ kaz.5 と同じです。

## 注意

- **block の chunk には slice の記録が最大 17 件残ります**（4 MiB のブロックの場合）。upstream では compaction の後は 1 件です。Read で組み立てる件数とメタデータの量が少し増えます。
- **block のメタデータの形式は upstream と同じです。** mode を upstream に戻しても、kaz の付かない JuiceFS でも、そのまま読めます。upstream の compaction は、ブロックごとの slice を 1 つにまとめ直します。
- block のとき、ブロック以上の大きさでブロックにそろわない書き込みの slice は、そのまま残します。そのため、アップロード待ちのうちに片付けられず、アップロードされることがあります（upstream なら書き直していた）。
- 書き込みの途中で compaction が失敗した場合、書き終えた新しい slice は消します。書いた直後にプロセスが落ちた場合は、参照の無い object が最大でブロック数だけ残ります。upstream と同じく `juicefs gc` で回収できます。
- block は、Redis（一時 Redis 8.0.5）、SQLite、メモリの TKV でテストしました。MySQL、PostgreSQL、TiKV、etcd では動かしていません。
- 複数の client が同じ chunk を同時に compaction したときは、後の方が失敗として扱われ、書いた slice を消します（upstream と同じ考え方）。
- このほかの注意（Windows 版の mount は未検証、armv7・macOS 向けはなし、稼働中の mount の置き換えなど）は kaz.1 と同じです。
