# tests/juicefs: 計測と流し直しの suite

JuiceFS（kaz 改修版）の compaction の設定を、本番と同じ形の書き込みで比べるための道具です。標準ライブラリだけの Python 3 で動きます。

| 道具 | 役目 |
|---|---|
| `jfsmeasure` | 本番または試験の mount の計測（start／check／stop／capture／progress／compare／boot） |
| `jfsreplay` | 書き込みの記録（record／stop／mark／verify）、試験の volume（testvol）、種（seed）、温めたキャッシュの写し（warm）、流し直し（run／suite） |

仕様は `docs/superpowers/specs/2026-10-21-kaz-measure-replay-suite-design.md`、計画は同 `plans/`。

## 1. 本番の計測の手順

本番の mount（`~/mnt_juicefs`）には読み取りだけで触れます。出力ディレクトリはキャッシュや mountpoint の外に置きます（`start` と `capture` は、出力ディレクトリが本番の mountpoint か本番のキャッシュと同じかその下なら、symlink を解いた先も含めて何も書かずに終了 3 で止まります）。

1. start: `./jfsmeasure start prod <出力ディレクトリ>`
   metrics（60 秒）、LLEN の分布（10 分）、キャッシュの出入り（5 分）の常駐を起動します。開始時の `/metrics` 全体とキャッシュの内訳も取ります。
   mount の設定は、動いている mount の `<mountpoint>/.config`（JSON）を写して `config.json` に残します。**設定の比較は、スクリプトの値でなく必ずこの `.config`（実効値）で行います**（mount スクリプトの既定値と実効値は違うことがある。例: cache-size、cache-eviction）。接続先や鍵らしい項目は伏せられます。
2. check: `./jfsmeasure check <出力ディレクトリ>` で、次を確かめます（問題があれば `NG:` の行を出して終了 1、無ければ 0）。
   - 3 つの常駐（metrics、llen、keys）の pid が生きた sampler であること。
   - `metrics.tsv` と `llen.tsv` があり、最後の更新が間隔の 3 倍より新しく、最新の標本が取得の失敗（`_scrape_error`／`error`）でないこと。
   - `keys-snapshot-*`、`metrics-start-*`、`breakdown-start-*`、`keys-events.tsv`、`config.json` があること。
   バイナリの version・sha256 は `run.json` に残しますが、check はその中身を確かめません。
3. capture: 再起動の直前に `./jfsmeasure capture <出力ディレクトリ> <ラベル>`（`/metrics` 全体、キャッシュの内訳とファイルの一覧、kaz-2q の管理ファイルの写し）。
4. boot: 再起動の後、`./jfsmeasure boot <出力ディレクトリ> <開始> <終了> [ラベル]`（4 つの時刻は `--boot-start`、`--login-screen`、`--login-done`、`--app`）。GET、ミス、ヒット率、ghost_hit の内訳などを出します。
5. compare: `./jfsmeasure compare 'ラベル:ディレクトリ:開始:終了' …` で区間を同じ表に並べます。先頭に `config.json` の違いが出ます。経過だけ見るなら `./jfsmeasure progress <出力ディレクトリ>`。

## 2. 試験の手順（record → mark → verify → seed → suite）

1. record: 本番の VM を起動する前に `./jfsreplay record <trace.tsv> --inode <VM のディスクの inode>`。**`--inode` は必須**です（既定値はありません。今の VM のディスクは `profiles.toml` の prod の inode 634040）。`.accesslog` を読むだけで mount には何も書きません。同時に `jfsmeasure start prod` も動かしておきます。
   出力の trace に中身があるか `<trace.tsv>.meta.json` があれば、追記せずに終了 2 で止まります（毎回新しい名前にする）。
2. mark: 起動の節目で `./jfsreplay mark <trace.tsv> <ラベル>`（起動開始、ログイン画面、ログイン完了、アプリ）。
3. 10〜15 分したら `./jfsreplay stop <trace.tsv>`。`<trace.tsv>.pid` の pid が `jfsreplay record` であることを `/proc/<pid>/cmdline` で確かめてから SIGTERM を送ります。違えば signal を送らず、古い pidfile だけを消して終了 2。
4. verify: `./jfsreplay verify <trace.tsv> <計測の出力ディレクトリ>`。記録した全 inode の write の回数とバイトを、同じ区間の `juicefs_fuse_written_size_bytes` の増分と比べ、差が 1%（`loss_tolerance`）以内か確かめます。
   - 終了 0: 回数とバイトの差がどちらも上限以内。終了 1: どちらかが上限を超えた。終了 2: 判定できない（meta.json が無い・壊れている、全 inode の write が 0、区間が metrics のサンプルの範囲の外、記録中の counter の減少＝mount の再起動）。
   - 解析できなかった行（Unknown）の数は表示するだけで、終了コードには入りません。1 を超える差か、Unknown が 0 でなければ、内容を確かめて記録を取り直します。
