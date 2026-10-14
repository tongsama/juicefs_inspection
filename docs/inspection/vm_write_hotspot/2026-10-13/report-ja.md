# VM の書き込みの偏りと compaction の書き直し（2026-10-13 計測）

PUT の回数を減らす目的で、会話の中で順に調べたことの記録。[事実] は観測またはソースで確認したこと、[推論] は観測からの推定、[未確認] は確かめていないこと。

## 0. 前提と証跡

- 本体: `juicefs/` の `1.4.1-improve-kaz`（8a64eb07、kaz.5）。ソースの行番号はこの版。
- mount の設定（起動スクリプトから）: `--writeback`、`--cache-size 15G`、`--slice-flush-wait 30s`、`--slice-flush-idle 16s`、`--writer-reuse-window 32`、`--writer-flush-scope=range`、`--meta-write-batch=64`、`--compaction-gc-mode deferred`、`--compaction-scheduler priority`、`--kaz-finish-canceled-get` など。計測時だけ `--debug` を付けた（ユーザーが mount し直した。計測後に外した）。
- VM: ubuntu2604-desktop-mt（ゲストは Ubuntu 26.04、ext4）、qcow2 `ubuntu2604-desktop-mt.qcow2`（inode 627902、仮想 300 GiB、`preallocation=metadata`）、QEMU は `discard=unmap`、`cache=unsafe`。
- 計測: 2026-10-13 20:02 に VM を起動、20:04 にほぼ起動（ユーザーは「22:04」と書いたが、時刻から 20:04 の誤記と判断）、20:04〜20:18 はアプリの起動。区間は「起動」20:02〜20:04、「アプリ」20:04〜20:18:05。

| 証跡 | 場所 | SHA256 |
|---|---|---|
| DEBUG ログの固定 prefix（226,806,428 byte、19:58:45〜20:18:13） | `~/.juicefs/diagnostics/vm-io-20261013-195837.log` の先頭（元は変更していない。このリポジトリには置かない） | `5b9f6890…7f0d0f`（全桁は `result-all.json` の inputs） |
| accesslog（20:01:50〜20:18:06、`~/gnu/bin/cat` で取得） | `accesslog-20261013-200150.txt.gz`（元は diagnostics に残す） | 展開後 `6c079f0b…639f22` |
| qemu-img map（23:05、VM 停止中にユーザーが取得） | `qemu-img-map-20261013-2305.json.gz` | 展開後 `ceb6e830…a4f608` |
| metrics | `~/.juicefs/diagnostics/metrics-before-20261013-200154.txt`、`metrics-after-20261013-201805.txt` | `1300bcb3…`、`095687f1…` |
| ゲストの ext4 の情報 | `guest-ext4-info.txt` | — |

解析は scratchpad で、固定 prefix を `log.prefix`、accesslog を `accesslog.txt`、map を `map.json` という名前にして実行した。スクリプトと出力（`*.py`、`*.out`）、既存の `metadata_random_io/2026-10-07/analyze_install.py` の結果（`result-{all,boot,apps}.json`）をここに置く。

## 1. object storage から消えた slice の、ローカルの read cache

### ソース上の動き [事実]

- `cachedStore.Remove` → `rSlice.Remove` は、先に `Retire`（`pendingKeys` の取り消し、`retireCache` → `cacheManager.retire` → `removeLocal(key, true)` でメモリのページ、索引、キャッシュファイル、stage ファイルを消す）を行い、そのあとで object の DELETE を出す（cached_store.go:193、:1538、disk_cache.go:1396）。ローカルの片付けが失敗（unlink の失敗、`errCacheDown`、`ErrSliceUploadInFlight`）すると DELETE に進まない。
- deferred（`--compaction-gc-mode deferred`）でも、dispatcher が `RetireSlice` の callback でローカルを先に片付けてから、`dslices` に非ブロッキングで渡す（compaction_gc.go:74）。`remote_deferred`／`hint_overflow`／`local_error` は、durable な印を残して回復スキャンに任せる。
- 対象はその削除を処理したプロセスのキャッシュだけ。ほかの mount のキャッシュは消えない。

