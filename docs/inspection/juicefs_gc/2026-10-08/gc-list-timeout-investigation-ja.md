# juicefs gc の `list all blocks` タイムアウトの調査（2026-10-08）

読み取り専用の調査。本番の JuiceFS・Redis・rclone（localhost:9090、rc）・Drive には一切要求を送っていない。

- JuiceFS ソース: `juicefs/`（HEAD 78acd63d、v1.4.1 ベース）
- rclone ソース: `rclone/`（dab33da31、1.75.1-improve-kaz）、gofakes3 は `$GOMODCACHE/github.com/rclone/gofakes3@v0.0.9`
- 本番の rclone のログ（読むだけ）:
  - `/home/kwatanabe/.rclone_serve_s3.log` の先頭の固定 prefix **15,493,592 バイト**（129,789 行、17:41:08〜20:19:48）を scratchpad にコピーして解析。sha256 = `5d226c737a86c1e1582c2623583d423c9457efd73583bc6bc1fd121844215b56`
  - 追記分（15,493,593 バイト目以降、20:22:19 時点でファイルは 15,494,084 バイト、5 行）は時刻とパスだけ確認した
  - ローテーション済みの `.log.*` は存在しなかった
  - 秘密情報（auth key、token など）は本書に転記していない

## 要点

1. **観測**: rclone は 20:07:38 に `LIST BUCKET`（prefix `juicefs-data/chunks/`、delimiter 空、`Marker: HasMarker:true MaxKeys:1000`）を受けた。JuiceFS は 30 秒後の 20:08:08 に `timeout awaiting response headers` で失敗した。時刻はちょうど 30 秒ずれている。
2. **観測**: JuiceFS が切断した後も、rclone は走査を続けている。進み具合は `Reset virtual modtime` の行から分かり、`chunks/0/103`（20:07:57）→ `0/16`（20:11:28）→ `0/346`（20:19:48）→ `0/384`（20:22:03）と、キーの辞書順に進んでいた。20:22 の時点では、まだ `chunks/0/` の途中だった。
3. **ソース上の条件**: rclone の ListBucket（`cmd/serve/s3/backend.go:91-124`）は、prefix 配下のディレクトリをすべて再帰でたどって全件を集める（`list.go:16-65`）。その後に並べ替えてから 1,000 件を切り出している（`pager.go:11-66`）。このため、**最初の 1 ページを返す前に全件の走査が終わっている必要がある**。ctx は使われておらず、クライアントが切断しても走査は止まらない。
4. **ソース上の条件**: ページの境目では、2 ページ目以降も毎回、全件の走査と並べ替えをやり直す。マーカー（`start-after`、continuation-token）は、そのキーと完全に一致する object を探して切るだけである（`pager.go:24-37`）。一致する object が無いときは、先頭から返す。
5. **ソース上の条件**: JuiceFS の gc は、メタデータ側の処理を先に全部済ませる（trash、detached node、遅延削除ファイル、`ListSlices`。`--delete` のときは削除も含む）。そのあと最後に `object.ListAll(chunks/)` を呼ぶ（`cmd/gc.go:229-233`）。最初の List が失敗すると、リトライせずに `logger.Fatalf` で終了する（`sharding.go:113-119`）。2 ページ目以降の失敗だけは、無限にリトライする（`sharding.go:147-152`）。
6. **ソース上の条件**: 30 秒は `pkg/object/restful.go:157` に `ResponseHeaderTimeout: 30s` として固定されている。フラグ・環境変数・URL のクエリのどれでも変えられない。`juicefs gc` のフラグは `--compact`、`--delete`、`--threads` だけである（`cmd/gc.go:57-74`）。`--get-timeout`／`--put-timeout` は chunk の読み書きにしか効かず、gc には存在しない。S3 SDK は `RetryMaxAttempts = 1`（`s3.go:587`）。
7. **ソース上の条件**: gc の object 一覧を省くオプションや、範囲を絞るオプションは無い。`--delete`、`--compact`、`--threads` のどれを指定しても一覧は必ず行われる。
8. **影響（ソース上）**: FATAL の時点で、leaked object の削除はまだ始まっていない（削除ワーカーは一覧の後）。`--delete` を付けていれば、メタデータ側の掃除と pending slice の削除は FATAL より前に完了している。`os.Exit` で終わるので、`CloseSession` の defer は実行されない。セッションは Redis 側で期限切れになるまで残る。データの整合を壊す途中状態は、ソース上は見当たらない。
9. **推測（見積もり）**: 20:07:38 から 20:22:03 の 864 秒で、`chunks/0/` の中を辞書順で "384" まで進んだ。`0/0`〜`0/999` がすべて存在すると仮定すると、約 318 ディレクトリになり、**約 2.7 秒／ディレクトリ**。1,442 フォルダなら約 65 分、`chunks/0..6` の約 6,476 フォルダがすべてあるなら約 4.9 時間かかる。走査は 1 スレッドで直列に行われ、Drive の応答待ちが律速になっている。`--tpslimit 20` が上限になっているわけではない（API 呼び出しは約 1 回／秒の見込み）。
10. **ソース上の条件**: たとえ JuiceFS が待ち続けても、rclone の `--server-write-timeout 3m`（http.Server.WriteTimeout、`lib/http/server.go:300`）を過ぎると応答を書けない。**JuiceFS 側のタイムアウトを延ばすだけでは解決しない**。
11. lookup モード（`--kaz-vfs-lookup-by-path`、本番で有効）でも、ListObjectsV2 の動きは変わらない。`ReadDirAll` → `_readDir`（`vfs/dir.go:530-584`）は、lookup で入った entry だけを持つディレクトリを、まだ読んでいない（stale）ものとして扱い、全件を一覧する。`Reset virtual modtime` は、lookup などで既に `d.items` にある名前（または Drive 上で名前が重複している object）を、一覧で更新したときにだけ出る（`vfs/dir.go:749-750`、`vfs/file.go:548-555`）。この行が、走査の進み具合を示す間接的な目印になっている。
12. rclone の版は、ログ冒頭（18:53:07 の起動）の `Version "v1.75.1-kaz.1"` で、kaz 版である。
13. 副作用（推測）: 切断された走査は、終わるまで（数十分〜数時間）Drive の API と tpslimit を、マウント側の I/O と取り合う。gc を再実行すると、走査がもう一本並行して走る。完了した結果は書き込み時に破棄される。

