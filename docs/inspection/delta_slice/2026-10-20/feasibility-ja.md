# 差分 slice 方式（delta slice compaction）の実現可能性の調査

- 日付: 2026-10-20
- 対象: `juicefs/` のブランチ `feat/kaz-cache-2q`（HEAD `57b3de8a`）。`1.4.1-improve-kaz`（`86321d84`）は取り込み済みで、`pkg/meta` の差分はない（`git diff --stat 1.4.1-improve-kaz HEAD -- pkg/meta` が空）。比較用の upstream は tag `v1.4.1`。
- 方法: ソースの読み取りのみ。コードの変更、Redis への書き込み、mount、テストの実行はしていない。
- 表記: **[source]** はソースで確認した事実（`ファイル:行`、パスは `pkg/meta/` を省略することがある）。**[推論]** はソースからの推論。**[未確認]** は確かめていないこと。

## 0. 結論

**メタデータのモデルとしては実現できる。** 「1 つの chunk の中の複数の record が同じ slice id を、別々の pos/off/len で参照する」状態は、upstream の `CopyFileRange` が普通に作る状態で、3 つのエンジンとも参照数を **record ごと**に増減する。読み込み、upstream の compaction、kaz の block compaction、削除、trash、gc、dump/load は、この状態を正しく扱う（§1〜§4）。

ただし、次の条件を全部満たす必要がある。

1. **新しい差分 slice の参照数を、commit と同じトランザクションで record の数 N に合わせる。** 今の `doKazReplaceChunk` は作った slice ごとに参照 1 で作る（Redis は `HSet "0"`、SQL は `refs=1` の insert、TKV は 0 を set）。`created` に同じ id を N 回入れるだけでは、Redis と TKV では参照が 1 のままになり（**参照の不足 → 先に object が消える → データ消失**）、SQL では主キーの重複で失敗する（§5 の B1）。
2. 外す record の参照は **record ごと**に 1 つずつ減らす（id ごとにまとめない）。今の kaz の planner と replacer は record ごとなので、そのまま使える（§3）。
3. 差分の中身は「外す record が見えていた範囲」を、外す前の見え方（`buildSlice` の結果）からコピーして作り、残す record の**上**に置く。今の kaz block compaction と同じ不変条件で正しさが決まる（§3.3）。
4. record の数を抑える。commit 時の `compactionWanted` は 350 を超えると**毎回の write で**compaction を要求し（base.go:2189）、2500（`maxSlices`）に達すると **block mode でも upstream の chunk 全体の書き直しを同期で**走らせる（base.go:2263-2271）。差分の record の数 N はこれより十分小さく保つ必要がある（§6）。

**改造していない JuiceFS（upstream v1.4.1）が同じボリュームを mount しても、データは壊れない**（読み込み、書き込み、compaction、削除、gc）。例外は 2 つで、どちらも差分 slice 方式で**新しく生まれるものではなく、頻度が上がるもの**である。

- **TKV の `juicefs clone`（単一ファイル、`kvMeta.doCloneEntry`）は、同じ slice を参照する record が複数あると参照を少なく数える**（tkv.go:4565-4579、upstream v1.4.1 にも同じコード）。差分 slice がある file を TKV 上で clone すると、後でどちらかを消したときに object が先に消える。Redis と SQL、TKV の batch clone は正しい（§2.6、§5 の B2）。
- upstream v1.4.1 の Redis `CopyFileRange` は source の chunk を WATCH しない。compaction と同時に copy すると、死んだ参照を生き返らせる競合がある（kaz の改修 `84f19ca4` で直してあるが、改造していない client には残る）。compaction 全般の既存の問題で、差分方式に固有ではない（§5 の B6）。

改造していない client の**費用**の問題はある。差分の record が 5 個以上ある chunk を読むと upstream の Read は compaction を要求し（base.go:2144）、upstream の compactChunk は chunk 全体を 1 つの slice に書き直す。正しいが、差分方式で避けたかった大きい PUT が起きる。これは今の block compaction の結果（16 個の block の record）でも同じ。

エンジンの parity（Redis／SQL／TKV）は、kaz のファイル（`*_compact_kaz.go`、`compaction_block_*kaz.go`）だけの変更で取れる見込み（§7）。

## 1. メタデータのモデル: 同じ id の複数の record は正当な状態か

### 1.1 record の形式と参照数の意味

- [source] record は `pos(4) id(8) size(4) off(4) len(4)` の 24 バイト（slice.go の `marshalSlice`、`sliceBytes = 24`）。chunk の中の record の並び順が新旧（後ろほど新しい）を表す。
- [source] 参照数の格納はエンジンで違う。

