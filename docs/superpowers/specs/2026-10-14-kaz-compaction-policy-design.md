# compaction の起動条件の見直し（kaz compaction policy）設計

日付: 2026-10-14。状態: 設計（ユーザーが節ごとに承認済み、設計書のレビュー待ち）。

背景と根拠の数字は [VM の書き込みの偏りと compaction の書き直し](../../inspection/vm_write_hotspot/2026-10-13/report-ja.md)（§7〜9）を参照する。

## 1. 目的と条件

### 目的

VM のディスクイメージ（qcow2）を置いた JuiceFS で、compaction による PUT と DELETE を減らす。2026-10-13 の 16 分の計測では、compaction が 85 回、4.12 GiB を書き直した（ゲストの書き込み 146 MiB の約 28 倍）。そのうち 54 回は slice が 99 個未満の時点で始まり、書き込みの集中する chunk は 16 回書き直された。

### ユーザーの方針（会話で確認）

- ゲスト側の対策（journald など）は別に取るが、基本は**ゲストの設定を変えずに効く JuiceFS 側の対策**を入れる。
- **upstream の変更に強いコード**にする（本体への手の入れ方を小さく、判断は kaz 専用のファイルにまとめる、外から見える振る舞いでテストする）。
- Read からの起動の下限と、書き込みの続く chunk の cooldown＋強制の個数を、**1 つの版に両方入れる**（オプションは別々）。
- 方式は「scheduler の手前に判断の層を置く」（案 1）。

### 守ること

- 既定値では今の動きと完全に同じにする。
- compaction の中身、commit、fsync、read-after-write、commit の順序、slice の削除（deferred GC）には触れない。
- 2,500 個での同期 compaction、`juicefs compact`、`gc --compact`、同じ chunk の重複を防ぐ `m.compacting` は今のまま残す。
- メタデータの形式や Redis の key は増やさない。状態はメモリだけに持ち、再起動で消えてよい。
- priority scheduler の「inode ごとに同時に 1 本」は変えない。

### 成功の基準

同じ手順（`--debug`、VM の起動とアプリの起動、約 16 分）で 2026-10-13 の計測と比べて:

- compaction による PUT が大きく減る（机上の見込みは、本番で最初に試す値で約 −88%）。
- 同期 compaction（2,500 個）が 0 回。
- ゲストの read と書き込みの待ち（Read 前の flush など）が目に見えて悪くならない。
- 実行待ちの時間と、compaction を始めるときの slice の数が metrics で見える。

## 2. 現状（kaz.5、`1.4.1-improve-kaz` 8a64eb07）

- 起動の経路は 2 つ。
  - commit の後: `afterWrite`（base.go:2254）で `compactionWanted(prev, now)`（99 個目を越えたとき、または 350 個超）が真なら `requestBackgroundCompaction`。2,500 個以上なら同期の `compactChunk`。単発の commit も batch の commit もここを通る。
  - Read で chunk のキャッシュがないとき: `baseMeta.Read`（base.go:2139〜2145）で slice が 5 個以上なら `requestBackgroundCompaction`。chunk のキャッシュは commit と mtime の変化で無効になるので、書き込みの続くファイルではよく通る。
- `requestBackgroundCompaction`（compaction_scheduler_lifecycle.go:38）は、priority なら scheduler に登録し、legacy なら `go compactChunk(...)` する。どちらも kaz のコード。
- priority scheduler は inode ごとに 1 本ずつ実行する（`state.active`）。worker は全体で 11 本、hint は最大 1,024 件。

## 3. 構成

### 3.1 本体側の変更（最小限）

1. `afterWrite` に `m.kazCompact.onCommit(inode, indx, ph.numSlices)` を 1 行足す。`compactionWanted` と同期 compaction の判定は変えない。
2. `requestBackgroundCompaction(...)` を呼ぶ 2 か所の名前を置き換える。
   - Read（base.go:2145）→ `requestCompactionFromRead(...)`
   - commit（base.go:2261）→ `requestCompactionFromCommit(...)`
   どちらの行も、もともと kaz が書き換えた箇所。
3. 配線: `meta.Config` に 3 項目、`cmd/flags.go` にオプション、`cmd/mount.go` で設定と検査（`--compaction-scheduler` と同じ形）。
4. metrics の登録（`compactionGCEvents` と同じ形）。

データベースごとのコード（redis.go、sql.go、tkv.go）、`compactChunk`、`compactionWanted` の中身には触れない。

### 3.2 新しいファイル `pkg/meta/compaction_policy_kaz.go`

