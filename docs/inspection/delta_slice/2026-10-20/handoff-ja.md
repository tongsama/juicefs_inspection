# 引き継ぎ: 差分 slice 方式の compaction（2026-10-20）

この件は、別の会話セッションと別のブランチで進める（ユーザーの方針、2026-10-20）。この文書だけで始められるように、経緯・分かっていること・決まっていないことをまとめる。

## 1. 目的（ユーザーの方針）

- どんな I/O が来ても最適に処理し、バックエンドとのやり取りを最適にするファイルシステムに育てる。今のミッションは、細かい部分 I/O が起きる巨大な VM ディスクファイルに、最適な基盤を提供すること。
- ゲストの書き込みの発生源（journald、chrome や拡張、electron 系アプリなど）はユーザーが把握済み。発生源の特定やゲスト側の設定を提案の中心にしない（agent_memo の 2026-10-20 の方針）。
- アップロードを遅らせる案は好まない。ログなどの中身をすぐ見たいことがあり、他のクライアントから見えるのも遅れるため。

## 2. 問題（観測）

- block モードの compaction は、小さい書き込みが 1 つでも触れた 4 MiB のブロックを丸ごと書き直す。
- VM のディスクでは、ゲストの ext4 の inode table などに 4〜8 KiB の書き込みが 20〜30 秒ごとに散らばって届く。chunk 1920 では、約 2 分ごとに約 11 ブロック（44 MiB）を書き直していた。新しいデータは 1 回 0.5 MiB 程度。
- 夜間（10/19 21:38〜10/20 06:05）は、staging 5.0 GiB に対して compaction の出力が 32.7 GiB（約 10 倍）だった。[夜間の解析](../../cache_churn/2026-10-20/compaction-overnight-ja.md)
- compaction の起動は、記録の数が x99 を通過したときか 350 を超えたとき（`pkg/meta/base.go` の `compactionWanted`、upstream のまま）。kaz の read-min 100 もほぼ同じ境目になる。
- 増幅は、PUT の量、キャッシュの押し流し（kaz-2q の判定でも主な損失）、ローカル SSD の書き込みに効いている。

## 3. 案: 差分 slice 方式

- 元の 4 MiB の slice／object は書き直さない。
- compaction では、chunk の中の異なるブロックの変更データ（今見えているもの。同じ位置の書き直しは最新だけ）を、1 つの新しい slice に詰めて PUT する（多くは 1 object）。
- Redis の chunk の record（pos, id, size, off, len）を、同じ新しい slice id を指す複数の record にして、元の上に重ねて読み分ける。
- 次の compaction では、前の差分と新しい小さい record をまとめて、新しい差分にする。
- 差分が大きくなったら、今の方式と同じく 4 MiB の書き直しに「畳み込む」。

## 4. 実現性（ソースの調査、[報告](feasibility-ja.md)）

- メタデータのモデルとしては実現できる。同じ chunk で同じ slice id を複数の record が指す形は、upstream の CopyFileRange が作っている（`redis.go:3345-3362`、record ごとに参照 +1）。
- 削除・compaction・trash・gc・dump/load・使用量の計算は、record ごとに数えている。id が chunk 内で一意という前提はない。retire は参照が死んだ後にしか走らない。
- 改造していない JuiceFS も、読み書き・削除・gc・compaction を正しく扱う（下の B2・B6 を除く）。ただし、記録が多い chunk を読むと compaction を要求し、全体を書き直す（結果は正しい）。

### 壊れる機序（必ず対処する）

- **B1:** kaz の `doKazReplaceChunk` は、作った slice の参照を 1 で作る（`redis_compact_kaz.go:55` の `HSet "0"`、sql は refs=1、tkv は 0）。N 個の record で指すなら、同じ txn で Redis/TKV は N-1、SQL は N に設定しないと、record を 1 つ外した時点で slice が DELETE され、データが失われる。
- **B2:** TKV の単一ファイルの clone（`tkv.go:4565-4579`、upstream にもある既存の不具合）は、同じ slice を複数の record が指すと参照が 1 しか増えない。TKV では差分方式を無効にするか、この不具合を先に直す。本番は Redis。
- **B4/B5:** 差分の中身は「外す前の見え方」からコピーし、残す record（kept）の上に置く。外した record の参照は record ごとに 1 回だけ減らす（通知だけを id でまとめる）。
- **B6:** upstream v1.4.1 の Redis CopyFileRange は source の chunk を WATCH しない（kaz では 84f19ca4 で修正済み）。

## 5. 効果の見積もり（[見積もり](estimate-ja.md)、10/20 12:28〜14:03 の実測から）

- chunk 1920: 今の方式の実績 3,748 MiB（PUT 937 回）に対し、同じ起動の頻度で差分方式は約 370〜400 MiB（PUT 約 130〜160 回）。約 9 割減。
- 差分は頭打ちにならず、1 時間あまりで 3〜4 MiB、record 200〜330 個に達した。20〜30 分に 1 回は 4 MiB の書き直し（畳み込み）が要る。
- 隙間 16 KiB までをつなげると、PUT の量はほぼ同じで、record の最大を約 200 に抑えられた（今回の中で一番釣り合いが良い）。

## 6. 実装で決める必要があること

- **起動の条件:** 記録の総数ではなく「新しい書き込みの record の数」で起動する。総数のままだと、差分の record が常に多いため、書き込みのたびに起動する（模擬で確認）。`compactionWanted` は upstream の `base.go` にあるので、upstream への変更を最小に保つ方法を考える（kaz の hook に差し替えるなど）。
- **record の上限:** 350 を超えると書き込みのたびに要求、2500 で同期の全体の書き直し（`base.go:2263-2271`）。畳み込みの条件で 300 程度に抑える。
- **畳み込みの条件:** 差分の大きさ（例 4 MiB）と record の数（例 300）。
- **隙間をつなげる幅:** 4〜16 KiB が候補。
- **差分を段に分けるか:** 毎回差分全体を送り直すか、前の差分を残して新しい分だけを足すか。
- **upstream の Read の起動条件:** record が 5 以上で compaction を要求する（`base.go:2144`）。kaz では read-min で抑えられるが、改造していない client が混ざると全体の書き直しが起きる（正しさは保たれる）。
- **3 つのエンジンの parity:** 変更は kaz のファイル（block の計画、block の処理、`*_compact_kaz.go` の 3 つ）でできる見込み。TKV の clone を直すなら upstream の `tkv.go` に触れる。
- **読み込みの手間:** 元の object と差分の object の 2 回の GET（キャッシュがあればローカル）。

## 7. 作業の進め方（ユーザーの方針）

- 新しいブランチで行う。起点は、その時点の `1.4.1-improve-kaz` か、kaz-2q（`feat/kaz-cache-2q`）を merge した後のブランチ。ユーザーに確認する。
- 設計は superpowers の brainstorming から。正しさ（参照の数、metadata の parity、無改良版との互換）を最優先する。
- 方針: upstream の変更に強いコード（kaz のファイルにまとめ、upstream 由来のファイルは hook 程度）。無改良の JuiceFS との互換を失わない。

## 8. 材料（このディレクトリ）

- `feasibility-ja.md`: 実現性の調査（file:line つき）
- `estimate-ja.md`、`delta_sim.py`、`write_watch.py`、`writes.tsv`、`sim-1404-*.txt`: 見積もりと、その元データ・スクリプト。`write_watch.py` の位置による分類は当てにならないので、1 MiB 以上の record を compaction の出力とみなしている。
- 関連: `../../cache_churn/2026-10-20/compaction-overnight-ja.md`（夜間の compaction の解析）
