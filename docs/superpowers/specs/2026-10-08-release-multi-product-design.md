# 配布を JuiceFS と rclone の2製品に対応させる（Phase 3）設計

- 日付: 2026-10-08
- 状態: 設計（2026-10-08 ユーザー承認済み）。
- 前の設計: [改修版バイナリの配布（2026-10-04）](2026-10-04-release-distribution.md)。本設計で配置・タグ・install スクリプトの使い方を置き換える。
- 対象: このリポジトリ（`tongsama/juicefs_inspection`）の `release/`、`.github/workflows/`、README。本体のリポジトリ（`tongsama/juicefs`、`tongsama/rclone`）は読むだけで、書き込まない。

## 1. 目的と前提

**目的**: 改修版の rclone を、JuiceFS と同じく、このリポジトリの GitHub Releases から1行のコマンドで各ホスト（Linux と Windows）に入れられるようにする。2台構成（複数ホスト）に進む前提の作業。

**ユーザーの判断**:
- 今の install の URL（`releases/latest/download/install.sh` など）は捨ててよい。今後を見据えて整理する。
- JuiceFS と rclone は更新が独立しているので、製品ごとに分ける。
- rclone の対象は linux-amd64・linux-arm64・windows-amd64。Windows でも `rclone mount` を使っているので、公式と同じ機能一式（full）にする。
- 既存の JuiceFS の Release（`v1.4.1-kaz.1`〜`.3`）は残す。

**前提（観測）**:
- GitHub の「latest」は、リポジトリ全体で1つの Release にしか付かない。製品ごとの latest を GitHub の仕組みだけでは作れない。
- GitHub Releases の URL の形は `releases/latest/download/<ファイル>` と `releases/download/<タグ>/<ファイル>` に固定されている。
- 公式の rclone は Linux・Windows を `bin/cross-compile.go` に `-tags cmount` を付け、CGO 無しでビルドしている。Windows の cmount は CGO 無しで組み込まれる（`cmd/cmount/mount.go` の build 条件 `cmount && (… || windows)`）。WinFsp は実行時に読み込まれ、ビルドには要らない。Windows 版の版情報とアイコンは `bin/resource_windows.go` で埋め込まれる。

**守ること**:
- SHA-256 の照合に通ったときだけ、既存のファイルを1回の `mv` で置き換える。途中で失敗したら既存のファイルに触れない。
- Release は draft で作り、公開はユーザーが GitHub の画面で行う。同じタグの Release を上書きしない。
- 一度出した Release とタグは動かさない。

## 2. 方式（ユーザー承認）

**製品ごとのタグと、install スクリプトによる製品ごとの最新の検索**（方式 (1)）。

- 製品ごとの「最新」用の Release を別に作り、タグを付け替える方式 (2) は採らない。Release を動かすことになり、差し替えの途中で本体とチェックサムが食い違う瞬間ができるため。
- Releases ページでは、版ごとの Release（1つの Release に1製品・1版）が出した順に混ざって並ぶ。検索欄（`…/releases?q=rclone`）で製品ごとに絞れる。README に製品ごとの絞り込みへのリンクを置く。

## 3. 配置、タグ、版の対応表（第1節）

```text
release/
├── install.sh              # 共通（Linux）。製品名を引数で受け取る
├── install.ps1             # 共通（Windows）
├── tests/
│   └── test_install.sh
├── juicefs/
│   ├── versions.json       # 今の release/versions.json を移す
│   └── notes/              # 今の docs/release-notes/*.md を移す
└── rclone/
    ├── versions.json
    └── notes/
.github/workflows/
├── release-juicefs.yml     # 今の release.yml を移して、新しいタグの形に対応
├── release-rclone.yml      # 新規
└── install-tests.yml       # release/ の変更時に install スクリプトのテストを流す
```

**タグと版**:

| 製品 | タグ | 表示 | 最初の版 |
|---|---|---|---|
| JuiceFS | `juicefs-v<版>-kaz.<n>`（次は `juicefs-v1.4.1-kaz.4`） | `juicefs version` は今と同じ形 | 既存の `v1.4.1-kaz.1`〜`.3` は古い形のまま残す |
| rclone | `rclone-v<版>-kaz.<n>` | `rclone version` は `rclone v1.75.1-kaz.1` | `rclone-v1.75.1-kaz.1`（`tongsama/rclone` の dab33da31、Phase 1・2 を含む） |

