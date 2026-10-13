# C: Read path / chunk metadata cache / Redis client-side cache 調査

- 対象: `/home/kwatanabe/tmp_local/juicefs_inspection/juicefs` branch `1.4.1-improve-kaz` HEAD `ea2c3757`（upstream v1.4.1 = `0b90c7db`）
- 方法: 読み取り専用の source 読解。実行・計測はしていない。
- go-redis は `go.mod:73` で `v9.18.0`（`$GOMODCACHE/github.com/redis/go-redis/v9@v9.18.0` を参照）。
- 凡例: **[事実]** = source 上で確認、**[推論]** = source から導いた挙動予測、**[未確認]** = 実機・kernel 挙動・計測が必要。

---

## 0. 結論サマリ

1. **[事実] chunk slice cache（`openfiles`）は `--open-cache` と無関係に有効**。`baseMeta.Read` の cache 参照（`pkg/meta/base.go:2113` `m.of.ReadChunk`）は `OpenCache>0` を条件にしておらず、`Open` 時の `m.of.Open`（`base.go:2091`）で entry が作られれば以後 cache される。expire 判定も無い（`openfile.go:206-219`）。`OpenCache` が効くのは attr（`GetAttr`/`Open` の Redis 省略）だけ。
2. **[事実] local Write の metadata commit 後はその chunk の cache が必ず消える**（`base.go:2201` `defer m.of.InvalidateChunk(inode, indx)`）。よって same-client の read-after-write は **必ず LRANGE 1 RTT**。
3. **[事実＋推論] さらに GetAttr 1 回で全 chunk cache が消え得る**。`Write` は `of.attr` を更新しない一方、Redis 上の mtime は doWrite で更新される（`redis.go:3162-3165`）。次の `GetAttr`→`of.Update`（`openfile.go:187-188`）で mtime 差分を検出し `invalidateChunk()`＝**全 chunk 消去**。VM image に書き込み続ける運用では、kernel の GETATTR（attr-cache 1s 期限切れ後の stat 等）のたびに巨大ファイル全体の chunk cache が消える。
4. **[事実] Read 前の writer flush は「範囲重なり」ではなく「同 inode に pending chunk があれば常に全 pending を flush」**（`vfs.go:799` → `writer.go:644-650` → `writer.go:409-483`）。しかも flush 中は同 inode の新規 Write が待たされる（`writer.go:369`）。fork の変更はエラー伝播（upstream は `_ =` で無視）と `removeOp` の defer 化のみで、全 flush の条件自体は upstream と同じ。
5. **[事実] Redis client-side cache（`client-cache` URL param）は upstream 由来（`76bce384` #6495, `7ff3c15d` #7021、fork 差分なし）**。cache 対象は inode attr（`GET <prefix>i<ino>`）と dir entry のみで、**chunk（LRANGE `c<ino>_<indx>`）は対象外**。巨大ファイル random I/O の hot path（Read の slice 解決、Write commit）にはほぼ効かず、効くのは GetAttr/Open/Lookup のみ。さらに書き込み中ファイルは commit ごとに inode key が更新され invalidate されるので hit 率は低い。
6. **[事実] hole chunk（LRANGE 結果が空）は cache されず、毎回 LRANGE + GET inode の 2 RTT**（`base.go:2127-2136`、`CacheChunk` に到達しない）。
7. **[推論] 改善案「local write 成功時に known slice を cache に反映」は、doWrite の MULTI/EXEC に `LRANGE` を同梱する形なら追加 RTT 0・原子的スナップショットで安全に実現できる**。cache 側で既存リストに追記する方式は remote compaction 等で不整合リスクがあり、generation を持たない現行構造では非推奨。詳細は §6。

---

## 1. Read path（FUSE → VFS → reader → Meta → object）

### 1.1 段階表

