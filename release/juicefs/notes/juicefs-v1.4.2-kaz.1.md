# juicefs-v1.4.2-kaz.1

本家の **JuiceFS v1.4.2** に、kaz の改修を載せた最初の版です。juicefs-v1.4.1-kaz.8 までの kaz の改修は、すべてそのまま含みます（[juicefs-v1.4.1-kaz.8](https://github.com/tongsama/juicefs_inspection/releases/tag/juicefs-v1.4.1-kaz.8) のリリースノートを参照）。ビルド元は `tongsama/juicefs` の `1.4.2-improve-kaz` ブランチで、このブランチを既定ブランチにしました。

## 本家 v1.4.2 から入ったもの

v1.4.2 は、本家 main から 35 件を選んで取り込んだパッチリリースです。kaz の使い方に関係する修正は次のとおりです。

- **読み込みの失敗が成功に化ける不具合の修正（#7500、#7503、#7509、#7520）**
  - kaz.8 までの kaz にも同じ不具合がありました。block の GET が timeout した後も放棄された処理が動き続け、失敗した読み込みを成功として、埋まっていない中身を返すことがありました。手元の試験で再現しています。
  - `--kaz-finish-canceled-get` の経路も同じ不具合を持っていました。誤った中身がディスクキャッシュに入りえました。
  - 本家の修正を採り、その上に kaz の `--kaz-get-header-timeout` などを載せています。
- **zstd の展開が、隣の slice のメモリを上書きする不具合の修正（#7556）**
  - zstd の volume で、upstream と共通の読み込みの経路に影響していました。
- **graceful upgrade（動いている mount への `juicefs mount` による入れ替え）で、処理中の要求を中断しない（#7417 と go-fuse の更新）**
  - kaz.8 までは、3 秒たっても終わらない要求を中断し、EINTR を返していました。kaz の watchdog の無効化では防げていなかった経路です。
- **その他:** metadata（format の扱い、SQL・Redis のトランザクションの再試行、session の heartbeat）、FUSE の setattr、ioctl の修正。

## 追加したオプション

- **`--kaz-upgrade-busy=fail|stack`**（既定 `fail`）
  - mount 中の場所にもう一度 `juicefs mount` したとき、古い mount の session を引き継げない場合の動きを決めます。引き継げないのは、古い mount が 10 秒以上忙しいときや、volume やオプションが違うときです。
  - `fail` では、上に重ねて mount せず、エラーで終わります。古い mount は動き続けます。
  - `stack` は本家と同じ動きで、上に重ねて mount します。root でなければ、fusermount が重ねる mount を拒むため、本家の再起動のループが古い mount を強制的に外してから mount し直します（手元で観測）。
  - バックグラウンドで起動したときは、元のコマンドは「10 秒で準備できなかった、ログを見よ」と表示します。理由は新しい mount のログに出ます。

## 名前を変えたオプション

kaz が足したオプションのうち、`kaz-` の付いていなかった 9 個に `kaz-` を付けました。**旧名は別名として残るので、今の mount スクリプトはそのまま動きます。** 旧名を使うと、起動時に旧名ごとに 1 行の警告を出します。

| 旧名 | 新しい名前 |
|---|---|
| `--compaction-gc-mode` | `--kaz-compaction-gc-mode` |
| `--compaction-scheduler` | `--kaz-compaction-scheduler` |
| `--writeback-fsync` | `--kaz-writeback-fsync` |
| `--slice-flush-wait` | `--kaz-slice-flush-wait` |
| `--slice-flush-idle` | `--kaz-slice-flush-idle` |
| `--writer-reuse-window` | `--kaz-writer-reuse-window` |
| `--writer-flush-timeout` | `--kaz-writer-flush-timeout` |
| `--writer-flush-scope` | `--kaz-writer-flush-scope` |
| `--meta-write-batch` | `--kaz-meta-write-batch` |

旧名の別名は、何版か後に外す予定です。mount スクリプトは、この版に入れ替えた後に新しい名前へ直してください。**kaz.8 以前のバイナリには新しい名前がないので、バイナリより先にスクリプトを直さないでください。**

## 既定で変わる動作

- **重ねて mount しない:** 上の `--kaz-upgrade-busy=fail` が既定です。本家と同じ動きにするには `stack` を指定します。
- **読み込みが時間切れになったとき:** range GET が `--get-timeout` で時間切れになった場合、すぐ block 全体の読み込みへ切り替えず、エラーとして上の層で再試行します（本家 #7509）。圧縮した volume（zstd、lz4）では range GET を使わないので、影響はありません。
- **ログ:** 調査スクリプトが読むログの文言は変えていません。変わるのは、行の末尾の `[関数@file:line]` だけです。

## 内部の整理（挙動は変えていません）

本家の新しい版に追従しやすくするため、kaz の変更を本家のファイルから kaz 用のファイル（`*_kaz.go`）へ移しました。

- 設定のフィールドは、埋め込み struct にまとめました。
- 計測のログは、helper にまとめました。
- オプションの説明は、`docs/{en,zh_cn,ja}/reference/kaz_options.md` に分けました。日本語版は kaz のページだけです。

その結果、本家のファイルへの kaz の差分は、25 ファイル・+2,061 行から 18 ファイル・+410 行に減りました。本家 main と merge したときの衝突のうち、kaz が原因のものは 13 から 6 ブロックに減っています。

挙動が変わっていないことは、次のように確かめています。

- `.config` のキーと値、mount のフラグ一式を、golden テストで固定しています。
- 全 30 コマンド・575 フラグの名前・別名・既定値・説明が、整理の前と完全に一致することを確かめました。
- 毎回通る経路で、メモリの確保を増やしていないこともベンチマークで確かめました。

## 互換と注意

- **バイナリの入れ替え方:** VM を止めて umount してから、新しいバイナリで mount し直してください。kaz.8（v1.4.1 ベース）が動いている mountpoint へ `juicefs mount` を重ねると、処理中の要求を中断し、ゲストに I/O エラーが返りえます。
- **版の表示:** `juicefs version` は `1.4.2+<日付>.<SHA>-kaz.1` の形になります。
- **メタデータの形式:** 変えていません。本家 v1.4.2 や kaz.8 の client と同じ volume を使えます。
- **`.config`:** キーの集合と値は変わりません。キーの並び順だけが変わります（kaz のキーが各節の末尾に移ります）。
- **テスト:** Go 1.25.11 で、次が通っています。
  - pkg/chunk、pkg/vfs（Redis を使用）、pkg/compress、pkg/utils
  - pkg/meta の kaz のテストと、Redis・SQLite・MemKV・Badger のテスト
  - cmd（環境に依存する `TestIntegration`、`TestJFS`、`TestRestore` は除く）
  - 実際の FUSE で、fsync を期限なしで待つテスト
  - 作者の環境での実機テスト
- **既存の失敗:** `pkg/fuse` の `TestFUSE/FstatDeleted` は、kaz.8 でも失敗する既存の失敗です。
- **Windows:** ビルドと起動の確認までで、mount の動作は確かめていません（これまでと同じ）。