- `compactionPolicy` 型:
  - 設定: 下限 `readMin`、`cooldown`、`forceSlices`。
  - 渡す先 `forward func(inode Ino, chunk uint32, count, tier int)`（今の `requestBackgroundCompaction` の処理）。テストで差し替えられる。
  - 時刻を取る関数 `now func() time.Time`（既定は `time.Now`、テストで差し替える）。
  - 保留中の要求の map（key は inode と chunk、値は slice の数・tier・保留を始めた時刻）。上限 1,024 件。
  - 最後に書いた時刻の map。上限 4,096 件。
  - 見回りの goroutine（cooldown が 0 より大きいときだけ）。
  - `mu`: map を触るときだけ持つ。`forward` はロックの外で呼ぶ。
- `requestCompactionFromRead` と `requestCompactionFromCommit` は baseMeta のメソッドで、policy が無い（read-only など）ときは今の `requestBackgroundCompaction` をそのまま呼ぶ。
- 起動と停止は priority scheduler と同じタイミング。停止は scheduler の停止より前に行う。

## 4. オプション

| オプション | 既定 | 範囲 | 意味 |
|---|---|---|---|
| `--kaz-compaction-read-min` | 5 | 5〜2,499 | Read からの要求は、slice の数がこの値以上のときだけ渡す |
| `--kaz-compaction-cooldown` | 0（無効） | 0、または 1s〜1h | 書き込みが続く chunk への要求を、最後の書き込みからこの時間がたつまで保留する |
| `--kaz-compaction-force-slices` | 1,000 | 100〜2,499 | 保留中でも、slice の数がこの値に達したら渡す。cooldown が 0 のときは使わない |

範囲外の値は mount の前に拒否する。Go から不正な値が渡されたときは、今の動き（既定値）に戻してログに出す（`ValidateCompactionScheduler` の扱いに合わせる）。

本番で最初に試す値（ユーザーの選択）: `--kaz-compaction-read-min 100 --kaz-compaction-cooldown 1m --kaz-compaction-force-slices 700`。

## 5. 判断の規則

### 要求が来たとき

Read からの要求は、slice の数が `readMin` 未満なら捨てる（`read_below_min`）。残った要求と commit からの要求に対して:

1. cooldown が 0 → すぐ渡す（`pass`）。
2. slice の数が `forceSlices` 以上 → すぐ渡す（`pass`）。保留していれば記録を消す。
3. その chunk に cooldown の間書き込みがない、または記録がない → すぐ渡す（`pass`）。
4. それ以外 → 保留する（新しく保留したときだけ `deferred`）。すでに保留していれば、slice の数（大きいほう）と tier を新しくする。保留が上限に達していれば、すぐ渡す（`overflow`）。

### commit のたび（`onCommit`）

cooldown が 0 なら何もしない。それ以外は最後に書いた時刻を新しくし（記録が上限なら新しい chunk は記録しない）、保留中で slice の数が `forceSlices` 以上になったら渡す（`released_forced`）。

### 見回り（間隔は cooldown の 1/4、1〜5 秒に収める）

- 保留中で、最後の書き込みから cooldown が過ぎたものを渡す（`released_idle`）。
- 最後の書き込みから cooldown が過ぎた時刻の記録を消す。

### 渡すとき

そのときの最新の slice の数と tier を渡す。priority scheduler の優先度はこの値で決まる。その chunk の compaction が実行中なら、今と同じく scheduler が終了後に並べ直す。

### 補足

- 350 個を超えると commit のたびに要求が来る。cooldown が有効なら保留を新しくするだけで、force に達するまで渡さない。
- 2,500 個での同期 compaction は `afterWrite` で policy の手前で判定するので、policy の影響を受けない。

## 6. 監視

### metrics

| 名前 | 種類 | 内容 |
|---|---|---|
| `kaz_compaction_policy_events_total{event}` | カウンタ | `read_below_min`、`pass`、`deferred`、`released_idle`、`released_forced`、`overflow`、`discarded_on_stop` |
| `kaz_compaction_policy_deferred` | ゲージ | 今保留している chunk の数 |
| `kaz_compaction_queue_wait_seconds` | ヒストグラム | priority scheduler に登録してから実行が始まるまでの時間 |
| `kaz_compaction_scheduled_slices` | ヒストグラム | 実行を始めるときの、hint に記録された slice の数 |

下の 2 つは priority scheduler（kaz のコード）で測る。upstream の `compactChunk` には手を入れない。

### ログ

- INFO: mount のときに 1 行（有効かどうかと 3 つの値）。
- DEBUG: 状態が変わったときだけ（保留を始めた、見回りで渡した、force で渡した、あふれた）。inode、chunk、slice の数、保留していた時間を出す。保留を新しくするだけのときは出さない。

## 7. 安全性とうまくいかないとき