| # | 段階 | file:line / 関数 | metadata access | network RTT | lock |
|---|---|---|---|---|---|
| 1 | FUSE Read | `pkg/fuse/fuse.go:263` `fileSystem.Read` | なし | なし | なし |
| 2 | VFS Read 入口 | `pkg/vfs/vfs.go:701` `VFS.Read` | handle 検索のみ（`findHandle`） | なし | `h.Rlock`（`vfs.go:791` 付近、Write の `Wlock` と排他） |
| 3 | Read 前 writer flush | `vfs.go:799` → `writer.go:644` `dataWriter.Flush` → `writer.go:409` `fileWriter.flush` | pending あり: 全 slice を freeze → upload → commitThread で `Meta.Write`（slice ごとに Redis txn） | pending なし: 0。あり: object PUT + slice 数 × Write txn（約 3 RTT/slice、§1.3） | `dataWriter.Lock`（find）、`fileWriter.Lock`、`flushwaiting++` で同 inode の Write をブロック（`writer.go:369`） |
| 4 | fileReader.Read | `pkg/vfs/reader.go:626` | なし | なし | `fileReader.Lock`、`dataReader.Lock`（acquire/release） |
| 5 | 既存 sliceReader 照合 / 新規作成 | `reader.go:463` `cleanupRequests`, `reader.go:501` `splitRange`, `reader.go:561` `prepareRequests`, `reader.go:310` `newSlice` | 既存 READY sliceReader の page に含まれれば metadata も data も不要 | なし | fileReader.Lock |
| 6 | sliceReader.run（goroutine） | `reader.go:162`、`reader.go:175` `f.r.m.Read(...)` | `Meta.Read(inode, indx)` を sliceReader 1 個につき 1 回 | §1.2 | Lock を外して呼ぶ |
| 7 | baseMeta.Read | `pkg/meta/base.go:2101` | `of.find` → `openFile.RLock`（`base.go:2108-2111`）→ `of.ReadChunk`（`base.go:2113`） | hit: 0、miss: LRANGE 1（`redis.go:3098-3104`）、空なら + GET inode 1（`base.go:2127-2131`） | **openFile.RLock**。`Meta.Write`/`Truncate`/`Fallocate` が `openFile.Lock` を Redis txn 中ずっと保持（`base.go:2194-2200`, `2231-2235`, `2266-2270`）するため、**cache hit でも同 inode の commit txn 完了待ちになる** |
| 8 | buildSlice / CacheChunk | `base.go:2138-2139`, `pkg/meta/slice.go:134` | CPU のみ | なし | `openfiles.Lock`（global mutex、短時間） |
| 9 | 背景 compaction 要求 | `base.go:2140-2146` → `compaction_scheduler_lifecycle.go:38` | miss かつ raw slice ≥5 で要求（fork: scheduler 経由） | 背景で LRANGE + NewSlice + object + compact txn | `m.compacting` で重複排除 |
| 10 | touchAtime | `base.go:2019`（`Read` の defer） | 既定 `--atime-mode=noatime`（`cmd/flags.go:363-364`）で即 return | 0 | なし |
| 11 | data 読み出し | `reader.go:840` `dataReader.Read` → `reader.go:813` `readSlice` → `store.NewReader().ReadAt` | なし | disk cache hit: 0、miss: object GET（slice/ block 単位で並列、>16 slice は concurrency 16） | chunk store 内部 |

### 1.2 sliceReader 単位と Meta.Read 回数 [事実＋推論]
- sliceReader は block（既定 4 MiB）境界で分割（`reader.go:316-323`）。read が block 境界をまたぐと sliceReader 2 個 → `Meta.Read` 2 回。同 chunk でも goroutine が並行に走るため、cache 空なら 2 回とも LRANGE になり得る [推論]。
- random read では `cleanupRequests`（`reader.go:463-482`）が、今回範囲と重ならず session の readahead 窓（`need`, `reader.go:442`）外の sliceReader を即 drop する。→ random read は毎回新しい sliceReader → 毎回 `Meta.Read` 呼び出し（cache hit なら RTT 0）[推論]。
- readahead（`checkReadahead`, `reader.go:419`）が立つと追加の sliceReader が作られ、同 chunk なら cache hit で済む。
- 短読み（`n != need`）時は `InvalidateChunkCache`（`reader.go:225`）してリトライ。