### 残り得る経路 [事実：ソース上の条件、未再現]

- 読み込みの GET（`fetchBlock`、prefetch、warmup の `FillCache`）が成功して `bcache.cache()` を呼ぶのが、`Retire` より後になると、片付けた key がもう一度登録される。`cacheStore.cache()` は `pages` と索引しか見ず、片付け済みの印（tombstone）は無い（disk_cache.go:449）。DELETE の後にもう一度 `Retire` は来ないので、追い出されるまで残る。データの正しさには影響しない（slice ID は再利用されない）。
- 書き出し中に片付けた場合は、`flush` が `pages` に key が無いのを見て消し直すので残らない（disk_cache.go:807）。
- `--kaz-finish-canceled-get` は、cancel された GET を最後まで走らせてキャッシュに入れるので、この窓を長くする（TODO の kaz-finish-canceled-get の既知の点と同じ）。新しい種類の経路ではない。cancel された分のキャッシュの使用量が増えるのは別の話（今も参照される slice）。
- 塞ぐ案（未設計）: `Retire` で key に世代番号か一時的な tombstone を付け、`cache()`／`add()` で再登録を拒む。

### 実測（2026-10-13 19:35、読み取りのみ） [事実]

キャッシュ（`raw/chunks`）の一覧を先に取り（4,359 ブロック、1,024 slice、14.82 GiB）、そのあと本番 Redis を `SCAN`／`LRANGE`／`HSCAN` だけで読んだ（chunk の key 459,925 個、生きている slice 458,828 個、約 26 秒）。**参照されていないブロックは 0 個**。`sliceRef` が負のもの、`delSlices`（空だった）にあるものも 0 個。スクリプトは `orphan_cache/main.go`（`META_URL` を環境変数で渡す。認証情報はファイルに書かない）、一覧は `orphan_cache/cache_list-20261013-193507.txt`。キャッシュがほぼ満杯で LRU が回っているので、できてもすぐ追い出される可能性がある [推論]。メモリ上のページと rawstaging は数えていない。

## 2. Read の前に slice を確定する理由 [事実：ソース]

- `VFS.Read` は読む前に `prelockFlush` と `flushForRange` を呼ぶ（vfs.go:796、:807）。`--writer-flush-scope=range` では `fileWriter.flushRange`（writer.go:705）が、読む範囲を含む chunk（64 MiB）の slice を全部集め、まだのものを `explicit_flush` で freeze し、commit を待つ。読む範囲と重ならない slice も、同じ chunk なら確定する。既定の `file` ではファイルの全 pending が対象。
- 理由: reader は Redis の chunk の一覧（commit 済みの slice）だけを見て、writer のバッファは見ない。commit できるのは長さの確定した（freeze した）slice だけ（object の key に長さが入り、書いた object は書き換えない前提）。同じ chunk の中は作成順に commit する。
- しないと read-after-write が壊れる（2026-10-01 の `TestVFSReadFlushError` で、flush の失敗を隠すと古い内容が返った）。`cache=unsafe` では QEMU がゲストの flush を無視するので、read-after-write を守っているのは主にこの flush。
- freeze を減らす方向の案（未設計）: pending read overlay（未 commit の内容を Read に重ねる）。

過去の計測: 2026-10-03 の耐久 run（file scope）では、明示 flush による freeze の 3.6〜8.4% が Read（主因は Fsync）。2026-10-07 のインストール計測では、qcow2（file）で Read 5,810・Fallocate 55,536、raw（file）で Read 12,744（freeze の 74%、fallocate 0）、qcow2（range）で Read 3,086・Fallocate 23,946、qcow2（range＋batch64）で Read 2,971。

## 3. 今回の計測：freeze の内訳（inode 627902） [事実]

| 区間 | freeze | Read 起因 | Fallocate 起因 | writable_window | idle | age |
|---|---:|---:|---:|---:|---:|---:|
| 起動 | 1,593 | 458（29%） | 178（11%） | 877（55%） | 78 | 2 |
| アプリ | 5,956 | 1,353（23%） | 224（4%） | 1,529（26%） | 2,763（46%） | 87 |

