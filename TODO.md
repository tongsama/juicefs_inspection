# JuiceFS 調査・修正 TODO

更新: 2026-10-03。現在の入口は [README](README.md) と [知見の要約](docs/findings.md)。`juicefs/` 本体は別 Git 管理とする。

## 現在の状態

- 本体ブランチ `release-1.4.1-kaz.2`（kaz の既定ブランチ）、HEAD `9268beb4`（2026-10-06 に push 済み）。旧 `fix/vm-io-wait-policy`（84f19ca4）はローカル・リモートとも削除済みで、このブランチに含まれる。
- ユーザーが staging 回収補正版を適用し、30s/10s で耐久試験合格と報告した。10/03 の再起動で15s/10sへ変更。15秒版は短期観測。
- 最終runtime確認はwriter/FUSE期限0、reuse16、GCdeferred、priority、max-deletes10、max-uploads18。
- 保存待機は耐久fsync335774件全成功。一方、Readfile EIOと旧clientのVFS EIO累積1207が見つかり、全I/O無エラーとは評価しない。
- ドキュメントは調査ルートへ分離した。文書と本体のcommitはユーザーが別々に行う。agentからadd/commit/pushしない。

## 次の優先作業

- [ ] **upstream v1.4.2 への追従（2026-10-26、調査済み・未実施）:** [調査報告](docs/inspection/upstream_v1.4.2/2026-10-26/port-investigation-ja.md)。kaz HEAD b6a0e24b に v1.4.2 を merge でき、衝突は cached_store.go・base.go・vfs.go の 3 ファイル。ほかに format の atomic 化（#7378）で TKV の `doWriteSlices` とテスト 4 ファイルが build できない（`getFormat()` に置き換える）。#7500／#7503／#7509 と同じ不具合が kaz HEAD にあり（#7500 は再現済み）、upstream の形を採る。2026-10-27 ユーザーの判断で、本家のタグごとにブランチを切り、前の kaz を merge する形にした。本体 `1.4.2-improve-kaz`（起点 v1.4.2、worktree `/home/kwatanabe/tmp_local/juicefs-1.4.2-wt`）に merge して衝突を解消し、whole read の注記と `--kaz-upgrade-busy`（既定 fail）を足した。テストは ok（報告 6 章）。
  - [x] 2026-10-27 本体を commit した（ユーザー承認）。項目ごとのブランチ（`port/kaz-onto-v1.4.2`、`docs/kaz-whole-read-compressed`、`feat/kaz-upgrade-busy`）を `1.4.2-improve-kaz` へ `--no-ff` で merge した。HEAD は `97254d5d`。
  - [ ] push、既定ブランチの切り替え、`juicefs-v1.4.2-kaz.1` の release は、それぞれユーザーに確認してから行う。調査リポジトリの文書の commit も確認待ち。
  - 2026-10-27 ユーザー方針: 本家へ PR は出さない。提案 P5（upstream-fix の分離）は行わない。
  - [x] 2026-10-27 P1 を本体 `refactor/kaz-upstream-comments`（1.4.2-improve-kaz から、未 commit）で実施。upstream の関数の直前の kaz の doc コメント 49 か所を元に戻し、関数の中の kaz の変更行の上に `// kaz:` を 39 か所入れた（コメントと空行だけの変更）。本家 main との衝突ブロックのうち kaz が原因の分は 16 → 13。調査リポジトリの AGENTS.md の例外の文面はユーザーの承認待ち。
  - [x] 2026-10-27 P4: 本体で `rerere.enabled=true`、v1.4.2 の解消を学習。手順書とスクリプトは `docs/development/upstream-sync/`。
  - [x] 2026-10-27 kaz- の接頭辞が付いていない kaz のフラグ 9 個（compaction-gc-mode、compaction-scheduler、writeback-fsync、slice-flush-wait、slice-flush-idle、writer-reuse-window、writer-flush-timeout、writer-flush-scope、meta-write-batch）を `kaz-` に改名した（本体 `feat/kaz-flag-prefix`、未 commit）。旧名は別名として残り、使うと起動時に警告が出る。実際の mount で旧名の値が `.config` に入ることを確認した。
  - [ ] 1.4.2 系を本番に入れるときに、本番の mount スクリプトと `tests/juicefs/mount_flags.txt`（44〜52 行）を新しい名前に切り替える。旧名のままでも動くが、警告が出る。kaz.8 以前のバイナリには新しい名前がないので、バイナリより先にスクリプトを変えない。旧名の別名は、何版か後に消す。
  - [x] 2026-10-27 取り込みやすさのリファクタリング（計画 `docs/superpowers/plans/2026-10-27-kaz-upstream-friendly-refactor.md`、Task 0〜7）を本体 `refactor/kaz-upstream-friendly` で実施。upstream のファイルの差分 25 files +2,061 → 20 files +425、kaz が原因の衝突 13 → 9。最終レビューは「実機テスト後に merge 可」。報告書 9 章。
  - [x] 2026-10-27 ユーザーの実機テストで問題なし（テスト版 `~/.local/bin/kaz-test/juicefs-142-refactor-6c558773`）。ユーザーの指示で `refactor/kaz-upstream-friendly`（HEAD 4049d608）を `1.4.2-improve-kaz` へ `--no-ff` で merge（d246b0f7）。ユーザーの指示で `1.4.2-improve-kaz` を kaz へ push し、GitHub の既定ブランチを `1.4.2-improve-kaz` に切り替えた（2026-10-27）。項目ごとのブランチは push していない。worktree（juicefs-1.4.2-wt、juicefs-delta-wt）は削除し、`juicefs/` の checkout を `1.4.2-improve-kaz`（追跡先 kaz/1.4.2-improve-kaz）に切り替えた。
  - [x] 2026-10-27 `_common_options.mdx` の kaz の行を `docs/{en,zh_cn}/reference/kaz_options.md` へ移した（本体 647b0a59、衝突 9 → 6）。日本語版 `docs/ja/reference/kaz_options.md` も追加（4049d608。本家に日本語の docs はなく、kaz のページだけ）。
  - [x] 2026-10-27 リリース: 調査リポジトリを commit（789bbb8 文書、a2e3912 release）して original へ push、タグ `juicefs-v1.4.2-kaz.1`（本体 d246b0f7）を push。release-juicefs の workflow は成功（run 40464374598）、draft を作成（linux amd64/arm64、windows amd64、install.sh/ps1、checksums）。linux-amd64 の checksum と `juicefs version`（1.4.2+2026-10-27.d246b0f7-kaz.1）を確認。
  - [x] 2026-10-27 ユーザーが公開（07:08 UTC、Latest の印も付いた）。install.sh／install.ps1 の版の比較（major, minor, patch, kaz の数値の順）で、kaz.8 より新しい版として選ばれることを、公開中の一覧で確認した。
  - [ ] 本番を 1.4.2-kaz.1 へ入れ替えたら、mount スクリプトと `tests/juicefs/mount_flags.txt` のフラグを新しい名前に直す（旧名でも動くが警告が出る）。
  - [ ] 1.5 系への追従のときに、`upload` の staging・`flush`・`commitThread` の移動と、`remove` の `scanned` の条件の見直し、phase の並びのテスト。
  - [ ] 取り込みやすさの改善 P1（upstream 関数に足した doc コメントを戻す、AGENTS.md の例外）→ P4（rerere）→ P2（埋め込み struct）→ P3（`*_kaz.go` へ移して hook 化、`RetireSlice` の番号、`WriteSlices` を任意の interface に）→ P6 → P5（upstream への PR 候補を分ける。PR はユーザー確認後）。
  - [x] 2026-10-27 findings に追記した: whole read は zstd では追加の効果がない（フラグ名は誤りで、内部の設定）。upstream の「上に重ねる」は、root でないと古い mount を強制的に外す。
  - [ ] VM が動いている状態で graceful upgrade が busy になるかを、試験の suite で確かめる（未検証）。

