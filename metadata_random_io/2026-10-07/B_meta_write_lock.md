# B: baseMeta.Write の open-file lock と Redis doWrite RTT の調査

- 対象: `/home/kwatanabe/tmp_local/juicefs_inspection/juicefs`、branch `1.4.1-improve-kaz`、HEAD `ea2c3757`（upstream v1.4.1 = `0b90c7db`）
- 調査方法: source 読解のみ（読み取り専用。build／実行／git 状態の変更はしていない）
- go-redis: `github.com/redis/go-redis/v9 v9.18.0`（go.mod:73、`$GOMODCACHE/.../v9@v9.18.0/tx.go`）
- 行番号は HEAD `ea2c3757` 基準

---

## 0. 結論（要約）

1. **#6398 の指摘は本 fork にもそのまま該当する（観測事実）**。fork の `baseMeta.Write` は upstream と同じく inode 単位の `openFile.RWMutex` を `doWrite` + stat 更新 + 同期 compaction の全区間で保持する（base.go:2194-2224）。fork の改修は計測と background compaction の投入先変更のみで、lock scope は変えていない。
2. ただし **Redis では open-file lock の内側に、同じ inode のすべての txn を client 内で直列化する `txLock(fnv(inodeKey))` がある**（redis.go:1153-1162、base.go:665）。`doWrite` の 4 RTT はすべてこの内側 lock の中なので、**open-file lock を per-chunk にするだけでは、同一 client・同一 inode の doWrite は直列化されたまま**（既存 memo の指摘は正しい）。
3. さらに `doWrite` は overwrite でも **毎回 inode key を SET する**（mtime/ctime を必ず更新、redis.go:3162-3172）。そのため txLock を外して並列化すると、同じ inode を WATCH している他の doWrite と必ず衝突し、`TxFailedErr` → 乱数 sleep → retry になる（redis.go:1190-1203）。**WATCH ベースの楽観 txn のままでは、同一 inode の並列 commit は原理的にスケールしない**。
4. 1 回の Meta.Write（Redis、単一 attempt、hardlink なし）は **4 RTT／7〜10 command**（WATCH, GET, MULTI…EXEC, UNWATCH）。
5. open-file lock の中で **backend RTT 以外に重いのは同期 compaction（numSlices >= 2500）**（base.go:2217-2221）。object storage の読み書きを含むため、遅い object 環境では数秒〜分単位で inode 全体（全 chunk の Write と、cache miss の Read）を止めうる。安全に効果が大きい第一候補は「同期 compaction を f.Lock の外に出す」。
6. fork の priority scheduler は **1 inode あたり同時 1 chunk しか compaction しない**（compaction_scheduler.go:136-185 `state.active`）。巨大な単一ファイルへの random write では background compaction が追いつかず、2500 到達 → Write 内同期 compaction → inode 全体停止、という連鎖が起きやすい（推論。実測は未確認）。

---

## 1. baseMeta.Write の全体と open-file lock が守るもの

### 1.1 コード（観測事実）

`pkg/meta/base.go:2182-2227` `baseMeta.Write`:

| 行 | 処理 | lock 状態 | backend RTT |
|---|---|---|---|
| 2194 | `f := m.of.find(inode)`（`openfiles.Mutex` は全 inode 共通の global mutex、openfile.go:254-258） | — | なし |
| 2195-2200 | `f.Lock()`（`openFile.RWMutex` の write lock）。`f == nil` なら lock なしで続行 | 取得 | なし |
| 2201 | `defer InvalidateChunk(inode, indx)` — defer は LIFO なので **f.Unlock より先に実行**（lock 保持中に無効化） | 保持 | なし |
| 2206 | `m.en.doWrite(...)` | 保持 | **Redis 4 RTT**（§2） |
| 2211 | `updateParentStat` → `updateStats`（atomic、redis.go:837-840）、`updateDirStat`（`dirStatsLock`、quota.go:181-192）、`updateDirQuota`（`quotaMu`、quota.go:446-468） | 保持 | 通常なし。ただし `delta != 0` かつ DirStats 有効で `dirParents` cache miss の場合 `getDirParent`→`GetAttr` の **backend RTT**（quota.go:350-361）。`attr.Parent == 0`（hardlink）なら `doGetParents` を **goroutine で非同期**（quota.go:205-210） |
| 2212 | `updateUserGroupStat`（`quotaMu`、quota.go:470-503） | 保持 | なし |
| 2214-2216 | `numSlices%100==99 \|\| numSlices>350` かつ `<2500` → `requestBackgroundCompaction`（priority なら scheduler の `s.mu` のみ、そうでなければ `go compactChunk`。compaction_scheduler_lifecycle.go:38-46） | 保持 | なし |
| 2217-2221 | `numSlices >= 2500 (maxSlices, base.go:64)` → **`compactChunk(inode, indx, once=true, ...)` を同期実行** | **保持** | doRead（LRANGE）＋ object 読み書き（`newMsg(CompactChunk)`）＋ `doCompactChunk`（txn）。さらに既に同 chunk を compaction 中なら 10ms sleep の busy-wait（base.go:2853-2861） |