- Read 起因はほとんどが `vfs.Read.prelock`。
- Read 起因で閉じた slice の後、1 秒以内に同じ chunk の直後の位置から始まる slice ができたのは、起動で 3／458、アプリで 11／1,353（`cont.out`）。Read 起因で閉じた slice は p50 が 4 KiB、99% が 64 KiB 以下で、ほかの原因のものと同じ。書き込みが 4 KiB 単位で散っているので、Read が無くても別々の slice だった可能性が高い [推論]。直後以外の位置や 1 秒より後の書き足しは数えていない。
- Read 前の flush は 52,616 回、合計 81 秒（prelock の p99 31 ms、最大 1.0 秒）。commit は 1,309 回、平均 47 ms。
- fallocate は 85 回で、すべて mode 16（ZERO_RANGE、64 KiB〜8 MiB）。402 件の freeze を起こした。

## 4. chunk の偏りとゲスト側の持ち主 [事実、journald の割合は下限]

- read は 518 chunk に散る（上位 10 chunk で 15.6%）。write は 125 chunk に集中（上位 1 で 29.8%、上位 5 で 59.2%）。freeze も同様（上位 1 で 37.4%）。read の 40% は書き込みのあった chunk に入る（`chunks.out`）。
- 最も多い chunk 1（ファイル上の 64〜128 MiB）は、書き込み 2,874 回、freeze 2,834 件（Read 起因 582）。
- `qemu-img map` で host の位置をゲストの位置に直し、ゲストの ext4 の情報と突き合わせた（`ext4_regions.out`）。

| 書き込み先 | 回数 | 割合 |
|---|---:|---:|
| journald `system.journal` | 2,709 | 28.1% |
| journald `user-1000.journal` | 1,340 | 13.9% |
| ext4 のジャーナル（jbd2） | 1,062 | 11.0% |
| inode テーブル | 881 | 9.1% |
| ビットマップ | 267 | 2.8% |
| スーパーブロックと GDT、orphan file | 153 | 1.6% |
| qcow2 のメタデータ（map に無い場所） | 162 | 1.7% |
| その他のデータ | 3,076 | 31.9% |

- chunk 1 の 76%（2,190 回）は `system.journal`。chunk 1930 は全部 `user-1000.journal`、chunk 2384 は全部 jbd2、chunk 1920 は主にグループ 960 の inode テーブル。
- journald のファイルの配置は再起動後の `filefrag` によるので、rotate などで計測時に別の場所にあった分は「その他」に入る（ブロック 80896 は今はどのファイルにも属さない）。journald の割合は下限。
- 経路の推定 [推論]: journald は mmap したファイルのあちこちを書き換え、ゲストのカーネルが約 30 秒ごとに書き出す。mtime の更新で inode テーブルと jbd2 も書かれる。
- 補足: 日本語ロケールのゲストでは `dumpe2fs` の "Group" 行が翻訳されるので、`env LANG=C` が必要。

## 5. preallocation=metadata の配置が崩れている [事実：map、理由は推論]

- 先頭は「ゲストの位置＋320 KiB＝ファイルの位置」で並んでいるが、全体ではゲストとファイルの位置の差が 2,118 通りある。ゲストの 70 MiB 付近の cluster がファイル上の 73、82、80 MiB とばらばらに置かれ、ゲストで遠い場所（171 GiB、58 GiB など）の cluster が同じ chunk 1 に並ぶ。
- `discard=unmap` で解放した cluster が、あとの書き込みで割り当て直されている可能性が高い [推論]。そうなら、preallocation=metadata でも ZERO_RANGE が出たこと（新しい cluster の、書き込みで埋まらない部分のゼロ化）と合う。map は計測の約 3 時間後のもの。

## 6. ブロックサイズを 8 MiB にした場合の見積もり [事実：集計、見積もり]

20:02〜20:18:05 の成功した PUT 4,272 件を slice ごとにまとめた（`blocks.out`）。

| 出どころ | slice | PUT（4 MiB） | 8 MiB の見積もり |
|---|---:|---:|---:|
| writer | 3,229 | 3,230 | 3,229（4 MiB 超は 1 個だけ） |
| compaction | 83 | 1,036 | 527 |
| 合計 | — | 4,272 | 3,762（−12%） |