### 1.3 Read 前 flush の詳細 [事実]
- `VFS.Read` は `v.writer.Flush(ctx, ino)` を**範囲に関係なく毎回**呼ぶ（`vfs.go:799`）。
- `dataWriter.Flush`（`writer.go:644`）は inode の `fileWriter` があれば `fileWriter.Flush`。`fileWriter` は書き込み可能 handle が開いている間存在（refs 管理、`writer.go:608-641`）。
- `fileWriter.flush`（`writer.go:409`）: `len(f.chunks) > 0` の間、**全 chunk の全 slice を freeze**（`explicit_flush`）し、全 chunk の commit 完了（`f.chunks` 空）まで待つ。pending が無ければループに入らず、ロック取得だけで終わる。
- 待機中 `flushwaiting>0` のため、同 inode の `fileWriter.Write` は `writer.go:369-374` で待たされる。→ **guest の read 1 回が、そのファイル全体の未 commit 書き込み（object PUT + metadata commit）完了待ち＋書き込み停止を誘発**。
- fork 差分（`git diff 0b90c7db HEAD -- pkg/vfs/vfs.go`）: エラーを返すようになった（upstream は `_ = v.writer.Flush(ctx, ino)`）、`removeOp` を defer 化。flush 条件は不変。
- commit（`writer.go:204` `commitThread`）では slice ごとに `Meta.Write`（`writer.go:226`）→ `reader.Invalidate`（`writer.go:230`）。

### 1.4 Meta.Write（commit）1 slice あたりの Redis RTT [事実＋推論]
- `redis.go:3141` `doWrite` は `m.txn`（WATCH inode）内で `tx.Get(inode)` → `TxPipelined{RPUSH chunk, SET inode, [INCRBY usedSpace], genLog}`。
- RTT: WATCH 1 + GET 1（client-cache hit 時 0、§4.4 のリスク参照）+ MULTI..EXEC 1 = **約 3 RTT**。`getParents` は `attr.Parent>0` なら RTT なし（`redis.go:3337-3340`）。`updateParentStat`/`updateUserGroupStat` は非同期集約 [推論、詳細未確認]。
- txn 全体の間 `openFile.Lock` を保持（`base.go:2194-2200`）→ 同 inode の `Meta.Read`（cache hit 含む）を止める。
- slice ID は `NewSlice`（`base.go` の `sliceIdBatch = 4<<10`, `base.go:51`）で 4096 個ごとに INCRBY 1 回。

### 1.5 read-after-write（same client）シーケンス [事実＋推論]
1. `VFS.Write`（`vfs.go:812`）: writer buffer へ。metadata RTT 0。`reader.Invalidate`（`vfs.go:870`）で重なる sliceReader を無効化、`invalidateAttr`（modifiedAt 更新）。
2. `VFS.Read`: flush → upload 完了待ち → `Meta.Write`（~3 RTT/slice、同 inode 内 txn は `txLock` で直列）→ `InvalidateChunk(inode, indx)`。
3. `fileReader.Read` → 新 sliceReader → `Meta.Read` → **cache miss 確定 → LRANGE 1 RTT** → buildSlice → CacheChunk。
4. data: 直前に書いた block。disk cache / staging にあれば local、なければ object GET [未確認: cache-partial-only 等の設定次第]。
5. slice ≥5 なら背景 compaction を要求（さらに Redis/object 負荷）。

**結論: local write 直後の same-client read は Redis に必ず行く（LRANGE ≥1 RTT）。flush で pending があれば、その前に slice 数 × ~3 RTT の commit 待ちが直列に乗る。**

---

## 2. chunk metadata cache（`pkg/meta/openfile.go`）

### 2.1 構造 [事実]
- `openFile{attr, refs, lastCheck, first []Slice, chunks map[uint32][]Slice}`（`openfile.go:19-26`）。chunk 0 は `first`、他は map。
- `ReadChunk`（`openfile.go:206-219`）: entry があれば返す。**expire / lastCheck を見ない**。indx 0 は `first != nil` が hit 条件。
- `CacheChunk`（`openfile.go:221-236`）: entry がある時だけ格納（未 Open の inode は cache されない）。
- `cleanup`（`openfile.go:68-117`）: refs<=0 かつ 12h 未チェック、または `open-cache-limit`（既定 10000、`cmd/flags.go:456-457`）超過時に古いものを解放。

### 2.2 cache が有効になる条件 [事実]
- `baseMeta.Open`（`base.go:2043`）は `OpenCache>0 && OpenCheck` で早期 return する以外、最後に必ず `m.of.Open(inode, attr)`（`base.go:2091`）。→ **open 中ファイルの chunk cache は `--open-cache=0`（既定、`cmd/flags.go:451-452`）でも有効**。
- `OpenCache` の意味: `GetAttr`（`base.go:1450`）・`Open`（`base.go:2052`）・ACL（`base.go:3716`）で `of.attr` を `expire` 内なら Redis を引かずに返す機能。0 なら attr は常に Redis（または client-cache）から。
- 既定（0）での帰結: 全 GetAttr が `doGetAttr` → `of.Update`（`base.go:1480`）を通る → **mtime が変わっていれば全 chunk cache 消去**（`openfile.go:187-188`）。これが「close-to-open + attr 経由の remote 変更検出」の仕組み。