5. seed: `./jfsreplay seed <trace.tsv> [--written-only] [--create-bucket]`。`--written-only` は write が触れた chunk だけを種にします（read だけの範囲は穴のままで GET が出ないので、読み込みの比較は弱くなるが、種が小さく速い）。試験の volume に、流し直しが clone する種のファイルを作ります（上限 32 GiB、中身は位置から決まる圧縮のよく効くバイト列）。
   種を書いた後、試験の mount の `juicefs_staging_blocks` と `juicefs_object_request_uploading` が 0 になる（Drive への書き出しが終わる）まで待ち、1 分ごとに今の値を出します。待たずに run へ進むと、run の最初の `umount --flush` が書き出しの途中で時間切れになるためです。上限は 6 時間で、超えたら「書き出しが終わらない」と出して終了 2（種と `seed.json` はできているので、2 つの値が 0 になってから run します）。
   `seed.json` には trace の名前が入り、run は名前が違う `seed.json`（別の trace の種）を使わずに止まります。
6. warm（任意）: `./jfsreplay warm <trace.tsv> [--force]`。温めたキャッシュの写しを作ります（下の「温めたキャッシュの写し」）。run／suite も、写しが無いか使えなければ最初の 1 回に自動で作るので、先に時間を使っておきたいときだけ実行します。
7. suite: `./jfsreplay suite <trace.tsv> [--only a,b] [--repeat N] [--age N] [--head-seconds N] [--create-bucket]`。`settings.toml` の設定を上から順に `run` し、結果を 1 枚の表にします。
   - 既定の 6 つ: `default`（trigger 99、block-ratio 0.5）、`trigger30`、`trigger50`、`trigger200`、`block-ratio75`、`block`（block モード）。
   - 1 つ流すだけなら `jfsreplay run <trace.tsv> --settings <名前>`。
   - `--head-seconds N`（run・suite）: 記録の最初の N 秒だけを流します（流し直しの時刻の精度を短く確かめる用。既定は記録の全体）。`summary.md` の見出しと `run.json` の `head_seconds`／`ops`／`ops_total` に残ります。温めたキャッシュの写しは記録の全体で作ります。
   - 失敗（EIO、rate limit、mount の停止、安全の確かめ）はその列を「失敗」として表に出し、**そこで止まります**。「未収束」（落ち着きの判定が上限 10 分で収まらない）は失敗ではなく、表に印を付けて続けます。
   - `--repeat N` は同じ設定を N 回流し、列は `名前`、`名前#2`… になります。
   - 1 回は記録の長さ（例 18 分）＋落ち着きの待ち＋写しを戻す時間で、6 設定で約 2〜3 時間です。最初の 1 回だけ、写しを作る時間（Drive から温める。初回の実走では 6.9 GB、約 13 分）が足されます。
   - **suite・run・seed は、本番の VM を止めた（idle の）状態で流します**。rclone（`rclone serve s3`）と Drive は本番と共有で、本番と試験の `max-uploads` は足し合わさって Drive にかかります。run の見張りは、本番の `/metrics`（`http://127.0.0.1:9567/metrics`、読み取りの GET だけ）の `juicefs_object_request_errors` も読み、流し直しの間に増えたらその回を止めます。本番の metrics が読めなかったときは、見張れなかったことを警告として `run.json` に残します。
   - 試験の `juicefs_object_request_errors` の増加は、そのままでは止めません（下の「見張りの規則」）。
   - 各回の計測は、run が書く `<out>/profiles.toml` の test を使います。キャッシュの場所は、JuiceFS が `--cache-dir` の下に作る volume の UUID のディレクトリ（`~/.juicefs/cache-test/<UUID>`）、ログは試験の mount の `~/.juicefs/test-suite/juicefs.log` だけです（`profiles.toml` の test の `cache_dir` は親なので、`jfsmeasure start test` を手で使うときはキャッシュの項目が空になります）。終わりの材料の取りこぼし（`kaz-cache-state.v1` がまだ保存されていないなど）は警告として表示し、`run.json` と `play.json` の `warnings` に残します。

