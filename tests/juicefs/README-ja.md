# tests/juicefs: 計測と流し直しの suite

JuiceFS（kaz 改修版）の compaction の設定を、本番と同じ形の書き込みで比べるための道具です。標準ライブラリだけの Python 3 で動きます。

| 道具 | 役目 |
|---|---|
| `jfsmeasure` | 本番または試験の mount の計測（start／check／stop／capture／progress／compare／boot） |
| `jfsreplay` | 書き込みの記録（record／stop／mark／verify）、試験の volume（testvol）、種（seed）、流し直し（run／suite） |

仕様は `docs/superpowers/specs/2026-10-21-kaz-measure-replay-suite-design.md`、計画は同 `plans/`。

## 1. 本番の計測の手順

本番の mount（`~/mnt_juicefs`）には読み取りだけで触れます。出力ディレクトリはキャッシュや mountpoint の外に置きます。

1. start: `./jfsmeasure start prod <出力ディレクトリ>`
   metrics（60 秒）、LLEN の分布（10 分）、キャッシュの出入り（5 分）の常駐を起動します。開始時の `/metrics` 全体とキャッシュの内訳も取ります。
   mount の設定は、動いている mount の `<mountpoint>/.config`（JSON）を写して `config.json` に残します。**設定の比較は、スクリプトの値でなく必ずこの `.config`（実効値）で行います**（mount スクリプトの既定値と実効値は違うことがある。例: cache-size、cache-eviction）。接続先や鍵らしい項目は伏せられます。
2. check: `./jfsmeasure check <出力ディレクトリ>` で、常駐と記録の状態、前回と同じ材料（バイナリの version、sha256、config.json）がそろっているかを確かめます。
3. capture: 再起動の直前に `./jfsmeasure capture <出力ディレクトリ> <ラベル>`（`/metrics` 全体、キャッシュの内訳とファイルの一覧、kaz-2q の管理ファイルの写し）。
4. boot: 再起動の後、`./jfsmeasure boot <出力ディレクトリ> <開始> <終了> [ラベル]`（4 つの時刻は `--boot-start`、`--login-screen`、`--login-done`、`--app`）。GET、ミス、ヒット率、ghost_hit の内訳などを出します。
5. compare: `./jfsmeasure compare 'ラベル:ディレクトリ:開始:終了' …` で区間を同じ表に並べます。先頭に `config.json` の違いが出ます。経過だけ見るなら `./jfsmeasure progress <出力ディレクトリ>`。

## 2. 試験の手順（record → mark → verify → seed → suite）

1. record: 本番の VM を起動する前に `./jfsreplay record <trace.tsv> --inode <VM のディスクの inode>`（既定は prod の inode 634040）。`.accesslog` を読むだけで mount には何も書きません。同時に `jfsmeasure start prod` も動かしておきます。
2. mark: 起動の節目で `./jfsreplay mark <trace.tsv> <ラベル>`（起動開始、ログイン画面、ログイン完了、アプリ）。
3. 10〜15 分したら `./jfsreplay stop <trace.tsv>`。
4. verify: `./jfsreplay verify <trace.tsv> <計測の出力ディレクトリ>`。記録した全 inode の write の回数とバイトを、同じ区間の `juicefs_fuse_written_size_bytes` の増分と比べ、差が 1% 以内か確かめます。超える、または解析できなかった行（Unknown）が 0 でなければ、記録を取り直します。
5. seed: `./jfsreplay seed <trace.tsv> [--create-bucket]`。試験の volume に、流し直しが clone する種のファイルを作ります（上限 32 GiB、中身は位置から決まる圧縮のよく効くバイト列）。
6. suite: `./jfsreplay suite <trace.tsv> [--only a,b] [--repeat N] [--age N] [--create-bucket]`。`settings.toml` の設定を上から順に `run` し、結果を 1 枚の表にします。
   - 既定の 6 つ: `default`（trigger 99、block-ratio 0.5）、`trigger30`、`trigger50`、`trigger200`、`block-ratio75`、`block`（block モード）。
   - 1 つ流すだけなら `jfsreplay run <trace.tsv> --settings <名前>`。
   - 失敗（EIO、rate limit、mount の停止、安全の確かめ）はその列を「失敗」として表に出し、**そこで止まります**。「未収束」（落ち着きの判定が上限 10 分で収まらない）は失敗ではなく、表に印を付けて続けます。
   - `--repeat N` は同じ設定を N 回流し、列は `名前`、`名前#2`… になります。
   - 1 回は 10〜15 分、6 設定で約 1.5 時間です。

### settings.toml

```toml
[[settings]]
name = "trigger30"
mode = "delta"                       # 省略すると mount_flags.txt のまま
flags = { "kaz-compaction-delta-trigger" = 30 }
```

`flags` は `mount_flags.txt` の `[compaction]` の印のある行の置き換えです。**書かなかった項目は `mount_flags.txt` の値（本番の実効値。例: trigger 50）のまま**になるので、既定の組は delta の各設定に mode、`kaz-compaction-delta-trigger`、`kaz-compaction-delta-block-ratio` を全部書いています（block は mode だけで、ほかは `mount_flags.txt` のまま固定）。`test_no_inheritance_in_mount_args` が、各設定の mount のオプションが意図した値であることを確かめます。ファイルを書き換えれば組を変えられ、`--settings-file` で別のファイルも使えます（この旗は `suite` だけ。`run` は `settings.toml` 固定）。