| エンジン | 格納 | 値の意味 | 新しく書いた slice | 死んだ判定 |
|---|---|---|---|---|
| Redis | hash `sliceRef` の field `k{id}_{size}` | 値 = 参照数 − 1。field がなければ参照数 1 | 何も作らない（無い＝1） | 値 < 0（`doCleanupSlices` は `-` で始まる値、redis.go:3583-3605） |
| SQL | 表 `chunk_ref` の `refs` | 値 = 参照数 | `doWrite` が `refs=1` を insert（sql.go:3388） | `refs <= 0`（sql.go:3846、4009） |
| TKV | key `K{id}{size}` の int64 | 値 = 参照数 − 1。key がなければ 1（backup で `parseCounter(v)+1`、tkv_bak.go:392） | 何も作らない | 値 < 0（tkv.go:3071） |

- [source] compaction の新しい slice は、Redis は `HSet ... "0"`（redis.go:3888）、SQL は `sliceRef{id,size,1}` の insert（sql.go:3954）、TKV は 8 バイトの 0 を set（tkv.go:3195）で「参照 1」として作る。成功後に Redis／TKV は値 0 の key を `cleanupZeroRef` で消す（無い＝1 に戻す）。

### 1.2 同じ id の複数の record を作る既存の経路

| 経路 | 同じ chunk に同じ id が複数並ぶか | 参照の増やし方 |
|---|---|---|
| `CopyFileRange`（Redis） | **並ぶ。** source の chunk を `buildSlice` で見え方に直し、見えている piece ごとに 1 record を書く。1 つの slice の真ん中が上書きされていると、その slice は左右 2 つの piece になり、dest に同じ id の record が 2 つできる。dest の chunk 境界をまたぐ piece は 2 つの chunk に分かれる。同じ file の中の copy でも同じ。 | **record ごとに `HIncrBy +1`**（redis.go:3351、3357、3362） |
| `CopyFileRange`（SQL） | 同上 | record ごとに `refs=refs+1`（sql.go:3549） |
| `CopyFileRange`（TKV） | 同上 | record ごとに `tx.incrBy(+1)`（tkv.go:2869-2879）。tx の中の書き込みは後の get に見える（memTxn/etcd は buffer、TiKV/Badger/FDB は read-your-writes。tkv_mem.go:62-66、tkv_etcd.go:44-48、tkv_fdb.go:216-219）ので累積する |
| clone（Redis） | source の chunk の list をそのまま複製 | record ごとに `HIncrBy +1`（redis.go:5488） |
| clone（SQL） | 同上 | record ごとに `refs=refs+1`（sql.go:5521） |
| clone（TKV 単一、`doCloneEntry`） | 同上 | **`tx.gets` で全部読んでから `old+1` を set するので、重複した key は +1 しか増えない**（tkv.go:4565-4579）→ §2.6 |
| clone（TKV batch、`doBatchClone`） | 同上 | `refCounts` で key ごとに数えてから足す（tkv.go:4781-4794）。正しい |
| truncate／fallocate | id 0 の record を足すだけ（redis.go の doTruncate 内の `RPush ... marshalSlice(..., 0, 0, 0, l)`） | 参照は変えない |
| upstream compaction、kaz block compaction | 新しい slice は 1 つにつき 1 record | 参照 1 |

- [source] SQL の `deleteChunk` は「同じ chunk に同じ id が複数ある」ことを前提に、id ごとに減らす数を数えて（`deltas[sl.id] += -1`、sql.go:3817）まとめて更新する。upstream v1.4.1 にもある（#7018）。
- **[推論] 同じ chunk の中で同じ id を複数の record が参照する状態は、upstream が普通に作り、全経路で record 単位の参照数として扱っている正当な状態である。** 差分 slice 方式はこの状態を compaction の出力として作るだけで、新しい record の形式は要らない。

### 1.3 参照を減らす経路（全部 record ごと）