### 温めたキャッシュの写し（warm copy）

各回は同じ温まったキャッシュから始めます。毎回 Drive から温め直すと、初回の実走（2026-10-21）では 85,409 範囲・6.9 GB を読むだけで 787 秒かかったためです。

- 作る（最初の 1 回、または `jfsreplay warm`、`--force` なら必ず）: 既定の設定で mount → 種を clone → 記録の read の範囲を 1 回読む（Drive から取る）→ 複製を消して後始末を待つ → **試験の mount を外して**（キャッシュと kaz-2q の状態のファイルが揃った状態で）→ `<cache-dir>/<UUID>/` を `rawstaging` を除いて写す。
- 置き場所: `~/.juicefs/test-suite/warm-cache/<trace の名前>/`（`cache/` が中身、`meta.json` が種の印・UUID・数・作った時の温めの数）。キャッシュと同じ大きさの空きが要ります（足りなければ容量不足で止まります）。
- 各回: キャッシュを空にした後、mount の前に写しを `<cache-dir>/<UUID>/` へ普通のファイルの複製で戻します（hardlink を使わないので、各回の mount は写しを書き換えません）。その回は温め直しません（`run.json` の `warm` は写しを作った時の数、`warm_cache` に写しの場所・作ったか・戻した数）。
- 写しが使えるのは、種の印（`seed.json` の name／inode／path／length と、`seed.json` の sha256・mtime、trace の sha256）が作った時と同じときだけです。種を作り直すと自動で作り直します。mount した volume の UUID が写しと違う（volume を作り直した）ときは、その回を失敗にして `jfsreplay warm --force <trace>` を促します。
- 写す元・戻す先とも、キャッシュを消すときと同じ確かめ（realpath、本番・試験の mountpoint・state との重なり、UUID の名前のディレクトリだけ、symlink をたどらない）を通します。写しの場所は `warm-cache/` の下だけです。`rawstaging` にファイル（上がっていない block）があれば写しません。

### 見張りの規則（流し直しと落ち着きの待ちの間）

止める（その回を「失敗」にする）のは次のときです。

- 試験の mount のログに Drive の rate limit の行（429、403 の rate limit）が出た。
- 本番の `juicefs_object_request_errors` が増えた（試験の負荷が本番に影響した）。
- 試験の mount が落ちた、または試験の metrics を 3 回続けて取れない。
- `profiles.toml` の `object_errors_stop = true` のときだけ: 試験の object の要求の誤りのうち、**説明できない誤り**の数が `object_error_tolerance`（20）を超えた。既定は `object_errors_stop = false` で、説明できない PUT／GET の誤りは止めずに数えて警告に出します。2026-10-21 の実走では、apt upgrade の大量のアップロードの間に、Drive の応答待ちの時間切れ（`timeout awaiting response headers`、30 秒）が 21 件出ました。JuiceFS は staging のアップロードを再試行するので、1 回ごとの失敗はデータの失敗ではありません。

compaction が staging の slice を retire すると、上げ途中の PUT が打ち切られて `slow request: PUT <key> … err: …` が出て、続けて同じ key の `Key <key> is not needed, abandoned` が出ます（初回の実走で `object_request_errors` が 0 → 1 になって止まった原因。試験している効果そのもので、失敗ではない）。そこで、試験の mount のログの誤りの行を次のように分けます（`run.json` の `object_errors` に数が残ります）。