- 保護解除: 関数 return 時（defer）。`updateParentStat` 等の dir stat／quota は **メモリ上に積むだけで、backend への flush は別 goroutine で 1 秒周期**（`flushDirStat` quota.go:215-250、`flushStats` quota.go:252-、`flushQuotas` quota.go:505-）。

### 1.2 open-file lock が実際に保護している state（列挙）

| state | f.Lock で守られているか | 実際の保護主体 | 根拠 |
|---|---|---|---|
| file length（backend の inode attr） | **No（実質）** | backend txn（Redis WATCH inode key／SQL `SELECT FOR UPDATE`／TKV txn） | doWrite 内で read-modify-write（redis.go:3145-3172）。`f == nil` の場合 lock なしで Write が走る設計（base.go:2195）→ backend 整合性は lock 非依存 |
| mtime/ctime | No（同上） | backend txn | redis.go:3162-3165 |
| slice list（chunk key） | No（同上） | RPUSH の原子性＋txn | redis.go:3169 |
| parent dir stats／quota／user-group quota（メモリ上） | No | `dirStatsLock`／`quotaMu`／`parentMu`／atomic | quota.go 各所 |
| usedSpace（Redis） | No | MULTI 内の INCRBY | redis.go:3174 |
| compaction trigger | No（重複抑止は `m.compacting` と scheduler の `s.mu`） | `m.Mutex`＋`m.compacting`、`compactionScheduler.mu` | base.go:2847-2868、compaction_scheduler.go:91-133 |
| `openFile.attr`／`first`／`chunks`／`lastCheck`（open-file cache） | **No** — これらの読み書きはすべて `openfiles.Mutex`（global）で守られる | `openfiles.Mutex` | openfile.go:118-258（ReadChunk/CacheChunk/InvalidateChunk/Update 全て `o.Lock()`） |
| **chunk cache の一貫性（Read の doRead→CacheChunk と Write の commit→InvalidateChunk の順序）** | **Yes — これが f.Lock の本質的役割** | `openFile.RWMutex`（Read が RLock、Write/Truncate/Fallocate/CopyFileRange が Lock） | Read: base.go:2108-2133（RLock 保持中に doRead→CacheChunk）。Write: InvalidateChunk を unlock 前に実行（base.go:2201） |
| Write ／ Truncate ／ Fallocate ／ CopyFileRange の client 内相互排他 | Yes | 同上 | base.go:2231-2235, 2266-2270, redis.go:3190-3194 |

推論: f.Lock がないと次の race で **古い slice list が chunk cache に残る**: Read が doRead で旧 list を取得 → Write が commit → InvalidateChunk → Read が CacheChunk で旧 list を格納 → 以降 open-cache 有効期間中、書いた内容が読めない（read-after-write 破れ）。よって「f.Lock を単純に削除」は不可。ただしこの invariant に必要なのは **chunk 単位** の排他（＋全 chunk を無効化する操作との排他）であって、inode 全体ではない。

### 1.3 VFS 側の前提（観測事実）

- `chunkWriter.commitThread`（pkg/vfs/writer.go:204-255）は **chunk ごとに 1 goroutine**、同一 chunk の slice は作成順に逐次 `m.Write` する。`fileWriter.Mutex` は `m.Write` 呼び出し中は解放している（writer.go:221-234）。
- growing write の順序は VFS 側の `s.dep`（前 chunk の最後の growing slice の commit 待ち）で担保（writer.go:217-219, 311-328）。
- したがって **同一 client 内で同一 inode の Meta.Write が並行するのは「異なる chunk」同士のみ**。同一 chunk の並行 Write は client 内では起きない。#6398 の「many chunkWriters can flush concurrently but metadata update is serialized」はこの構造そのもの。