### 2.3 invalidation 経路（全 call site） [事実]

| 呼び出し元 | file:line | 範囲 |
|---|---|---|
| `baseMeta.Write`（local commit 後、成功/失敗とも defer） | `base.go:2201` | 当該 indx |
| `baseMeta.Truncate` | `base.go:2236` | 全 chunk |
| `baseMeta.Fallocate` | `base.go:2271` | 全 chunk |
| `redisMeta.CopyFileRange`（dst） | `redis.go:3197`（sql `sql.go:3413`, tkv `tkv.go:2704`） | dst 全 chunk |
| compaction 成功時 | `base.go:2963` | 当該 indx |
| `InvalidateChunkCache` API | `base.go:2095-2098`。呼び出し: 短読みリトライ `reader.go:225`、SDK `fs.Truncate` `pkg/fs/fs.go:1434` | 指定 indx |
| `invalidateAttrOnly`（lastCheck=0 のみ、chunk は消えない） | `base.go:1497`(SetAttr), `base.go:1747`, `base.go:3785`(SetFacl), `redis.go:1773/1840/1844/2210/2613` 等 | attr のみ |
| `openfiles.Open` で mtime 変化 | `openfile.go:140-143` | 全 chunk |
| `openfiles.Update`（GetAttr/Lookup/Readdir plus/touchAtime/ACL 経由）で mtime 変化 | `openfile.go:187-188`; 呼び出し `base.go:1480,1498,2037`, `redis.go:1020,2853,5962` | 全 chunk |

- **remote 通知（pubsub 等）による chunk invalidation は存在しない**。remote client の書き込みは mtime 変化を GetAttr/Open/Lookup で観測した時のみ反映 [事実: grep で他経路なし]。
- 注意: `invalidateAttrOnly`（0xFFFFFFFE）は `InvalidateChunk` 内で `delete(of.chunks, 0xFFFFFFFE)`（no-op）+ `lastCheck=0`（`openfile.go:238-252`）。

### 2.4 自分の Write で自分の cache 全体が消える問題 [事実＋推論]
- `baseMeta.Write` は doWrite が返す新 attr（mtime 更新済み）を `of.Update` に渡さず、該当 indx の invalidate のみ（`base.go:2201`）。`of.attr.Mtime` は古いまま。
- 以後 `GetAttr` が来ると Redis の mtime（自分が書いた値）と `of.attr.Mtime` が異なる → `invalidateChunk()` で **file 全 chunk の slice cache を消去**。
- GETATTR の発生源 [推論/未確認]: kernel attr-cache（`--attr-cache` 既定 1s、`cmd/flags.go:428-429`）期限切れ後の stat/fstat/lseek(SEEK_END) 等。FUSE（writeback_cache なし）では write 後に kernel 側 attr が無効化される挙動があり、書き込み後の最初の stat は GETATTR になりやすい [未確認: kernel version 依存]。さらに `replyAttr`（`fuse.go:55-70`）は `ModifiedSince`（直近 Write あり）なら追加で `Meta.GetAttr` を呼ぶ → 1 GETATTR で GetAttr 2 回。
- 帰結 [推論]: 書き込みが継続する VM image で GETATTR が周期的に来ると、random read の chunk cache hit 率は「最後の GETATTR 以降に読んだ chunk」に限られる。巨大ファイル（例 100 GiB = 1600 chunk）でこれは致命的に cache を薄める。

### 2.5 Read と compaction の race [推論]
- compaction の `doCompactChunk` は `openFile` lock を取らない（`base.go:2875-2965` 付近、`of.find` 呼び出しなし）。
- `Read`: LRANGE(旧) → [compaction EXEC → InvalidateChunk] → `CacheChunk(旧)` の順に起こると、旧 slice list が cache に残る。内容は等価だが旧 slice の object が削除されると読み失敗 → `reader.go:225` で invalidate してリトライ（自己回復）。fork は削除を `enqueueCompactionDelete`（`redis.go` diff）で遅延させるので窓は小さい。upstream 由来の構造。

---

## 3. Redis doRead のコストと slice 処理