- [ ] **長時間の稼働でキャッシュが入れ替わり、起動が遅くなる（2026-10-19、ユーザー報告、検討中）:** 仮説 H1（無効なブロックが容量を占める）、H2（書き込みと compaction の出力が LRU の先頭に入り、起動用のブロックを押し出す。ソース上はこちらが有力）、H3（キャッシュミス以外が原因）。計測案 A-1〜A-3（読み取りのみ）と対策案（早期回収、2Q の受け入れ方針）は agent_memo 2026-10-19。**前提（ユーザー方針）:** 無改良の JuiceFS との互換性を保つ（キャッシュのファイル名・配置・形式は変えない、追加の状態は無改良版が無視でき、無いときは従来の LRU で動く）、upstream の更新に載せ直しやすい作り。
  - [x] 2026-10-19 A-2（[報告](docs/inspection/cache_churn/2026-10-19/report-ja.md)）: 無効な容量は約 1.3 GiB（7%）、dead 0。H1 は小さい。
  - [ ] A-1 の時系列（`metrics.tsv`、5 分間隔で常駐、PID 197922）から、アイドル中の流入の速さと 20G が一周する時間を見積もる。
  - [ ] A-3: 次の VM 起動のとき（ユーザーが声をかける）、起動の前後で A-1 の差分と A-2 を取る。`keys-events.tsv`（key_tracker、PID 200030）で、起動時に GET したブロックが以前に del されていたか（追い出しによる GET か）を判定する。
  - 10/19 10 時の再起動の GET の補充は 2.99 GiB、アイドル中の流入は約 2.8 GiB（報告の追記）。
  - [x] 2026-10-19 通常使用（2 時間で流入 29.5 GiB、compaction の出力が 45%）で、追い出した直後の再 GET 3.02 GiB を観測した。
  - [x] 2026-10-19 A-3（13:10 の再起動、ログインまで 13.5 分）: GET で補充した 3.45 GiB は、すべて当日の追い出しによるもの。**H2 を確認。**
  - [x] 2026-10-19 実装計画を作成: [計画](docs/superpowers/plans/2026-10-19-kaz-cache-2q.md)。
  - [x] 2026-10-19 subagent-driven で実装。本体 `feat/kaz-cache-2q`（89776b30..bf9efe73、push・merge なし）。最終レビュー済み。
  - [ ] ブランチの扱い（1.4.1-improve-kaz への merge、リリース）をユーザーが決める。
  - [x] 2026-10-19 本番にテスト版（bf9efe73）を入れた。1 回目 16:09（アプリまで 21.3 分）、16:43 に probation-ratio 0.25 で mount し直し、2 回目 16:45（4.7 分、GET 75 件、途中経過）。
  - [x] 2026-10-19 判定（3 回目の起動、20:25）: 補充 1.03 GiB（13:10 の lru 3.45 GiB から 70% 減、基準 0.35 GiB は未達）。アプリまで 11.4 分（lru 30.5 分）。常連用の 37% が compaction の出力。
  - [x] 2026-10-19 改良を実装（設計書 §8、計画 2026-10-19-kaz-cache-2q-placement.md）: `--kaz-cache-compaction-placement inherit|probation|replace`、ghost_hit の区分別、bytes_by_origin、管理ファイル v2。本体 ba511485..57b3de8a。テスト版 ~/.local/bin/kaz-test/juicefs-kaz2q-57b3de8a。
  - [ ] 判定（検証は 1 回だけ）: テスト版を `--kaz-cache-compaction-placement probation` で mount → 半日（流入 30 GiB）→ VM 再起動。補充 0.35 GiB 以下か。ghost_hit の内訳（降格を経た／上がらなかった／compaction）で原因を切り分ける。デプロイ直後の起動の後に counter の基準値を取る。
  - [x] 2026-10-20 判定（07:18、placement=probation）: アプリまで 7.9 分、補充 0.68 GiB（基準 0.35 は未達）。主な損失は夜間に compaction で作り直された起動のブロック。
  - [x] 2026-10-20 夜間の compaction の解析（[報告](docs/inspection/cache_churn/2026-10-20/compaction-overnight-ja.md)）: 書いた量の約 10 倍を compaction が書き直す。commit 時の x99 の起動条件と、block compaction の 4 MiB 単位の書き直しが原因。
  - [ ] **差分 slice 方式の compaction（別の会話セッション・別のブランチで行う、ユーザー方針 2026-10-20）:** [引き継ぎ](docs/inspection/delta_slice/2026-10-20/handoff-ja.md)。効果の見積もりは chunk 1920 で PUT 約 9 割減。参照数（B1）・record の上限・起動の条件の変更が要点。
    - [x] 2026-10-20 brainstorming: 設計書 [2026-10-20-kaz-delta-compaction-design.md](docs/superpowers/specs/2026-10-20-kaz-delta-compaction-design.md)（builder check 済み、2026-10-20 ユーザー承認）。段分け（既定 8 段）・ブロック単位の畳み込み・record 上限 500・gap 16 KiB・min-file-size（既定 0）のノブ、ブロックごとに安い方を自動選択、TKV は block に戻す。模擬 `delta_sim2.py`／`sim2-*.txt`（chunk 1920 で 5,112 → 約 200 MiB）。
    - [x] 2026-10-20 設計の承認（ユーザー）。計画 [2026-10-20-kaz-delta-compaction.md](docs/superpowers/plans/2026-10-20-kaz-delta-compaction.md)（7 Task、レビュー待ち）。
    - [x] 2026-10-21 実装（subagent-driven、ユーザーが Task ごとの commit を許可）。本体 `feat/kaz-delta-compaction`（worktree `/home/kwatanabe/tmp_local/juicefs-delta-wt`、c5c5658b..67c5c96d、13 commit）。push・merge なし。最終レビューは「直してから merge 可」→ 直して再レビュー済み。ビルド 1.4.1+2026-10-21.67c5c96d（sha256 1cb99637…）。
    - [ ] 2026-10-21 00:32 から本番で delta を計測中（[計測](docs/inspection/delta_slice/2026-10-21/README-ja.md)、metrics_sampler PID 1154836・llen_sampler PID 1155339、比較は compare_runs.py で staging 1 GiB あたり）。ユーザーが実機で試験する（テスト版 `~/.local/bin/kaz-test/juicefs-delta-67c5c96d`、sha256 1cb99637…）。案は `--kaz-compaction-mode delta --kaz-compaction-delta-min-file-size 1G`。夜間を含めて PUT・読み込み・Redis の負荷を見る。push・merge はその後にユーザーが判断する。
    - [x] 2026-10-21 ユーザー依頼: 起動の数と半分の規則をノブにし、書き直しの理由ごとの metrics を足した（`--kaz-compaction-delta-trigger` 既定 99・10〜400、`--kaz-compaction-delta-block-ratio` 既定 0.5、amount `delta_blocks_{whole,fallback,nobase,ratio}`・`delta_fold_blocks`・`delta_merge_bytes`）。本体 7336466c で commit（ユーザー承認）、レビュー済み。既定値では計画は 67c5c96d と同じ。テスト版 `~/.local/bin/kaz-test/juicefs-delta-7336466c`（1.4.1+2026-10-21.7336466c、sha256 2b243f78…）。
    - [x] 2026-10-21 ユーザー方針: 一晩の試験は長すぎる。10〜15 分の試験にするため、本番の書き込みを記録して流し直す試験の suite と、本番にも使える計測の suite を `tests/juicefs/` に作る。設計書 [2026-10-21-kaz-measure-replay-suite-design.md](docs/superpowers/specs/2026-10-21-kaz-measure-replay-suite-design.md)（ユーザー承認）。試験は、一時 Redis（RTT 10 ms の中継）と、本物の Drive（rclone serve s3 の試験用バケット）で行う。記録は VM の起動からアプリまで。種は作り物の中身。
    - [x] 2026-10-21 計画 [2026-10-21-kaz-measure-replay-suite.md](docs/superpowers/plans/2026-10-21-kaz-measure-replay-suite.md)（8 Task、レビュー待ち）。
    - [x] 2026-10-21 実装（SDD、ユーザーが commit を許可）。`tests/juicefs/`（main、dfcfabf..041a428 の 19 commit。試験の Redis はユーザーの依頼で docker compose（redis:8.0.5）に変更）。試験 116 件 OK。最終レビュー → 修正 → 再レビュー済み。使い方は `tests/juicefs/README-ja.md`。
    - [x] 2026-10-21 最初の本物の実行（記録 12:56〜13:14、種 316 chunk、6 設定）。結果は [比較](docs/inspection/delta_slice/2026-10-21-suite/README-ja.md)。trigger 30 で PUT 回数が 99 より約 16% 少ない。block モードは compaction の出力が約 4.5 倍。各 1 回なので、ばらつきは未確認。
    - [ ] 同じ設定を `--repeat 2` で流してばらつきを見る。候補: trigger 30 と 50、99。
    - [ ] （元の項目）最初の本物の実行: 本番の VM を idle にして、`jfsmeasure start prod` と `jfsreplay record`（VM の起動からアプリまで）→ `verify` → `seed`（試験の volume は 2026-10-21 に誤って作られた UUID 0181a906 を再利用。消す場合は `testvol destroy`）→ `suite`。確かめること: 試験の `.config` と本番の config.json の全項目の差、juicefs.log の rate limit の行、見張りが止めた理由。
    - [ ] 後回しの小さい課題は SDD の台帳 `.superpowers/sdd/2026-10-21-kaz-measure-replay-suite/progress.md` の minor (deferred)。
    - [ ] 2026-10-21 10:59 から trigger 50 を本番で計測中（[計測](docs/inspection/delta_slice/2026-10-21-t50/README-ja.md)、常駐 PID 1332183・1332185・1332187、前と同じ材料）。
    - [ ] 次の夜の試験の値（例: trigger 50 と 200、block-ratio 0.75）。前回と同じ材料（metrics・llen・key_tracker・再起動直前の pre_reboot_capture.sh）で比べる。
    - [ ] 後回しにした小さい課題: delta モードで書き込みのたびに大域の mutex を取る（使えるかを mount 時に覚える）、block の client・小さいファイルのパターンのテスト、discard の多いブロックが delta_block_fallback になる件、TKV の clone の参照不足（B2）の修正。
  - [x] 2026-10-20 壁打ち: 差分 slice 方式（変更データだけを 1 つの slice に詰め、record で読み分ける）。実現性の調査済み（[報告](docs/inspection/delta_slice/2026-10-20/feasibility-ja.md)、メタデータ上は可能、参照数を N に合わせる・record 数を抑える・TKV clone の既存不具合に注意）。ユーザーは upload を遅らせる案を好まない。
  - [ ] 対策を選ぶ: B4（slice-flush のオプション、今のバイナリで可能）／C7（block mode の commit の閾値を設定可能に）／C8（record の少ないブロックは書き直さない）／ゲスト側（dirty_expire、lazytime）。
  - [x] 2026-10-20 A-c（share、share-ratio）と B（demote-unread-first）を実装（設計書 §9、計画 2026-10-20-kaz-cache-2q-share-unread.md、本体 08e6580d..022870ba）。テスト版 ~/.local/bin/kaz-test/juicefs-kaz2q-022870ba。
  - [x] 2026-10-20 判定（19:46、share＋B）: アプリ 8.7 分、補充 0.90 GiB（未達）。A-c は効いたが B は実績なしを使い切った後に起動のブロックを降格。
  - [x] 2026-10-20 §10 hot-age を実装（07aaf254）。テスト版 ~/.local/bin/kaz-test/juicefs-kaz2q-07aaf254。
  - [x] 2026-10-20 軽い検証（約 30 分）: 常連用の compaction の出力の増加が止まった（8.38 → 7.70 GB で横ばい）。判定の再起動は未実施。本番は 07aaf254 で動作中。
  - [x] 計測の常駐を止めた（2026-10-20 20:58、データは gzip で保存）。
  - [x] 2026-10-20 feat/kaz-cache-2q を 1.4.1-improve-kaz に merge（c5c5658b）・push、juicefs-v1.4.1-kaz.7 の draft を作成（公開はユーザー）。
  - [ ] 旧項目（参考）: 判定: テスト版を share 0.5＋demote-unread-first で mount（前に管理ファイルを複製）→ 入れ替え直後の起動（判定外）→ 流入 30 GiB → VM 再起動。補充 0.35 GiB 以下か。ghost_hit の内訳と demote_unread／demote_read を見る。 入れてから 3 回目の起動で GET の補充が 0.35 GiB 以下か。各起動の前後で metrics（kaz_cache_2q_*）と cache_churn の道具で記録。2 回目と 3 回目の間に demote と protected の bytes を確認し、多ければ probation-ratio を下げる。
  - [x] 2026-10-19 設計書をユーザーが承認: [設計書](docs/superpowers/specs/2026-10-19-kaz-cache-2q-design.md)。承認後に writing-plans。運用の案内: compaction は `juicefs compact <mount のパス>` を使い、`gc --compact` は使わない。
  - [x] 対策の設計（brainstorming から）: 2Q などの受け入れ方針、compaction の出力をキャッシュに入れる条件（入れたことが後のヒットにつながるかを先に計測する案もある）。記録の常駐（PID 244409、244411）は、必要がなくなったら止める。