| 経路 | Redis | SQL | TKV |
|---|---|---|---|
| 削除（`deleteChunk`、`doDeleteFileData` から） | record ごとに `HIncrBy -1`、1 つの MULTI の中で順に減るので `< 0` になるのは最後の 1 回だけ（redis.go:3736、3748） | id ごとに合計を減らし、その後 record ごとに参照を読んで `<= 0` なら `deleteSlice`（sql.go:3814-3848）。同じ id で N 回 `deleteSlice` を呼ぶ（無駄な DELETE が N−1 回。既存の挙動） | record ごとに `incrBy -1`、累積して `< 0` は最後の 1 回（tkv.go:3071） |
| upstream compaction（`doCompactChunk`） | 外す record ごとに `-1`（redis.go:3896） | record ごとに `refs-1`（sql.go:3968） | record ごとに `-1`（tkv.go:3203） |
| kaz block（`doKazReplaceChunk`） | `removed` の record ごとに `-1`（redis_compact_kaz.go:62-65） | record ごとに `refs-1`（sql_compact_kaz.go:55-61） | record ごとに `-1`（tkv_compact_kaz.go:47-51） |
| trash（trash-days > 0） | compaction は外す record ごとに `(id,size)` を delayed に encode（base.go:3061、compaction_block_kaz.go:219-224）、期限後に entry ごとに `-1`（redis.go:3833） | entry ごとに `refs-1`（sql.go:3900） | entry ごとに `-1`（tkv.go:3154） |

- [source] compaction の後の「死んだ slice の通知」は、Redis は同じ MULTI の中の `HIncrBy` の結果が `< 0` の record だけ（redis.go:3916-3925 付近、redis_compact_kaz.go:89-94）。SQL と TKV は commit の後で record ごとに参照を読み直して `<= 0`／`< 0` なら通知するので、同じ id で複数回通知しうる（sql_compact_kaz.go:91-106、tkv_compact_kaz.go:77-87）。

## 2. 各経路の詳細と、差分 slice に対する挙動

### 2.1 読み込み（B）

- [source] `buildSlice`（slice.go:134）は record を順に二分木へ「上から重ねる」だけで、id で引く map、id での重複除去、id での並べ替えはない。出力は位置順の piece の列で、piece ごとに `Id/Size/Off/Len` を持つ。
- [source] VFS の reader は piece ごとに `store.NewReader(s.Id, s.Size)` を作り、`off + s.Off` から読む（pkg/vfs/reader.go:823-847）。位置だけで決まり、同じ id が何度出てきても関係ない。
- [source] object とキャッシュの key は slice ごと・block ごと（`chunks/.../{id}_{indx}_{bsize}`）。差分 slice が 4 MiB 以下なら 1 object・1 block になる。小さい読み込みでキャッシュがなく、block の途中から 1/4 以下を読むときは range GET（cached_store.go:150-157）。
- [source] kaz-2q のキャッシュ配置の `kazCompactionSources`（pkg/vfs/compact_cache_kaz.go:42-79）は、同じ id の piece が何度あっても `(id, block)` で重複を除いて数える。
- **[推論] 読み込みに id の一意性の仮定はない。** 費用は §6。

### 2.2 upstream の compactChunk が差分 slice を含む chunk を compaction するとき（C、F）

- [source] `compactChunk`（base.go:2958-）は record を最大 `maxCompactSlices=1000` 読み、`skipSome` で先頭を飛ばし（base.go:3015）、残りを `compactChunk(compacted)` で 1 つの slice に書き直す。飛ばした record と書き直す範囲が重なると panic で止める（base.go:3021-3028）。
- [source] `skipSome`（slice.go:183-208）は先頭の record が 1 MiB 以上で、見え方の先頭にそのまま見えているときだけ飛ばす。比較は `pos/id/off/len`（`isFirst`）と構造体の一致（`*s == *first`、slice.go:203）で、id だけでは判断しない。
- [source] commit（`doCompactChunk`）は読んだ時点の record の列（`origin`）が今も chunk の先頭にあるかをバイト列で比べ、外した record ごとに参照を 1 減らす（§1.3）。
- **[推論] upstream の compaction は差分 slice を含む chunk を正しく書き直す**（見え方を 1 つの slice にコピーし、差分の record も base の record も record ごとに参照を減らすので、差分 slice は最後の record で死ぬ）。ただし書き直す範囲は「先頭を飛ばせない限り chunk のデータ全体」で、VM の image では 64 MiB 近くになる。

### 2.3 kaz block compaction が差分 slice を含む chunk を見たとき（C）