- [事実] `doRead` は `LRANGE c<ino>_<indx> 0 -1` 1 コマンド（`redis.go:3098-3104`）。pipeline/txn ではない。
- [事実] 1 要素 `sliceBytes`（24B）。`readSlices`（`slice.go:103`）で decode、`buildSlice`（`slice.go:134`）で非平衡二分木に挿入（`cut` は木の深さ比例）→ 最悪 O(n²)、n は raw slice 数。
- [事実] raw slice 数上限: Write 時 `numSlices%100==99 || >350` で背景 compaction 要求、`>= maxSlices(2500)`（`base.go:64`）で同期 compaction（`base.go:2214-2221` 付近）。Read 時は raw ≥5 で背景 compaction 要求（`base.go:2140-2146`）。
- [推論] 通常は Read miss のたびに compaction が要求されるため raw slice 数は小さく保たれ、LRANGE の payload（≤数 KB）・buildSlice CPU は RTT に比べ無視できる。数百 slice 時でも 2500×24B=60KB 程度で、RTT 支配。重くなるのは compaction が追いつかない/無効化されている場合のみ。
- [推論] 返却 `[]Slice` が多いと `readManySlices`（`reader.go:881`）で object GET が多数並列化（concurrency 16）され、RTT ではなく object 側の負荷になる。

---

## 4. Redis client-side cache（`pkg/meta/redis_csc.go`）

### 4.1 設定名・由来 [事実]
- **meta URL query param**（mount option ではない）: `client-cache`（`redis.go:130-131`、`""`/`"false"` 以外で有効。例 `?client-cache=true`）、`client-cache-size`（既定 12800、`redis.go:132`）、`client-cache-expire`（既定 1m、`redis.go:134`）、`client-cache-preload`（既定 0、`redis.go:135`）。`query.getInt/duration` の第 2 引数で `client_cache_size` 等 underscore 表記も受理。
- 有効化: `redis.go:286-292` で `newRedisCache` + `init`。失敗時は警告して無効化。
- 由来: upstream（`76bce384` "meta/redis: support client cache (#6495)"、`7ff3c15d` #7021）。`git log 0b90c7db..HEAD -- pkg/meta/redis_csc.go` は空 → **fork 改修なし**。

### 4.2 cache 対象 [事実]
- `inodeCache`: `GET <prefix>i<ino>` の結果（attr バイト列）。`ProcessHook` の `beforeProcess`（`redis_csc.go:175-206`）で hit 時は Redis に送らず応答。
- `entryCache` / `entryTerms`: `doLookup` の dir entry（`redis.go:978-1028`）。negative lookup は mark エントリ（`ino==0`）として扱う。
- コメント明記: 「cache attrs and entries only, chunks are already cached in OpenCache」（`redis_csc.go:46-47`）。**chunk（`c...` key, LRANGE）は対象外**。
- tracking は `CLIENT TRACKING ON BCAST PREFIX <prefix>i PREFIX <prefix>d`（`redis_csc.go:312`）。chunk key prefix `c` は追跡外。

### 4.3 TTL / invalidation / reconnect [事実]
- TTL: `expirable.LRU` の expiry（既定 1 分）。`entryTerms` は 10×expiry。
- invalidation source: Redis の BCAST tracking push（RESP3）を `RegisterPushNotificationHandler("invalidate")`（`redis_csc.go:90-93`, handler `135-169`）で受信、pubsub 接続（`__redis__:invalidate` subscribe）経由。inode key → `inodeCache.Remove`、entry key → term bump。
- local 同期 invalidation: `afterProcess`（`redis_csc.go:208-258`）で単発 `set`/`hset`/`hdel` のみ。**`ProcessPipelineHook` は nil を返す（`redis_csc.go:271-285`）ため pipeline / TxPipelined 内の SET は hook されない** — doWrite の `pipe.Set(inode)` は local では即時 invalidate されず、BCAST push を待つ。
- reconnect: pubsub 接続の `OnConnect`（`redis_csc.go:302-317`）で inode/entry/term cache を全 Purge し TRACKING を再設定。

### 4.4 hot path への効き [事実＋推論]