- workflow はタグの接頭辞（`juicefs-v*`、`rclone-v*`）で起動する。古い形 `v*-kaz.*` での起動はやめる。

**版の対応表**（`release/<製品>/versions.json`）: キーはタグ名。項目は製品で共通の名前にそろえる。

```json
{
  "rclone-v1.75.1-kaz.1": {
    "repo": "tongsama/rclone",
    "commit": "<40桁の SHA>",
    "go_version": "1.26.0",
    "notes": "release/rclone/notes/rclone-v1.75.1-kaz.1.md"
  }
}
```

- JuiceFS の既存の項目は、キーを古い形（`v1.4.1-kaz.3` など）のまま移し、`juicefs_repo`・`juicefs_commit` を `repo`・`commit` にする。`notes` は移した先のパスにする。

**Release の見た目**: タイトルは `JuiceFS v1.4.1-kaz.4`・`rclone v1.75.1-kaz.1`。asset はその製品のものだけ。新しい Release は「Latest」を付けない指定（`gh release create --latest=false`）で作る（「Latest」の印は既存の `v1.4.1-kaz.3` に残る。紛らわしければ、印を外す方法を実装時に確かめる）。

## 4. install スクリプト（第2節）

**使い方**（スクリプトはリポジトリの `main` から直接取得する）:

```bash
curl -fsSL https://raw.githubusercontent.com/tongsama/juicefs_inspection/main/release/install.sh | sh -s -- rclone
curl -fsSL …/install.sh | sh -s -- juicefs /opt/bin
curl -fsSL …/install.sh | KAZ_VERSION=rclone-v1.75.1-kaz.1 sh -s -- rclone
```

```powershell
$env:KAZ_PRODUCT='rclone'; irm https://raw.githubusercontent.com/tongsama/juicefs_inspection/main/release/install.ps1 | iex
```

- 製品名（`juicefs` か `rclone`）は必須。省略や不明な名前はエラー。
- 環境変数: `KAZ_PRODUCT`（PowerShell）、`KAZ_VERSION`（タグ名そのまま。省略すると最新）、`KAZ_INSTALL_NAME`（ファイル名）、`KAZ_INSTALL_DIR`（PowerShell）、テスト用の `KAZ_DOWNLOAD_BASE`・`KAZ_API_BASE`。`JFS_*` はやめる。
- 既定のインストール先: Linux は `/usr/local/bin/<製品名>`（JuiceFS・rclone とも）。Windows は `%LOCALAPPDATA%\Programs\<製品名>\<製品名>.exe`（PATH には追加しない）。

**最新の版の探し方**:
- GitHub API（`/repos/tongsama/juicefs_inspection/releases?per_page=100`）で一覧を取り、タグの接頭辞で絞る（`rclone-v`、`juicefs-v`。JuiceFS は古い形 `v<版>-kaz.<n>` も含める）。版の番号（ベースの版、次に `kaz.<n>`）が最も大きいものを選ぶ。draft は認証なしの API には出ない。prerelease は使わない運用とする。
- jq が無いホストでも動くよう、JSON は sh と awk だけで読む。PowerShell は `Invoke-RestMethod` を使う。
- API の回数制限（認証なしで1時間60回）や API の失敗のときは、その旨と「`KAZ_VERSION` で版を指定すれば API を使わない」ことを表示して止める。

**ダウンロードと置き換え**（今の方式を共通化）:
- asset: `<製品名>-linux-amd64.tar.gz`・`-linux-arm64.tar.gz`（中身は `<製品名>` の1ファイル）、`<製品名>-windows-amd64.zip`（中身は `<製品名>.exe`）、`checksums.txt`。
- `checksums.txt` の SHA-256 と一致したときだけ、同じディレクトリの一時ファイルから1回の `mv` で置き換える。失敗したら既存のファイルに触れない。
- install スクリプトは、各 Release にも添付する（その版のときのスクリプトを後から再現できるように）。

**rclone 向けの表示**:
- インストール後、PATH 上で先に見つかる別の `rclone` があれば警告する（例: `/usr/bin/rclone` を直接指定する起動スクリプト）。置き換えたい場所は引数で指定してもらう。
- 実行中の rclone は止めずに置き換えるので、反映には再起動が要ることを表示する。
- Windows では、mount に WinFsp が要ることを表示する（自動では入れない。JuiceFS と同じ）。