- [source] planner（compaction_block_plan_kaz.go:233-297）は、後の record に完全に隠れた record と、`small`（block より短く、block の範囲をちょうど埋めていない、61-64 行）の record を外し、`small` が触れる block を「書き直す block」にする。外す・残すは **record ごと**に決め、`removed` に record を入れる（253-259 行）。
- [source] `kept` と書き直す block の数の合計が `maxSlices`（既定は block 数 + 1 = 17、compaction_block_config_kaz.go:59-64）を超えると、全部の block を書き直す（269-275 行）。
- [source] 書き直した各 block は新しい slice 1 つ・record 1 つで、`marshalSlice(o.pos, c.Id, c.Size, 0, c.Size)`（compaction_block_kaz.go:210）。replacer は `created` ごとに参照 1 を作り、`removed` の record ごとに 1 減らす（redis_compact_kaz.go:54-65 など）。
- **[推論] 今の（差分方式を知らない）kaz の block compaction は、差分の record を全部 `small` として外し、触れる block を全部書き直す。** 結果は正しい（差分の record は record ごとに減り、差分 slice は死ぬ）。差分方式を入れた client と入れていない kaz client が同じボリュームを使っても壊れないが、入れていない側は今と同じ 44 MiB の書き直しをする。
- [source] forced（`juicefs compact`、`gc --compact`）は `planKazBlockCompactionForced`（211-220 行）で、隠れたデータが残る chunk を全体の書き直しにする。差分 slice は base の一部を隠すので、forced では全部 block の形に戻る。

### 2.4 書き込みの経路と同期 compaction（H）

- [source] commit 時の `compactionWanted(prev, now)`（base.go:2189-2199）は、record の数が x99 を通過したとき、または **350 を超えていれば毎回** true。
- [source] true のとき record の数が `maxSlices=2500` 未満なら background、2500 以上なら **`m.compactChunk(..., once=true, ...)` を同期で呼ぶ**（base.go:2263-2271）。block mode でも upstream の chunk 全体の書き直しになる（compaction_block_kaz.go:339-341 のコメントのとおり）。
- [source] Read（open-file キャッシュに無いとき）は、record の数か piece の数が 5 以上なら compaction を要求する（base.go:2144）。kaz policy の read-min（本番 100）は record の数で判定する（compaction_policy_kaz.go:135-141）。

### 2.5 削除、truncate、fallocate（A、D）

- [source] ファイル削除（`doDeleteFileData` → `deleteChunk`）は record ごとに減らす（§1.3）。truncate と fallocate（punch hole）は id 0 の record を足すだけで、参照に触らない。隠れた差分の record は、後の compaction が「完全に隠れた record」として record ごとに外す（plan 243-244 行、upstream は `compactChunk` の見え方に出てこないので外れる）。
- **[推論] 差分 slice の object が消えるのは、全部の record が外れた（参照が死んだ）ときだけ。**

### 2.6 clone（A、F）

- [source] TKV の `doCloneEntry`（tkv.go:4555-4580）:

```go
refs := tx.gets(refKeys...)
for i := range refKeys {
    tx.set(refKeys[i], packCounter(parseCounter(refs[i])+1))
}
```

  `refKeys` は record ごとに追加される（4569-4573 行）ので、同じ slice を参照する record が k 個あると同じ key が k 回入る。`gets` は全部の set より前に古い値を読むため、k 回とも `old+1` を set し、**参照は k ではなく 1 しか増えない**。upstream v1.4.1 の tkv.go:4505-4508 も同じコード。
- [source] TKV の `doBatchClone`（ディレクトリの clone、tkv.go:4770-4795）は `refCounts` で数えてから足すので正しい。Redis（redis.go:5485-5490）と SQL（sql.go:5518-5525）の clone は record ごとに増やすので正しい。
- **[推論] TKV で、同じ slice を複数の record が参照する file を `juicefs clone` すると参照が足りなくなり、後でどちらかの file の削除や compaction で参照が早く死に、もう一方の file が消えた object を指す（データ消失）。** upstream の CopyFileRange でも起きうる既存の不具合だが、差分方式では「1 つの差分 slice を数百の record が参照する」のが普通になるので、起きる確率が大きく上がる。本番が Redis なら直接は影響しない（[未確認] 本番のエンジンが Redis であることは agent_memo.md の記述からの推定）。

### 2.7 gc、fsck、dump／load、info、使用量（D）

