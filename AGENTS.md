# 調査プロジェクトでの agent 作業ルール

- やり取り、計画、仕様は日本語にする。
- このルートは JuiceFS 個人改善版の調査・設計・検証記録を管理する。`juicefs/` は Git ignore された独立リポジトリであり、このルートと別に扱う。
- 最初に `README.md`、`docs/findings.md`、`TODO.md`、`agent_memo.md` を読む。実装を変更するときは本体の `juicefs/AGENTS.md` も読む。
- 調査の計画・仕様は `docs/superpowers/`、確定した知見は `docs/findings.md`、詳細な履歴・継続方針は `agent_memo.md`、解析証跡は日付別ディレクトリへ保存する。
- 今後も重要な方針や訂正は `agent_memo.md` と `TODO.md` へ反映する。memo が1,000行を超える場合は、入口を残して分割を検討する。
- 大きい調査は許される範囲で sub-worker に分担し、ネスト max_depth=4 以内にする。
- 観測事実、source 上の条件、仮説、未検証事項を分ける。保存側の成功を全 Read の成功や全条件の耐障害性保証へ一般化しない。
- 本番 VM、metadata、object storage、cache／staging、元診断ログを勝手に変更・削除しない。元ログを解析する場合は固定 prefix と SHA を記録する。
- fsync、書き込み順序、read-after-write を弱めず、実保存失敗・容量不足・quota エラーを隠さない。
- 新規・変更する関数やクラスには目的が分かるコメントを付ける。Mermaid ノード内の改行は `<br>` を使う。
- 初期 untracked ファイルを黙って add／commit しない。GitHub への push・リモート変更は事前に確認する。
- ユーザーが文書 commit と本体 commit を別々に行う方針を維持する。明示依頼なしに agent が add／commit／push しない。