- [ ] **PUT を減らす: compaction の書き直しと書き込みの偏り（2026-10-13 計測、2026-10-14 記録）:** [報告](docs/inspection/vm_write_hotspot/2026-10-13/report-ja.md)。16 分で compaction が 4.12 GiB を書き直した（ゲストの書き込みの約 28 倍）。最も書かれた chunk は 16 回 compaction され、その書き込みの 76% は journald。
  - **ユーザー方針（2026-10-14）:** ゲスト側の対策（journald など）は別途取るが、基本は**ゲストの設定を変えずに減らせる JuiceFS 側の対策**を入れたい。
  - [x] 2026-10-14 机上比較を実施（報告 §9、`compaction_sim.py`）。わかったこと: Meta.Read のミスでも slice 5 個以上で compaction を要求する（base.go:2139）、実際の 85 回中 54 回は 99 個未満で開始、priority は inode ごとに 1 本で実行。read による起動の下限を 100 にすると PUT 約 −50%（read 時の slice 数はほぼ同じ）。書き込みの多い chunk には cooldown＋強制の個数（700〜1,000）が要り、組み合わせで PUT 約 −90%（read 時の slice 数 p99 206、最大約 1,100）。モデルは今の方式を 105 回と再現（実際は 85 回）。
  - [x] 2026-10-14 設計（brainstorming、architectural）: [設計書](docs/superpowers/specs/2026-10-14-kaz-compaction-policy-design.md)。案 1（scheduler の手前に判断の層）、`--kaz-compaction-read-min`／`--kaz-compaction-cooldown`／`--kaz-compaction-force-slices`、本番で最初に試す値は 100／1m／700。**方針: upstream の変更に強いコード**（本体は `afterWrite` の 1 行と呼び出し名 2 か所と配線だけ）。
  - [x] 2026-10-14 設計書のレビュー（ユーザー承認）、[実装計画](docs/superpowers/plans/2026-10-14-kaz-compaction-policy.md)を作成（Task 1〜6、未 commit）。
  - [x] 2026-10-14 実装（Native、ユーザーが Task ごとの commit を許可）。本体 `feat/kaz-compaction-policy`（1.4.1-improve-kaz 8a64eb07 から）: 9e54ec94 設定、f86d20fe policy 本体、0381133a scheduler の観測、b7f33dc1 本体へのつなぎ込み、7e32cde7 mount のオプション。push・merge なし。テスト: 新しいテストは `-race -count=3` で DATA RACE 0、Redis の batch のテストは一時 Redis 8.0.5（127.0.0.1:6379、終了後に停止）で PASS、`pkg/meta` の core は TiKV／MySQL が要る `TestLoadDump`・`TestMySQLClient`・`TestTiKVClient` を除いて ok（3 件は接続拒否による環境の問題、変更はエンジンのファイルに触れない）。ビルド `juicefs/juicefs` 1.4.1+2026-10-14.7e32cde7（sha256 ec614b4e…）。upstream 由来のファイルの差分は base.go +7/-2、config.go +9、flags.go +15、mount.go +1。最終 review（opus）は「マージ可」、Critical・Important なし、Minor 7 件は保留（legacy と cooldown の組み合わせ、spec とログの差、時刻の記録のあふれの可視化、cooldown の NaN、CLI の 0、削除テストの確認不足、既定でも scheduler の metrics が出る）。
  - [x] 2026-10-14 ユーザーの依頼で価値の高い Minor を直した（TDD）: 11faab81（無効時も INFO を 1 行、cooldown と legacy の組み合わせで WARN、保留のあふれで DEBUG、削除テストで scheduler まで届いたことを確認）、850498d5（CLI で 0・NaN・Inf を拒否、read-min の help に範囲）。残した Minor: 時刻の記録のあふれの metrics（記録されない commit ごとに数えるとノイズになる）、既定でも scheduler の metrics が出る（設計どおり）。core テスト（一時 Redis、環境の 3 件を除く）ok、Redis の batch のテスト ok。ビルド 1.4.1+2026-10-14.850498d5（sha256 b2de79e3…）。
  - [x] 2026-10-14 実機計測（policy 100/1m/700、01:51〜01:59:49、ログ `vm-io-20261014-014935.log`）: compaction の書き直し（ゲストの 1 MiB あたり）−87%、compaction の PUT −87%、キャッシュのミス 2.0%→0.9%、GET 694→240。**訂正（同日）: PUT の総数は減っていない可能性が高い。** freeze した書き込みの slice のうち、アップロード前に compaction で片付けられた割合が 52%→34% に下がり、書き込みの slice の PUT（ゲストの 1 MiB あたり、最終的な数）は 24.9→37.2（+49%）。合計は約 32→38。compaction はアップロード待ち（writeback の staging）の小さな slice をまとめて片付け、その PUT を省いていた。cooldown で書き込みの続く chunk を保留すると、その間に小さな slice がアップロードされてしまう。計測は各 1 回で使い方もそろっていない（方向は確か、量は目安）。
  - [ ] 次（2026-10-14 ユーザー合意）: (1) `--kaz-compaction-read-min 100` だけ（cooldown 0）で実機計測。VM を止めて rawstaging が空に近くなってから mount、計測後も 10〜15 分 `--debug` のまま slice の最終的な行き先を追う。(2) 並行して、シミュレータにアップロード待ちと片付けを入れ、read の下限だけの場合と「staging を見て判断する policy」（アップロード待ちが多い chunk はすぐ compaction）を PUT の総数で比べる。
  - [x] 2026-10-14 実機計測（`--kaz-compaction-read-min 100` だけ、03:42〜03:47:38、ログ `vm-io-20261014-034158.log`）: アップロード前に片付けられた割合 71%、PUT の総数（freeze した slice を最後まで追った値＋compaction の PUT、ゲストの 1 MiB あたり）は policy なし 36.5、100/1m/700 で 39.9、read の下限だけで **17.6（−52%）**。cooldown は逆効果（5 秒程度の短い値でもモデル上は差が誤差の範囲）。read の p99 も 3.5s→0.11s（主にキャッシュが温かいため）。
  - ユーザー決定（2026-10-14）: FUSE `writeback_cache` は以前の試験で大きく遅くなり動作も不明瞭なので使わない。ブロック単位の compaction（変化した 4 MiB だけ書き直す）は魅力的だが後回し。
  - [x] 2026-10-14 `--upload-delay` を 30s と 60s で実機計測: アップロードされる割合 29%→27%（30s）→18%（60s）、PUT の合計（1 MiB あたり）17.6→20.1→17.4（compaction の PUT が書き込みの量に比例しないので 1 MiB あたりでは差が見えない）。ユーザー判断: 60s は反映の遅れが大きく本番には入れない。
  - [x] 2026-10-14 ユーザー承認で cooldown を削除（本体 49908265。オプション 2 つ、保留、見回り、`afterWrite` の hook を外し、commit の経路を upstream に戻した。read の下限と scheduler の metrics は残す）。core テスト（一時 Redis、環境の 3 件を除く）ok、race ok。ビルド 1.4.1+2026-10-14.49908265（sha256 a74868b7…）、説明の明確化 a5bfcec7 のビルドは 1.4.1+2026-10-14.a5bfcec7（sha256 fd7628f4…）。記録 `docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md`。
  - [x] 2026-10-14 本番を `--kaz-compaction-read-min 100` だけで mount（ビルド a5bfcec7、ユーザーは後の解析のため `--debug` を残す。ログ `vm-io-20261014-054238.log`）。ユーザーの指示で `feat/kaz-compaction-policy` を `1.4.1-improve-kaz` に `--no-ff` で merge（6d9a3095、tree は a5bfcec7 と同じ、merge 後のテスト ok）。push なし。
  - [x] （2026-10-14 86321d84 で push 済み。リリースの判断は下の block の項へ）動きを見てからリリースの判断（push、`juicefs-v1.4.1-kaz.6` のノート。新しいオプションは `--kaz-compaction-read-min` の 1 つ）。
  - [ ] 後回しの候補: ブロック単位の compaction（変化した 4 MiB だけを書く。upload-delay 60s の回では PUT の約 4 割が compaction の出力）、staging の待ちで起動する compaction（モデルで −23〜26%）。
  - [ ] 2026-10-14 ブロック単位の compaction の設計（brainstorming、architectural）: [設計書](docs/superpowers/specs/2026-10-14-kaz-block-compaction-design.md)（未 commit、ユーザーのレビュー待ち）。見積もり（`docs/inspection/vm_write_hotspot/2026-10-14/block_estimate.py`、2 回目以降の compaction）: compaction の PUT は C で −47%、A〜E で −47〜−68%（範囲の方式は −14〜−43%）、PUT の総数は C で約 −12〜17% [推論]。ユーザー決定: 3 エンジンすべて、今の方式は残して `--kaz-compaction-mode upstream|block`（既定 upstream）で選ぶ、全体の書き直しもブロックごとの slice、全体に戻す条件は隠れたデータ 50% 超か残る件数（ブロック数＋1）超、案 1（kaz の振り分けの関数＋エンジンごとの kaz のファイル、`base.go` は変えない）。次は設計書の承認の後に writing-plans。
  - [x] 2026-10-14 設計書をユーザーが承認。[実装計画](docs/superpowers/plans/2026-10-14-kaz-block-compaction.md)（Task 0〜7、未 commit、レビューと実行方法の選択待ち）。計画で足した細部（設計書にも反映）: `doKazReplaceChunk` に `delayedKey`、後の件に完全に隠れた件はブロックに触れずに消す、ブロックの並行の書き込みは最大 16 本。
  - [x] 2026-10-14 実装（subagent-driven、ユーザーが Task ごとの commit を許可）。本体 `feat/kaz-block-compaction`（1.4.1-improve-kaz 6d9a3095 から）: 1b73e474 オプション、ff4cbb94 計画の関数、b4b72491・aae8a57a エンジンの置き換え（Redis／SQL／TKV）、3a9227ae・2f838467 本体、08886406 振り分け、a4c367ec vfs のデータのテスト、1d36ae04 最終レビューの修正（ログを upstream の形に、件数超過時の全体の書き直し、metrics の分割）。push・merge なし。テスト: kaz と GC のテストは一時 Redis 8.0.5（127.0.0.1:6379、終了後に停止）を含め -race で PASS。core は環境の無いもの（KeyDB／RedisCluster／Etcd／LoadDumpSlow／PostgreSQL／MySQL／TiKV／LoadDump）を除いて ok。cmd の失敗は環境（MinIO、root、FUSE）のみ。upstream 由来のファイルの差分は config.go +11、flags.go +15 だけ。ビルド 1.4.1+2026-10-14.1d36ae04（sha256 50cbd785…、後の 7bb71894 で置き換え）。最終レビューは「修正後マージ可」、修正の確認も済み。
  - [x] 2026-10-14 ユーザーの依頼で、確認のレビューで見つかった件を直した（ユーザーが案 A を選択、TDD）: `--kaz-compaction-block-max-slices` をブロック数より小さく設定すると、ブロックの形の chunk を書き込みが無くても毎回全体で書き直していた。件数が上限を超えても、全体の書き直しで件数が減る（件数 > データの範囲のブロック数）ときだけ書き直すようにし、ブロック数＋1 より小さい値には mount 時に WARN を出す（本体 7bb71894）。kaz と GC のテストは一時 Redis を含め -race で ok。ビルド 1.4.1+2026-10-14.7bb71894（sha256 11580474…）。
  - 残した点:`TestKazBlockCompactionLogs` は inode で絞らずに行を数える。vfs のデータのテストの writeback 有効は、upstream の `cacheStore.stageFull` の data race（pkg/chunk/disk_cache.go:361 と :855、v1.4.1 由来）で -race では落ちる。
  - [x] 2026-10-14 実機計測（回 F、09:30〜10:02、ビルド 7bb71894、read-min 100＋block、[報告 §6](docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)）: compaction 1 回あたりの PUT 14.1→5.0（−64%）、1 回あたりの書き直し約 59→20 MiB、WARN／ERROR なし。ただしゲストの書き込みが C の約 15 倍（1,937 MiB）で使い方がそろわず、PUT の合計（1 MiB あたり 4.7、1 分あたり約 287）と片付けの割合（71%→52%）は比べられない。片付けの割合が下がった理由は未確認。
  - [x] 2026-10-14 再計測（回 G、10:21〜10:26、ゲストの書き込み 119 MiB、[報告 §7](docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)）: compaction の PUT は 1 回あたり −67%、1 MiB あたり −59%（4.6→1.9）。片付けの割合は 74%（C は 71%）で損なわれていない。PUT の合計（1 MiB あたり）は 17.6→19.0（+8%）だが、書き込みの slice が細かかった（1 MiB あたり 45→65 個）ため。freeze した slice 1 個あたりでは −25%。同期 compaction 0、chunk 1 は 1,000 件で compaction を始めた回があった。
  - [x] 2026-10-14 ユーザーの依頼で、block のとき `juicefs compact`／`gc --compact` も block にした（案 a: 隠れたデータ 0・ブロックごとの形になるまで、既にその形なら何もしない）。本体 5bb23494（`base.go` の `Compact`／`CompactAll` の呼び出し 2 か所を `forcedCompactChunk` に）、99023190（help の文言）。TDD、kaz と GC のテスト（一時 Redis を含む）-race ok、core ok、vfs・cmd の kaz ok。ビルド 1.4.1+2026-10-14.99023190（sha256 a2fe92b5…）。upstream 由来のファイルの差分は base.go +2/−2、config.go +11、flags.go +15。
  - [x] 2026-10-14 read の下限 30 の計測（回 H、11:25〜11:29、[報告 §8](docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)）: PUT の合計は 1 MiB あたり 19.0→24.7（+30%）、片付け 74%→71%、区間の後も compaction が続き compaction の PUT が増えた。read の下限は 100 のままがよい（各 1 回の計測）。
  - 2026-10-14 ユーザー決定: 本番は当面 `--kaz-compaction-read-min 50 --upload-delay 1s` と block（ビルド 1.4.1+2026-10-14.99023190、12:14 に mount、ログ vm-io-20261014-121431.log）。この組み合わせは計測していない（read-min 50 は 100 と 30 の間、upload-delay 1s は以前の 30s／60s より短い）。
  - [x] 2026-10-14 read-min 50＋upload-delay 1s＋block の計測（回 I、12:30〜12:33:45、[報告 §9](docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)）: 片付け 72%、PUT の合計 1 MiB あたり 23.2（G 19.0、H 24.7）、slice 1 個あたり 0.33（G 0.29）、read の p99 319 ms（G 137）。区間の後の I/O が多かった（アプリを落とすのが遅れた）。
  - [x] 2026-10-14 read-min 100＋block の再計測（回 J、12:50〜12:53、[報告 §10](docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)）: PUT の合計 1 MiB あたり 21.1、slice 1 個あたり 0.34、片付け 70%、read p99 106 ms。G と J のばらつき（10〜15%）が I との差と同程度で、PUT では設定の差を言い切れない。read の p99 は I（upload-delay 1s、read-min 50）だけ 319 ms。訂正: これまでの回で区間の後に落としていたのは VM ではなくアプリ。
  - [x] 2026-10-14 upstream に戻した同日の計測（回 K、13:27:36〜13:37、[報告 §11](docs/inspection/vm_write_hotspot/2026-10-14/report-ja.md)）: PUT の合計 1 MiB あたり 26.2（block の G・J は 19.0／21.1）、slice 1 個あたり 0.47（0.29／0.34）、compaction の PUT 1 MiB あたり 6.9（1.9／2.2）、片付け 66%（70〜74%）、ゲストの read の p99 2,984 ms・最長 9.6 秒（block は 106〜137 ms）、アプリの起動 約 7 分（block は 2〜4 分）。最初の試みは VM の GUI が固まりやり直した。
  - [x] 2026-10-14 ユーザーの指示で `feat/kaz-block-compaction` を `1.4.1-improve-kaz` に `--no-ff` で merge（86321d84、tree は 99023190 と同じ）し、`kaz` に push（8a64eb07..86321d84。read-min の merge 6d9a3095 も含む）。本番の mount はユーザーが block（read-min 100）に戻す。
  - [x] 2026-10-14 リリースの準備（ユーザー依頼）: `release/juicefs/versions.json` に `juicefs-v1.4.1-kaz.6`（本体 86321d84e5162619ac675f23806b56447837a963、Go 1.25.11）、ノート `release/juicefs/notes/juicefs-v1.4.1-kaz.6.md`。commit・push（5c04d5e）とタグ `juicefs-v1.4.1-kaz.6` の push 済み。workflow release-juicefs（run 38513843030）成功、draft「JuiceFS v1.4.1-kaz.6」（linux-amd64／arm64、windows-amd64、checksums、install スクリプト）、表示は `juicefs version 1.4.1+2026-10-14.86321d84-kaz.6`。2026-10-14 ユーザーが公開した。
  - [x] （旧）次: ユーザーの実機計測（`--kaz-compaction-read-min 100 --kaz-compaction-mode block --debug`、2026-10-14 と同じ手順、設計書 §8）。解析スクリプトは `compaction block-object` の行を新しい slice の id として数えるよう直す必要がある（`comp_puts.py` は `phase=object` の slice= を 1 つだけ拾う）。その後 merge とリリースの判断。
  - [ ] 旧: ユーザーが実機で計測（`--kaz-compaction-read-min 100 --kaz-compaction-cooldown 1m --kaz-compaction-force-slices 700` と `--debug`、2026-10-13 と同じ手順）→ 比較 → `1.4.1-improve-kaz` への merge とリリースの判断。Minor の扱いもあわせて判断する。
  - 元の計画: 今回のログと accesslog で、compaction の方式を机上で比べる。比べる方式は、今の方式（16 回を再現できるかで確認）、cool-down（最後の書き込みから 1／3／5／10 分）、しきい値 200／300、chunk ごとの書き込み頻度による調整。出す数は、compaction の回数、PUT と DELETE の見積もり、Read 時の slice の数。必要なら、今のゲストの設定のまま 1 時間ほど `--debug` で記録する。
  - [ ] そのあと: 設計（既定 off、350／2,500 の上限と整合性に関わる部分は変えない）→ 実装 → 同じ手順で実機の前後比較。
  - [ ] ほかの候補（未設計）: 範囲を限った compaction、FUSE `writeback_cache`（flush・fallocate との整合を要確認）、pending read overlay（Read 起因の freeze を減らす）。
  - [ ] 後回し: journald を `Storage=volatile` にした計測（ゲスト側の効果の確認用）、raw イメージの計測（同じ `discard=unmap`／`unsafe`／`--debug` で比べる）。raw のほうが JuiceFS に向くと見ている（ZERO_RANGE が無く、配置が崩れない）が、未比較。
  - 判断済み: ブロックサイズ 8 MiB は約 −12%（compaction の分だけ）で、既存のボリュームでは変えられないので採らない方向。