---

## 2. Redis doWrite の RTT／command 数

### 2.1 txn の実装（観測事実、redis.go:1143-1209）

- `keys[0]`（doWrite では `inodeKey(inode)`）の FNV32 hash で `txLock(h)`＝`txlocks[h % 1024]`（base.go:52, 665-671）を **txn 全体（retry 含む）で保持**。
- 1 attempt = `m.rdb.Watch(ctx, fn, keys...)`。go-redis v9.18.0 の `Client.Watch`（tx.go:59-68）は `WATCH keys` → `fn(tx)` → `defer tx.Close()` で **必ず `UNWATCH` を送る**（tx.go:71-74）。
- retry: `redis.TxFailedErr` のみ retry（redis.go:1079-1082）、最大 50 回、`rand % (i+1)^2` ms sleep（redis.go:1202）。I/O timeout は retry しない（`retryOnFailure=false`）。
- Lua: doWrite では未使用（Lua は lookup 用 `scriptLookup` のみ、redis.go:461, 998）。

### 2.2 doWrite（redis.go:3141-3185）の command 列

| # | command | RTT | 条件 |
|---|---|---|---|
| 1 | `WATCH inodeKey` | 1 | 常時 |
| 2 | `GET inodeKey` | 1 | 常時（attr の read-before-write） |
| 2' | `HGETALL parentKey` | +1 | `attr.Parent == 0`（hardlink）。`checkQuota` の引数として **delta.space==0 でも評価される**（redis.go:3157、Go の引数評価順） |
| 3 | `MULTI` / `RPUSH chunkKey slice` / `SET inodeKey attr` / [`INCRBY usedSpace`] / [`RPUSH txnLog` + `INCR txnLastLog`] / `EXEC` | 1（pipeline） | INCRBY は `delta.space > 0` のみ、changelog は `fmt.ChangeLog` 有効時のみ（redis.go:1622-1631） |
| 4 | `UNWATCH` | 1 | 常時（go-redis の Tx.Close） |

- **合計: 4 RTT（hardlink なら 5）、command 7〜10 個**。
- numSlices は `RPUSH` の戻り値（list 長）で取得（redis.go:3180-3182）、LLEN 等の追加 RTT はない。
- parent stat／dir usage／quota は **同 txn ではなく** メモリ集計→周期 flush（§1.1）。usedSpace だけは同 txn の INCRBY。
- **overwrite（length 不変）でも SET inodeKey は必ず実行**（mtime/ctime を毎回更新）。

### 2.3 同一 inode の並列 commit で起こること（推論）

- 同一 client: `txLock(fnv(inodeKey))` により doWrite 全体が直列。open-file lock を外しても **4 RTT × 件数** の直列は残る。スループット上限 ≒ 1 / (4 × RTT) commit/s/inode/client（例: RTT 0.5 ms → 約 500 commit/s）。
- 複数 client: 各 client の doWrite は inode key を WATCH して SET するので、**別 chunk でも必ず WATCH 衝突**し retry。client 内の local mutex では守れず、Redis の WATCH だけが整合を担保する（正しさは保たれるが retry コストが増える）。
- txLock は 1024 スロット共有なので、hash 衝突した無関係な inode／chunk key の txn とも直列化される（例: background compaction の `doCompactChunk` は `chunkKey` で txLock、redis.go:3814）。
- compaction との相互作用: `doCompactChunk` は chunk key を WATCH → LRANGE → MULTI(LTRIM/LPUSH…) （redis.go:3807-3851）。doWrite の RPUSH が間に入ると compaction 側が `TxFailedErr` で retry。**random write が激しい chunk ほど compaction が失敗・retry しやすい**（推論、未計測）。

---

## 3. lock 一覧表

