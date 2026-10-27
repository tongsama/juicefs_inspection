# kaz の改修を upstream v1.4.2 へ載せる調査（2026-10-26）

## 対象と方法

- kaz: `tongsama/juicefs` の `1.4.1-improve-kaz`、HEAD `b6a0e24b`（juicefs-v1.4.1-kaz.8 のビルド元）。v1.4.1（`0b90c7db`）から 81 commit（merge を除く）。
- upstream: タグ `v1.4.2`（`46af7afa`、2026-10-22）。v1.4.1 から 35 commit で、release 分岐への cherry-pick。参考として origin/main `f8222e33` とも比べた。
- 方法: scratchpad の一時 worktree で、kaz HEAD に `git merge --no-commit v1.4.2` を実行し、衝突を解消してビルドとテストを行った。本体のブランチは変えていない（commit・push なし）。
- 調査は 3 つの sub-worker に分けた。A は cached_store.go の衝突、B は衝突しないが意味が重なる箇所、C は今後 upstream を取り込みやすい形にするための評価を担当した。
- 凡例: 【観測】は source を読んだか実行して確かめたこと、【推測】は検証していない見立て。

## 結論

**kaz の改修は v1.4.2 に載せられる。** 必要な手当ては、テキスト衝突 3 ファイルと、テキストは衝突しないのにビルドが壊れる 1 か所（format の atomic 化）だけだった。手当て後は `go build ./...` と `go vet` が通り、テストの結果は[検証](#検証)のとおり。

重なる修正のうち 4 件は、upstream の形を採るべきもの（kaz と同等以上）だった。kaz HEAD には同じ不具合が残っていて、そのうち 1 件は再現もした。

## 1. テキスト衝突（3 ファイル）

| ファイル | 原因 | 解消 |
|---|---|---|
| `pkg/meta/base.go` | kaz が upstream の `NewSession` の直前に足した doc コメントと、upstream が同じ位置に追加した `currentSessionInfo` | 両方を残す |
| `pkg/vfs/vfs.go` | kaz が `Truncate` の直前に足した doc コメントと、upstream が追加した `marshalConf` | 両方を残す |
| `pkg/chunk/cached_store.go` | ① `loadRange` の直前に kaz の header-timeout の補助関数、upstream の `getResult` 型<br>② `loadRange` が成功したときの `fetcher.fetch` を kaz が `kazCompactionRead` で包んだ箇所<br>③ `fetchBlock` の Get の行（kaz は `getWithHeaderTimeout`） | upstream の `getResult`（tmp に書き込み、成功したときだけ採用する形）を採り、その上に kaz の header-timeout と compaction read の判定を載せた（[2 章](#2-重なる修正upstream-を採るもの)） |

解消の diff: [conflict-resolution.diff](conflict-resolution.diff)

## 2. 重なる修正（upstream を採るもの）

| upstream | 内容 | kaz HEAD の状態 | 判定 |
|---|---|---|---|
| #7500 `434c9248` | WithTimeout で放棄された GET が後から `err=nil` を書き込み、失敗した read を成功に変える | **同じ不具合あり（再現済み）**。kaz の `fetchBlock` と `loadRange` は、closure の中から外側の名前付き変数に書き込む。v1.4.2 の新テスト `TestReadAtNeverReportsSuccessWithUnfilledBuffer` を kaz HEAD で流すと 5 回中 3 回 FAIL（埋まっていない page を成功として返した）。`--kaz-finish-canceled-get` の `loadDetached` 経路も同じ `fetchBlock` を通り、5 回中 1 回 FAIL した。この経路は `cache=true` なので、誤った中身がディスクキャッシュに入り得る【推測】 | **upstream の形を採る**。マージ版では 10/10 PASS |
| #7509 `0e8f8ada` | `loadRange` が timeout したあと full read へ切り替え、まだ書き込み中の page を上書きする | 同じ構造あり（source で判定。専用の再現テストは作っていない）。kaz の `readBlockDetached` の `copy` も同じ | **upstream を採る**。timeout はエラーとして返す |
| #7520 `0d8187ed` | #7509 で落ちた timeout のメトリクスを元に戻す | kaz には #7509 が無いので該当しない | upstream の形にそろえる |
| #7503 `0546c23e` | `utils.WithTimeout` が callback の結果を race で読む | 同じ不具合あり。kaz HEAD の `-race` で `utils.go:116/121` を検出 | 自動マージのまま upstream を採る |
| #7417 `8047ebfb` と go-fuse の更新 | graceful shutdown のときに処理中の request を中断しない | kaz は FUSE の watchdog を止めた（`opt.Timeout = 0`）が、旧 go-fuse の `Shutdown()` は 3 秒後に request を `close(req.cancel)` で中断していた。引き継ぎのときに、生きている request に EINTR を返す経路もあった。どちらも kaz では防げていなかった | **upstream を採り、`Timeout = 0` は残す**。マージ版にも残っている（`fuse.go:576`） |
| #7556 `c0109222` | zstd の展開が `cap(dst)` まで書き込み、隣の slice を上書きする | upstream と共通の `ReadAt` 経路に同じ不具合がある。本番は zstd なので重要。kaz 独自の経路（`readBlockDetached`、compaction の `readSlice`）は自前の page を使うので影響しない | upstream を採る |

**kaz の改修が upstream に入ったもの:** v1.4.2 には無かった。origin/main には、kaz `84f19ca4` の staging timeout 後の片付けの競合（`stageMu`／`stageCompleted`）と同じ修正が #7517 `43aa5003` として入っている（v1.4.2 には未収録）。1.5 系へ移るときは upstream 版を採り、kaz 固有の部分（`retireCache`、`beginStageWrite`）だけを hook として足し直す。

## 3. 意味の衝突（テキストは衝突しないが壊れるもの）

1. **format の atomic 化（#7378 `7521f43b`）で TKV の WriteSlices がビルドできない。必須の対応。**
   - 【観測】v1.4.2 では `baseMeta.fmt` が `atomic.Pointer[Format]` になり、読むときは `getFormat()` を使う。
   - 【観測】kaz の `kvMeta.doWriteSlices`（795d08db）は `m.fmt.ChangeLog` を直接読んでいるため、`tkv.go:2721` で build が失敗する。kaz のテスト 4 ファイルも同じ理由でコンパイルできない。
   - 今回の検証では `m.getFormat().ChangeLog` と `X.getFormat().F = v` に置き換えた（[format-atomic-fix.diff](format-atomic-fix.diff)）。
   - 本番で載せるときは、テストを #7669 の考え方（公開中の format を書き換えない）に合わせ、`Clone()` してから `setFormat` する形にするのがよい。
2. **他の format 関連（#7661、#7669、#7679）**: kaz の設定は format ではなく `meta.Config`、`chunk.Config`、`vfs.Config` にあり、format は読むだけなので影響しない。`.config` の出力（`marshalConf`）にも kaz のフィールドはそのまま出る。
3. **SQL の delayed slice の claim（#7449）、retry で状態が残る種類の修正（#7460、#7476、#7477、#7407）**: kaz の txn（Redis・SQL・TKV の `doWriteSlices`、`doKazReplaceChunk`、Redis の clone と CopyFileRange）を点検した。どれも試行ごとに状態を初期化していて、同じ種類の不具合は見つからなかった。
4. **PostgreSQL の retry の拡大（#7650）【推測】**: COMMIT 済みで接続が切れた場合に再試行すると、`doKazReplaceChunk` は EINVAL になって新しい slice を消す。これは upstream の `compactChunk` と同じ構造で、本番は Redis なので影響は小さい。
5. **Redis の session heartbeat（#7336）、setattr（#7697）、ioctl**: kaz の変更とは干渉しない。

## 4. 運用上の注意と、ついでに分かったこと

- **graceful upgrade**【推測】: kaz の fsync は期限なしで待つ。ストレージが遅いと、新しい mount は 10 秒で「busy」と判断して upgrade を諦める。その後は古いプロセスがそのまま動き続け、60 秒後に新しいプロセスが上に重ねて mount する。データの安全側に倒れる動きだが、upgrade の手順書に書いておく。
- **zstd の volume では delta モードの whole read に追加の効果がない**【観測】（2026-10-27 訂正: 初版は存在しないフラグ名 `--kaz-whole-read-max` を書いていた）: whole read は delta モードのときだけ入る内部の設定 `KazWholeReadMax`（1 MiB）で、単独のフラグはない。`seekable` が `compressor.CompressBound(0) == 0`（`cached_store.go:1142`）で決まり、lz4・zstd では false になる。whole read の判定は `seekable &&`（`:155`）の内側にある。圧縮した volume のキャッシュミスは最初から常に object 全体の GET なので、効かないのではなく、もう丸ごと読んでいる。help と findings に記載した。

## 5. 今後 upstream を取り込みやすくするための改善提案

kaz が upstream の既存ファイルに入れた差分は 26 ファイル・257 hunk。そのうち次のものが衝突の元になっている。

- **コメントだけの hunk が 36 個。** v1.4.2 の衝突 3 ファイルのうち 2 ファイル（base.go、vfs.go）は、これだけが原因だった。
- **gofmt の桁揃えの巻き添え。** `meta.Config`、`vfs.Config`、`chunk.Config`、`cachedStore`、`baseMeta`、mount.go のリテラルで、長いフィールド名を 1 つ足したために既存の行がすべて変更扱いになっている。
- **upstream 関数の中身の書き換え。** cached_store.go（+511/−85）、writer.go（+476/−56）、base.go の `Write` の分解が該当する。
- **番号と interface の衝突の危険。** `RetireSlice = 1009` は upstream の message 番号の続きなので、upstream が同じ番号を足すと handler が黙って上書きされる。`Meta` interface に足した `WriteSlices` は、upstream が実装を増やすと compile エラーになる。
- **origin/main（1.5 系）では、さらに衝突が増える。** `cacheStore` が `diskCache` に改名され（#7404）、disk_cache.go も分割された（#7365）。kaz の `*_kaz.go` 約 10 ファイルは、テキストの衝突が無くてもビルドが壊れる。

| 優先度 | 提案 | 効果 | 作業量 |
|---|---|---|---|
| P1 最優先 | upstream の関数に足した doc コメントを戻す。本体 AGENTS.md の「変更した関数にはコメント」に、upstream 由来の関数は関数内の `// kaz:` の行コメントにとどめるという例外を足す | v1.4.2 と main の両方で base.go・vfs.go の衝突が消える | 0.5 日 |
| P4 高 | `git rerere` を有効にし、`git merge-tree --write-tree` で衝突を事前に見る手順にする | 繰り返しの解消が楽になる | 1 時間 |
| P2 高 | kaz の設定・状態のフィールドを、埋め込み struct（`KazChunkConfig` など）にまとめる | config.go、cached_store.go、mount.go の桁揃えの差分が消える。JSON は平坦化されるので `.config` のキーは変わらない見込み【推測、未確認】 | 1〜1.5 日 |
| P3 高 | 新しく足した関数を `*_kaz.go` へ移し、upstream 側には hook の 1 行だけを残す。対象は cached_store.go の read・retire 系、writer.go、base.go の WriteSlices、各エンジンの `doWriteSlices`、フラグ（`cmd/kaz_flags.go` にまとめる）。あわせて `WriteSlices` は任意の interface にし、`RetireSlice` は upstream の番号から離す | upstream ファイルの差分が大きく減る。cached_store.go は +511 から +80 程度になる見込み【推測】 | 4〜5 日 |
| P5 高 | upstream にも価値がある一般的な修正を `upstream-fix/*` として分ける。候補は、Read で flush のエラーを返す（2ae17f94 の一部）、reader の EIO が残り続ける問題（145b8b85）、staged block の fdatasync（e6ab89b8）、redis の CopyFileRange／Clone での WATCH、mtime の巻き戻り（18e641b8 の一部）、`get %s: %w` | 採用されれば kaz の差分から消える。v1.4.2 と main に同等の修正が無いことは確認済み | 3〜5 日（PR はユーザーの確認後） |
| P6 中 | 診断のコードを upstream の頻繁に変わる関数から外す（`WithWriterFlushOrigin` を `runtime.Callers` にすれば fs.go の差分は 0 になる。redis の `txn` の slow ログ） | fs.go、redis.go の衝突が減る | 0.5〜1 日 |
| P7 中 | `1.4.2-improve-kaz` へ移すときに、topic ごとの commit に載せ直す（巨大な `84f19ca4`、`2ae17f94` を分割する） | 次の rebase の単位が小さくなる | 2〜3 日 |
| P8（1.5 系では必須） | `cacheStore` の改名への追従を単独の commit にする | main への移行の準備 | 0.5 日 |

推奨の順序は P1 → P4 → P2 → P3 → P6 → P5。P7 と P8 は、次に upstream へ追従するときに行う。

## 検証

環境: Go 1.25.11（`GOENV_VERSION=1.25.11`）。TMPDIR は scratchpad 配下で、root filesystem ではなく tmpfs 上だった。

| 対象 | 結果 |
|---|---|
| マージ版（衝突解消と format の修正済み）`go build ./...` | OK |
| `go vet ./pkg/meta/ ./pkg/chunk/ ./pkg/vfs/ ./cmd/` | OK |
| `go test ./pkg/chunk/ ./pkg/utils/`（A） | 1 回目 153 PASS / 1 FAIL（`TestFinishCanceledGetSharesResultWithWaiter` の TempDir 削除。kaz HEAD でも起きる不安定なテスト）、2 回目 154 PASS。kaz HEAD は 152 PASS（差の 2 件は v1.4.2 の新テスト） |
| v1.4.2 の新テストと header-timeout 系の `-race -count=20`（A） | ok |
| `TestReadAtNeverReportsSuccess*` `-count=5`（A） | マージ版 10/10 PASS。kaz HEAD は load 経路が 3/5 FAIL、detached 経路が 1/5 FAIL |
| `-race`（A） | マージ版に残るのは `disk_cache.go:363`↔`:454` の rawFull の race だけ（kaz HEAD にもある。disk_cache.go は v1.4.2 で変更なし） |
| kaz の meta テスト一式、`TestRedisClient`／`TestSQLiteClient`／`TestMemKVClient`／`TestBadgerClient`、CompactionGC（Redis を使用）（B） | ok |
| `go test ./pkg/vfs`（Redis を使用）、`./pkg/compress`、`./pkg/utils`、`./cmd` の kaz の 26 件（B） | ok |
| 実際の FUSE mount: `TestGenFuseOptHasNoRequestDeadline`、`TestFUSEWaitsForPendingFsync`（B） | PASS |
| `go test ./pkg/fuse` の `TestFUSE/FstatDeleted`（B） | FAIL。マージ前の kaz HEAD でも同じように FAIL する（ctime と mtime のずれ、原因は未調査） |
| `go test ./pkg/chunk`（B） | 1 回目は `getDiskUsage` で SIGSEGV、2 回目は ok（不安定） |

B の検証は、同じ形で解消した別の worktree で行った。

最終の状態（衝突解消と format の修正をした worktree）で、Redis なしで流し直した結果は次のとおり。

- `go test ./pkg/chunk/ ./pkg/utils/ ./pkg/compress/` は ok。
- `./pkg/vfs/` はパッケージごと FAIL。`--- FAIL` の行は出ず、127.0.0.1:6379 への接続を拒否されて止まっていた。
- `./pkg/meta/` の kaz 系と SQLite／MemKV は、`TestWriteSlicesRedis` だけが FAIL した（同じく Redis が無いため）。
- vfs と meta の Redis を使うテストは、B が Redis ありで ok を確認している。

## 未検証

- 実機（本番の Redis・rclone・Drive）での動作。
- #7509 の上書きを、kaz HEAD で専用の再現テストにしたもの。
- `ErrFuncTimeout` を返した後も、放棄された GET は page に書き得る（upstream と同じ構造）。vfs の retry と kaz 145b8b85 との組み合わせは調べていない。
- P2 で `.config` の JSON の形が変わらないこと。
- `TestFUSE/FstatDeleted` が FAIL する原因。

## 6. 実施（2026-10-27）

ユーザーの判断で、ブランチは「本家のタグごとに切り、前の kaz ブランチを merge する」形にした。

- 本体に `1.4.2-improve-kaz` を作った。起点は `v1.4.2`（`46af7afa`）。worktree は `/home/kwatanabe/tmp_local/juicefs-1.4.2-wt` で、`juicefs/` の checkout は `1.4.1-improve-kaz`（kaz.8 のビルド元）のまま。
- `git merge --no-commit 1.4.1-improve-kaz` を実行し、1 章のとおり衝突を解消した。解消済みの分は index に載せてあり、commit はユーザーの確認待ち。
- format の修正のうちテスト側は、3 章の推奨どおり `Clone()` してから `setFormat` する形にした。helper は `kazTestUpdateFormat`（`pkg/meta/compaction_block_kaz_helpers_test.go`）。
- whole read の注記を足した。
  - `--kaz-compaction-mode` の help（`cmd/flags.go`）に「delta は 1 MiB 以下の object をキャッシュミスのとき丸ごと取るが、lz4・zstd では最初から丸ごと取るので何も変わらない」と書いた。
  - `kazWholeRead` のコメント（`pkg/chunk/cached_store_kaz.go`）にも同じことを書いた。
  - upstream の docs には compaction mode の項がないので、docs には書いていない。
- `--kaz-upgrade-busy=fail|stack` を足した（既定は `fail`）。
  - ファイルは `cmd/kaz_upgrade_unix.go` と、そのテスト。upstream のファイルへの差分は `cmd/mount_unix.go` の 4 行（`launchMount` の hook と、フラグの追加）だけ。
  - `fail` の動き: graceful upgrade で session を引き継げなかったとき（busy、volume やオプションの違いなど）、mountpoint がまだ JuiceFS の root（inode 1）なら、上に重ねずにエラーで終わる。古い mount はそのまま動き続ける。
  - `stack` は upstream と同じ動きになる。不正な値は起動時に拒否する。

### upstream の「上に重ねる」の実際の動き（実 FUSE で観測）

手元（root ではない、fusermount を使用）で、mount 中の mountpoint に別の volume を `--kaz-upgrade-busy=stack` で mount した。

- 最初の子プロセスは `fusermount exited with code 256` で失敗した。root でないと、FUSE の mount の上に重ねる mount を fusermount が拒否する。
- supervisor の再起動ループ（`cmd/mount_unix.go` の `if fuseFd == 0 && (attempt > 0 || …) { doUmount(mp, true) }`）が、**古い mount を強制的に外し**、そこへ新しい volume を mount した。古いプロセスは「exit successfully」で終わっていた。
- 結論: upstream の「上に重ねる」は、root なら 2 重 mount になり、root でなければ古い mount を強制的に外す。後者は、VM が開いているファイルを持ったまま mount が消えることを意味する。graceful upgrade が busy で諦めた後も、同じ経路を通る（`shutdownGraceful` は busy のとき `fuseFd = 0` にする）。
- `fail`（既定）で同じ操作をすると、新しいコマンドはエラーで終わった。mount は 1 つのままで、古い mount のファイルは読めた。
  - バックグラウンド起動では、元のコマンドの表示は「The mount point is not ready in 10 seconds … please check the log」になる。理由は、新しい mount のログに出る。
  - 不正な値（`stak`）は起動時に拒否された。

### 検証（1.4.2-improve-kaz の worktree、Go 1.25.11、一時 Redis 8.0.5 を 127.0.0.1:6379 で起動し、終了後に停止）

| 対象 | 結果 |
|---|---|
| `go build ./...`、`go vet ./pkg/meta/ ./pkg/chunk/ ./pkg/vfs/ ./cmd/` | OK |
| `go test ./pkg/vfs/ ./pkg/utils/ ./pkg/compress/` | ok |
| `go test ./pkg/chunk/`（TMPDIR は root filesystem 上） | ok。TMPDIR を tmpfs に置くと `TestInRootVolume` が落ちる。テストの前提（作業ディレクトリと TMPDIR が同じ volume）によるもの |
| `go test ./pkg/meta/ -run 'Kaz\|Compaction\|WriteSlices\|Delta\|Block\|Retire\|DeleteQueue\|TestRedisClient\|TestSQLiteClient\|TestMemKVClient\|TestBadgerClient'` | ok（99s） |
| CompactionGC（Redis） | ok |
| `go test ./cmd/ -run 'Kaz\|WriterFlush\|MaxDeletes\|MetaWriteBatch'`（`--kaz-upgrade-busy` の 3 テストを含む） | ok |
| `go test ./pkg/fuse/ -run 'TestGenFuseOptHasNoRequestDeadline\|TestFUSEWaitsForPendingFsync'`（実 FUSE） | PASS |

未検証:
- Windows のビルド。手元では cgo の依存でクロスビルドできない。新しいファイルは `!windows` 限定。
- root での 2 重 mount の観測。
- VM が動いている状態で busy になるかどうか。
- 次の版のタグは `juicefs-v1.4.2-kaz.1` になる見込み。workflow はタグの `<base>` と本体の版（1.4.2）を照合し、go.mod は `go 1.25.0` なので Go 1.25.11 のまま使える。

### commit（2026-10-27、ユーザー承認）

ユーザーの方針で、`1.4.2-improve-kaz` には直接 commit しない。項目ごとにブランチを切り、`--no-ff` で merge する。push はしていない。

| 1.4.2-improve-kaz の merge | 項目のブランチ | 中身 |
|---|---|---|
| `aaf4e6e3` | `port/kaz-onto-v1.4.2`（`7255adb5`、v1.4.2 から切って 1.4.1-improve-kaz を merge） | 衝突の解消と、format の atomic 化への追従 |
| `44eb26da` | `docs/kaz-whole-read-compressed` | whole read の help とコメント |
| `97254d5d` | `feat/kaz-upgrade-busy` | `--kaz-upgrade-busy` |

merge 後に build、vet、cmd の kaz 系、meta の kaz 系と SQLite／MemKV、`TestWriteSlicesRedis`（一時 Redis）、chunk の read 系を流し直し、ok だった。

## 7. 取り込みやすさの改善と、フラグの改名（2026-10-27、未 commit）

本家へ PR は出さない方針（ユーザー）なので、提案の P5 は取りやめた。

- **P1（本体 `refactor/kaz-upstream-comments`）:**
  - upstream の関数・型の直前にあった kaz の doc コメント 49 か所を、元の文面に戻した。v1.4.2 の merge のときに足した `getResult` と `loadRange` の前のコメントも含む。
  - 関数の中の kaz の変更行の上に、`// kaz:` を 39 か所入れた。kaz の変更箇所にすでに説明がある 10 か所には入れていない。
  - 変更はコメントと空行だけ。
  - 本家 main と試しに merge したときの衝突ブロックのうち、kaz が原因の分は 16 → 13 になった。base.go と vfs.go の分が消え、cached_store.go が 1 つ減った。残りは cached_store.go 5、disk_cache.go 4、docs 3、writer.go 1。
  - 調査リポジトリの AGENTS.md に、コメントの規則の例外を足した。
- **P4:** 本体で `rerere.enabled=true` にし、v1.4.2 の解消を学習させた。手順書と、衝突ブロックを数えるスクリプトは [`docs/development/upstream-sync/`](../../../development/upstream-sync/README-ja.md)。
- **フラグの改名（本体 `feat/kaz-flag-prefix`）:**
  - kaz が足したフラグのうち、`kaz-` で始まらない 9 個を `kaz-` 付きに改名した。旧名は urfave/cli の別名（Aliases）として残り、使うと起動時に警告が出る。
  - help、エラーの文言、本体の docs（英語・中国語）を新しい名前にした。
  - 実際の mount で、旧名で渡した値が `.config` に入ること、警告がログにも出ることを確かめた。
  - `flag.FlagSet` を直接パースするテストでは、別名の値が正式名へ写らないため（`cli.App` のパースの中で写す）、そうしたテスト 2 ファイルは新しい名前に直した。`cli.App` を使うテストは旧名のまま残し、別名の確認を兼ねている。
- **検証:** build と vet、cmd の kaz 系・オプション系、meta の設定系、vfs の全体（一時 Redis）が通った。

## 8. リファクタリングの計画の性能の確認（2026-10-27、実装の前）

[実装計画](../../../superpowers/plans/2026-10-27-kaz-upstream-friendly-refactor.md) の各 Task が、毎回通る経路の速さを落とさないかを確かめた。測定は Go 1.25.11、WSL2 上で、一時的なベンチマークのファイルを置いて行い、終わった後に消した。

| 測ったもの | 1 回あたり | 確保 | 関係する Task |
|---|---|---|---|
| INFO のときの、今の無条件の `logger.Debugf`（metadata write の 1 行、引数 4 個） | 2.3 ns | 0 | 既存（変えない） |
| 同じログを `IsLevelEnabled` で囲んだ場合 | 0.3 ns | 0 | — |
| 任意 interface への型アサーション（commit ごとに 1 回） | 2 ns | 0 | Task 3 |
| 今の計測の形（`time.Now` と defer の closure） | 120〜130 ns | 0 | 既存 |
| 計測を値型の helper にまとめた形 | 122 ns | 0 | Task 7 |
| 計測の helper をポインタで返す形 | 147 ns | 1 回（64 B） | Task 7 では使わない |
| zstd の展開 4 MiB、v1.4.1 の `zstd.Decompress` | 約 0.97 ms（約 4.3 GB/s） | — | v1.4.2 の #7556 |
| 同、v1.4.2 の `DecompressInto` | 約 0.96 ms（約 4.35 GB/s） | — | 同上 |

結論は次のとおり。

- **ファイル間の移動（Task 1〜4）と型エイリアス（Task 5）は、生成されるコードが変わらない。** Go の inline 展開は、ファイルをまたいでも同じように働く。
- **埋め込み struct（Task 6）は、フィールドの位置がコンパイル時に決まるので、アクセスのコストは変わらない。** 並びの変更でパディングが数バイト変わる程度。`cachedStore` と `baseMeta` は 1 つずつ、`fileWriter` は開いているファイルの数だけある。
- **型アサーション（Task 3）の 2 ns は、Redis への 1 回の書き込み（0.1〜数 ms）に比べて無視できる。**
- **Task 7 の helper は、値型にすれば今と同じ。** ポインタで返すと、metadata の操作ごとに確保が 1 回増える。そのため計画では値型にし、確保が 0 であることをベンチマークと `-gcflags=-m` で確かめる手順を入れた。
- **v1.4.2 の本家の変更で、毎回通る経路のもの:**
  - zstd の展開: 新旧で差がない。
  - format の atomic 化: 読むたびに atomic load が 1 回増える程度。
  - #7500 の `getResult`: 小さい struct のコピーだけ。
  - Redis の session の heartbeat（#7336）: WATCH を使わなくなり、軽くなる方向。
  - バックアップ（#7599）: `StatFS` の引数が変わるだけ。
  - `Flock` の読み取りロック（#7340）: Redis への操作が 1 回増えるが、`flock` を使うときだけ。QEMU は `fcntl` のロックを使う。
- **この結論はマイクロベンチマークとソースからの判断で、実機の負荷での比較はしていない。**

## 9. 取り込みやすさのリファクタリングの実施（2026-10-27、未 merge）

[実装計画](../../../superpowers/plans/2026-10-27-kaz-upstream-friendly-refactor.md) を subagent-driven で実行した。本体のブランチ `refactor/kaz-upstream-friendly` を `1.4.2-improve-kaz`（7dfb8fd1）から切り、Task ごとに commit した（Task ごとのブランチは切っていない。ユーザー指示）。`1.4.2-improve-kaz` への `--no-ff` の merge は、ユーザーの実機テストの後に行う。台帳は `.superpowers/sdd/2026-10-27-kaz-upstream-friendly-refactor/progress.md`。

| Task | commit | 内容 | upstream のファイルの差分 | kaz が原因の衝突（本家 main） |
|---|---|---|---|---|
| 開始時 | 7dfb8fd1 | — | 25 files +2,061 −239 | 13 |
| 0 | 76dc5cd0..3141704a | `.config` のキーと mount のフラグ一式の golden テスト、`upstream_diff_size.sh` | 同上 | 13 |
| 1 | e7258be2 | pkg/chunk の kaz の定義を `*_kaz.go` へ | 21 files +1,612 −252 | 10 |
| 2 | dccb05ba | pkg/vfs の writer・flush の補助を `writer_kaz.go`・`vfs_kaz.go` へ | +1,217 | 10 |
| 3 | 5f10f1b0 | pkg/meta の WriteSlices 等を移し、`Meta.WriteSlices` を任意の `KazSliceWriter` に、`RetireSlice` を `1<<16+1` に | 20 files +841 −257 | 10 |
| 4 | 2503a094..8c6e4312 | cmd の kaz のフラグを `kaz_flags.go` へ、設定の取り込みを `applyKaz*` へ | +608 −249 | 10 |
| 5 | d613f436..a0e2bf3b | 1.5 系の準備（`kazDiskCache` エイリアス、本家と同じ `flushPage` と kaz の `flushPageOpt`、`kazSliceKeys`） | +586 −246 | 9 |
| 6 | 65772e8e..60463621 | 設定・状態のフィールドを埋め込み struct へ（7 つの struct の桁揃えが v1.4.2 に戻った） | +460 −196 | 9 |
| 7 | b9186f9e | compaction・transaction・Write の計測を値型の helper へ（ログの書式は不変、0 allocs） | 20 files +425 −196 | 9 |

### 最終レビュー

最終レビュー（全体）の判定は「実機テストの後に merge してよい」。

- 本番で使う経路の挙動、フラグ、`.config`、ログの書式、性能に変化はなかった。
  - フラグ: 全 30 コマンド・575 フラグについて、名前・別名・既定値・Usage・EnvVars・Hidden・Action の有無を比べ、完全に一致した。
  - ログの書式: 調査スクリプトがパースする行（writer flush、slice freeze/finish、metadata write／transaction、compaction の phase、`payload_bytes=`）は変わらない。
- テストだけの問題（golden テストが、`go test ./cmd/` で全体を流したときの順序によって落ちる）が 1 件あり、直した。

### 残っている衝突の元（kaz が原因の 9 ブロック）

| ファイル | ブロック数 | 中身 | 扱い |
|---|---|---|---|
| `cached_store.go` | 2 | `wSlice.upload` の staging の分岐（本家 #7517 と重なる） | 1.5 系への追従のときに行う（スコープ外） |
| `cached_store.go` | 2 | `loadRange` の prefetch の抑止、`load` の `getWithHeaderTimeout` | 1 行の hook。避けにくい |
| `disk_cache.go` | 1 | `remove`（本家の改名と重なる） | 1.5 系のときに、本家の `cache.scanned ||` の条件と kaz の `removeLocal` の違いを見直す |
| `writer.go` | 1 | `flush`（本家 #7693） | スコープ外 |
| `_common_options.mdx` | 3 | kaz のオプションの説明 | 次の候補（kaz の別文書へ出せば 3 つ減る） |

### 1.5 系へ移るときに要ること

- エイリアス（`type kazDiskCache = cacheStore`、`var kazNewDiskCache = newCacheStore`）を、`diskCache`／`newDiskCache` に張り替える。`flushPageOpt` が使うメソッドは main にもあることを確かめた。
- `flushPageOpt` は本家の `flushPage` の写しなので、本家の修正を手で入れる。
- `TestKazSliceKeysMatchesKeys` は `rSlice.keys()`（main には無い）と比べているので書き換える。
- `commitThread`、`flush`、`upload` の staging の分岐を、本家の修正と一緒に kaz の関数へ移す。Write／compactChunk／txn を DEBUG で流して phase の並びを照合するテストも、このときに足す。

### 最終の状態と実機テスト用のバイナリ

- ブランチの HEAD は `6c558773`。最終レビューの指摘の修正（golden テストを、他のテストが設定する `debugAgent` に左右されないようにした）を含む。
- 一時 Redis を立てて `go test ./cmd/ -skip 'TestIntegration|TestJFS|TestRestore'` を流し、ok だった。除いた 3 つは環境のせいで起点でも落ちる（MinIO の変数がない、root でない）。
- 実機テスト用のバイナリ:
  - パス: `~/.local/bin/kaz-test/juicefs-142-refactor-6c558773`
  - `juicefs version`: `1.4.2+2026-10-27.6c558773`
  - sha256 の先頭 16 桁: `c8b41c8e49cab74f`

### 追加（2026-10-27）: docs の kaz の行の移動

- commit `647b0a59`。kaz が本家の `_common_options.mdx`（英語・中国語）に足していた行（英語 10 行、中国語 5 行。`--max-deletes` の説明の書き換えを含む）を、`docs/{en,zh_cn}/reference/kaz_options.md` へそのまま移した。2 つの `_common_options.mdx` は v1.4.2 と同一に戻した。
- 本家のサイトの目次（sidebar）はこのリポジトリにないので、新しいページは Markdown として置くだけにした。kaz の全オプションは `juicefs mount --help` の `[kaz]` で確かめる、とページに書いた。
- 計測: upstream のファイルの差分は 18 files +410 −195。kaz が原因の衝突は 9 → 6 ブロック（cached_store.go 4、disk_cache.go 1、writer.go 1。どれも 1.5 系への追従のときに本家の修正と一緒に扱う）。
- 実機テスト用のバイナリは作り直していない（docs だけの変更なので、中身は 6c558773 と同じ）。
- 日本語版 `docs/ja/reference/kaz_options.md` を追加した（commit `4049d608`）。本家の docs に日本語版はなく、kaz のページだけを訳した。英語版と同じ 10 個のオプションを同じ順で載せている。
- 2026-10-27 ユーザーの実機テストで問題がなかったので、ユーザーの指示で `refactor/kaz-upstream-friendly` を `1.4.2-improve-kaz` へ `--no-ff` で merge した（`d246b0f7`）。merge 後のツリーは、まとめ用ブランチと同一。push はしていない。