| 操作 | client-cache の効果 |
|---|---|
| Read（slice 解決） | **効かない**（LRANGE は非対象）。chunk cache は openfiles 依存 |
| Read の hole chunk 判定 GET inode | hit すれば 1 RTT 削減（空 chunk 時のみ） |
| Write commit（doWrite） | `tx.Get(inode)` は Tx が hook を clone するため（go-redis `tx.go:24-35` `hooksMixin.clone()`、`redis.go:135-160` chain）cache hit 可能 → 1 RTT 削減し得る。ただし下記リスク |
| GetAttr | 効く（GET inode）。ただし書き込み中ファイルは commit ごとに inode key が SET されて BCAST invalidate → hit 率低 [推論] |
| Open | GetAttr 部分のみ効く |
| Lookup | 効く（entry + attr） |

- **[未確認・要注意] doWrite が stale attr を読むリスク**: doWrite の `tx.Get(inode)` が cache hit した場合、その値は WATCH 前に取得された古い値の可能性がある。直前の自 commit の `SET inode` は TxPipeline 内のため local invalidate されず、BCAST push は別接続で非同期に届く。push 到着前に次の doWrite が走ると、WATCH 以降には変更が無いので EXEC は成功し、**古い Length を基準に `attr.Length` を計算・SET する可能性**（length 後退、mtime/ctime 上書き）。Redis の push 送出順と go-redis の受信 goroutine タイミング次第。upstream 由来の潜在問題として要検証（実機で client-cache を使っていないなら影響なし）。

---

## 5. VFS 層 attr/entry cache と Write/Read の GetAttr

- [事実] JuiceFS VFS は attr を自前で cache しない。`VFS.GetAttr`（`vfs.go:211-224`）は毎回 `Meta.GetAttr`（`OpenCache>0` なら `of.attr`、それ以外 Redis/client-cache）。`--attr-cache`/`--entry-cache`/`--dir-entry-cache`/`--negative-entry-cache`（`cmd/flags.go:428-445`, `mount_unix.go:1118-1121`）は **kernel FUSE への timeout 返却値**（`fuse.go:55-70`）。
- [事実] `VFS.Write`/`VFS.Read` 自体は GetAttr/Lookup を呼ばない。Write は `invalidateAttr`（`vfs.go:1306`、modifiedAt 記録）のみ。
- [事実] `replyAttr` は `ModifiedSince(ino, ctx.start)` が真なら追加の `Meta.GetAttr`（`fuse.go:58-65`）。書き込み継続中ファイルの GETATTR/LOOKUP は Meta.GetAttr 2 回（=Redis 2 RTT、OpenCache=0 時）になり得る。
- [事実] `writeback_cache` は `-o writeback_cache` 明示時のみ（`fuse.go:503-504`）。既定 off。on の場合 kernel が write-only handle で read を発行し得る（`vfs.go:786` 付近のコメント）。
- [事実] `Open` 応答の `FOPEN_KEEP_CACHE`（`fuse.go:251`）は `openfiles.Open` が mtime 不変時に `KeepCache=true` を立てる（`openfile.go:140-149`）。それ以外は `InodeNotify` で kernel page cache を破棄。
- [未確認] QEMU が `cache=none`（O_DIRECT）なら kernel page cache を経由せず全 read が FUSE Read になる。`cache=writeback` 等なら kernel page cache で吸収され FUSE Read 回数が減る。実機の QEMU cache mode と kernel の write 後 GETATTR 挙動は要確認。

---

## 6. 改善案「local write 成功時に known slice を local chunk cache に反映」

### 6.1 既存 API [事実]
- cache への追記・merge API は無い。`CacheChunk` は丸ごと置換（`openfile.go:221-236`）、cache は `buildSlice` 後の**平坦化済み** `[]Slice`（hole は `Id=0`）で raw list 長や generation を持たない。
- cache generation / versioning は無い（`lastCheck` は attr 用）。
- `Meta.Write` は `openFile.Lock` 下で doWrite するため、同一 client の `Read`（RLock）とは直列化済み。`doWrite` は `numSlices = RPUSH の戻り値`（raw list 長）を得ている（`redis.go:3175-3177`）。