修正・回避の案（詳細は後述）:

| 案 | 変更箇所 | 効果 | 推奨 |
|---|---|---|---|
| (a) rclone の ListBucket を、キー順の DFS で max-keys+1 件たまったら打ち切る方式にする。マーカーより前の部分木は読まない | rclone `cmd/serve/s3/list.go`、`pager.go`、`backend.go` | 1 ページあたり数ディレクトリ分の読み込みで済む（30 秒以内）。全体の Drive 呼び出しは 1 周分のまま | ◎（本命） |
| (d) JuiceFS gc の一覧を `ListAllWithDelimiter`（ディレクトリごと、10 並列）に切り替える | JuiceFS `cmd/gc.go:230` | 1 要求あたり 1 ディレクトリ（数千件）。rclone を変えずに済む | ○（JuiceFS だけで完結する） |
| (b) JuiceFS の ResponseHeaderTimeout を設定できるようにする | `pkg/object/restful.go:157` ほか | rclone の write-timeout 3m と、全件走査（1 時間以上）に阻まれ、単独では効かない | △ |
| (c) gc の前に一覧のキャッシュを温める運用 | 運用のみ（dir-cache-time の延長と再起動が要る） | 1 時間以上かかる走査では、dir-cache-time 1h のうちに先頭が期限切れになる。キャッシュが温まっていても、ページごとに数百万件を並べ替え直す | × |

---

## 詳細

### 1. JuiceFS gc の流れ（`cmd/gc.go`）

| 段階 | 行 | 内容 | 一覧が必要か |
|---|---|---|---|
| 準備 | 76-101 | meta の Load、NewSession、createStorage | – |
| trash／detached node | 138-146 | `--delete` のときだけ実行。`CleanupTrashBefore`、`CleanupDetachedNodesBefore` | 不要 |
| 遅延削除ファイル | 148-164 | `ScanDeletedObject`（`--delete` で削除） | 不要 |
| compact | 166-191 | `--compact` のときに `CompactAll` | 不要 |
| slice の一覧 | 196-201 | `m.ListSlices(c, slices, true, delete, …)`。Redis では `cleanupLeakedInodes`（`found dangling inode`、`redis.go:3998`）、`cleanupLeakedChunks`（`found leaked chunk`、`redis.go:3657`）、`cleanupOldSliceRefs`、`doCleanupSlices` を順に行う（`redis.go:4064-4071`） | 不要 |
| trash slice | 203-226 | `ScanDeletedObject` | 不要 |
| **object の全件一覧** | **229-233** | `object.WithPrefix(blob, "chunks/")`、`object.ListAll(ctx, blob, "", "", true, false)`。失敗すると `logger.Fatalf("list all blocks")` | **必要（省けない）** |
| 照合と削除 | 270-367 | `threads` 本のワーカーが、leaked と判定されたものを `blob.Delete`（`--delete` のとき）。size 0 または mtime 0 の object だけ Head する | – |

