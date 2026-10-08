# JuiceFS 個人改善版の調査記録

このプロジェクトは、JuiceFS の個人的な改善版を作るための、調査結果・知見・設計・検証記録を管理します。今回のスコープは **Google Drive をバックエンドとする `rclone serve s3` への最適化**です。特に、VM の仮想ディスクなど、部分更新・ランダム書き込み・fsync が多い巨大ファイルについて、正常な更新とデータ整合性を守りながら、不要な PUT と長い I/O 停滞を減らすことを目的にしています。

対象構成は `QEMU/KVM → qcow2 → JuiceFS CE v1.4.1 ベースの改善版 → rclone S3 → Google Drive` です。rclone v1.75.1 はユーザー申告による情報です。

## 管理範囲

このルートと `juicefs/`・`rclone/` は、**別々の Git リポジトリ**として管理します。

- **このルート:** 調査ドキュメント、計画・仕様、解析スクリプト、解析結果、検証証跡。
- **`juicefs/`:** JuiceFS 本体のソース、実装、回帰テスト、CLI に付随する製品ドキュメント。
- **`rclone/`:** rclone 本体のソース（`rclone serve s3` の改修）、実装、回帰テスト。

`.gitignore` の `/juicefs` により、本体のリポジトリをこのルートの commit に含めません。submodule ではありません。本体は後から配置する構成でも、このプロジェクトの文書を読めます。公式リポジトリを取得しただけでは、この個人改善版の差分は含まれません。

本体の改修は `tongsama/juicefs` の `1.4.1-improve-kaz` ブランチ（既定ブランチ）で管理しています（2026-10-08 時点の HEAD は `78acd63d235db5f275068d3ff75b45baa4b2f616`、v1.4.1-kaz.3 のビルド元）。v1.4.1-kaz.2 のビルド元は `release-1.4.1-kaz.2` ブランチ（`9268beb4`）に残っています。以前の `fix/vm-io-wait-policy`（`84f19ca4`、v1.4.1-kaz.1）はこのブランチに含まれ、2026-10-06 に削除しました。このルートの文書を commit しても本体の差分は保存されないため、本体は別途 commit・push します。2026-10-03 の整理時点の HEAD `3bed0d82` は、author の書き換えにより `2ae17f94` になっています（内容は同一）。