### 6.2 案 A（推奨）: doWrite の MULTI に LRANGE を同梱 [推論]
- `TxPipelined` に `pipe.LRange(chunkKey, 0, -1)` を RPUSH の後に追加 → EXEC の原子性により「自分の RPUSH 直後の正確な list」を**追加 RTT 0**で取得。`baseMeta.Write` で `buildSlice` → `CacheChunk(inode, indx, ...)` を `InvalidateChunk(indx)` の代わりに実行（失敗時は従来どおり invalidate）。
- 正当性: 他 client の append・remote compaction が混ざっても EXEC 内スナップショットなので正しい。cache 化後の remote 変更は従来と同じ（mtime 経由検出）。
- コスト: commit ごとに list 転送（通常 ≤数 KB、最悪 60 KB）と buildSlice CPU。raw 数が大きい時（例 >100）は従来の invalidate にフォールバックする閾値を設けるとよい。
- あわせて必要: `of.attr` を doWrite の結果 attr（mtime/length）で更新しないと、§2.4 の通り次の GetAttr で全 chunk cache が消える。ただし `of.attr` を自 commit の attr で上書きすると「自 commit 前に入った remote 変更（他 chunk）」の検出機会を失う → **単一 writer 前提でのみ安全**。安全側案: doWrite の `tx.Get` で得た旧 attr の mtime が `of.attr.Mtime` と一致した場合に限り `of.attr` を新 attr へ進める（一致しなければ remote 変更ありとして全 invalidate）。
- 同様に compaction の `doCompactChunk` も結果 list を cache に入れられるが、優先度は低い（compaction は Read 時に起きる背景処理）。

### 6.3 案 B: cache 側で既存 list に新 slice を overlay [推論]
- 平坦化 list を raw slice 列として再解釈し新 slice を追加して `buildSlice` し直すことは可能。
- 条件: 「cache が Redis 上の自分の RPUSH 直前状態と一致」を保証する必要。`numSlices == cachedRawCount+1` 判定には raw 数の保持が必要だが、remote compaction（件数減）＋ remote append（件数増）で偶然一致し得るため generation 無しでは不完全。
- 危険条件: 他 client の同 chunk 書き込み、他 client / `juicefs compact|gc` による compaction、Truncate/Fallocate/CopyFileRange（他 client 発）、cache 未存在（miss 時は何もしない）、doWrite エラー（ENOSPC/EDQUOT 含む: 必ず invalidate）。→ 案 A の方が単純で安全。

### 6.4 safe fast-path の範囲（まとめ）[推論]
- 安全: doWrite 成功 & EXEC 内 LRANGE の結果で cache 置換（案 A）。Truncate/Fallocate/CopyFileRange/SetAttr(size)/compaction 失敗系は従来の全 invalidate を維持。
- 条件付き: `of.attr` の前進（旧 mtime 一致時のみ）。
- 非推奨: generation 無しの local overlay（案 B）。
- 不変条件の維持: commit 失敗（実保存失敗・ENOSPC・EDQUOT）を隠さない、Read 前 flush（read-after-write）を弱めない。cache 更新は「commit 成功後の Redis 真値」だけを使う。

---

## 7. 付随して見つかった改善候補 [推論]
1. **hole chunk の negative cache**: `len(ss)==0` かつ file 確認済みなら空 slice（非 nil の `[]Slice{}`）を cache。indx 0 の `first != nil` 判定とも両立。sparse な raw image / 未割当領域の read で毎回 2 RTT を削減。
2. **Read 前 flush の範囲限定**: 読む範囲と重なる pending chunk だけを flush（他 chunk の commit 完了を待たない）。ただし read-after-write 保証は維持（重なる範囲は必ず commit 完了待ち）。`flushwaiting` による Write 全停止も範囲限定化の検討余地。fsync/順序を弱めないこと。
3. **openFile lock の粒度**: `Meta.Write` が txn 中（~3 RTT）inode 全体の `openFile.Lock` を保持し、無関係 chunk の cache hit Read まで止める。chunk 単位 lock / 世代番号化の検討余地。
4. **自 Write 後の mtime 起因の全 chunk invalidate**（§2.4）の抑制。

## 8. 未確認事項（要実機確認）
- 実機の meta URL に `client-cache` が付いているか、`--open-cache`/`--attr-cache`/`-o writeback_cache` の実設定。
- QEMU の cache mode（O_DIRECT か）と FUSE Read/GETATTR の実発生頻度（`.accesslog` で `getattr`/`read` 回数と `Read`/`GetAttr` meta 呼び出しの相関を測定可能）。
- §4.4 の doWrite stale attr リスクの再現性（client-cache 有効時）。
- Redis BCAST invalidation と EXEC 応答の到着順。
- read-after-write で直前書き込み block が disk cache から読めるか（cached_store の cache-on-write 設定依存）。