- 症状のログで 20:07:26〜20:07:31 に出ていた dangling inode と leaked chunk は、`ListSlices` の中の処理である。20:07:38 に一覧が始まり、30 秒後の 20:08:08 に FATAL になった。rclone 側の受信時刻と一致する。
- 一覧を省く・絞るオプションは無い。`--compact`／`--delete`／`--threads` は一覧の有無に影響しない（229-233 行は無条件に実行される）。環境変数は `JFS_GC_SKIPPEDTIME`（新しい object を無視する秒数、113-122 行）だけである。
- FATAL の影響（ソース上）:
  - logrus の Fatal は os.Exit。`defer m.CloseSession()`（88 行）は実行されず、セッションは期限が切れるまで残る。
  - leaked object の削除は、一覧の後（270 行以降）なので、まだ 1 件も実行されていない。
  - `--delete` のときは、trash、detached node、遅延削除ファイル、pending slice（`doCleanupSlices` と `DeleteSlice` → `store.Remove`）の掃除が、すでに完了している（または完了した分だけが反映されている）。これらは、メタデータを先に消して object を後で消す順序の既存処理なので、途中で終わっても「参照されている object が消える」方向の不整合にはならない（ソース上の判断。実データでは確認していない）。
  - `--delete` を付けない実行なら、変更はほとんど無い（ListSlices の `delete=false`）。ユーザーの実行に `--delete` があったかどうかは、この調査では未確認。

### 1.1 ListAll のページングと 1 回目の要求

- `object.ListAll`（`pkg/object/sharding.go:103-158`）:
  - store（`withPrefix` → `s3client`）の `ListAll` は notSupported を返す（`s3.go:294-296`、`prefix.go:199-`）。そのため `store.List(ctx, prefix, marker, "", "", maxResults=10000, …)` を呼ぶ（113 行）。
  - `s3client.List` は limit を 1,000 に丸める（`s3.go:240-243`）。そのうえで `ListObjectsV2(Bucket=buckets, Prefix=juicefs-data/chunks/, MaxKeys=1000, EncodingType=url, StartAfter="", Delimiter="")` を送る。これが症状の URL（`delimiter=&…&max-keys=1000&prefix=juicefs-data%2Fchunks%2F&start-after=`）である。
  - **1 回目の失敗はそのまま返る**（117-119 行 → gc.go:232 の Fatalf）。2 回目以降は `StartAfter=直前のキー`、`ContinuationToken` を付けて送り、失敗しても 100 ms 間隔で無限にリトライする（146-152 行）。
- 30 秒の出どころ: `pkg/object/restful.go:151-185` の init で作られる共有 `httpClient`。`ResponseHeaderTimeout: 30s`、`Timeout: 1h`、Dial 10s。S3 クライアントは `cfg.HTTPClient = httpClient`（`s3.go:621`）、`RetryMaxAttempts = 1`（`s3.go:587`）。この値を書き換えているのは TLS の設定だけ（`cmd/format.go:250,274-275`）で、タイムアウトを変える手段（フラグ、環境変数、bucket URL のクエリ）は無い。`--get-timeout`／`--put-timeout`（`cmd/flags.go:120,125`）は `chunk.Config` の Get/PutTimeout で、mount などのデータ I/O の上限にしか使われない。gc のフラグにも存在しない。

### 2. rclone serve s3 の ListObjectsV2

処理の流れ（ソース上）:

1. gofakes3 `listBucket`（`gofakes3.go:247-`）が `LIST BUCKET` をログに出し、`listBucketPageFromQuery`（1334-1365）でページ指定を作る。
   - V2 では `continuation-token`（base64 の NextMarker）を優先し、無ければ `start-after` を使う。
   - `start-after=` が空でもクエリにキーがあるので `HasMarker:true, Marker:""` になる（ログと一致）。
   - その後 `storage.ListBucket(ctx, …)` を呼ぶ。