- [ ] **read cache に参照されない slice が残り得る race（2026-10-13、ソース上の条件、未再現）:** 並行した GET が `Retire` の後に `bcache.cache()` すると再登録され、DELETE 後は追い出されるまで残る（tombstone なし）。容量の無駄だけで、データの正しさには影響しない。`--kaz-finish-canceled-get` は窓を長くする。2026-10-13 の本番の実測は 0 個。塞ぐ案: `Retire` で世代番号か一時的な tombstone を付け、`cache()`／`add()` で拒む。優先度は低い。

- [x] **GitHub Actions の Node.js 20 の警告（2026-10-13）:** `.github/workflows/` の3ファイルで checkout v4→v7（10か所）、setup-go v5→v7（3）、upload-artifact v4→v7（3）、download-artifact v4→v8（5）に上げた（ユーザー承認、actionlint ok）。変更点の確認: download-artifact v5 の出力先の変更は ID 指定だけ（この repo は名前・pattern）、v8 はハッシュ不一致がエラー・zip でないものは展開しない（この repo は全部 zip）、setup-go v6 は toolchain の扱いの変更（go-version は明示）。確認は main への push（install-tests）と release の手動実行（draft は作られない）。

- [ ] **rclone を公式 v1.75.2 に載せ直す（2026-10-13 追加、調査済み・方針の決定待ち）:** [調査報告](docs/inspection/rclone_v1752_port/2026-10-13/port-investigation-ja.md)。
  - 必須: kaz の lookup モードが upstream のセキュリティ修正 2f43b99ab（unsafe な名前の拒否）を迂回する（memory backend で root の外への書き込みを再現）。修正案あり。
  - serve s3 の状態がユーザーごとの tenant に移ったので、persist-metadata のメタデータの扱いと multipart の Create の位置を書き直す。
  - 一覧は upstream が同じ方式を常に有効にした。kaz_list は捨て、取り消しとログだけ足し、`--kaz-s3-list-by-key-order` は受け付けるだけにする案。
  - 2026-10-13 決定: タグは `rclone-v1.75.2-kaz.1`。一覧は upstream の方式に任せる。載せ直しは案 B（機能ごとに cherry-pick で当て直す）。[実装計画](docs/superpowers/plans/2026-10-13-rclone-port-to-v1.75.2.md)。2026-10-13 Task 0〜8 を実装済み（`port-1.75.2/kaz-s3-list` c4ed92bad まで、最終レビュー済み）。2026-10-13 Task 9 merge（9816e18ae）と Task 10 実 Drive の確認（[記録](docs/inspection/rclone_v1752_port/2026-10-13/drive-verification-ja.md)）済み、versions.json とノートを用意。2026-10-13 push とタグ `rclone-v1.75.2-kaz.1` を実施。残り: draft の公開（ユーザー）、本番適用（ユーザー）、cancel-get の取り消しと JuiceFS を動かしての確認（本番で観察）。

- [ ] **client-cache（CSC）の不具合調査（2026-10-08 追加、未着手）:** メタデータの変更（例: ファイルのパーミッション）が、client-cache（meta URL の `client-cache=true`）を有効にした他の client に反映されない。
  - ユーザーが確認した事実: 裏側のメタデータ DB（Redis）では、正しく変更されている。
  - 関連: 計画書 §5.4（自分の commit が local cache を消さない、BCAST の invalidation が遅れる窓）、`pkg/meta/redis_csc.go`。実機は client-cache が有効（client-cache-expire=24h）。
  - 進め方: 一時 Redis で2つの client（client-cache 有効）を使い、chmod 等が他方に反映されない状態の再現テストを先に作る。invalidation（tracking・BCAST・pubsub）の経路を確認する。本番の Redis（56379）には接続しない。

- [ ] **キャッシュが空の起動で、ゲストの systemd が時間切れになる（2026-10-09 夜、実機）:** Drive の遅い時間帯に、dbus などが起動待ちの 90s を超えて失敗し、VM が Plymouth で止まった（詳細は agent_memo 2026-10-09）。2026-10-09 ユーザーがゲストに `DefaultTimeoutStartSec=600s` を入れ、GRUB の `quiet splash` を削除した（次のキャッシュが空の起動で効果を見る）。残りの候補: 起動前にキャッシュを温める方法の検討、起動中の compaction が qcow2 の chunk 0 を数十秒おきに作り直す件（inode 623800、chunk 0 が30回、chunk 255 が22回、mount 後に計 約 4.8 GB）の調査。