- [source] `juicefs gc` は chunk の record から id → size の map（`vkeys`）を作り（cmd/gc.go:251-254）、負の参照の slice（pending）と compaction の slice の map と合わせ、object の key の id が map に無ければ leaked とする（cmd/gc.go:325-336）。**参照数と record の数を比べる処理はない。** 同じ id が何度あっても set として 1 つ。
- [source] 参照数の整合を検査・修復するコマンドは見当たらない（`pkg/meta/base.go` と `cmd/` に該当する処理が無い）。**参照数の誤りは黙って残る**（不足は早すぎる削除、過剰は object が消えないだけで、全 record が消えた後は gc が leaked として見つけて `--delete` で消せる）。
- [source] JSON の dump は record をそのまま出し（redis.go:4806 など）、load は record ごとに `refs[ck]++` で数え直す（dump.go:561-568）。Redis は `v-1` を HSet（redis.go:5296-5313）、SQL は `refs=v`（sql.go:5326、5357）、TKV は `v-1`（tkv.go:4449）。**JSON の dump／load は record ごとに正しく数え直す。**
- [source] binary の backup（`dump --binary` 系）は保存されている参照数をそのまま出して戻す（sql_bak.go:376-385、tkv_bak.go:392、599）。**参照数が誤っていれば誤ったまま移る。**
- [source] 使用量（`usedSpace`）と quota は file の長さ（`align4K`）で数え、compaction の commit（`doCompactChunk`、`doKazReplaceChunk`）は触らない。record の数や slice の大きさで二重に数えることはない。
- [未確認] `juicefs fsck` と `juicefs info` は piece／record ごとに object を確かめたり表示したりするので、同じ id を何度も見る（費用と表示だけで、正しさには影響しない見込み）。`juicefs warmup` は piece ごとに slice 全体の `FillCache` を呼び、既にあれば skip する（cached_store.go:1595-1602）。

### 2.8 deferred compaction GC と retire（A、E）

- [source] `enqueueCompactionDelete` は、参照の減少が commit され、死んだと判定された slice についてだけ呼ばれる（compaction_gc.go:150-163、呼び出し元は §1.3）。dispatcher は local の `Retire`（pending の upload の取り消し、キャッシュと stage の削除、cached_store.go:1562-1586）をしてから削除の channel に送る（compaction_gc.go:74-117）。
- **[推論] retire は参照が死んだ後にしか走らないので、他の record が差分 slice を参照している間に retire されることはない。** ただし前提は「参照数が正しい」ことで、参照の不足（§5 の B1、B2）があると、まだ参照されている差分 slice の stage／キャッシュも消える。
- [source] SQL と TKV は commit の後で record ごとに参照を読み直すので、同じ id を N 回通知しうる（§1.3）。`Retire` と `Remove` を同じ id で複数回呼ぶことになるが、どちらも消えている key を消すだけ（[推論] 正しさには影響しない。無駄な DELETE が増える）。

## 3. 差分 slice の作り方と正しさの条件

### 3.1 object の書き込み

- [source] `vfs.Compact`（pkg/vfs/compact.go:53-）は piece の列を前から順に連結して 1 つの新しい slice に書く。id 0 の piece は 0 で埋める。`SetWriteback(false)` なので、commit の前に object storage への PUT が終わる。
- **[推論] 差分 slice の object は、既存の `CompactChunk` のメッセージに「外す record が見えていた範囲の piece（`kazClipPieces` と同じ切り出し）を位置順に並べた列」を渡せば書ける。新しいデータの経路は要らない。** 各 piece が差分 slice の中で何バイト目から始まるか（= 新しい record の `off`）は、前の piece の長さの累積。

### 3.2 commit（参照数）

- [source] 今の `doKazReplaceChunk` は `created` の slice ごとに参照 1 を作る（Redis `HSet "0"` redis_compact_kaz.go:55、SQL `mustInsert(sliceRef{...,1})` sql_compact_kaz.go:44、TKV 0 を set tkv_compact_kaz.go:40）。
- **必要な変更**: 差分 slice 1 つを N 個の record が参照するなら、同じトランザクションの中で Redis は値 `N-1`、SQL は `refs=N`、TKV は `packCounter(N-1)` にする。成功後の `cleanupZeroRef` は N=1 のときだけ意味がある。失敗（EINVAL）のときは今と同じく「参照 1 からの −1」（Redis/TKV は −1、SQL は 0 を insert）で死なせて消す。
- [source] 「commit できたか不明なエラー」の確認は `created[0]` の参照 key の有無で判定している（redis_compact_kaz.go:74-80 など）。N 個の参照でも、key を必ず作るなら同じ判定が使える（Redis で N=1 のとき値 0、N>1 のとき N−1。どちらも key はある）。

### 3.3 見え方を変えない条件