- policy は「いつ要求するか」だけを決める。要求を捨てても遅らせても slice が増えるだけで、データは失われない。
- 資源: 保留 1,024 件、時刻の記録 4,096 件、goroutine は見回りの 1 本。あふれたら今の動きに戻す。
- ロック: map を触るときだけ。`afterWrite` で待つのはこの短いロックだけ。
- 時刻: `time.Now()` の単調時計で経過時間を測る。
- 動かさない場合: read-only の mount、compaction を起動しない構成では policy を作らない。cooldown が 0 なら見回りと時刻の記録を作らない。
- 停止: 保留中の要求を捨てて数え（`discarded_on_stop`）、見回りの goroutine の終了を待つ。停止後の要求は何もしない。

想定しているリスク:

1. 書き込みの続く chunk に slice が最大約 `forceSlices` 個までたまり、その chunk の Read が重くなる（意図した引き換え。metrics と実機の比較で確かめる）。
2. 静かな時間がないまま止まると、保留した要求は失われる。次の mount で書き込みか Read が来たときに改めて要求される。
3. upstream が `afterWrite` や Read の周りを作り直したときは、commit の後と Read で slice を読んだ後の 2 種類を追い直す。振る舞いのテストが落ちて気づける。

## 8. テスト

Go 1.25.11、`TMPDIR` はルートのファイルシステム。本番の Redis には接続しない（Redis のテストは一時的に起動した Redis で行う）。

1. policy 単体（時刻を差し替え、渡した要求を記録する）
   - 既定値: すべての要求がそのまま同じ順で渡り、`onCommit` は何もしない。
   - 下限: Read の下限未満は捨て、以上は渡す。commit からの要求は下限に関係なく渡す。
   - cooldown: 書き込みの続く chunk を保留し、cooldown が過ぎたら見回りで渡す。書き込みが続く間は渡さない。
   - force: 要求の時点で force 以上ならすぐ渡す。保留中に `onCommit` で force に達したら渡す。
   - 上限: 保留と時刻の記録があふれたら今の動きに戻す。
   - 停止: 保留を捨てて数え、停止後は何もしない。goroutine が終わる。
   - 並行: 要求、`onCommit`、見回りを同時に行う（`-race`）。
2. 本体につないだ状態（外から見える振る舞い）
   - 渡す先をテストで差し替え、実際のメタデータエンジンで commit と Read を行い、渡された要求の回数と slice の数を数える。
   - MemKV と SQLite: 既定値で今と同じ要求が出る（commit は 99 個目を越えたとき、Read は 5 個以上）。下限 100 と cooldown の動き。
   - 一時的な Redis: batch の commit（`doWriteSlices`）でも同じ結果。
   - 2,500 個での同期 compaction が設定に関係なく起きる。
3. 設定と scheduler
   - オプションの範囲の検査。
   - metrics の登録と件数の増加。
   - priority scheduler の待ち時間と slice の数のヒストグラムに値が入る。
4. 既存のテスト
   - `pkg/meta` の compaction、scheduler、GC のテストと `test.meta.core` を流し直す。新しいテストは `-race` で 3 回。
   - 既存の race の警告（`utils.WithTimeout`、`rawFull` など）は既存の問題として記録し、新しいコードの分と区別して報告する。
5. 実機
   - ブランチ `feat/kaz-compaction-policy`（`1.4.1-improve-kaz` から）でビルドし、ユーザーが mount する。
   - 2026-10-13 と同じ手順（`--debug`、約 16 分）で、compaction の回数、PUT と DELETE、slice の数の分布、Read 前の flush の待ち、同期 compaction、新しい metrics を比べる。机上の見込み（約 −88%）との差も記録する。

## 9. 範囲に含めないもの

- 範囲を限った compaction（小さい slice の範囲だけを書き直す）。
- FUSE の `writeback_cache`、pending read overlay。
- `compactionWanted` の「100 個ごと」「350 個超」のオプション化（机上比較で効果が小さかった）。
- 保留中の要求の永続化、ほかの client との調整。
- ゲスト側の設定、raw イメージへの切り替え。

## 追記（2026-10-14、実機計測の後）

実機の計測（[報告](../../inspection/vm_write_hotspot/2026-10-14/report-ja.md)）で、cooldown（§4〜§5 の `--kaz-compaction-cooldown` と `--kaz-compaction-force-slices`）は PUT の総数を増やすことがわかった。compaction は writeback の staging にあるアップロード待ちの小さな slice を片付けて PUT を省いており、保留するとその前にアップロードされる。§1 の「compaction は PUT を増やすだけ」という前提が誤りだった。ユーザーの承認で cooldown、force、保留、見回り、`afterWrite` の hook、関係する metrics を削除した（本体 49908265）。残るのは Read からの要求の下限（`--kaz-compaction-read-min`）と scheduler の観測だけで、commit の経路は upstream のまま。本番の設定は `--kaz-compaction-read-min 100`。