2. `s3Backend.ListBucket`（`backend.go:91-124`）:
   - bucket を Stat する。
   - delimiter が空なので `HasDelimiter=false` にする。
   - `prefixParser` で `juicefs-data/chunks/` を path=`juicefs-data/chunks`、remaining=`""` に分ける（`utils.go:133-139`）。
   - `entryListR(..., addPrefix=false)` を呼ぶ。
   - **ctx は使わない。**
3. `entryListR`（`list.go:16-65`）:
   - `getDirEntries` → `VFS.Stat` と `dir.ReadDirAll()`（`utils.go:18-38`）でディレクトリを読む。
   - サブディレクトリには再帰する（49 行）。
   - ファイルはすべて `response.Add` する（54-61 行）。
   - **件数の上限も、途中で打ち切る条件も無い。** prefix 配下の全 object（数百万件規模の見込み）を、メモリ上の ObjectList に積む。
4. `pager`（`pager.go:11-66`）:
   - CommonPrefixes と Contents を全件並べ替える。
   - Marker と**完全に一致するキー**の次から切る。一致しなければ切らない。
   - 先頭から MaxKeys 件を返す。NextMarker は最後のキー。
   - 2 ページ目以降も、毎回 3 → 4 を最初からやり直す。
5. VFS の `ReadDirAll` → `_readDir`（`vfs/dir.go:530-584`、1024-1040）:
   - `d.read` が空、または `--dir-cache-time`（1h）より古ければ、`list.DirSorted` → drive `ListP` で全件を一覧する。
   - 初回の読み込みはログに出ない。2 回目以降の再読み込みだけ `Re-reading directory` が出る（533-535 行）。
   - 一覧の後、`cleanupTimer` を DirCacheTime×2 にセットする。
6. Drive:
   - `list_chunk` の既定は 1000（`backend/drive/drive.go:503,1139-1140`）。1 ディレクトリにつき files.list が ceil(件数/1000) 回。
   - `--drive-kaz-properties` では、list の fields に `properties` が加わる（`backend/drive/kaz_properties.go:11-14`）。応答が少し大きくなる。
   - DEBUG ログには files.list の呼び出しが 1 回ずつ出るわけではない（`--dump` が無いため）。**回数はログから数えられない。**
- lookup モード（`--kaz-vfs-lookup-by-path`、本番で有効）の影響:
  - Stat（GET/HEAD）は `d.lookup`（`vfs/dir_kaz_lookup.go:22-65`）で 1 件だけ解決し、ディレクトリを読んだことにしない（`d.read` は空のまま、`vfs/dir.go:869-878`）。
  - そのため、ListObjectsV2 の `ReadDirAll` では、そのディレクトリを必ず全件一覧する。**一覧のコストは lookup モードの有無で変わらない。**
  - lookup で `d.items` に入っていた名前は、一覧で `setObjectNoUpdate` が呼ばれ、`Reset virtual modtime` が出る。
- `--kaz-s3-persist-metadata`: `entryListR` は、一覧で得た Size、ModTime、md5 だけを使う（`list.go:54-60`）。object ごとの追加の API 呼び出しは、ソース上は無い。

Drive API 回数の見積もり（推測）:
- Σ ceil(n_i/1000) に、上位ディレクトリと path 解決の十数回を足したもの。1,442 フォルダ × 数千件なら、約 4,000〜7,000 回。
- 観測した速度（約 2.7 秒／ディレクトリ）からは約 1〜2 回／秒で、tpslimit 20 よりずっと少ない。1 スレッドの直列走査で、Drive の遅延が律速になっている。

### 3. 本番の rclone のログ（固定 prefix 15,493,592 バイト）

- 19:52 以降の出来事（括弧内は解析した prefix 内の行番号）:
  - 18:52:44 SIGTERM → 18:53:07 起動、`Version "v1.75.1-kaz.1"`（119458-119459 行）。つまり、gc の時点のディレクトリキャッシュは、18:53 以降に読んだ分しか無い。
  - 18:53 以降、`Re-reading directory` は 0 件。
  - 19:52:46、20:03:16、20:06:46 に、同じ 6 object（`chunks/0/16/16385_1_4194304` など）への GET が繰り返されている。いずれも `broken pipe` と `superfluous response.WriteHeader` で終わっている（129601-129756 行）。gc の前の段階か、別のクライアントの読み込みかは未確認。
  - **20:07:38 `serve s3: LIST BUCKET` と `bucketname: buckets prefix: prefix:"juicefs-data/chunks/", delim:"" page: {Marker: HasMarker:true MaxKeys:1000}`（129757-129758 行）。** 解析範囲内の LIST BUCKET はこの 1 件だけ。
  - その後は `Reset virtual modtime` だけが、辞書順に出ている（129759-129789 行、追記分にも続きがある）:
    `0/103`（20:07:57）、`0/110`（20:08:24）、`0/16`（20:11:28）、`0/182`（20:12:47）、`0/20`（20:13:28）、`0/27`（20:15:10）、`0/280`（20:15:49）、`0/309`（20:17:35）、`0/33`（20:18:48）、`0/346`（20:19:48）、`0/37`（20:21:11）、`0/384`（20:22:03）。
  - 20:12:10 に Drive の token を更新（値は転記しない）。
