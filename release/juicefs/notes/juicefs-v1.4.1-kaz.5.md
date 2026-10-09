# juicefs-v1.4.1-kaz.5

juicefs-v1.4.1-kaz.4 の不具合を直した版です。kaz.4 までの改修は、すべてそのまま含みます（[juicefs-v1.4.1-kaz.4](https://github.com/tongsama/juicefs_inspection/releases/tag/juicefs-v1.4.1-kaz.4) のリリースノートを参照）。

## kaz.4 で `--kaz-get-header-timeout` を使っている場合の注意

kaz.4 の `--kaz-get-header-timeout` には、ファイルの read が EIO になり続ける不具合があります。**kaz.4 ではこのオプションを使わず、この版に上げてください。**

- Google Drive などには、毎回 30 秒ほど経ってから応答する object があります。kaz.4 は、そうした object の GET を、取り直すたびに header-timeout で切っていました。rclone serve s3 の `--kaz-s3-cancel-get-on-disconnect` を併用すると、Drive 側のダウンロードも毎回取り消されます。そのため、いつまでも取れませんでした。
- 取り直しの回数の上限（`--io-retries`）を使い切ると、read は EIO になります。そのうえ、ファイルを全部閉じるまで、そのファイルへの read がすべて EIO になり続けました（下の「既定で変わる動作」を参照）。
- 実機では、VM の仮想ディスクの read が EIO になり続け、ゲストが I/O エラーを出して起動できなくなりました。

## 直したこと

- **`--kaz-get-header-timeout`：** 一度切った object を 5 分間覚えておき、その間の取り直しには header-timeout をかけません（`--get-timeout` までは待ちます）。取れたら忘れます。毎回 30 秒かかる object も、10 秒で 1 回切られた後、2 回目で取れます。覚えておく object は最大 4,096 個です。

## 既定で変わる動作

- **read の失敗が、ファイル全体に広がらなくなりました。** これまで（upstream 由来）は、ファイルのどこか 1 か所で取り直しの上限を使い切ると、ファイルを全部閉じるまで、そのファイルへの read がすべて、すぐに EIO を返していました。QEMU のように仮想ディスクを開いたままにする使い方では、一時的な失敗で、ディスク全体が読めなくなっていました。
- これからは、失敗した場所を待っていた read にだけ EIO を返します。その後に来た read は、改めて object storage に取りに行きます。失敗が続いていれば、また EIO を返します。エラーを隠したり、古いデータを返したりはしません。
- 1 か所の失敗で、同じファイルの別の場所の読み込みをまとめて止めることも、しなくなりました。それぞれが独立に取り直します。

## 確認したこと

- 単体テスト: 毎回 30 秒かかる object が 2 回目で取れること、5 分経つとまた切られること、EIO の後も開いたままのファイルで次の read が取りに行き、object storage が戻れば読めること。
- 実機（rclone serve s3 → Google Drive、`--kaz-finish-canceled-get --kaz-get-header-timeout=10s`、rclone は `--kaz-s3-cancel-get-on-disconnect` 付き）: VM を起動して使い、header-timeout で切った GET 8 件はどれも 1 回だけで、EIO は 0 件。VM の read のための GET は、取り直しが 1〜2 秒で成功した。compaction のための GET は、その場では取り直さず、後でやり直す（エラーにはならない）。

## 注意

- このほかの注意（Windows 版の mount は未検証、armv7・macOS 向けはなし、稼働中の mount の置き換えなど）は kaz.1 と同じです。