- **[推論]** 差分の record の中身を「外す前の見え方（`buildSlice(ss)`）からのコピー」にすれば、その record を残す record のどれより上に置いても見え方は変わらない（同じ内容を上書きするだけ）。外した record が見えていた範囲が、全部どれかの差分の record で覆われていれば、record を外しても見え方は変わらない。今の block の planner はこれを block 単位で満たしている。差分方式は覆う単位を「外す record が見えていた範囲（を位置順に連結し、隣り合うものはまとめたもの）」にするだけ。
- [source] 置き換えは読んだ時点の先頭の record（`origin`）を `kept + 新しい record` に置き換え、その後に来た record（compaction 中の書き込み）はそのまま上に残る（redis_compact_kaz.go:50-53、sql_compact_kaz.go:39、tkv_compact_kaz.go:33）。後から来た書き込みは差分より上にあるので、read-after-write は保たれる。
- [推論] 見え方が id 0（穴、truncate の跡）になっている範囲は、0 で埋めた piece として差分に入れるか、今の planner の mask record（id 0）にする。
- [推論] 前回の差分の record を次の差分に取り込むときは、前回の差分の record を `removed` に入れて record ごとに減らす。全部外れれば前回の差分 slice は死んで消える。一部だけ外す（ある block だけ block の書き直しにする）と、前回の差分 slice は残りの record で生き続け、外した部分のバイトは object の中のゴミとして残る（大きさは差分の大きさが上限）。

### 3.4 差分の record の見分け方

- [推論] メタデータには「これは差分 slice」という印が無い。kaz の client が再起動しても判別できるように、record の構造から見分ける必要がある（例: 同じ id を複数の record が参照し、`size` が小さい）。CopyFileRange が作った同じ id の record を差分と誤認しても、次の差分に取り込むだけなので正しさには影響しない。誤認して「base として残す」側に倒すと、外すべき record が残るだけ。

## 4. 並行性と crash（E）

| 状況 | 結果 | 根拠 |
|---|---|---|
| PUT の後、commit の前に crash | 差分 slice の object は どの chunk にも無い → `juicefs gc` が leaked として検出、`--delete` で削除。参照 key は作られていないので残らない | [source] commit 前に参照 key を作らない（3.2）。gc の判定（cmd/gc.go:325-336）。既存の compaction と同じ |
| commit 中の crash | chunk の list と参照数の変更は同じトランザクション（Redis MULTI/EXEC、SQL txn、TKV txn） | [source] redis_compact_kaz.go:35-72 など |
| commit の後、通知の前に crash | 死んだ参照（負の値）が残り、`doCleanupSlices` が後で消す | [source] compaction_gc.go:28-29、redis.go:3583-3605 |
| 同じ chunk の同時の書き込み | `origin` の比較。先頭が変わっていれば EINVAL で差分 slice を消す。末尾に足された record は保たれる | [source] redis_compact_kaz.go:36-48、compaction_block_kaz.go:256-261 |
| 2 つの client が同じ chunk を同時に compaction | 先に commit した方だけが通り、後の方は EINVAL で自分の差分を消す | 同上 |
| writeback の staging にまだ upload していない小さい slice から差分を作る | 読み込みは local の stage／キャッシュから読める（既存の block compaction と同じ）。差分 slice 自身は同期で PUT。commit 後に小さい slice の参照が死ぬと retire で pending の upload が取り消される（PUT の削減になる） | [source] compact.go の `SetWriteback(false)`、cached_store.go:1562-1586 |
| 別の client の open-file キャッシュが古い見え方を持っている | 消えた object を読むと失敗しうる。既存の compaction 全般と同じで、差分方式に固有ではない | [推論] |

## 5. 壊れ方の一覧（G）