- 解釈:
  - 観測: rclone は 20:08:08 に JuiceFS が切断した後も、少なくとも 20:22:03 まで走査を続けている。応答はまだ書かれていない。完了の行も、エラーの行も無い。
  - 観測: `0/16` の 3 件は、GET（lookup）で既に知っていた名前なので、Reset の行が出た。`0/103/103360_0_1572` や、`0/33/33094_0_65536`（同じ秒に 2 回）は、解析範囲内で他に出てこない。lookup（ログに出ない HEAD など）か、Drive 上の同名重複（同じ一覧で 2 回目に出たとき、`vfs/dir.go:741-750` で既存の node を再利用する）によるものと推測する。重複の有無は未検証。
  - 推測: 初回応答までに要る時間は、走査全体の時間（1,442 フォルダなら約 1 時間、全フォルダがあるなら約 5 時間）。走査が終わった時点では、`--server-write-timeout 3m` も JuiceFS の切断も過ぎているので、応答は書けずに破棄される見込み。
  - 「最後まで処理を続けたか」「完了までにかかった時間」は、20:22 の時点で走査中のため未確定。後で、同じログの追記分で `chunks/6/…` 付近の Reset の行と、`readfrom … broken pipe`、`superfluous response.WriteHeader` を探せば分かる。

### 4. 修正・回避の案

#### (a) rclone の ListBucket を「キー順の DFS で必要な分だけ読む」方式にする（推奨）

- 変更箇所: `cmd/serve/s3/list.go`（entryListR）、`pager.go`、`backend.go:91-124`。
- 方式:
  - ディレクトリの子を「名前＋（ディレクトリなら `/`）」の順に並べて、深さ優先でたどる。
  - この順序は、一般に S3 のキーの辞書順と一致する。ディレクトリ配下のキーは、すべて `name/` で始まるため。JuiceFS のキーは数字、`_`、`/` だけなので、名前のままの順でも一致する。
  - ただし `-`、`.`、空白などの `/` より小さい文字を含む名前では、名前のままの順では一致しない。
  - マーカー以下の部分木は読まずに飛ばす（`dirKey+"/" <= marker` で、かつ marker がその prefix で始まらない場合）。
  - Contents と CommonPrefixes の合計が MaxKeys＋1 件になったら、打ち切って IsTruncated にする。
  - マーカーとの比較は、完全一致ではなく `key > marker` で行う。これで、削除されたキーをマーカーにしたときに先頭から返す既存の不具合も直る。
  - ctx を受け取り、切断されたら走査を止める。
- 効果:
  - 1 ページで読むのは、親の数ディレクトリと葉の 1 ディレクトリ（数千件、Drive 数ページ、数秒）だけになる。30 秒にも 3 分にも収まる。
  - 全ページを通した Drive 呼び出しは、従来の 1 周分と同じ。ページごとに全件を並べ替え直す O(N²/1000) の CPU コストも無くなる。
  - 走査の途中でキャッシュの期限が切れて、再読み込みになる可能性はある。
- 副作用:
  - delimiter ありのときや、prefix が途中の名前で終わるとき（remaining）の扱いを保つ必要がある。
  - lookup で入った entry や、virtual entry（アップロード中の entry）の扱いは、ReadDirAll の結果を使う限り今と同じ。
  - 一覧の途中で他の要求がディレクトリを変更しても、S3 の弱い一貫性の範囲に収まる。
  - 上流の rclone から離れる差分になる。
- テストのしやすさ: 高い。
  - `cmd/serve/s3` に `pager_test.go` と `backend_test.go` がある。
  - local／memory backend で、ランダムな木（`-`、`.`、`_` を含む名前、空のディレクトリ、深さの違い）を作り、MaxKeys を 1〜N で全ページをつないだ結果を比べる。比べる相手は「全件を並べ替えたもの」。
  - start-after に存在しないキーを渡した場合、continuation-token、ctx の取り消しも確認する。
  - 読んだディレクトリの数を数えるテストで、打ち切りを確かめる。