| lock | scope | protected state | contention target | can narrow? | risk |
|---|---|---|---|---|---|
| `openFile.RWMutex`（`m.of.find(inode)`、openfile.go:19-26） | inode 単位、client 内。Write/Truncate/Fallocate/CopyFileRange(fout) が W、Read が R | chunk cache（first/chunks）と backend commit の順序（Read の doRead→CacheChunk の原子性）。Write 系同士の client 内排他 | 同一 inode の別 chunk の Write、同 inode の cache miss Read、Truncate 等 | **Yes（条件付き）**: Write と Read は chunk 単位で十分。Truncate/Fallocate/CopyFileRange は全 chunk を無効化するので inode 全体の排他が必要 → 2 階層化（inode RW + chunk stripe）または世代番号方式 | stale chunk cache（read-after-write 破れ）、lock order 逆転による deadlock |
| `txlocks[fnv(keys[0]) % 1024]`（Redis、redis.go:1153-1162） | key hash 単位、client 内、txn 全体（retry 含む） | 同一 key に対する client 内 WATCH 衝突の回避（性能目的。正しさは WATCH が担保） | 同一 inode の全 txn（doWrite／Truncate／touchAtime／SetAttr 等）、hash 衝突した他 key | per-chunk にしても inode key を SET する限り WATCH 衝突になるので **単独では無意味**。Lua 化すれば不要化可能 | 外すと client 内 retry storm（rand sleep）で逆に遅くなる |
| `txBatchLock(inode)`（SQL/TKV、base.go:673-700、sql.go:1242、tkv.go:1104） | inode % 1024、client 内。**SQLite は inode=1 固定で全 txn を client 内直列**（sql.go:1237-1240） | 同上（性能目的） | 同一 inode の全 txn／SQLite は全 txn | SQL は `SELECT FOR UPDATE` で backend 側でも直列なので narrow の利得小 | — |
| `openfiles.Mutex`（global、openfile.go:44-258） | 全 inode 共通、短区間 | of.files map、attr/chunks cache、refs | 全 open file の find/Read/Invalidate | 短区間なので現状問題になりにくい（推論）| — |
| `m.Mutex` + `m.compacting`（base.go:2847-2868） | client 全体、短区間＋ busy-wait | 同一 chunk の compaction 重複防止 | 同期 compaction 待ち（10ms sleep ループ） | busy-wait を f.Lock 外に出すべき | f.Lock 保持中の長時間待ち |
| `compactionScheduler.mu`（compaction_scheduler.go:91-133, 189-232） | client 全体、短区間 | job map、per-inode 1 active | Write からの schedule | 問題なし | per-inode 同時 1 chunk の制約が巨大ファイルで compaction 不足を招く |
| `dirStatsLock` / `quotaMu` / `parentMu`（quota.go） | client 全体、短区間 | メモリ上の stat/quota 差分 | 全 Write | 問題なし | — |
| Redis WATCH inodeKey（server 側楽観） | inode、client 間 | attr（length/mtime/ctime）＋chunk RPUSH の一貫した組 | 他 client の同 inode 操作 | Lua 化で置換可能 | WATCH を外し MULTI のみにすると length の lost update |
| Redis WATCH chunkKey（compaction、redis.go:3814） | chunk、client 間 | compaction の origin 一致確認 | doWrite の RPUSH | — | write 激しい chunk で compaction retry |

---

## 4. lock ordering と deadlock risk

### 4.1 現在の順序（観測事実）

- Write: `f(inode).W` → `txLock(fnv(inodeKey))` → 解放 → `dirStatsLock`/`quotaMu`/`parentMu`/`s.mu` → [同期 compaction: `m.Mutex`(compacting, busy-wait) → `freeMu`(NewSlice) → `txLock(fnv(chunkKey))`] → `openfiles.Mutex`(InvalidateChunk) → `f.Unlock`
- Truncate/Fallocate: `f(inode).W` → `txLock(inodeKey)`（base.go:2231-2242, 2266-2280）
- CopyFileRange(Redis): `f(fout).W` → `txLock(fnv(inodeKey(fout)))`。`fin` の open-file lock は取らない（redis.go:3190-3194, 3330 付近 `m.inodeKey(fout), m.inodeKey(fin)`、txLock は keys[0] のみ）
- Read: `f(inode).R` → `openfiles.Mutex`（ReadChunk/CacheChunk）→ RUnlock → touchAtime（`txLock(inodeKey)`、defer 順により RUnlock 後。base.go:2101-2112）
- compactChunk（background／scheduler）: `m.Mutex`(compacting) → `txLock(fnv(chunkKey))`。**open-file lock は取らない**（base.go:2843-2971）。完了時 `openfiles.Mutex`（InvalidateChunk）。

### 4.2 評価