| # | 壊れ方 | 場所と条件 | 結果 | 避けられるか・方法 |
|---|---|---|---|---|
| B1 | **差分 slice の参照数の不足** | `doKazReplaceChunk` に同じ id を N 回 `created` として渡す（redis_compact_kaz.go:54-56、tkv_compact_kaz.go:39-41 は値 0 を set するだけ）。SQL は主キーの重複で txn が失敗（sql_compact_kaz.go:43-46） | Redis／TKV: 参照 1 なのに record が N 個 → 1 つの record を外した時点で参照が死に、retire（stage とキャッシュの削除）と object の DELETE → **残りの record がデータを失う**。SQL: compaction が常に失敗し、差分 object が orphan（gc で回収） | 避けられる。新しい slice ごとに参照の数を渡し、Redis は `N-1`、SQL は `N`、TKV は `N-1` を同じ txn で設定する。単体テストで「N 個の record のうち 1 つを外しても object が消えない」「全部外すと消える」を 3 エンジンで確かめる |
| B2 | **TKV の単一 clone の参照不足（既存の不具合）** | tkv.go:4565-4579（upstream v1.4.1 にもある）。同じ slice を参照する record が複数ある file を `juicefs clone` | 参照が 1 しか増えない → 片方の file の削除や compaction で参照が早く死に、もう一方が消えた object を指す（**データ消失**）。改造していない client が clone しても起きる | kaz 側だけでは完全には避けられない（改造していない client が clone すると起きる）。対策: (a) TKV では差分方式を無効にする、(b) kaz の `doCloneEntry` を `doBatchClone` と同じく key ごとに数えるよう直す（upstream にも報告する価値がある）、(c) TKV の volume では改造していない client に clone をさせない運用 |
| B3 | 外した record の参照を id ごとにまとめて減らす実装 | 新しい planner／replacer で、外す record を id で重複除去してから減らした場合 | 参照の過剰 → 差分 slice が死なず、object が残る（gc で leaked として回収できるが、参照 key は残る） | 避けられる。今の kaz の `removed` は record ごと（compaction_block_plan_kaz.go:253-259）なので、それを保つ |
| B4 | 外した record ごとに減らしたうえで、さらに同じ id の分を足して減らす（二重の減算） | 同上（例: 「前回の差分 slice を捨てる」処理を別に足した場合） | 参照の不足 → B1 と同じデータ消失 | 避けられる。参照の変更は `removed`（record）と `created`（参照数）の 2 つの経路だけにする |
| B5 | 差分の中身が見え方と一致しない（外した record の見えていた範囲を覆っていない、残す record の下に置く） | 新しい planner の不具合 | 読み込みが古いデータか 0 を返す | 避けられる。中身は `buildSlice(ss)` から切り出し、差分の record は `kept` の後（上）に置く。planner の単体テストで「置き換えの前後で `buildSlice` の結果のバイト列が同じ」を確かめる（今の block の planner のテストと同じ方針） |
| B6 | upstream v1.4.1 の Redis `CopyFileRange` と compaction の競合（既存） | upstream は inode の key だけ WATCH し、source の chunk を WATCH しない（v1.4.1 redis.go:3183-3305）。kaz は直してある（redis.go の CopyFileRange、`84f19ca4`） | compaction が死なせた slice の参照を、古い list から copy した側が +1 して生き返らせ、消えた object を指す record ができる | 差分方式に固有ではない。改造していない client で `cp --reflink` などを compaction と同時にしないこと。差分方式は compaction の回数を変えない（PUT の量を減らす）ので、危険が増えるのは record の数が増えた分だけ |
| B7 | 同期 compaction（upstream）で大きく書き直す | record の数が 2500 に達すると block mode でも upstream の chunk 全体の書き直しを同期で走らせる（base.go:2263-2271） | 正しいが、書き込みが止まり 64 MiB 近い PUT | 避けられる。差分の record の数 N と、次の差分化までの小さい record の数の和を 2500 よりずっと小さく保つ |
| B8 | 350 を超えると毎回の書き込みで compaction を要求 | base.go:2189-2192 | 差分の作り直しが毎回の書き込みで走ると、差分全体の PUT が繰り返される（例: 2 MiB の差分を 20〜30 秒ごと） | 避けられる。(a) N を 350 未満に抑える、(b) 差分方式の planner は「差分より上の新しい小さい record の数」が閾値に達するまで noop にする |
| B9 | binary の backup で参照数の誤りがそのまま移る | sql_bak.go:376-385、tkv_bak.go:392、599 | B1 などで参照数が誤っていると、復元先でも誤ったまま | B1 を避ければ問題にならない。JSON の dump／load は数え直す |
| B10 | SQL／TKV で同じ id の削除通知が複数回 | sql.go:3834-3848、sql_compact_kaz.go:91-106、tkv_compact_kaz.go:77-87 | 同じ slice の DELETE と retire が N 回（正しさには影響しない、無駄な API 呼び出し） | 新しい replacer では通知の前に id で重複除去する（参照の減算は record ごとのまま） |

**[推論] データを壊しうるのは B1、B2、B4、B5（と既存の B6）。** B1、B4、B5 は kaz の実装で避けられ、テストで確かめられる。B2 は改造していない client の操作で起きるので、TKV では運用で避けるか差分方式を使わない。

## 6. 費用（H）