- [ ] **rclone serve s3 の修正の検討（2026-10-08 追加。次の会話で継続。引き継ぎは agent_memo の「引き継ぎ（2026-10-08、次の会話へ）」）:**
  - 症状（ユーザーの観察）:
    - JuiceFS は object のパスを直接指定するので、ディレクトリのリストは要らない。それなのに、パスを直接取得しようとすると、rclone は毎回ディレクトリのリストを取りに行く。
    - dir cache を使うと、今度はファイルの有無までキャッシュしてしまい、存在するはずのファイルを「無い」と返す。
    - 背景: 2026-10-06 の調査（`docs/inspection/rclone_put_timeout/2026-10-06/report-ja.md`）。現在は `--poll-interval 0 --dir-cache-time 1h` で運用中。
  - 方針（やる場合）: JuiceFS と同じ形にする。
    - rclone のソースを、このリポジトリの中の Git ignore したディレクトリ（例: `/rclone`）に clone して修正する。
    - このリポジトリで release を作る。release のディレクトリ構成（`release/`、`versions.json`、workflow、install スクリプト）を、JuiceFS と rclone の2つの成果物に対応する形へ変える必要があるかもしれない。
  - 先にやること:
    - rclone VFS の、パス指定での取得（HEAD・GET）がディレクトリのリストを必要とする経路を、ソースで確認する。
    - 「存在するはずのファイルを無いと返す」negative cache の条件（dir cache の有効期間、自分の PUT がキャッシュに反映されるか）を、ソースで確認する。
    - 修正案と upstream への報告の要否を比較する。実装はユーザーの承認後。
  - 補足（ユーザー、2026-10-08）: 複数台から mount するので深刻。dir cache が有効だと、他の client が新しく作ったファイルは metadata では見えるのに、rclone が「無い」をキャッシュしていて input/output error になる。
  - 構成（ユーザー回答、2026-10-08）: (A) 各ホストがそれぞれ rclone serve s3 を動かし、同じ Drive フォルダ（`/rclone-s3`）を見ている。
  - 2026-10-08 ソース調査: [報告](docs/inspection/rclone_dir_cache/2026-10-08/source-investigation-ja.md)。方式 (b') VFS の lookup モードを軸にすることをユーザーが承認。
  - Phase 分け（ユーザー承認、2026-10-08）:
    - [ ] Phase 1（コード実装済み、実機検証と本番適用が残り）: (b') lookup モード（`--vfs-lookup-by-path`）、HEAD/GET のエラーを 404 にしない、`--no-cleanup` の配線、DELETE 時に `b.meta` を消す（メモリが増え続ける問題）。[仕様](docs/superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md) はユーザー承認済み（2026-10-08）。[実装計画](docs/superpowers/plans/2026-10-08-rclone-lookup-by-path.md) を作成（ユーザーのレビュー待ち）。
      - 2026-10-08 実装: rclone `feat/kaz-vfs-lookup`（1.75.1-improve-kaz から分岐、c27d12d03..9b75066b4 の11 commit、push なし、未 merge）。タスクごとのレビューと最終レビューを通過。race つきテスト ok（`cmd/serve/s3` は docker が要る TestS3Minio を除く。変更前から失敗）。
      - 追加の変更（レビューでの判断）: ListBucket・PutObject・DeleteBucket の bucket 確認も「無い」ときだけ 404 にした。lookup 中にディレクトリ名が変わった場合は1回やり直す。
      - JuiceFS は Get/Put で 404・500・503 を区別せず再試行する → 500 で十分（[記録](docs/inspection/rclone_dir_cache/2026-10-08/juicefs-error-handling-ja.md)）。
      - 残る注意: `b.meta` は他ホストが消した key の分が残る（Phase 2 まで定期再起動で抑える）。key 指定の新規 PUT ごとに Drive API が1回増える（実機で測る）。
      - [x] 実際の Drive での検証（2026-10-08、[記録](docs/inspection/rclone_dir_cache/2026-10-08/drive-verification-ja.md)）: 他ホストの新規 object がすぐ読める、DELETE で実際に消える、一覧の取り直し0回を確認。テスト用フォルダは削除済み。
      - [x] 新しい key の PUT で Drive API が2回増える問題（名前の検索が1→3回）: ユーザー承認の追加修正（lookup モードかつ cache-mode off の `O_CREATE|O_TRUNC` では VFS が存在を問い合わせない、0e050fab1・9b75066b4）で、lookup モードなしと同じ1回に戻した。実機で再計測済み。
      - [x] 本番適用の手順書（2026-10-08、[手順書](docs/inspection/rclone_dir_cache/2026-10-08/deploy-runbook-ja.md)）
      - [ ] 本番への適用（ユーザー）。全ホストで `--no-cleanup` を指定すること。適用後は RSS（`b.meta`）と rate limit を観測する。
      - [x] 2026-10-08 ユーザーの指示で `feat/kaz-vfs-lookup` を `1.75.1-improve-kaz` へ `--no-ff` で merge（dd03d0243、merge 後もテスト ok）。ユーザーが作成した `tongsama/rclone` を `kaz` リモートとして追加し、`1.75.1-improve-kaz` を push（既定ブランチ）。`feat/kaz-vfs-lookup` はローカルに残してある。
    - [ ] Phase 2: S3 のユーザーメタデータ（JuiceFS の `x-amz-meta-crc32c`）を Drive の properties に保存し、どのホストからも・再起動後も返す。現状はプロセスのメモリ（`b.meta`）にだけあり、他ホストの PUT や再起動後は JuiceFS のチェックサム検証が黙って省略される。
      - 2026-10-08 着手（brainstorming、architectural）。ユーザー判断: 保存するのは `x-amz-meta-*` すべて（A）。既存の object への後付けは範囲外（前提、未確認）。
      - 方式 A＋C2（PUT 時に properties を同梱、Drive に properties だけを取るオプション）をユーザーが承認。オプション名は `--kaz-s3-persist-metadata`（serve s3）と `--drive-kaz-properties`（Drive backend。backend のオプションは `--<backend>-kaz-<内容>` とする規則の補足をユーザーが承認）。[調査](docs/inspection/rclone_dir_cache/2026-10-08/phase2-metadata-investigation-ja.md)、[仕様](docs/superpowers/specs/2026-10-08-rclone-s3-persist-metadata-design.md)（ユーザー承認済み）。既存データ（メタデータ無し）に対する取得・上書き・削除・再取得のテストをユーザーが要望し、仕様に入れた。[実装計画](docs/superpowers/plans/2026-10-08-rclone-s3-persist-metadata.md)（Task 0〜7）をユーザーが承認、subagent-driven で実装中（rclone `feat/kaz-s3-persist-metadata`）。
      - 2026-10-08 実装完了（`feat/kaz-s3-persist-metadata` 875d6df78..e1509bad8 の7 commit、push・merge なし）。タスクごとのレビューと最終レビューを通過、race つきテスト ok。最終レビューを受けて、Drive では `--drive-kaz-properties` が必須と明記（仕様 §2 を訂正）。
      - [x] 実際の Drive での確認（[記録](docs/inspection/rclone_dir_cache/2026-10-08/phase2-drive-verification-ja.md)）: ホストをまたぐ往復、上書きでの置き換え（一括・分割の両経路）、上限超えで PUT 失敗・object 作られず、multipart、既存データとの互換（取得・上書き・削除・再取得）、API の回数が Phase 1 と同じ。テスト用フォルダは削除済み。
      - [x] 手順書に Phase 2 を追記（[手順書](docs/inspection/rclone_dir_cache/2026-10-08/deploy-runbook-ja.md)）。
      - [x] 2026-10-08 ユーザーの指示で `1.75.1-improve-kaz` へ `--no-ff` で merge（dab33da31、merge 後もテスト ok）し、`kaz`（tongsama/rclone）へ push。`feat/kaz-s3-persist-metadata` はローカルに残す。
      - [ ] 本番適用（ユーザー、[手順書](docs/inspection/rclone_dir_cache/2026-10-08/deploy-runbook-ja.md)）。2026-10-08 単体（1台）での試行を開始（ユーザーが `/usr/bin/rclone` を改修版で上書き、動作 OK。新しい chunk の `s3m-crc32c` も本番で確認済み）。観測: 新しい chunk の `s3m-crc32c`、RSS、`InternalError`・`verify checksum failed`。バイナリ `~/tmp_local/rclone-kaz-build/rclone-kaz`（v1.75.1-kaz-dev.dab33da3、sha256 c81a4ac0…59fdc）。2台での構成はリリースの後。
      - [ ] 任意: JuiceFS を動かして、他のホストで書いた chunk のチェックサム検証が効くことを確かめる。
    - 独自オプションの命名（ユーザー承認、2026-10-08）: 接頭辞 `--kaz-<対象>-<内容>`。ただし backend のオプションは rclone が backend 名を先頭に付けるので `--<backend>-kaz-<内容>`（例 `--drive-kaz-properties`）（例 `--kaz-vfs-lookup-by-path`）、ヘルプ先頭に `[kaz]`、可能なら flag グループ「Kaz」。既存オプションの不具合修正（`--no-cleanup` 等）は upstream の名前のまま。
    - [ ] 範囲外の既知のリスク: Drive の同名フォルダの重複（複数ホストが同時に新しい chunks フォルダへ最初の PUT をすると、それぞれ作成し得る。lib/dircache の FindLeaf→CreateDir に、ホスト間の排他が無い）。今も同じリスクがある。2026-10-08 に読み取りのみの問い合わせで確認し、`rclone-s3` 配下（1,442 フォルダ）に重複は 0（[記録](docs/inspection/rclone_dir_cache/2026-10-08/dup-folders-ja.md)）。回避策の候補（全ホストで同じ規則で正のフォルダを選び、作成直後に検索し直して寄せる、Drive backend の opt-in オプション）は Phase 1b として別の仕様にする。object の key は metadata DB の slice ID で一意だが、フォルダ（ID 1000 ごと）は 4096 個単位の払い出しの境界で2ホストが共有し得る。発生の幅は狭いので、ユーザー判断で優先度を下げ、TODO に残すだけにする（2026-10-08）。
    - [x] Phase 3（2026-10-08 完了）: release 構成を JuiceFS と rclone の2成果物に対応させる（別の spec）。
      - 2026-10-08 実装（subagent-driven、`feat/release-multi-product` を main へ ff、000c7eb）。タスクごとのレビューと最終レビューを通過、install テスト20件、actionlint ok。手動実行（JuiceFS は古い形 `v1.4.1-kaz.3`、rclone は `rclone-v1.75.1-kaz.1`）で全 job 成功。
      - 2026-10-08 `rclone-v1.75.1-kaz.1` を公開（タグは 000c7eb、run 37752233096、ユーザーが Latest を外して公開。Latest は `v1.4.1-kaz.3` のまま）。このホストで install.sh による rclone・juicefs の実インストールを確認、Windows はユーザーが install.ps1 で確認し動作も OK。
      - 2026-10-08 ユーザーが `rclone-v1.75.1-kaz.1` で2台構成を試し、v1.75.1 の公式版でうまくいかなかったところ（他ホストが作った object が見えない等）が期待どおり動作することを確認。
      - 2026-10-08 着手（brainstorming、architectural）。ユーザー判断: 既存の install URL は捨ててよい（整理を優先）。製品ごとに分ける（更新は可分）。方式 (1): タグを製品ごとに分け（`juicefs-v…-kaz.N`、`rclone-v1.75.1-kaz.N`）、install スクリプトが GitHub API で製品ごとの最新を探す（GitHub の latest はリポジトリで1つのため）。rclone は linux-amd64・linux-arm64・windows-amd64、公式と同じ full（`bin/cross-compile.go -tags cmount`、Windows の mount を含む、CGO 不要）。既存の JuiceFS の Release（`v1.4.1-kaz.1`〜`.3`）は残し、install スクリプトが古い形のタグも JuiceFS として扱う（a）。install の既定先は両製品とも `/usr/local/bin`。[仕様](docs/superpowers/specs/2026-10-08-release-multi-product-design.md)（ユーザー承認済み）。[実装計画](docs/superpowers/plans/2026-10-08-release-multi-product.md)（Task 0〜9、ユーザー承認、subagent-driven で実装中）。
  - 2026-10-08 rclone v1.75.1 を `rclone/`（Git ignore、独立リポジトリ、ブランチ `1.75.1-improve-kaz`）に clone した。fork `tongsama/rclone` は未作成。

- [ ] **`juicefs gc` が object の全件一覧で止まる（2026-10-08、ユーザー報告、調査中）:** 2026-10-08 19:57 の `juicefs gc` は dangling inode・leaked chunk の処理までは進むが、`ListObjectsV2`（`prefix=juicefs-data/chunks/`、再帰、`max-keys=1000`）の最初の応答を30秒待って `timeout awaiting response headers` → `list all blocks` で FATAL（gc.go:232、sharding.go:118）。仮説: rclone serve s3 が全件を集めてから最初のページを返し、chunks 全体（約1,400フォルダ）の Drive の一覧に30秒以上かかる。JuiceFS の ResponseHeaderTimeout は30秒固定。調査結果は `docs/inspection/juicefs_gc/2026-10-08/` に保存。本番の Redis・rclone・gc には触れない。
  - 2026-10-08 調査済み（[報告](docs/inspection/juicefs_gc/2026-10-08/gc-list-timeout-investigation-ja.md)）。原因: rclone serve s3 の ListObjectsV2 が prefix 配下を全件再帰で集めて並べ替えてから最初のページを返す（cmd/serve/s3/list.go:16-65、pager.go:11-66。公式 v1.75.1 から同じ）。本番ログで 20:07:38 の LIST を JuiceFS が30秒で切った後も、rclone は走査を続け 20:22 時点で chunks/0/ の途中（約2.7秒/ディレクトリ、全体で1時間〜数時間の見込み）。付随の不具合: start-after がキーの完全一致でしか探さない（pager.go:24-37）。JuiceFS の30秒は restful.go:157 で固定、gc に一覧を絞るオプションは無い。gc は一覧の最初の要求で Fatalf し、漏れた object の削除は未着手（整合を壊す途中状態は無い）。
  - [ ] 修正案 (a)（ユーザーが選択）: rclone の一覧をキーの順にたどり、MaxKeys 件で返す（start-after の大小比較も直す）。2026-10-09 に設計を各節ユーザー承認。[仕様](docs/superpowers/specs/2026-10-09-rclone-s3-list-by-key-order-design.md)（レビュー待ち、未 commit）。要点: `--kaz-s3-list-by-key-order`（既定 off）、DFS で「名前 + `/`」の順、マーカー以下の部分木は読まない、ctx の取り消しで止める、旧方式の pager のマーカーも「より大きい」に直す。本番では最初 `juicefs gc` を `--delete` なしで確認する。2026-10-09 仕様書をユーザーが承認、[実装計画](docs/superpowers/plans/2026-10-09-rclone-s3-list-by-key-order.md) をユーザーが承認、Native で実装済み: rclone `feat/kaz-s3-list-by-key-order`（`1.75.1-improve-kaz` dab33da31 から、f8584af74..35b4fc36d の4 commit、push なし、未 merge）。race つきテスト ok、最終レビュー（opus）は merge 可（Critical・Important なし）。2026-10-09 `feat/kaz-s3-cancel-get` と合わせて `merge/kaz-s3-cancel-get-list`（`1.75.1-improve-kaz` から、853a5284b、push なし）に merge した（s3.go の衝突は両方の項目を残した。race つきテスト ok）。同日 `1.75.1-improve-kaz` に merge（fe0572749）し、`rclone-v1.75.1-kaz.2` を用意（調査リポジトリ 356c591、タグを push。draft の公開はユーザー）。2026-10-09 夜、本番で kaz.2 + `--kaz-s3-list-by-key-order` にして `juicefs gc`（`--delete` なし）を実行。最後まで完了（20:21:27〜21:09:48）。Redis の前半が約10分（cleanupLeakedInodes が 20:31 頃まで）、object の一覧が 38m21s。scanned 499,017、valid 491,669、leaked 3,416（4.4 GiB）、skipped 3,932（6.0 GiB、作成1時間以内）、dangling inode 2（i610491・i610498、2026-10-07 01:11・01:16 作成、parent 572598）、leaked chunk 1（c580896_0）。2ページ目以降で timeout awaiting response headers が3件（chunks/1/1717、5/5239、5/5347 の続き）→ ListAll は2ページ目以降を無制限に再試行するので完走（1ページ目だけは Fatal）。後半は object の多いディレクトリで遅くなった（ユーザー観察）。残り: `--delete` の実施（ユーザーが折を見て。Drive の use_trash は既定 true なので、消した object はゴミ箱に残るはず）、必要なら rclone 側で時間を区切った短いページ（`--delete` なし、`-vv` の `kaz list:` 行で1ページ目の dirs=/took= が30s に収まるか。1ページの時間の上限は入れていない）。 案 (d)（JuiceFS gc 側）は採らない。
  - 注意: 修正まで gc を再実行しない（走査が並行して増える）。切られた走査は rclone を再起動するまで Drive の API を使い続ける。
  - 本番の rclone（18:53 起動）は kaz のオプション（`--kaz-vfs-lookup-by-path --no-cleanup --kaz-s3-persist-metadata --drive-kaz-properties`）付きで動いている（agent が表示を400文字で切って見落としたため、一時「付いていない」と誤って伝えた。ユーザーの指摘で訂正）。

- [ ] **キャッシュ cold の VM 起動が遅い（2026-10-08、ユーザー報告、調査中）:** [解析報告](docs/inspection/vm_boot_cold_read/2026-10-08/report-ja.md)。律速は GET の往復の遅延（4MiB で中央値1.9s、同時数1〜2本）。`--prefetch` は zstd では効かない。
  - [x] ゲストの I/O エラーと read-only 化は、約30s かかった read／write による SATA コマンドのタイムアウトと照合した（ホストは EIO なし）。VM の disk が virtio のつもりで `bus='sata'` だった（ユーザーが確認）。対処（virtio への変更、ゲストの fsck）はユーザーが行う。
  - [x] GET が25〜30s かかる原因（2026-10-08、報告の追記）: Drive のダウンロードの応答待ちが、ときどき15〜30s になる（tpslimit ではないと推測）。30s で切っているのは JuiceFS の `ResponseHeaderTimeout`（restful.go:157）。対策の候補は、ヘッダ待ちを短くして再試行するか hedge にすること（未実施、ユーザーの判断待ち）。
  - [ ] **キャンセルされた GET を最後まで取ってキャッシュに入れる `--kaz-finish-canceled-get`（2026-10-08 実装、本体 `feat/kaz-finish-canceled-get` 117dfafe、push・merge なし、既定 off）:** 4 の前に、まず要求を減らすためにユーザーが選んだ。ダウンロードの枠を待っている間に cancel されたら、取得をやめる。最後まで走らせる GET は `--max-downloads` の半分まで（設計では「同じ数」としていたが、枠を持ち続けるので半分に変えた）。singleflight で先に GET を出した側がキャンセルされると、同じブロックを待っていた read にもキャンセルが伝わる、という既存の問題も解消する。counter は `kaz_canceled_get_finished`／`_dropped`。2026-10-09 01:27 に on（virtio）で1回計測した: GET −20%、broken pipe 787→1。起動時間は約26分で変わらず。Drive の15s 超の応答（35→68）と、キャッシュの追い出し後の取り直し（372件）が効いていた。ゲストのI/Oエラーなし。2026-10-09 に条件をそろえて on（08:11、約16分）と off（10:56、約19分）を比べた。on では Drive へのダウンロードが −28%（1,477→1,063）、broken pipe 576→0。ただし on の回は、追い出した後の取り直しが多かった（239 対 68）。比べたのは各1回で、Drive の状態の差が大きい。既知の点: slice の削除と GET の完了が競合すると、参照されないブロックがキャッシュに残り得る（今の GET にも同じ隙間があり、長くなるだけ）。race テストの警告は既存の WithTimeout／rawFull の分のみ。
  - [ ] **応答ヘッダを待つ時間の上限 `--kaz-get-header-timeout`（2026-10-09 実装、本体 `feat/kaz-get-header-timeout` a3ab4998、push・merge なし、既定 0＝無効）:** ブロック全体を取る GET で、応答ヘッダが返るまでの待ちだけを制限する。時間を過ぎたら、その要求をやめ、今ある再試行の経路で取り直す。**`--get-timeout` との違い:** `--get-timeout` は GET 全体（応答を待つ時間と本体を受け取る時間の合計、既定 60s）の上限。`--kaz-get-header-timeout` は、データが流れ始めるまでの待ちだけの上限で、遅くても流れている本体の受け取りは切らない。共通の HTTP client の `ResponseHeaderTimeout: 30s`（restful.go:157）は、PUT・DELETE・LIST と、全部の object storage に効くので変えない（PUT は rclone が Drive へのアップロードを終えてから応答するので、短くすると成功している PUT まで再試行になる）。GET の所要時間は 0〜6s と 15〜30s の2つに分かれていて、8〜10s で切るのがよいと見込む。counter は `kaz_get_header_timeouts`。`feat/kaz-finish-canceled-get` とは `pkg/chunk/cached_store.go` で衝突する（merge のときに解消する）。**進め方（2026-10-09 ユーザー決定）:** `feat/kaz-finish-canceled-get` は、そのまま残す。まず `--kaz-get-header-timeout` を単独で実機で試す。うまくいったら、merge 用のブランチで2つを merge し、もう一度テストする。2026-10-09 12:30 の単独の計測で機能を確認した（10s で切った GET は 2 回、2 回とも約 2s で取り直しに成功、正常な GET を誤って切った例は無し）。ただし、Drive の遅い応答が少ない時間帯だったので、起動時間への効果は測れていない。そのあと、merge 用のブランチ `merge/kaz-read-improvements`（1.4.1-improve-kaz から分岐）で2つを merge した（7576aa75、7c15c23d。衝突はどれも両方を残して解消した）。2つを組み合わせたテストも追加した（4847c5ee）。ビルドは 1.4.1+2026-10-09.4847c5ee（sha256 86a81042…）。2026-10-09 に両方を有効にして計測した。6回目（13:05）は、ゲストで fsck が走ったので参考の回とした（10s で切った GET 35 回、そのうち 21 回は 0.7〜2.2s で取り直しに成功。同じ object が 3 回続けて切れた例もある）。7回目（14:17〜14:27）は約 10 分で、5回目の約 20 分の半分。Drive へのダウンロードは −36%、broken pipe は 760→0。ただし、Drive の応答が速い時間帯だった。2026-10-09 ユーザーの指示で `1.4.1-improve-kaz` に取り込んだ（bbd672b3、`merge/kaz-read-improvements` を --no-ff で merge、中身は 4847c5ee と同じ）。`juicefs-v1.4.1-kaz.4` としてリリースする。残る検討: 同じ key で続けて切れたときの扱い（待ち時間を延ばす／取り直しの回数の上限）、10s の余裕（成功した GET の最大が 9s 近い回がある）、`1.4.1-improve-kaz` への取り込み、rclone 側の改修。
  - [ ] rclone serve s3: JuiceFS が接続を切ったら、Drive へのダウンロードも止める。2026-10-09 に `--kaz-s3-cancel-get-on-disconnect` として実装した（rclone `feat/kaz-s3-cancel-get` 72dfdb817、push・merge なし、既定 off、`--vfs-cache-mode off` が必要）。VFS が object を知っている GET は、VFS を通さず、要求の context で backend から直接開く（chunk の設定と転送の統計は VFS と同じ）。書き戻し中のファイルなどは今までどおり VFS を通す。テスト4件（取り消し、範囲指定、off、cache-mode の確認）が通り、race も警告なし。golangci-lint は、手元の版が Go 1.26 に対応していないため未実行。試験用バイナリ `~/tmp_local/rclone-kaz-build/rclone-kaz-cancel`（v1.75.1-kaz-dev.72dfdb81、sha256 879cc104…）。次は本番で確かめる（header-timeout で切った GET の broken pipe が消え、rclone のログで Drive の要求が取り消されること）。原因: GetObject は要求の context を受け取るが、VFS（vfs/read.go:79）がファイルごとの context で Drive を開くので、切っても Drive の要求が続く。案: cache-mode off で object が分かっている GET は、VFS を通さず、要求の context で開く。
  - [ ] **（重要）header-timeout と rclone の cancel を組み合わせると EIO になる（2026-10-09 実機、ユーザーは後で直す方針）:** 14:54〜16:00 に、重い VM（inode 603330）の操作中、object `6571941_1_4194304`（09:54 に PUT されたもの）が 10s で 84 回切られ、一度も取れなかった。同じファイルの read の失敗が続いて再試行の上限（`--io-retries`）を使い切り、待っていた read がまとめて EIO になった（15:13、15:25、15:29、15:50、16:00 の 5 回、各回で数十件）。この object を直接読むと、33.7s で全部返った（Drive が毎回約 30s 後に応答するファイル）。改修前の rclone では、切られた要求も裏で続いて完了し、その直後の取り直しは速く返っていた（6回目の計測、`6295646_2`）。`--kaz-s3-cancel-get-on-disconnect` がその完了も取り消すので、永久に取れなくなった。仮想ディスクの破損とのつながりは、まだ確かめていない（ゲストのログは見ていない）。以前から起きている「正常にシャットダウンした後の起動で壊れている」型とは分けて扱う。
    - 2026-10-09 16:38 には、バックアップから cp（CoW、slice を共有）で戻したディスク（inode 623800）でも同じことが起きた。object `6346699_1`（バックアップの slice、file offset 2220355584。1回目の EIO の束にも毎回出ていた位置）で再試行を使い切り、`fileReader.f.err = EIO`（reader.go:127）が記録された。この値は 0 に戻されないので（sticky EIO、upstream 由来）、以後このファイルへの read はすべて、待たずに EIO になった。ゲストは gdm の画面で `I/O error, dev vda` を出し続けた。header-timeout を外して mount し直した後、同じ場所は 27.3s で読めた。バックアップ自体は壊れていない可能性が高い。ユーザーは header-timeout を外して運用中。
    - 直す方針の案（sticky EIO）: 一時的な失敗で、ファイル全体の read が永久に失敗し続けないようにする（f.err を、成功した時点やしばらく経った時点で消すなど）。upstream の動きを変えるので、設計から始める。
    - 2026-10-09 修正を2つ実装した（どちらもローカルの commit のみ、push・merge なし）。
      - `fix/kaz-get-header-timeout-retry` e63196d8: header-timeout で切った key を5分間覚えておき、その間の取り直しは header-timeout をかけずに `--get-timeout` まで待つ。取れたら忘れる。覚える key は最大4096個。
      - `fix/kaz-read-sticky-eio` 145b8b85（ユーザーが案 A を承認）: 再試行を使い切ったエラーを、ファイル（`fileReader.f.err`）ではなく、失敗した slice の読み込み（`sliceReader.err`、状態は INVALID）に持たせる。待っていた read には EIO を返し、それより後の read は新しい slice の読み込みで取り直す。再試行の回数もその時点で0に戻す。使われなくなった `f.err` は取り除いた。素直な案 A（新しい read が f.err を0に戻す）では、失敗した slice を待っていた read が戻らなくなるため、この形にした。upstream の動きを変える修正。
      - テスト: pkg/chunk 全体、pkg/vfs（Redis が必要な readdir のテスト4件を除く）、race がどれも通過。
      - 2026-10-09 `merge/kaz-read-fixes`（1.4.1-improve-kaz から分岐）で2つを merge した（b8e2caf8、948431c8。衝突なし）。テストは pkg/chunk、pkg/vfs（Redis が必要な4件を除く）、cmd のフラグが通過。race の失敗は kaz.4 にもある既存のもの（WithTimeout、rawFull）だけ。ビルド 1.4.1+2026-10-09.948431c8。
      - 2026-10-09 本番で確認した（17:40〜、header-timeout で切った GET 8件はどれも1回だけ、EIO 0）。`1.4.1-improve-kaz` に merge した（8a64eb07）。`juicefs-v1.4.1-kaz.5` としてリリースする。
      - 2026-10-11 ユーザー報告: kaz.5（sticky EIO の修正を含む）にしてから、仮想ディスクが壊れることがほとんど無くなった。ユーザーは、これまで「ゲスト OS を正常に終了しても、次の起動でファイルシステムが壊れている」の主な原因が、sticky EIO だったと見ている。ゲストのログなどで個々の破損との因果関係を確かめたわけではなく、運用での観察（相関）として記録する。
      - 次: 2つを merge 用のブランチでまとめ、実機で確かめてから `juicefs-v1.4.1-kaz.5` として出す。リリースノートに、kaz.4 の `--kaz-get-header-timeout` の危険（再試行を使い切って EIO、sticky EIO でファイル全体が読めなくなる）を書く（ユーザーの指示: kaz.4 のノートは直さない）。
    - 直す方針の案: 同じブロックの2回目以降の GET には header-timeout をかけない（または待ちを延ばしていく）。header-timeout で切ったことで、再試行の上限を使い切らないようにする。`juicefs-v1.4.1-kaz.4` のリリースノートに、この危険を書き足す。
    - rclone の `--kaz-s3-cancel-get-on-disconnect` 自体は設計どおりに働いていた。14:51 以降、GET 4,211 回で broken pipe は 0。取り消しは 102 回で、そのうち 85 回がこのファイルへの要求。取り消しは ERROR レベルのログ（`open file failed: ... context canceled`）になるので、うるさい。ほかの ERROR は Drive の 500 系（Internal Error、Unknown Error）で、この改修とは関係なさそう。
  - [ ] 隣の block の先読み（ランダムな読み込みでも働くもの）の設計と試行。ユーザーはこれを試す方針。記録と再生は、汎用の仕組みにしにくいので採らない。見込みは GET の待ちの3〜4割減、外れのダウンロードが最大で約650回増える。

- [ ] **JuiceFS mount の `getDirParent ... cache miss` が約3秒ごとに続く（2026-10-08、ユーザー報告。呼び出し元は virt-manager で、閉じると止まることをユーザーが確認。キャッシュが毎回効かない理由は未調査、優先度低）:** mount（PID 3333）の DEBUG ログ（`vm-io-20261008-185307.log`）で、同じ約15個の inode（614590、1027、580898、1026、596152…）について親をたどる `getDirParent` が毎回 cache miss になり、19:11:29 から約3秒周期（各1,451回、毎分約300行、Redis の GetAttr が毎秒約5件）。全体の走査ではない。呼び出し元は quota・dirstat の集計か statfs（quota.go の checkDirQuota/updateDirQuota、base.go:1166）。ソース上はディレクトリなら dirParents にキャッシュされるはず（base.go:1480-1484）なのに毎回 miss になる理由と、3秒ごとの呼び出し元（QEMU の statfs など）は未確認。gc・rclone とは無関係。

- [ ] **JuiceFS 改修版の独自オプションに `kaz` 名前空間を付ける（2026-10-08、ユーザー要望、いずれ）:** rclone と同じ規則（`--kaz-...`、ヘルプ先頭 `[kaz]`）に揃える。対象例 `--writer-flush-scope`、`--meta-write-batch`、`--writeback-fsync` など。既存の設定・起動スクリプトとの互換（旧名を別名として残すか）を検討する。

- [ ] **metadata path 最適化（2026-10-07。Phase 1・2 は実装済みで `1.4.1-improve-kaz` に merge 済み、残りは次の機会）:** [調査結果と実装計画](docs/superpowers/specs/2026-10-07-metadata-random-io-optimization-plan.md)。主指標は qcow2 への Ubuntu Desktop 26.04 インストール時間（現状 約1時間）。ユーザーの計画承認後に着手する。
  - [x] 2026-10-07 ユーザー実施の計測（unsafe、debug付き、12:52〜14:16）を解析した: [報告](docs/inspection/metadata_random_io/2026-10-07/install-report-ja.md)。最大の待ちは fallocate(ZERO_RANGE) の全 inode flush（3,330s／173ms が flush 待ち）。commit は 43ms×62,317件。
  - [x] 同日 raw でも計測した: 約36分（qcow2 約84分）、fallocate 0回、最大の待ちは Read 前 flush 799s。
  - [x] 計画書を実測に合わせて改訂（§7 の順番、C4 の詳細設計・テスト・見込み）。
  - [ ] **最優先（実測で変更、Phase 1）:** Read 前と Fallocate 前の writer flush を、対象範囲と重なる chunk の pending に限定する（計画書 C4）。その後 group commit。writeback（QEMU）条件での計測は未実施。
    - [x] 2026-10-07 実装・検証済み（本体 `feat/range-flush` 18e641b8。`1.4.1-improve-kaz` から作成。2026-10-07 に merge 済み）。`--writer-flush-scope=range` で有効。詳細・懸念は agent_memo の「Phase 1 C4 実装」。計測用バイナリ `~/tmp_local/juicefs-builds/juicefs-rangeflush-20261007`。
    - [x] qcow2 の range 計測（2026-10-07 17:34〜18:27）: **約84分 → 約53分**。fallocate 3,330s → 1,887s、commit −44%、write 合計 −80%。Read 前の待ちは変わらず。[報告](docs/inspection/metadata_random_io/2026-10-07/install-range-report-ja.md)
    - [x] raw の range 計測は、ユーザーの判断で行わない（ほとんど変わらない見込みのため、2026-10-07）。
    - [x] 本体の commit: `feat/range-flush` 18e641b8（2026-10-07、未 push）。バイナリ `~/tmp_local/juicefs-builds/juicefs-rangeflush-18e641b8`。
    - [x] 同じ handle の Write が preflush 中に待つ件 → 2段階 preflush（Read・Fallocate、range のみ）で対応した（2026-10-07）。
    - [x] mtime の巻き戻り → commit の順序と mtime の下限で対応した（file にも有効）（2026-10-07）。
    - [ ] 要判断: range では、範囲外の追記の quota エラーが Fallocate ではなく後続の I/O で返る。
    - [ ] `TestSmallPUTDiagnostics` が全体実行で1回だけ FAIL した（payload サイズの照合）。再現せず、原因は未特定。
  - [ ] 実機で client-cache が有効なので、stale attr の再現テストを先に行う。statfs を約1秒周期で呼ぶ uid0 の pid 565/567/569 の正体（WSL 側？）は未確認。compaction GC local_error 487件も別途確認。
  - [ ] Phase 0: metadata metrics（op 別 p95/p99、open-file／txn lock 待ち、Redis cmd RTT、flush 時 pending 数、slices/chunk、compaction 回数）と、Redis RTT・インストール時の baseline 計測。
  - [ ] Phase 1: 同期 compaction（≥2500）を open-file lock の外へ。実機 meta URL の `client-cache` 有無を確認し、doWrite の stale attr（ファイル長後退）の可能性を再現テストで確認。
  - [x] Phase 2: 同 chunk の slice commit をまとめる（案C、3 engine、`--meta-write-batch` 既定0）。本体 `feat/meta-write-batch` f9308391（push なし）。[設計](docs/superpowers/specs/2026-10-07-meta-write-batch-design.md)・[計画](docs/superpowers/plans/2026-10-07-meta-write-batch.md)。最終レビューの Critical 2件・Important 3件を修正済み。
    - [x] qcow2 の計測（2026-10-07 21:19〜22:02）: **約53分 → 約43分**。transaction −41%、Read 前の待ち −81%、Fallocate 前の待ち −28%。[報告](docs/inspection/metadata_random_io/2026-10-07/install-batch-report-ja.md)
    - [x] 2026-10-07 `1.4.1-improve-kaz` へ `--no-ff` で merge した（78acd63d）。2026-10-08 にユーザーが push。feat/range-flush・feat/meta-write-batch のブランチは残してある。
    - [x] 2026-10-08 **v1.4.1-kaz.3 を公開した**（本体 78acd63d、調査リポジトリのタグ 852b8f5）。ユーザーが install の動作を確認済み。
  - **次の機会に回す改善（2026-10-07 ユーザー方針）**。根拠は [batch 版の報告](docs/inspection/metadata_random_io/2026-10-07/install-batch-report-ja.md) の「次の候補」:
    - [ ] Fallocate の metadata 更新を、範囲内の pending の commit と同じ transaction にまとめる（Fallocate 前の待ち 905s ＋ Meta.Fallocate 約590s が対象。meta API と 3 engine に関わる）。
    - [ ] chunk をまたぐまとめ（案A）。まず、単発の commit 14,235件のうち、他の chunk とまとめられる割合をログから見積もる。
    - [ ] Redis の transaction の RTT 削減（計画書 C7。WATCH／GET／MULTI-EXEC／UNWATCH を pipeline や Lua で 1〜2 RTT に）。
    - [ ] 計画書の残り: C2（同期 compaction を lock の外へ）、C5・C6（chunk cache の更新、空 chunk の cache）、client-cache の stale attr の再現テスト、`--large-file-mode`。
    - [ ] Phase 2 で先送りにした Minor 6件（`.superpowers/sdd/2026-10-07-meta-write-batch/progress.md`）。加えて、`TestWriteSlicesRedis` は Redis がないと skip せずに失敗する（`newRedisMeta` が接続エラーを返さないため。既存の TestRedisClient と同じ挙動）。
    - [ ] writeback（QEMU cache=writeback）条件での計測（fsync の待ちへの batch の効果）。
    - [ ] 先送りにした Minor 6件（ledger `.superpowers/sdd/2026-10-07-meta-write-batch/progress.md`）。chunk をまたぐまとめ（案A）と `--large-file-mode` は計測の後に判断する。
  - [ ] Phase 3: Read 前 flush の範囲限定、自分の commit 結果で chunk cache 更新、空 chunk の cache。
  - [ ] Phase 4: Redis txn の RTT 削減（WATCH+GET pipeline、Lua）を実測次第で判断。
  - 前提: fio・redis-server は未導入、RTT 注入（netem／toxiproxy／docker image）はユーザーの承認が必要。

- [ ] **staging fsync（2026-10-06）:** `--writeback-fsync` は e6ab89b8 として commit 済み、`release-1.4.1-kaz.2`（9268beb4）へ merge 済み（未 push）。実 VM 負荷での書き込みレイテンシ・PUT への影響を計測し、必要なら次の配布版（v1.4.1-kaz.2 など）に含める。

- [ ] **Read EIO:** shared retry counter／singleflight先頭ctx取消fanout→sticky EIOを隔離再現し、3bed baselineと比較する。実エラーは隠さず、再現回帰を先行させる。
- [ ] Read異常のcaller、sliceReader identity、取消元・理由、retry値の診断を検討する。既存counterだけでQEMU／guestへの帰属を決めない。
- [ ] 15s/10sの継続評価、負荷を合わせたreuse-window単独比較、CPU内訳を必要時に計測する。
- [ ] fallocate mode、Read前flushが対象範囲外のpendingを閉じる程度を分類し、整合性を保った改善余地を検討する。
- [ ] hint overflow／local busyの周期回復、pending-only GC index、cold workの回復保証は後段の設計課題とする。
- [ ] 元ログの保管とverbosity運用を整理する。checksum検証を維持し、正常DEBUG出力抑制を候補にする。openなproducerを再確認し、勝手に削除・圧縮・切り詰めしない。

## 完了した調査・文書化

- [x] 同期DELETE→compaction待ち→旧writer期限EIO、外側FUSE15分watchdogの別経路を特定し補正。
- [x] Read前flushのエラー伝播、既定期限なし待機、実writer error伝播を保持。
- [x] GC local retirement、upload競合保護、priority scheduler、reuse16と診断を実装・対象検証。
- [x] 固定ログのraw/payload/normal/compaction/GC、freeze origin、fsync、健全性、DEBUG容量を集計。
- [x] 明示的freezeの内訳Fsync85.2%／Fallocate11.2%／Read3.6%を確定。呼出数とslice数を区別。
- [x] 調査ドキュメントをこのルートへ移設し、README・findings・資料一覧・リンク・内容保持を確認。

## 過去の作業履歴

以下は10/01以降に追記した当時の作業記録。『現在』『未適用』『未計測』等はその時点の状態であり、現在の優先作業は上の一覧を参照する。古い未チェック項目だけを現在の未完了作業と解釈しない。

更新: 2026-10-02

## 最優先: 2026-10-02 VM filesystem障害の再発

ユーザー報告: 3VM中少なくとも2台で以前同様のguestFS障害、全3台停止済み。小PUT最適化は保留。

- [x] 失敗返却経路をログとsourceで特定。VFS期限0の外側に固定15m go-fuse watchdogが残り、05:57/06:34のfsync等をEINTRで終了させた。
- [x] GenFuseOpt Timeout0へ修正。実error/明示writer期限/kernelcancel通知保持、globalwatchdogの強制replyはoff。RED→GREEN、realFUSEpending/error/watchdog/kernelcancel、CLI伝達、既存VFS/Meta回帰とrace、ビルド確認。
- [x] positive deletionqueue backpressureをread-onlylivepprofで確認(07:43:48)。長metadata cleanup＋inodeLock＋同期compact待ちが継続。過去障害時刻stackとは区別。
- [ ] 本番への修正版適用とconfig FuseOpts.Timeout0確認（明示指示なしで実行しない）。現在稼働版はsmallput-diag-local/Timeout15mのまま。
- [ ] backend/削除queueの30分級cleanup停滞への対策。必要ならenqueue wait/queue長・cleanup producerの診断とdurablecleanup設計を検討。単にqueuewaitをerror/未保存successへ変えない。
- [ ] 隔離VMでの継続再現とguestFS状態確認。既に障害を受けたdataの復旧/repairは別作業、勝手に実行しない。
- [x] rclone serve s3 の PUT 30s timeout（2026-10-06）: 原因は、Drive の変更通知による VFS ディレクトリの全件読み直し。ユーザーが `--poll-interval 0`／`--dir-cache-time 1h` を 10:40 に適用し、短期の観測で解消を確認した。詳細は `docs/inspection/rclone_put_timeout/2026-10-06/report-ja.md`。
- [ ] 上記対策の長時間観測: chunks ディレクトリの切り替わり、1時間ごとのキャッシュ期限切れのとき、`/rclone-s3` を変更するのがこの rclone だけであることの維持。
- [ ] 任意: JuiceFS の `ResponseHeaderTimeout` 30s 固定の扱い（設定可能にするか）。staging 再送が上限に達したときに WARN を出すか。zstd レベル 1 と 3 の圧縮率・CPU 比較（どれも未着手、実装はユーザーの指示があってから）。
- [ ] 任意: rclone の `--rc` は localhost のみで、`--rc-user`／`--rc-pass` がない。同じホストの他ユーザーから操作できるので、必要なら認証を付ける。

## 優先1: VM向けの待機・エラー方針

- [x] 既定のflushを期限なし待機へ変更し、処理遅延・compaction待ち・削除queue待ちを経過時間だけでEIOに変換しない。
- [x] 実際の保存失敗/容量不足/quota超過は伝播し、未完了の保存を成功として返さない。
- [x] 削除queue飽和→背景compact後処理待ち→同期compact待ち→Write→Flushまでの境界を回帰テストで確認する。
- [x] 負のmax-deletesを同期削除と明示する起動WARN、CLI help、option docsを追加する。
- [x] 対象テスト/race、5分を実時間で越える長時間回帰、ビルド、差分レビューを完了する。

- [x] PostgreSQLのSQL共通・固有経路をソース確認する（実テスト不要のユーザー指示に従い未実施）。今回の待機修正の固有不整合は見つからず。

## 優先2: sliceタイマーと小さいPUTの調査（優先1の完了後）

ユーザーの意図: `--slice-flush-wait 30s --slice-flush-idle 10s` によりraw staging slice/blockがより大きく育ち、object PUT回数が減ること。実機では1KB未満のPUTが多数あるように見える。

- [ ] PUTの実payloadサイズと、圧縮前のslice/blockサイズ、stagingファイルサイズを分けて計測する。圧縮やゼロの多いデータが小さいpayloadになっているだけか確認する。
- [x] timer設定が実行中のConfigへ届いていることを確認する（現在のFUSE `.config`: 30s/10s、writer timeout=0、待機版Version）。
- [x] sliceの再利用条件を追い、連続write/ランダムwrite/上書き/複数chunkでの違いを説明する（実VFS＋mem object＋temp stagingで比較、latest data readbackあり）。
- [ ] timer以外のfreeze/flush契機（fsync、read前flush、fallocate等、full block/slice、slice数・メモリ圧迫）を計測する。
- [ ] qcow2の小さいランダムwriteとfsync頻度を再現し、timer延長でまとめられるwriteと、整合性を守るために即座の保存が必要なwriteを区別する。
- [ ] まとまることを確認する回帰/計測テストを先に作り、正常なfsync・read-after-write・writeback耐久性を保って意図した挙動に修正する。
- [x] timerだけで達成できない条件と必要な集約設計・制約を説明する。fsync成功の先送り・保存未完了の成功応答でPUT数を減らさない。

2026-10-01 続きセッションで調査と診断追加を実施。集約の動作修正は未実施。観測サイズの意味や実機のfreeze原因を仮説から断定しない。

- [x] key suffix=raw block、slice長、圧縮後payload、stage file長の意味を確定。現volumeはzstd/4MiB、実機の各payload分布は未計測。
- [x] 旧診断ログを固定範囲・SHA付きで通常write/compactionに分類（通常成功15,719件、raw4KiB=61.24%、raw<1KiB=11件。compaction成功737件、raw4MiB=700件）。
- [x] freeze reason + ID確定対応 + PUT payload_bytesの診断追加。診断テストRED→GREEN、計測テスト3回、VFS通常suite、memory経路＋従来待機/Read失敗回帰race3回、ビルド成功。
- [x] cloud PUTを止め、512 raw→516 stage bytes、fsync完了とreadback、解放後zstd19 payload bytesを確認。writebackローカルcommit待機と非同期cloud uploadを区別。
- [x] 新診断版の実機freeze理由を取得。10/02最新run: explicit55.94%、writable_window43.97%、idle0.085%。この時点では明示flush内訳が未確定だったが、10/03にorigin付きログで分類済み。
- [x] 観測元を照合。ユーザー回答は `juicefs stats --verbosity` のobject.put / put_c。通常更新行は圧縮bytes/sとAPIcall/s、比率は期間平均。normal/compaction/failedretry混在。任意データの圧縮率や全PUT<1KiBの分布は未確定。
- [ ] 実機freeze理由が確認できてから、pending read overlay / sparse range集約 / durable stage indirection等の必要性と範囲を決める。今回の直接VFS試験は実QEMU/qcow2ワークロード全体の再現ではない。

### 別途検出した既存課題

- [ ] disk writebackの `stageFull` 対 `checkFreeSpace` raceを別途検討。新計測テストを未変更HEAD3bed0d82へコピーしたarchiveでも再現。今回の診断変更には原因を帰属しない。フルdisk race成功とはしない。
- [ ] 初期の直接upload試験で `rawFull` とcache/noOp.Compressのraceも観測。こちらは独立baseline未検証。データ破壊との因果は未確認。
- [ ] pkg/chunk既存DATA RACE（disk_cache/disk_cache_state、未変更HEADでも11テスト失敗）。今回対象外。
- [ ] 極端な `SliceFlushWait*2` duration overflow候補（通常30sとは無関係、ソース指摘のみ未再現）。


### 2026-10-02 最新runによる進捗

- [x] raw/payloadを実logで別集計。成功PUT11,718中<1KiB4,754。旧stage再送8,337とcompaction3,381を分離、5分後も旧stage再送継続。
- [x] timer外freezeが主群と確認。17,696sliceの65.87%がraw4KiB、全metadata done errno0。
- [x] 明示flush callerをorigin付きログで分類。耐久explicit800560slice中Fsync682152(85.2%)、Fallocate89607(11.2%)、Read28801(3.6%)。close由来の新規freeze0。
- [ ] 新stage→upload/compaction/obsoleteを相関。このprefixで新freeze同IDのPUTは0、DELETE同ID15,495。残pending/obsolete内訳を断定しない。
- [ ] writable_window制限を維持/拡大した安全な局所比較を設計し、再利用効果・memory/metadata数・fsync/read-afterwriteを計測。設定timerだけを再延長しない。

最新run解析結果はdocs/inspection/small_put_investigation/2026-10-02/。sourceの集約動作はまだ変更していない。


### VM inode長停滞の設計候補（2026-10-02、未実装）

- [ ] 保存済みdataとmetadata commitからobsolete GCのRAMqueue待ちを分離するdurablecleanup設計を比較。参照数/再試行/crash/全metadata engine parityを前提にする。
- [ ] 同chunk背景compactionのactive状態がcleanup待ちで長く残ることを改善し、hotchunk優先schedulerとrawlist高低watermarkを検討。
- [ ] 【保留・今回対象外】inodewideLock構造変更。ユーザーは複雑性/リスクに対して効果が不明瞭として案2を今回選択せず。→ 2026-10-07 の依頼で再び対象。計画では lock 分割ではなく group commit を推奨（上記 metadata path 最適化）。
- [ ] 新slice発生数低減案とdeletedata実throughput案を比較。threshold/queuecap引上げだけを解決としない。

ユーザーの追加要件: VM用filesystemとして、データ整合性と待機方針を維持しつつ通常使用時の数十分のinode全体停止を減らす。方法の比較段階、個別案の実装は未合意。


### 選択された進め方（2026-10-02、設計段階）

- [x] ユーザーは1/3/4を選択、2は今回対象外。既存挙動と新GC分離modeの切替を希望。
- [ ] 1のv1仕様: legacydefault＋optin分離mode、永続deadmarkerを根拠にbounded GC通知/dispatcherを設計。foregroundが既存dSliceMu/queue満杯を待たないことを検証。
- [ ] 1の安全監査: ref再追加/共有CopyFileRange、crash/restart/mode戻し、DELETEretry、0/負MaxDeletes、NoBGJob、全engineparity。既存markers保証なし経路へ一括拡張しない。
- [ ] pending-only永続GCindexは後段候補。既存collectorを高頻度fullrefs走査へ変更しない。
- [ ] 3は既存numSlices通知/平均O(1)更新/有界候補/bucketと公平性。全inodechunkscanと毎write追加DBlookup禁止。overflow保証と既存2500fallbackを仕様化する。
- [x] 4のexplicitflush origin+barrierID診断と実機内訳を確認。
- [ ] writablewindow単独の実機比較は継続課題。局所4→16 commits10→9は確認済み、実PUT削減率の単独帰属は未実施。

### FUSE＋1/3/4のローカル統合版（2026-10-02）

- [x] FUSEwatchdog修正を統合。実kernel・error・cancel回帰保持。
- [x] GC legacy/deferred切替、永続marker＋有界独立dispatcher。新schema/scan増加なし。
- [x] priority opt-in scheduler、通知平均O(1)、固定bucket・有界1024・inode公平性。old2500/manual安全網保持。
- [x] explicitcaller/barrierDEBUG診断、writer-reuse-window4既定/1..64、fsync/readデータ回帰。
- [x] Rediscopy/clone source snapshot競合をRED→WATCH修正→retry/Error回帰。newmode評価は全writer更新条件。
- [x] plan/spec全3file日本語化、英語内容の要件欠落なし。
- [x] 対象race・fullVFS/FS/FUSE・CLI・build確認。allpkgは環境依存失敗。
- [ ] 実機configの独立確認とVM継続試験・PUT/latency/memory総合計測。2026-10-02にユーザーが補正版へ更新したと報告（下記参照）。
- [ ] pending-only永続GC indexとcoldjob eventual保証は後段、初版には含めない。


### 統合版実機での再起動とGC滞留（2026-10-02、優先調査）

- [x] ユーザー適用のcombined版/configをread-only確認。FUSE0・writer0・deferred・priority・reuse16有効。旧『未適用』は過去の状態。
- [x] proxmox01 vol1/inode596152の42〜46.79s成功I/O遅延、itco reset設定を確認。ゲスト再起動原因/readonlyは未確定、前bootjournal異常なしというユーザー観測を保存。
- [x] del_c集計経路継続をsource確認、新旧高速DELETE burst差を実log比較。53823未PUT/DELETE成功slice集合がruntime staging53823と一致。
- [x] livepprof/Prometheus取得、GC通知受信待ち・DELETE10workerリモート待ち・upload30枠占有・cleanupSlices回収25669を確認。実機hintoverflowは未証明。
- [ ] 少数staging sampleの存在/rawsizeとmetadata負refを照合し、live upload待ち/obsolete回収待ちを分離する。
- [ ] GC modeのhourly recovery・backpressure除去のsteady-state回帰を先に作成し、bounded local-retirement/remoteDELETE分離やbackground admissionの補正設計を比較。FIFO remote backlogとstaging取消遅延、inflightupload整合性を対象にする。
- [ ] GC hintdrop/queue/localstage age・dead backlogの可視化。全file/chunk走査やperwrite追加DB参照を導入しない。
- [ ] 本番変更はユーザー所有。元ログ/staging/VM/DBは削除・変更しない。


### ユーザー承認のstaging回収補正（2026-10-02）

- [x] 「改善して」を受け、日本語spec/planとTDDでlocal retirementをremoteDELETE待ちから分離。
- [x] RetireSlice callback/nonblockinghandoff/marker保持/固定metrics/DEBUGを実装、3backendのshared/error/recovery/legacy安全性を検証。
- [x] optionalcachedStore.Retireと同storeupload/stagecallback busyguard、ACK前pending、scanner取消競合、localunlinkerror/retryを実装・race検証。
- [x] cmd接続/customstore互換とerror伝播、独立review、cachedStore通常試験を確認。
- [x] fullVFSの診断capture競合をcontrolledRED→testhookで解消、fullVFS/FS/FUSE再検証PASS。
- [x] 別名binary build/version/SHA、docs/memo/testevidence最終化。ownisolatedRedis停止結果はmemoへ記録。
- [x] 統合版＋補正をローカルコミット 84f19ca4（旧69fd077b、fix/vm-io-wait-policy、author tongsama、2026-10-03）。push無し。
- [ ] 実機適用後にlocal_retire_success/hint_overflow/remote_deferredとstagingage/PUTを定量照合。ユーザーによる更新・低staging・安定という初期報告あり。agentからの本番変更は行わない。

fullpkgchunkはGo1.25.11(goenv)で通常49PASS（TMPDIRはルートFS上）。raceはHEADにも既存の11失敗あり、新retire testsはrace PASS。hourlyhintoverflow回復・別clientのinflightuploadlease不在は残る。guest再起動解消を未実証のまま確定扱いしない。


### 補正版のユーザー実機試験（2026-10-02、継続中）

- [x] ユーザー報告の適用手順を記録: 全VM停止→新規書き込み停止→rawstaging/deletionの全排出を待機→JuiceFS更新→VM3台再起動。
- [x] 初期観測を記録: 高いdisk I/O負荷でも安定、rawstagingは常に低水準、PUT減少とゲストI/O速度向上の印象。これらはユーザー観測で定量比較前。
- [x] ユーザー実施の継続負荷・耐久試験では問題なく、2026-10-03に耐久テストクリアと報告。試験条件を越えた一般保証には広げない。
- [ ] stage件数/bytes/age・PUT率・I/O/fsync待ちの定量評価と、compaction時CPU負荷の内訳は未計測。

ユーザーはもう少し様子を見る方針。現時点では追加の設定変更/実装/自動監視を始めず、今の設定での継続観測を優先する。


### 耐久合格後の設定評価（2026-10-03）

- [x] ユーザー方針を記録: compaction集中時の少量CPU増は許容、耐障害性を優先。
- [x] 提示値wait15s/idle10s/timeout0s/reuse16をsourceで再確認し、timerfreezeと保存待機を区別。
- [x] reuse16は同chunk pendinglistの非適合候補freeze距離であり、探索数/保持数の厳密上限ではないこと、適合判定が先行・frozen entriesも位置へ数えることを確認。
- [ ] 定量性能比較/CPUprofileは別途必要時のみ。今はユーザーが許容した現設定を維持し、本番変更は行わない。


### オプション効果の固定ログ解析（2026-10-03）

- [x] タイマー時系列を訂正: 耐久合格30s/10s→直前再起動で15s/10s。起動scriptと現在configの15s/10s一致をread-only確認。
- [x] 3logの固定completeprefix、sourceinventory、runtime/config/metricsを保存。元logの変更なし。
- [x] freeze/origin/barrier・PUTraw/payload/normal/compaction/GC・health/longwait・DEBUG容量内訳を集計し、日本語report-ja.txtとSHA/JSON/CSV/例外証跡を保存。
- [x] 実測と未分離の因果を区別し、loginventoryにproduceropen状況を保存。旧耐久logもopenなので終了済み扱いにしない。元log変更なし。


### ログ解析で判明したRead側の残課題（2026-10-03、優先）

- [x] 保存側は耐久Fsync335774全成功・writerbarrier560104全errno0と確認。一方、Readfile EIO1行と旧PIDのVFS EIO累積1207を確認し、『全I/O無エラー』と分けて記録。
- [ ] reader共有retrycounter・singleflight先頭ctx取消fanout→stickyEIOの安全な再現、3bed baseline比較、呼出元/取消理由の診断を検討。実保存失敗を隠さず、再現回帰先行で対応。今回source修正なし。
- [x] 最新GET404はsamekey3.63秒後成功・同ID削除記録無し、永久欠落とは評価しない。
- [ ] CPU時系列・window16単独効果は未分離。タイマー30→15のagefreezeは全0で直接効果未観測。
- [ ] 元ログ保管/verbosity運用は別途実行する。checksum検証を維持し、正常DEBUGログ出力の抑制を候補とする。openな旧/現producerのlogは勝手に整理しない。


### 明示flushの中身の分析結果（2026-10-03）

- [x] 呼出元は耐久期間Fsync85.2%/Fallocate11.2%/Read3.6%（新しくfreezeしたslice数の比率）。writerbarrier呼出回数335774/56437/167768とは区別する。
- [x] filewriter.Flushは同inode全pendingchunksのsliceをfreezeし、Finishの保存と作成順metadata commitの完了を待つ。Read前も範囲限定ではなく同file全pendingを対象。WBではcloud全upload完了と別。
- [ ] Fallocate mode(通常予約/punch-hole/zero-range等)と、Read時のどの範囲のpendingがflushされたかの実機内訳は未分類。整合性維持の上で必要性を検討する際の次の診断対象。


### 改修版バイナリの配布（2026-10-04）

- [x] 設計の合意（Actions、3対象、draft Release、install.sh/ps1）。armv7は対象外。
- [x] 仕様 docs/superpowers/specs/2026-10-04-release-distribution.md のユーザーレビュー
- [x] 仕様の承認と実装計画の作成（docs/superpowers/plans/2026-10-04-release-distribution.md）。
- [x] Task1〜4の実装と全体レビュー（未コミット）。install.shのテストは13/13合格。
- [x] shellcheck / actionlint 実行、指摘0（2026-10-04）
- [x] commitとpush（085a43b、4f69f00）、Task5の手動実行 run 37189012217 が全job成功（2026-10-04）
- [x] Task6: タグ v1.4.1-kaz.1 をpushし、draft Releaseを作成（run 37190020196、2026-10-04）
- [x] v1.4.1-kaz.1 を公開し、実URLからのインストールを確認（2026-10-04）
- [ ] 保留したMinor 10件（ledgerを参照）