- ブロックサイズは format に記録され、`juicefs config` には項目が無い。`format` での変更は `--force` なしでは拒否される（pkg/meta/config.go:145）。`--force` で変えると既存の slice のブロック分割が合わなくなる [推論]。新しいボリュームへの移行が必要。
- 代償 [推論]: cold read の GET が 2 倍の大きさになる、キャッシュに入るブロック数が半分になる。
- 結論: 効果は compaction の分だけで、負担が大きい。PUT の約 3/4 は小さい writer の slice。

## 7. compaction の書き直し [事実]

`compaction_chunks.out`（ログの prefix 全体。最初の compaction は 20:02:50、最後は 20:18:08）。

- compaction 85 回で 4.12 GiB を書き直した。同じ時間帯のゲストの書き込みは 146 MiB で、約 28 倍。
- chunk 1 は 16 回（1 回あたり約 63.4 MiB、まとめた slice は合計 2,859 個）。chunk 1920 は 7 回、1930 は 4 回、168 は 3 回。
- 起動の条件はソースに固定（base.go:2185 の `compactionWanted`: slice が 99、199、… 個目を越えたとき、または 350 個超。2,500 個で同期 compaction）。`--compaction-scheduler priority` は順番だけを決める。
- 1 回の compaction は chunk の先頭の大きい slice しか飛ばせず（`skipSome`）、残りを全部書き直す。書き込みが 4〜20 KiB でも 64 MiB を書き直す。

## 8. 対策の候補

ゲスト側（ユーザーは別途取る方針）: journald を `Storage=volatile`、または `/var/log/journal` をローカルのディスクへ、ログの多いサービスを抑える、`lazytime`、`commit=`／`vm.dirty_expire_centisecs` を延ばす、qcow2 なら `lazy_refcounts`／discard の扱い、raw への切り替え。

JuiceFS 側（ユーザーの方針: ゲストの設定を変えずに減らせる対策を入れたい。いずれも未設計）:

1. 書き込みが続く chunk の compaction を遅らせる（cool-down、最後の書き込みから N 分）。350／2,500 の上限は残す。
2. バックグラウンドの compaction を始める個数をオプションにする（100 → 200／300）。代償は Read で組み立てる slice の数、LRANGE の大きさ、cold read の GET、Redis の使用量。
3. chunk ごとの書き込み頻度をメモリで数え、頻度の高い chunk ほど compaction を控える。
4. 範囲を限った compaction（小さい slice の範囲だけを書き直す）。chunk 1 は書き込みが全体に散るので効果は小さい [推論]。
5. FUSE の `writeback_cache`（カーネルで書き込みをまとめる）。flush の時点、fallocate／truncate との整合は未確認。

## 9. compaction の方式の机上比較（2026-10-14）

`compaction_sim.py`／`compaction_sim.out`。今回の commit（2,025 回、7,571 slice）と read（51,968 回）を時刻順に流し、方式ごとに compaction を再現した。

### 比較の途中でわかったこと [事実：ソースとログ]

- compaction の起動は commit の個数（`compactionWanted`）だけではない。**Meta.Read（open-file の chunk キャッシュのミス）でも、slice が 5 個以上なら要求する**（base.go:2139）。chunk キャッシュは commit で無効になり、mtime の変化で次の GetAttr のときにファイルの全 chunk が無効になるので、書き込みの続くファイルでは read のたびにミスしやすい。
- 実際の 85 回のうち 54 回は、slice が 99 個未満（中央値 35、最小 2）で始まっていた。Read の経路による起動が多い。1 回の compaction は slice の数に関係なく chunk 全体（約 63 MiB、PUT 約 16）を書き直す。
- `--compaction-scheduler priority` は **inode ごとに 1 つずつ**実行する（`state.active`）。実行中の chunk に要求が来ると、終わった後にもう一度並べる。1 回の中央値は 8.8 秒で、16 分のうち約 8 割はこの inode の compaction が動いていた計算になる。

### モデル（簡略化）