- 現状、`f.Lock` → `txLock` の一方向のみで、txLock 保持中に f.Lock を取る経路は見当たらない → deadlock なし（観測範囲）。
- 同期 compaction（Write 内）は f.Lock を持ったまま `m.compacting[k]` を busy-wait するが、待たれる側（background compactChunk）は f.Lock を必要としないので deadlock にはならない。ただし **待ち時間＝相手の object I/O 時間**であり、その間 inode の全 Write／cache miss Read が止まる（tail latency リスク）。
- Redis txn は txLock を 1 つしか取らない（keys[0] のみ）ので、txLock 同士の順序問題はない。SQL/TKV の複数 inode は `txBatchLock` で slot をソートして取得（base.go:681-699）。
- fork の deferred GC（`enqueueCompactionDelete`、compaction_gc.go:152-）は txn 後に呼ばれ、f.Lock とは無関係。

---

## 5. SQL / TKV の doWrite 比較

| backend | client 内 lock | txn 内の往復 | 1 Write あたり | 備考 |
|---|---|---|---|---|
| Redis | `f.W` → `txLock(inodeKey)` | WATCH, GET, [HGETALL], MULTI..EXEC, UNWATCH | **4 RTT**、O(1) データ量 | 楽観 txn。client 間は WATCH 衝突→retry |
| SQL（sql.go:3355-3401, 1230-1267, 1570-1593） | `f.W` → `txBatchLock(inode)`（**SQLite は inode=1 固定＝client 内全 txn 直列**） | BEGIN, `SELECT … FOR UPDATE`(node), [getParents], `INSERT … ON CONFLICT DO UPDATE slices = slices \|\| ?`(chunk), `INSERT slice_ref`, `UPDATE node`, `SELECT chunk`(slice 数取得のため **blob 全体を再読込**), [changelog INSERT], COMMIT | **約 7〜8 RTT** + COMMIT の WAL fsync | PostgreSQL/SQLite では `insert` フラグが常に false（MySQL のみ RowsAffected で判定、sql.go:1573-1590）→ **毎回 SELECT chunk で blob 全体読込**。bytea 連結は行全体の書き直し（TOAST/WAL も全体）で slice 数 n に対し O(n)/write、最大 2500×24B≒60KB。`FOR UPDATE` で client 間も server 側直列 |
| TKV（tkv.go:2650-2694, 1100-1130） | `f.W` → `txBatchLock(inode)` | batch get(inode, chunk) 1 RTT、commit（TiKV なら 2PC: prewrite＋commit、＋TSO） | 約 2〜4 RTT | chunk value 全体を read-modify-write（O(n)）。inode key を毎回書くので client 間は楽観衝突→retry |

推論: PostgreSQL/SQLite が遅い主因の候補は (1) RTT 数（7〜8）、(2) chunk blob 全体の書き直し＋再読込で slice 数に比例するコスト、(3) commit ごとの fsync、(4) SQLite の client 全体単一 writer。いずれも実測は未確認。

---

## 6. 改善案の評価

前提として「同一 chunk の Write は VFS で既に逐次」「backend の整合性は txn が担保」「f.Lock の本質は chunk cache の一貫性」（§1.2, §1.3）。

### (0) 同期 compaction を f.Lock の外へ出す【推奨・最小・安全度高】

- 変更関数: `baseMeta.Write`（base.go:2182-2227）のみ。`f.Unlock` と `InvalidateChunk` を doWrite＋stat 更新の直後に明示実行し、その後 `compactChunk(..., once=true, ...)` を呼ぶ（defer 構造を分ける）。
- back-pressure（2500 超で書き手を待たせる）は呼び出し元 commitThread が同期で待つので維持される。他 chunk の Write／Read は止まらなくなる。
- race: compactChunk は元々 f.Lock なしで動く設計（background 経路）なので新たな race はない。`doCompactChunk` の origin 比較（redis.go:3816-3826）で並行 Write とは整合。
- lock order: f.Lock 解放後に `m.Mutex`/txLock(chunkKey) を取るので順序は単純化される。
- リスク: 低。計測（compactTime）もそのまま残せる。
- 効果: 2500 到達時の inode 全体停止を解消。doWrite 直列化そのものは解消しない。

### (a) non-growing overwrite に限り chunk-level lock