#### (d) JuiceFS gc の一覧をディレクトリ単位にする（JuiceFS だけで完結）

- 変更箇所: `cmd/gc.go:230`。たとえばフラグか環境変数で、`object.ListAllWithDelimiter(ctx, blob, "", "", "", true)`（`pkg/object/object_storage.go:216-`、既存）を使う。または `chunks/<a>/<b>/` ごとに `ListAll` を呼ぶループにする。
- 効果:
  - rclone 側では、1 要求あたり 1 ディレクトリの `ReadDirAll` だけになる。delimiter=`/` では `entryListR` が再帰しない（`list.go:43-48`）。数秒で返る。
  - 10 並列（`object_storage.go:233`）なので、tpslimit 20 の範囲で速くなる見込み。
  - 本番の rclone を変えずに済む。
- 副作用:
  - 出力は DFS の順で、完全なキー順ではない。gc は sort=false で、順序に依存しない（`gc.go:285-367`）。
  - `ListAllWithDelimiter` のトップの List は、hasMore を無視する（224 行）。`chunks/` 直下は 7 件程度なので問題ない。
  - 子の List が失敗したときにリトライするかどうかの実装を確認する必要がある（失敗すると gc 全体が止まる可能性）。
  - rclone の `pager` の完全一致マーカーの不具合（gc の `--delete` が並行して消したキーがマーカーになると、ディレクトリの先頭から返し直して重複する）は残る。重複は「二重に leaked と数える」「二重に削除しようとして warn」程度の影響。
  - 上流の JuiceFS から離れる差分になる。
- テストのしやすさ: 中くらい。
  - gofakes3 の in-memory backend か、ローカルの `rclone serve s3`（local backend）に、遅延を入れたモックを当てて gc を実行する。
  - 既存の `cmd/gc_test.go`、`pkg/object` のテストの流儀に合わせられる。

#### (b) JuiceFS の ResponseHeaderTimeout を設定できるようにする

- 変更箇所: `pkg/object/restful.go:157`（環境変数か bucket URL のクエリで上書きする）。List だけを長くするなら、別の http.Client／Transport を `s3.go` の List 用に持つ。
- 効果: 単独では効かない。今の rclone は、最初のページを返すまでに全件の走査（1 時間以上）が要る。さらに rclone 側の `--server-write-timeout 3m` で、書き込みが切られる。両方を数時間に延ばしても、2 ページ目以降のたびに全件の走査（キャッシュが温まっていても数百万件の並べ替え）が繰り返される。
- 副作用: 共有の httpClient なので、全要求の「応答が無いことの検知」が遅くなる。
- テストのしやすさ: 高い（httptest で遅いサーバを作るだけ）。ただし価値は低い。

#### (c) gc の前に一覧のキャッシュを温める運用

- 内容: 事前に同じ prefix を一覧させて VFS のキャッシュを温め、dir-cache-time のうちに gc を実行する。
- 問題:
  1. 走査そのものが 1 時間以上かかる見込みなので、`--dir-cache-time 1h` では、先頭のディレクトリが期限切れになる。延長には再起動が要る（今回の調査の範囲外。rc は使わない方針）。
  2. キャッシュが温まっていても、ListBucket はページごとに全件をメモリに集めて並べ替える。数百万件 × 数千ページで、1 ページが 30 秒を超える恐れがあり、メモリも大きい。
  3. 温めるための走査そのものも、Drive の API を消費する。
- 結論: 実用的でない。

#### 当面の運用上の注意

- 今動いている走査（20:22 の時点で `chunks/0/384` 付近）は、終わるまで Drive の API を消費し続ける見込み。完了した結果は破棄される。
- このまま gc を再実行すると、走査がもう一本増える。終わるのを待つか、rclone を再起動するかは、ユーザーの判断に委ねる（本調査では何もしていない）。

## 未検証事項

- ユーザーの gc の実行に `--delete`／`--compact` があったか。
- chunks 配下の実際のフォルダ数と object 数（約 1,442 という値と、`0/0`〜`0/999` がすべてあるという仮定との整合）。
- Drive 上に同名の重複 object があるか（`Reset virtual modtime` の一部の原因の候補）。
- rclone の走査が完了した時刻と、その後の書き込みエラーの有無（ログの追記分で後から確認できる）。
- files.list の実際の回数（DEBUG ログでは数えられない）。