- 起動: commit の個数（99 個目ごと、350 個超）と、read のミス時の「5 個以上」。read のミスは「前回のミス以降にファイルへ commit があった、またはその chunk が compaction された」で判定。
- 実行: inode ごとに 1 本、優先度（1000 個以上、100 個以上、それ以外）の順、同じ優先度は先着順。1 回 8.8 秒（中央値）。開始時の slice を 1 個にまとめ、実行中に来た commit は残す。
- 費用: PUT は chunk の大きさ ÷ 4 MiB、DELETE はまとめた slice の数。
- 検証: 今の方式の再現は 105 回（実際は 85 回）、chunk 1 は 15 回（実際は 16 回）。99 個未満で始まったものは 75 回（実際は 54 回）。read のミスを多めに見積もっていると思われる。絶対値ではなく、方式の間の比で見る。

### 結果（抜粋）

| 方式 | compaction | PUT | DELETE | chunk 1 | read 時の slice 数 p95／p99／最大 | 終了時に残る要求 |
|---|---:|---:|---:|---:|---|---:|
| 今の方式（read 5 個以上、100 個ごと、350 超） | 105 | 1,370 | 6,664 | 15 | 32／84／697 | 35 |
| read 100 個以上 | 51 | 689 | 5,902 | 18 | 26／89／687 | 0 |
| read による起動なし | 45 | 605 | 5,896 | 17 | 33／106／687 | 0 |
| read 100 個以上＋200 個ごと・700 超 | 43 | 592 | 5,452 | 16 | 26／89／681 | 0 |
| cooldown 1 分（強制 2,500） | 88 | 1,195 | 4,501 | 1 | 34／101／2,024 | 29 |
| read 100 個以上＋cooldown 1 分＋強制 700 | 12 | 164 | 4,533 | 4 | 45／206／1,045 | 10 |
| read 100 個以上＋cooldown 1 分＋強制 1,000 | 9 | 120 | 3,108 | 2 | 45／206／1,166 | 12 |
| read 100 個以上＋cooldown 3 分＋強制 1,000 | 2 | 32 | 2,018 | 2 | 75／209／1,166 | 13 |

- **read による起動の下限を 5 → 100 にするだけで、compaction と PUT が約半分**。read 時の slice 数はほとんど変わらない（p99 84 → 89）。少ない slice のために 64 MiB を書き直す回が減る。
- **書き込みの多い chunk（chunk 1）は、個数のしきい値を変えても減らない**（read による起動を止めても 17 回）。commit が速く、99 個目ごとの条件にすぐ届くため。cooldown と、それを破る強制の個数の組み合わせで減る。
- **両方を組み合わせると PUT は約 −90%**（1,370 → 120〜164）。代わりに、read 時の slice 数の p99 が 206、最大が約 1,000〜1,166 になり、終了時に残る要求（あとで静かなときに実行される）が 10〜12 件になる。cooldown だけ（強制 2,500）では chunk 1 が 2,000 個を超える。
- cooldown は、journald のように約 30 秒ごとに書かれる chunk ではほとんど満たされないので、実際には強制の個数で compaction の間隔が決まる [推論]。

### 限界 [未確認]

- 16 分（915 秒）だけ。cooldown が満たされたあとの動きや、静かな時間帯の後始末は、この時間帯では評価できない。
- slice の多い chunk を読むときの費用（Meta.Read の LRANGE の大きさ、ブロックをまたぐ read で小さな object を何個も読む GET、キャッシュのミス）は、slice の数の分布でしか見ていない。最近書いた slice はローカルのキャッシュにある可能性が高い [推論]。
- 2,500 個の同期 compaction（書き込みが止まる）にはどの方式でも届かなかった。

次の手順（会話で提案、ユーザーの承認待ち）: 今回のログと accesslog で、今の方式（16 回を再現できるかで確認）、cool-down（1／3／5／10 分）、しきい値 200／300、頻度による調整を机上で比べ、compaction の回数、PUT と DELETE の見積もり、Read 時の slice の数を出す。16 分では cool-down の評価に短いので、必要なら今のゲストの設定のまま 1 時間ほど `--debug` で記録する。journald を volatile にした計測と raw の計測は、JuiceFS 側の対策の評価の後に回す。