- 案: `openFile` に inode RW lock（現 RWMutex）＋ chunk stripe（例: `[64]sync.RWMutex` を `indx % 64`）を持つ。
  - Write（全ケースで可、growing でも可）: inode **R** → stripe(indx) **W**
  - Read: inode **R** → stripe(indx) **R**
  - Truncate/Fallocate/CopyFileRange: inode **W**（stripe 不要）
  - 順序: inode lock → stripe → txLock → openfiles.Mutex。stripe を 2 つ以上同時に取る経路は作らない。
- 「non-growing 限定」にする必要は client 内 cache の観点ではない（length は backend txn が max 更新、growing 順序は VFS の dep が担保）。ただし length 増加時の `of.attr` 更新は InvalidateChunk の `lastCheck=0` のみなので現状と同じ。
- **Redis では効果がほぼない**: 内側の `txLock(fnv(inodeKey))` が doWrite 全体を直列化するため（§2.3、memo の指摘を source で確認）。効果があるのは Read との並行性、stat 更新・InvalidateChunk の重なり程度。
- 変更関数: `openFile`（openfile.go:19-42、pool 再利用時の初期化含む）、`baseMeta.Write`/`Read`/`Truncate`/`Fallocate`、各 backend の `CopyFileRange`（redis.go:3187-、sql.go:3403-、tkv.go:2696-）。
- 代替（世代番号方式）: `openFile` に chunk ごとの generation を持たせ、`InvalidateChunk` で ++、`Read` は doRead 前の世代と `CacheChunk` 時の世代が一致したときだけ cache する（`openfiles.Mutex` 下で CAS）。これなら Read は lock 不要、Write は inode lock が不要になりうる。ただし Truncate 等との排他は別途必要。正しさの検証（race test）が必須。

### (b) inode attr 更新を小さな lock に分ける

- client 内で attr 更新だけを小 lock にしても、**Redis の attr は 1 つの string blob で GET→SET の RMW** なので、server 側原子性は WATCH か Lua に依存する。client 内 lock の分割だけでは RTT は減らない。
- multi-client では local mutex は無力（WATCH が唯一の保護）。
- 単独での効果は小さい。(c) と組み合わせて初めて意味がある。

### (c) Redis command 削減

| 案 | RTT | 安全性 | 評価 |
|---|---|---|---|
| c1: WATCH と GET を 1 pipeline（非 MULTI）で送る | 4→3 | WATCH は GET より先に server で実行されるので意味論は不変 | go-redis `Tx.Pipelined` で可能（未検証）。変更は doWrite のみ。低リスク |
| c2: UNWATCH 省略 | 3→2 | EXEC 後は server が自動で unwatch するので冗長 | go-redis の `Client.Watch` が強制送信（tx.go:61,72）。回避には conn 管理を自前化する必要があり侵襲的 |
| c3: **Lua script（EVALSHA）で doWrite を server 側原子実行** | **1** | Redis は script を原子実行するので WATCH 不要、txLock も正しさ上は不要 | 最大効果。script 内で attr blob を parse（type 確認、length max 更新、mtime/ctime 書換え）、RPUSH、[INCRBY]、[changelog]、戻り値に list 長と delta を返す。課題: attr の binary layout（`m.marshal`/`parseAttr`）を Lua 側と同期させる必要、quota 判定は client 側（`checkQuota` は delta.space>0 のときだけ意味がある、quota.go:276-279）なので **growing write は従来 txn にフォールバックし、non-growing overwrite のみ Lua** とするのが安全。Truncate 等の txn は inode key を WATCH しているので、Lua の SET で EXEC が失敗→retry となり整合は保たれる。cluster では全 key が同一 hash tag である前提（要確認） |
| c4: mtime 更新の coalesce（同一 mtime なら SET 省略など） | 減らない／少 | mtime/ctime は write ごとに更新されるべき（POSIX、close-to-open で他 client に見える必要）。lastMod は ns 精度で毎回異なる | 非推奨。省略すると WATCH 衝突は減るが意味論が変わる |
| c5: txLock を per-chunk 化 | 変わらず | 正しさは WATCH が担保 | 同一 inode key を SET するので client 内でも WATCH 衝突→retry storm。**c3 なしでは逆効果** |

### 単純な f.Lock 削除が不可な理由（再掲）

- chunk cache の stale 化（§1.2）。
- Truncate/Fallocate/CopyFileRange の `InvalidateChunk(invalidateAllChunks)` と Read の CacheChunk の順序が崩れる。

---