## 3. mount_flags.txt の作り方（本番の実効値から）

試験の mount は、本番と同じオプションで立て、compaction の値だけを差し替えます。

1. 本番の実効値: 動いている mount の `.config`（`jfsmeasure start prod` が伏せて `config.json` に写したもの）。これが正です。
2. 本番の mount スクリプトの `JUICEFS_CUSTOM_OPTIONS`（カスタムのオプションの配列）を、`# 本番の JUICEFS_CUSTOM_OPTIONS` の節に 1 行 1 オプションで写します。
3. スクリプトの既定値と `.config` の値が違う項目は `.config` に合わせます（例: cache-size 15G と 20G、eviction lru と kaz-2q）。
4. 置き換えてよい行に `# [compaction]`、試験用に別の値にする行に `# [test]` の印を付けます。`--log`、`--cache-dir`、mountpoint、`--background` は testvol が試験の path で足すので書きません。本番の `--check-storage` は実効値で確かめられないので入れません。
5. 確かめ: 試験の mount を 1 回立てた後の `.config`（各回の `config.json`）を、本番の `config.json` と比べます。違う項目は、`jfsmeasure compare` の「設定 …」の行にそのまま出ます。意図した差（compaction の値、試験の metrics port など）だけであることを確認します。最初の本番の試験の前に、全項目を 1 度目で突き合わせてください。

## 4. 安全の約束

- 本番の Redis、本番のバケット（`buckets`）、本番の mountpoint（`~/mnt_juicefs`）、本番のキャッシュ（`~/.juicefs/cache/…`）には、試験の道具は書き込みません。試験の Redis は `127.0.0.1:16379`（db 1）、バケットは `juicefs-kaz-test`、mountpoint は `~/mnt_juicefs_test`、キャッシュは `~/.juicefs/cache-test`。プロファイルの書き間違い（本番と同じ host、バケット名、path）は、書き込みの前に止まります。
- 本番の Redis には読み取りの命令（LLEN、LRANGE、HGET、DBSIZE、SCAN）だけを送ります。接続先や鍵は表示せず、`config.json` や `run.json` にも書きません（実行時に mount スクリプトから読みます）。
- **`--create-bucket`**: 試験の volume がまだ無いとき、本物の Drive（`rclone serve s3`）にバケットと volume を作る操作です。**この旗を付けたときだけ**行います。付けずに volume が無ければ、何も作らず止まります。作ってよいかをユーザーが確かめてから付けてください。
- 試験の並列のアップロードは本番以下にします。Drive の rate limit（429、403 の rate limit）が出たら、その回を止めて表に出します。
- clone は各回の終わりに消します。試験のバケットと Redis は次の回のために残します。全部消す `jfsreplay testvol destroy` は確認を求めます。
- 試験の Redis の state ディレクトリ（`~/.juicefs/test-suite/`）は mode 0700 です。volume の Format には storage の認証情報が JuiceFS の仕様で入ります（本番と同じ）。

## 5. 既知の限界（未検証を含む）

- 種の中身は作り物です。PUT と GET のバイト数（圧縮後）は本番より小さくなるので、回数と圧縮前の量で比べます。
- 読み込みの速さ（起動の時間）は参考です。キャッシュの中身と大きさが本番と違います。
- 記録は 1 回の起動のものだけです。ゲストの書き込みの形は日によって違います。
- 試験の間、本番と同じ rclone と Drive に負荷がかかります。
- **試験の volume は暗号化していません**（本番は aes256gcm-rsa）。暗号化の CPU や PUT のサイズへの影響は含まれません。
- **Drive の rate limit のパターンは未検証**です。`RATE_LIMIT_RE`（429／403 の rate limit）が本物の Drive のログと合うかは、最初の本番の試験で `~/.juicefs/test-suite/juicefs.log` を確かめます。
- **種の形は、4 MiB ごとの fsync で決まります**（各 block が別の slice）。block モードの `juicefs compact` は 1 つの 64 MiB の record を保つので、compact は安全のための手順にすぎません。形は LRANGE で確認します。
- 試験の RTT は片道 5 ms の中継（合計約 10 ms）で、本番の揺らぎは再現しません。

## 6. 結果の置き場所

- 各回: `tests/juicefs/results/<日時>/<設定>/`（`run.json`、`metrics.tsv`、`llen.tsv`、`play.json`、`config.json`、capture の材料）。`results/` は git に入れません。
- 全体の表: `tests/juicefs/results/<日時>/summary.md`。列は設定ごと（`--repeat` なら回ごと）で、`report.compare` の行（config.json の違い、staging 1 GiB あたりの PUT と compaction の出力、片付けなど）に play の待ち時間を足し、脚注に各行の出どころの系列を書きます。失敗・未収束の列には印を付けます。
- 本番の計測: `jfsmeasure start` に渡した出力ディレクトリ。報告にまとめるときは `docs/inspection/<テーマ>/<日付>/` へ写します。

## 7. 試験

```
cd tests/juicefs && python3 -I -m unittest discover -s . -p 'test_*.py' -v
```

Drive には触れません。FUSE とバイナリが要る結合の試験は、無ければ skip されます。