rclone の改修は `tongsama/rclone`（リモート名 `kaz`、upstream は `origin`）の `1.75.1-improve-kaz` ブランチ（既定ブランチ、v1.75.1 から分岐）で管理しています（2026-10-08 時点の HEAD は `dd03d0243`。`feat/kaz-vfs-lookup` を `--no-ff` で merge）。`.gitignore` の `/rclone` により、このルートの commit には含めません。改修の内容は [rclone の dir cache の知見](docs/findings.md#rclone-serve-s3-の-dir-cache-と複数ホスト2026-10-08) と [本番適用の手順書](rclone_dir_cache/2026-10-08/deploy-runbook-ja.md) を参照してください。rclone の配布（release）はまだありません（TODO の Phase 3）。

## 改修版バイナリの配布とインストール

このリポジトリは、改修版 JuiceFS のビルド済みバイナリの配布元も兼ねています。本体のリポジトリは公式の構成に近いまま保ち、配布に必要なもの（インストールスクリプト、ビルド用 workflow、版の対応表、リリースノート）はすべてこちらに置いています。

- 配布先: [GitHub Releases](https://github.com/tongsama/juicefs_inspection/releases)（最新は [v1.4.1-kaz.3](https://github.com/tongsama/juicefs_inspection/releases/tag/v1.4.1-kaz.3)、最初の版は [v1.4.1-kaz.1](https://github.com/tongsama/juicefs_inspection/releases/tag/v1.4.1-kaz.1)）
- 対象: `linux-amd64`、`linux-arm64`（aarch64）、`windows-amd64`。32bit ARM（armv7）と macOS は対象外です。
- 各版のビルド元となる本体の commit は [`release/versions.json`](release/versions.json) に、変更内容は [`docs/release-notes/`](docs/release-notes/) にあります。

### Linux

```bash
curl -fsSL https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.sh | sh
```

- 既定では `/usr/local/bin/juicefs` に置きます。書き込めない場合は sudo を使います。
- インストール先は第1引数で変えられます: `curl -fsSL …/install.sh | sh -s /opt/bin`
- 版を固定する場合（本番ホストではこちらを推奨）: `curl -fsSL …/install.sh | JFS_VERSION=v1.4.1-kaz.3 sh`
- 公式版と並べて置く場合: `JFS_INSTALL_NAME=juicefs-kaz` を指定します。
- `checksums.txt` の SHA-256 と照合し、一致した場合だけ既存のファイルを1回の `mv` で置き換えます。途中で失敗したときは既存のファイルに触れません。

**稼働中の mount についての注意:** 置き換えても、稼働中の mount は再起動されません。ただし、mount の子プロセスが異常終了して supervisor が自動で再起動した場合は、置き換え後の新しい版で動き始めます（`juicefs/cmd/mount_unix.go` は起動時に取得した実行ファイルのパスで子プロセスを起動し直すため）。本番ホストで置き換えるときは、この点を踏まえて計画してください。

### Windows（PowerShell）

```powershell
irm https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.ps1 | iex
```

- 既定では `%LOCALAPPDATA%\Programs\juicefs\juicefs.exe` に置きます。PATH には追加しません。
- 環境変数 `JFS_INSTALL_DIR`（インストール先）と `JFS_VERSION`（版の固定）で変えられます。
- mount には別途 [WinFsp](https://winfsp.dev/rel/) が必要です。スクリプトは WinFsp を自動では入れず、見つからない場合に警告だけを出します。
- Windows 版は、ビルドと起動（`juicefs version`）までしか確認していません。改修したコードの Windows での mount 動作は未検証です。

### 新しい版を出す手順

1. 本体（`tongsama/juicefs`）で修正し、`kaz` リモートへ push する。
2. このリポジトリの `release/versions.json` に、新しいタグ（例: `v1.4.1-kaz.2`）と本体の 40 桁の commit SHA を追加し、`docs/release-notes/<タグ>.md` を書いて commit・push する。
3. 必要なら、Release を作らずにビルドだけ試す: `gh workflow run release.yml --repo tongsama/juicefs_inspection -f tag=<タグ>`
4. タグを push する（`git tag <タグ> && git push original <タグ>`）。workflow が3種類をビルド・確認し、draft の Release を作る。
5. draft の中身を確認して、GitHub の画面で公開する。

`juicefs version` は `1.4.1+<本体commitの日付>.<SHAの先頭8桁>-kaz.<n>` と表示されます。仕組みの詳細は [配布の仕様](docs/superpowers/specs/2026-10-04-release-distribution.md) を参照してください。

## 最初に読む資料

| 資料 | 内容 |
|---|---|
| [知見の要約](docs/findings.md) | 障害原因、修正の意味、flush の分析、実測効果、限界、残課題 |
| [TODO](TODO.md) | 現在の優先作業と、過去の作業履歴 |
| [agent_memo](agent_memo.md) | セッションを跨ぐ方針、判断、詳細な経緯 |
| [オプション効果の総合報告](option_effects/2026-10-03/report-ja.txt) | 固定ログの比較、PUT／fsync／GC、Read 異常、DEBUG 容量内訳 |
| [技術診断の詳細](docs/development/vm_io_diagnostics.md) | 診断ログ・ソース経路・待機方針・制約。既存の英語資料を保持 |
| [元の障害引き継ぎ](juicefs_vm_corruption_handoff_codex.md) | 初期障害の証拠と当初の調査状況 |

最新の評価は `docs/findings.md` と日付付きレポートを優先してください。計画・memo・JSON の状態やパスは、当時の記録である場合があります。

## 今回の成果と残課題

- 同期 object 削除による compaction 停滞と、旧 writer 期限による EIO の経路を確認しました。正常な遅延は完了まで待ち、実保存エラーは返す方針に変更しました。
- VFS の外側に残っていた 15 分の FUSE watchdog を修正し、Read が writer flush エラーを無視して古いデータを返す経路も修正しました。
- 不要な staging のローカル回収をリモート DELETE の順番待ちから分離し、未開始の不要 PUT を取り消せるようにしました。
- 明示的 flush の主因は fsync と特定しました。耐久期間では fsync が 335,774 回すべて成功し、通常 PUT 試行／slice finish 比率は補正前より 86.4% 低い観測値でした。負荷が異なるため、機能単独の保証された削減率ではありません。
- **保存側の成功と、全 I/O の無エラーは別です。** Readfile EIO と旧プロセスの VFS EIO 累積カウンタ 1,207 が見つかり、取消の伝播・共有 retry counter の再現調査が残っています。

ユーザーが耐久テスト合格と報告した期間は **30s / 10s** です。その後、10/03 の再起動で **15s / 10s** に変更しました。15 秒版を同じ長期耐久試験の結果として扱いません。設定や評価の根拠は [選択した runtime config](option_effects/2026-10-03/current-config-selected.json) と [解析 manifest](option_effects/2026-10-03/manifest.json) に保存しています。

## ディレクトリ構成

```text
juicefs_inspection/
├── README.md
├── AGENTS.md
├── agent_memo.md
├── TODO.md
├── .github/workflows/release.yml  # バイナリのビルドと draft Release の作成
├── release/                  # install.sh / install.ps1、versions.json、テスト
├── docs/
│   ├── findings.md
│   ├── release-notes/        # 配布版ごとのリリースノート
│   ├── development/vm_io_diagnostics.md
│   └── superpowers/
│       ├── specs/
│       └── plans/
├── small_put_investigation/   # raw／payload／freeze の初期調査
├── incident_recurrence/      # 障害再発・FUSE watchdog・待機経路
├── combined_vm_io/           # FUSE・GC・scheduler・reuse 統合版の記録
├── restart_check/            # ゲスト再起動と I/O 遅延の調査
├── gc_backlog/               # staging 滞留・local retirement・回帰検証
├── option_effects/           # 導入効果、明示 flush、健全性、ログ容量
├── rclone_put_timeout/       # rclone serve s3 の PUT 詰まり（2026-10-06）
├── rclone_dir_cache/         # rclone serve s3 の dir cache と複数ホスト（2026-10-08）
├── juicefs/                  # 独立管理する本体リポジトリ（Git ignore）
└── rclone/                   # 独立管理する rclone 本体リポジトリ（Git ignore）
```

解析結果は日付別に保存しています。CSV／JSON は集計値、TXT／MD は所見、LOG は検証出力、Python は再現用の解析手順です。外部にある実機の元 DEBUG ログそのものは、このリポジトリにコピーしていません。固定 prefix のサイズと SHA を記録しており、末尾に追記があっても分析対象を区別できます。

## 計画・仕様とソースとの関係

[計画・仕様の一覧](docs/superpowers/README.md)から各フェーズを参照できます。以前 `juicefs/docs/superpowers/` に置いた内容は、このルートの `docs/superpowers/` へ移設しました。調査用の `vm_io_diagnostics.md` も `docs/development/` へ移設しています。[移設記録](docs/documentation-migration.json)に、移設時の内容保持確認を残しています。

移設済みの計画で `pkg/`、`cmd/`、`go test` 等を記載する場合、作業ディレクトリは本体の `juicefs/` です。本体の一般的な CLI reference は、実装と対応するため本体側に残しています。本体 Git で移設元の tracked 文書が削除状態になるのは、この分離によるものです。

## 作業・検証方針

データ整合性、fsync、書き込み順序、read-after-write を優先します。実保存失敗や容量・quota エラーを隠さず、未保存の状態で成功を返しません。writeback のローカル保存・metadata commit と、非同期 cloud upload を区別します。

本番 VM、metadata、cache／staging、元ログを調査の都合で変更しません。実装の変更は安全なローカル再現と回帰テストを先行させます。PostgreSQL はユーザー指示で source review のみです。全体 race suite の既存失敗、writeback の host power-loss 耐久性、他 client との upload lease の限界を、解決済みとして扱いません。

本体の開発ルールは配置した `juicefs/AGENTS.md` を参照します。Go1.25.11 による検証条件と、Go1.26／mockey の制約は [知見の要約](docs/findings.md#検証範囲) に記載しています。解析スクリプトには当時の絶対パス・PID・固定 prefix を含むものがあり、別環境で再実行する際は入力を確認してください。

文書と本体はそれぞれのリポジトリで確認・commit します。今回の文書整理では add／commit／push を行っていません。