- **record の数が減らない。** 差分の record の数 N は「別々の dirty の範囲の数」になる。chunk 1920 では compaction 1 回あたり約 99 record（commit 時の x99）、新しいデータ約 0.5 MiB（cache_churn/2026-10-20/compaction-overnight-ja.md）。差分は累積の dirty の範囲を持つので、同じ位置が繰り返し書かれれば N は頭打ちになるが、数百になりうる [推論]。
  - 隣り合う範囲を差分の中でも隣に置けば（位置順に連結すれば）1 record にまとめられる。間の短い隙間を base のデータごと差分にコピーすれば、さらに record を減らせる（バイトと record の数の交換）[推論]。
- **閾値との関係**: x99 の通過（99、199、299）、350 超で毎回、2500 で同期 upstream（§2.4）。read-min（本番 100）も record の数で判定するので、N ≥ 100 の chunk は open-file キャッシュに無い読み込みのたびに compaction を要求する。差分方式の planner は「新しいものが無ければ noop」を安く返す必要がある（今の block の planner も noop を返せる）。
- **改造していない client**: upstream の Read は record か piece が 5 以上で compaction を要求する（base.go:2144）。差分の chunk は必ず該当するので、upstream の client が読むと chunk 全体の書き直しが起きる。今の block の形（16 record）でも同じ。
- **Redis の list**: 1 record 24 バイト。N=500 で 12 KB。読むたびに LRANGE（open-file キャッシュに無いとき）。SQL は chunk の行の blob、TKV は 1 つの value。etcd は `maxCompactSlices` が 100 に下がり、kaz block の読み込み上限は 84（compaction_block_kaz.go:67-76）。差分の record がそれより後ろにあると、その回の compaction では見えない（正しさには影響しない）。
- **読み込みの組み立て**: `buildSlice` は record ごとに木を切る。piece が 16 を超えると `readManySlices`（reader.go:850）。差分の piece は小さい読み込みが増えるが、差分 slice は 1 object（4 MiB 以下なら 1 block）なので、キャッシュに入れば局所性は良い [推論]。
- **保存量**: base の block は全体を保持し続け、差分に隠れた部分は base の中のゴミになる。差分の一部だけを block で書き直すと、差分 object の中にもゴミが残る（上限は差分の大きさ）。
- **trash（trash-days > 0）**: 外した record の参照は期限まで減らない（delayed）。差分を作り直すたびに古い差分 slice が期限まで残る（既存の compaction と同じ）。

## 7. 実装の範囲とエンジンの parity

- [source] 置き換えの interface `kazChunkReplacer` は kaz の中だけの interface（compaction_block_kaz.go:27-37）で、実装は `redis_compact_kaz.go`、`sql_compact_kaz.go`、`tkv_compact_kaz.go`。
- **[推論] kaz のファイルだけで実装できる見込み。**
  1. planner（`compaction_block_plan_kaz.go` に追加か別の kaz ファイル）: 外す record（小さい record、前回の差分の record、隠れた record）と、外す record が見えていた範囲を決める。差分が大きくなったら今の block の書き直しへ切り替える。
  2. object の書き込み（`compaction_block_kaz.go`）: 範囲を位置順に連結した piece の列を 1 つの `CompactChunk` で書く（`vfs.Compact` は変更不要）。各範囲の `off` を計算して `marshalSlice(pos, id, size, off, len)` の record を作る。
  3. replacer の 3 実装: 作った slice ごとの参照数（N）を受け取り、同じ txn で設定する（§3.2）。通知は id で重複除去する（B10）。
  4. 設定（mode、差分の上限など）は kaz の config ファイル。
- upstream の関数（`doCompactChunk`、`deleteChunk`、`CopyFileRange`、clone、gc、dump/load）は変更不要。TKV の clone の不具合（B2）を直す場合だけ `tkv.go` を変える（upstream のファイル）。
- テスト: 3 エンジンの共有テスト（kaz block の既存テストと同じ形）で、置き換えの前後の見え方の一致、参照数（N 個の record のうち一部を外す、全部を外す、削除、trash を有効にしたとき、clone の後）、EINVAL のときの差分 slice の削除を確かめる。Redis が要るテストは `make test.meta.non-core`。

## 8. 未確認の事項

- [未確認] 本番のメタデータエンジンが Redis であること（agent_memo.md の記述からの推定）。TKV なら B2 が直接の危険になる。
- [未確認] chunk 1920 などで、差分に溜まる範囲の数 N と差分の大きさが、時間とともにどこで頭打ちになるか。ログの位置の情報から見積もれる（PUT の量の削減効果もこれで決まる）。
- [未確認] `juicefs fsck`、`juicefs info`、`warmup` で同じ id を何度も扱うときの費用（正しさには影響しない見込み）。
- [未確認] 実機での動作。今回はソースの読み取りのみで、テストは実行していない。