- `abandoned_explained`: PUT の誤りで、同じ key の abandoned が誤りの後 `abandon_window_seconds`（既定 30 秒）以内に出た。数えない。
- `pending`: PUT の誤りで、まだ窓の中。待つ。
- `warned`: PUT と GET 以外の誤り（DELETE など）。書いたデータは失わない（DELETE の失敗は object が残り得るだけ）ので止めず、`warnings` に残します（実の確かめで rclone／Drive の一時的な HTTP 500 の DELETE が出たため。Ruling 13）。
- `unexplained`: GET の誤り、窓の中に abandoned が来なかった PUT、ログに行の無い誤り（`unlogged`。metrics の増分がログの誤りの行より 5 秒以上多い。JuiceFS は遅い要求だけを `slow request` として書くので、速い誤りはログに出ない）。
- `errors_total` は metrics の増分、`logged` はログの誤りの行の数です。説明できない誤りは窓（最大 30 秒）が過ぎてから数えるので、止まるのはその分遅れます。
- 1 つの Guard を、育てる全ての回とその後の待ち、計測する流し直しとその後の待ちで使い回します（回の境目の誤りを見落とさない）。終わり（待ちが「未収束」で上限に達した場合も）には確定（finalize）として、窓と猶予が過ぎるまで（上限 30＋5＋1 秒）読み直し、残った待ち中・ログに行の無い誤りを説明できない誤りに数えて許容と比べます（超えれば失敗、許容以内でも 1 件以上なら `warnings` に残す）。例外で途中で止まった回は待たずに確定します（`object_errors.finalized`）。

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
- 試験の並列のアップロードは本番以下にし、本番の VM は idle にしておきます（本番と試験の upload は同じ rclone と Drive に足し合わさる）。Drive の rate limit（429、403 の rate limit）か、本番の `juicefs_object_request_errors` の増加が出たら、その回を止めて表に出します。試験の説明できない object の要求の誤りは、既定では止めずに数えて警告に出します（§2 の「見張りの規則」）。
- 温めたキャッシュの写し（`~/.juicefs/test-suite/warm-cache/`）は試験のキャッシュの複製で、本番のキャッシュとは重なりません（写す・戻すの前に確かめます）。
- **試験の Redis は docker compose で動かします**（`replay/docker-compose.yml`、project 名 `jfs-kaz-test`、image `redis:8.0.5` の固定 tag、`127.0.0.1:16379` だけに公開、AOF は `~/.juicefs/test-suite/redis` を `/data` に bind mount、container は自分の UID:GID）。`jfsreplay testvol up` が `docker compose up -d redis` で起動し、応答を待ちます（docker が無い、または image が無いと止まります。image は `docker pull redis:8.0.5` で事前に取得）。`down` は Redis を残します。止めるだけなら `docker compose -p jfs-kaz-test -f replay/docker-compose.yml down`（AOF のデータは残る。UID、GID、JFS_TEST_REDIS_DIR は env で渡す）、データごと消すなら `jfsreplay testvol destroy`（確認の後に compose down と AOF の削除）。
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
- 流し直しは、pid を複数の worker の process（既定 min(16, pid の数)、pid の crc32 で決めるので同じ pid の順は保つ）に分け、各 process が自分の fd と受け持つ pid ごとの thread を持ちます。各 thread は予定の時刻まで眠って待ち（回して待たない）、worker の switch interval は 0.5 ms です。時刻は全 process で共通の CLOCK_MONOTONIC で合わせ、どこかの process のエラーで全 process を止めます。1 つの process に 163 thread を置いた版は、実の試験の mount で write の p50 が 409 ms、遅れが 29 秒になりました（GIL の取り直し）。それでも山の時刻に多くの操作が重なると数 ms〜数十 ms の遅れは出ます（`play.json` の `max_lag_ms`、`lag` の p50／p99、`procs`）。
- retire で打ち切られた PUT が速く失敗した場合（`slow request` の行が出ない場合）は、abandoned で説明できず「説明できない誤り」として止まります（初回の実走では 30 秒の時間切れで、全部ログに出ていた）。

## 6. 結果の置き場所

- 各回: `tests/juicefs/results/<日時>/<設定>/`（`run.json`、`metrics.tsv`、`llen.tsv`、`play.json`、`config.json`、capture の材料）。`results/` は git に入れません。
- 全体の表: `tests/juicefs/results/<日時>/summary.md`。列は設定ごと（`--repeat` なら回ごと）で、`report.compare` の行（config.json の違い、staging 1 GiB あたりの PUT と compaction の出力、片付けなど）に play の待ち時間を足し、脚注に各行の出どころの系列を書きます。失敗・未収束の列には印を付けます。
- 本番の計測: `jfsmeasure start` に渡した出力ディレクトリ。報告にまとめるときは `docs/inspection/<テーマ>/<日付>/` へ写します。

## 7. 試験

```
cd tests/juicefs && python3 -I -m unittest discover -s . -p 'test_*.py' -v
```

Drive には触れません。FUSE とバイナリが要る結合の試験は、無ければ skip されます。