## 5. workflow とテスト（第3節）

### 5.1 `release-rclone.yml`（新規）

- 起動: `rclone-v*` のタグの push と、手動実行（タグを指定。Release は作らず、ビルドと確認だけ）。
- resolve: タグの形（`^rclone-v([0-9]+\.[0-9]+\.[0-9]+)-kaz\.([0-9]+)$`）、`release/rclone/versions.json` の項目、`repo` にその commit があること、リリースノートがあることを確かめる。版名 `v<版>-kaz.<n>` を後続へ渡す。
- build: `repo` をその commit で checkout し、`go_version` の Go で、公式と同じ `bin/cross-compile.go`（`-tags cmount`）で linux-amd64・linux-arm64・windows-amd64 をビルドする。出力を §4 の asset の名前にまとめる（`cross-compile.go` の出力先と `-compile-only` の挙動は実装計画で確かめる）。確認: `rclone version` の1行目が `rclone v<版>-kaz.<n>`、Linux 版が静的リンク、`serve s3 --help` に kaz のオプションが出る。arm64 版は arm の runner で起動して確かめる。
- verify-windows: Windows の runner で、手元の HTTP サーバーから install.ps1 で入れ、`rclone version` が期待どおりで `go/tags: cmount` を含むこと、SHA-256 が合わないと失敗して既存のファイルが変わらないことを確かめる。
- release: すべて成功したときだけ、タグの push で draft の Release を作る（`--latest=false`、タイトル `rclone v<版>-kaz.<n>`、asset 3つと `checksums.txt`、install スクリプト2つ、ノートの末尾にビルド元の commit と Go の版）。同じタグの Release があれば止める。

### 5.2 `release-juicefs.yml`（今の `release.yml` を移す）

- タグの形を `^juicefs-v([0-9]+\.[0-9]+\.[0-9]+)-kaz\.([0-9]+)$` にし、対応表の場所と項目名を §3 に合わせる。install.ps1 での確認を新しい環境変数に合わせる。`--latest=false`、タイトル `JuiceFS v<版>-kaz.<n>`。
- 手動実行では、古い形のキー（`v1.4.1-kaz.3`）も受け付ける（作り直した workflow を既存の版で試すため。手動実行では Release を作らない）。
- ビルドの方法（musl の静的リンク、Windows は mingw と WinFsp のヘッダー）と `juicefs version` の形は今のまま。

### 5.3 テスト

- `release/tests/test_install.sh` を広げる。手元の HTTP サーバーに偽の API の応答と偽の配布物を置き、次を確かめる:
  - 製品ごとに最新を選ぶ（JuiceFS は古い形のタグも含めて選ぶ）。
  - `KAZ_VERSION` で版を固定でき、API を使わない。
  - 製品名が無い・不明ならエラー。
  - API の回数制限・失敗のとき、分かりやすく止まる。
  - チェックサムが合わないと失敗し、既存のファイルが変わらない。
  - 先に見つかる別の rclone があると警告が出る。
- `install-tests.yml`: `release/` や workflow が変わった push で、`test_install.sh` を流す。
- install.ps1 は、各製品の workflow の verify-windows で確かめる（今と同じ）。

## 6. 移行と最初の Release

1. 実装と、手元でのテスト（`test_install.sh`、workflow の構文の確認）。
2. ユーザーの確認のうえで push し、両方の workflow を手動実行で試す（JuiceFS は `v1.4.1-kaz.3`、rclone は `rclone-v1.75.1-kaz.1`。どちらも Release は作らない）。
3. ユーザーの確認のうえで `rclone-v1.75.1-kaz.1` のタグを push し、draft の Release を作る。中身を確かめてからユーザーが公開する。公開後、install スクリプトで実際に入れて確かめる。
4. README の配布とインストールの節、「新しい版を出す手順」を書き直す。前の設計（2026-10-04）には、本設計で置き換えたことを追記する。

JuiceFS の新しい Release は今回は出さない。次に JuiceFS を直したときに `juicefs-v1.4.1-kaz.4` として出す。

## 7. 範囲外

- macOS・32bit ARM などの対象の追加。
- rclone の Windows 版で、WinFsp を自動でインストールすること。
- 署名（コード署名・GPG）。今の JuiceFS の配布と同じく、SHA-256 の照合だけ。