## 7. memo の指摘「txLock(inode key hash) があるので per-chunk 化では直列化解消にならない」の検証

- 観測事実: `redisMeta.txn` は `fnv32(keys[0])` で txLock を取り（redis.go:1153-1162）、doWrite は `keys = [inodeKey(inode)]`（redis.go:3184）。txLock は WATCH→GET→MULTI/EXEC→UNWATCH と retry の全区間で保持（redis.go:1172 の `defer m.txUnlock(h)`）。
- したがって open-file lock を per-chunk にしても、同一 client・同一 inode の doWrite は txLock で直列（**memo の指摘は正しい**）。
- 追加の観察: txLock を per-chunk にしても、doWrite が必ず inode key を SET するため WATCH 衝突で retry になる（§2.3）。本当に直列化を解くには **WATCH を使わない server 側原子実行（Lua）** が必要。SQL は `SELECT FOR UPDATE` で server 側行ロック、TKV は inode key の楽観衝突で、いずれも inode 単位の直列化が backend 側にもある。

---

## 8. #6398 の判定

- Issue 本文（WebFetch で取得）: 10GB ファイルへの 4K random write で、`Write()` の `f.Lock()` 待ちが発生し、chunkWriter は並列 flush できても metadata 更新が直列化される、lock scope を chunk 単位に縮小できないか、という提案。取得時点でコメントは表示されなかった（取得内容に comments なし）。
- 判定: **本 fork に該当する**（Write の lock 構造は upstream と同一、base.go:2194-2201 vs upstream 0b90c7db base.go:2163-2168）。
- ただし Redis では Issue の提案（chunk 単位 lock）だけでは doWrite の直列化は解消せず、txLock と inode key WATCH の二重の inode 単位直列化がある。fork では加えて「同期 compaction を f.Lock 内で実行」「priority scheduler の per-inode 1 active」が inode 全体の停止を長引かせる要因になりうる。

---

## 9. 観測事実／推論／未確認

### 観測事実（source）
- Write は f.Lock 内で doWrite、stat 更新、background compaction 投入、2500 以上で同期 compaction を実行（base.go:2194-2224）。
- InvalidateChunk は f.Unlock より前に実行（defer 順、base.go:2198, 2201）。
- openFile の cache フィールドは global `openfiles.Mutex` で保護、RWMutex は Read/Write の排他用（openfile.go）。
- Redis txn は keys[0] の FNV hash で 1024 スロットの txLock を txn 全体で保持（redis.go:1153-1172、base.go:52, 665）。
- go-redis v9.18.0 の Watch は WATCH→fn→UNWATCH（tx.go:59-74）。
- doWrite は overwrite でも inode SET（redis.go:3162-3172）、numSlices は RPUSH 戻り値。
- compactChunk は open-file lock を取らない（base.go:2843-2971）。
- priority scheduler は per-inode 同時 1 job（compaction_scheduler.go:136-185）。
- SQL: PostgreSQL/SQLite では毎回 SELECT chunk（sql.go:3389-3393, 1570-1593）、SQLite は全 txn を inode=1 で直列（sql.go:1237-1240）。
- Read は RLock 中に `requestBackgroundCompaction` と `f.attr.Tier` 参照（`f.attr` は openfiles.Mutex 外で読んでおり data race の可能性、base.go:2133-2138）。

### 推論
- 巨大ファイル random write では、Redis 直列 4 RTT/commit が per-inode スループット上限を決める。
- 遅い object storage 環境では同期 compaction が f.Lock 保持時間を支配し、slow metadata write WARN の `compact=` が大きくなるはず。
- per-inode 1 active の compaction scheduler により 2500 到達が増える。
- PostgreSQL の遅さは O(n) blob 書き直し＋再読込と RTT 数が主因候補。

### 未確認
- 実機ログでの lock_wait／doWrite／compact／txLock lock_wait の内訳（fork の WARN/DEBUG で分離可能）。
- `Tx.Pipelined` による WATCH+GET 同時送信が go-redis v9.18.0 で期待通り 1 RTT になるか（コード未確認）。
- Lua 化時の Redis Cluster の hash tag 条件、attr layout の互換性。
- multi-client 同一 inode 書き込みでの WATCH retry 頻度（txRestart metric で確認可能）。
- `f == nil`（open file map に無い inode）での Write が実運用で発生する経路（gateway／SDK 等）。
