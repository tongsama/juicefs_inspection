# 改修版 JuiceFS バイナリ配布の仕様 — inspection repo からの Release と install スクリプト

> 2026-10-08 の [2製品対応の設計](2026-10-08-release-multi-product-design.md) で、配置・タグ・install スクリプトの使い方を置き換えた。以下は当時の記録である。

> 対象は調査ルート repo（`tongsama/juicefs_inspection`）です。本体（`tongsama/juicefs`）はソースを読み取るだけで、ファイルもタグも追加しません。

## 目的と前提

改修版 JuiceFS のビルド済みバイナリを GitHub Releases に置き、よくある `curl -fsSL … | sh` の1行でインストールできるようにする。

- 本体 repo はなるべく公式と同じ形に保つ。配布用のスクリプト、workflow、版の対応表、リリースノートは、すべて inspection repo に置く。
- 両 repo とも公開 repo（2026-10-04 に確認）。そのため、curl も Actions での本体 checkout も認証なしで行える。
- inspection repo の Releases は、この JuiceFS 配布専用に使う。`releases/latest` は常に最新の配布版を指す。
- 本番ホストへの適用は利用者が行う。この仕組みは、稼働中の mount を止めたり再起動したりしない。

## 配布対象

| 対象 | 添付ファイル | ビルド方法 |
|---|---|---|
| linux-amd64 | `juicefs-linux-amd64.tar.gz` | `ubuntu-24.04` runner。musl で静的リンク |
| linux-arm64（= aarch64） | `juicefs-linux-arm64.tar.gz` | `ubuntu-24.04-arm` runner（ネイティブ）。musl で静的リンク |
| windows-amd64 | `juicefs-windows-amd64.zip` | `ubuntu-24.04` 上で mingw-w64 を使い、本体の `hack/winfsp_headers` でクロスビルド。Makefile の `juicefs.exe` と同じ方式 |

- tar.gz は先頭階層に `juicefs` を1つだけ、zip は `juicefs.exe` を1つだけ含める。install スクリプトはこの構成を前提にする。
- 添付ファイル名には版を含めない。`releases/latest/download/<名前>` が固定 URL になるようにするため。
- ビルド対象は workflow の matrix で一覧として持つ。後から1行で追加できるようにする。
- **armv7（32bit）は今回の対象外。** 2026-10-04 の試しビルドでは、`davies/groupcache/consistenthash`（`pkg/chunk/disk_cache.go` が使用）と `tikv/client-go` で int のオーバーフローによりコンパイルできなかった。対応するには本体の依存を差し替える必要がある。upstream も 32bit を配布しておらず、32bit での整合性は検証されていない。対応するときは、別途の設計と 32bit でのテストを前提にする。
- darwin は対象外。install.sh は darwin で実行されたとき、未対応として終了する。

## 版の管理

- タグは `v<upstreamの版>-kaz.<連番>` とする（例：`v1.4.1-kaz.1`）。upstream の土台が変わったら、連番を 1 から振り直す。
- `juicefs version` の表示は `juicefs version <upstreamの版>+<本体commitの日付>.<本体commitの先頭8桁>-kaz.<連番>` とする（例：`juicefs version 1.4.1+2026-10-03.84f19ca4-kaz.1`）。
  - 本体の `pkg/version` で ldflags の `-X` から設定できるのは文字列の `revision` と `revisionDate` だけで、`1.4.1` の部分はコード内の固定値である。そのため本体を変更せず、`revision=<先頭8桁>-kaz.<連番>`、`revisionDate=<本体commitの日付>`（Makefile と同じ `git log -1 --format=%cd --date=short`）を設定する。
  - `-kaz.<連番>` を semver の pre-release 部分に入れないことで、metadata 側のクライアント版の比較（pre-release は正式版より古いと判定される）に影響しない。これまでの手元ビルド（`1.4.1+2026-10-02.3bed0d82-vm-io-gc-retire-local`）と同じ形でもある。

### `release/versions.json`

タグから本体の commit を引くための唯一の対応表。

```json
{
  "v1.4.1-kaz.1": {
    "juicefs_repo": "tongsama/juicefs",
    "juicefs_commit": "84f19ca43f77c8e52eaffc2955a42bd9ac1d4c9b",
    "go_version": "1.25.11",
    "notes": "docs/release-notes/v1.4.1-kaz.1.md"
  }
}
```

- `juicefs_commit` は 40 桁のフル SHA で書く。後から動くブランチ名は使わない。
- `go_version` は upstream の CI と同じ 1.25 系に固定する（Go 1.26 では、mockey の問題などで既知の差異がある）。
- `notes` のリリースノートは inspection repo 内に置く。workflow はこれを Release の本文に使う。

## Release の作成手順

1. 手元で `versions.json` に1件追加し、リリースノートを書いてコミットする。使う commit は、手元でテスト済みのものに限る。
2. inspection repo にタグを打ち、`git push original <tag>` で送る。
3. `.github/workflows/release.yml` が次の順に動く。
   1. **検証**：タグが `versions.json` にあること、`juicefs_commit` が 40 桁であり `juicefs_repo` 上に実在すること、`notes` のファイルが存在することを確認する。どれかが満たされなければ、ビルドを始めずに失敗させる。
   2. **ビルド**（matrix で並行）：本体をその SHA で checkout し、指定の Go でビルドする。
      - linux は、`file` で静的リンクであることを確認し、`juicefs version` を実行して期待したバージョン文字列が出ることを確認する（amd64・arm64 ともネイティブ runner で実行）。
      - windows は、`file` で PE32+ の x86-64 実行形式であることだけを確認する。
      - tar.gz / zip にまとめ、Actions の成果物として次の工程へ渡す。
   3. **install.ps1 の確認**：`windows-latest` で、ビルドした zip と、それから作った `checksums.txt` を `python -m http.server` で配信し、`JFS_DOWNLOAD_BASE` をそこへ向けて install.ps1 を実行し、展開と `juicefs.exe version` の実行が成功することを確認する。
   4. **公開準備**：すべての job が成功したときだけ、同じタグの Release（draft を含む）がまだ無いことを書き込み権限で確認したうえで、`checksums.txt`（sha256）を作り、6 ファイル（3 バイナリ、`checksums.txt`、`install.sh`、`install.ps1`）を**下書き（draft）の Release** に添付する。Release の本文にはリリースノートと本体の commit を書く。
4. ユーザーが draft の中身を確認し、GitHub の画面で公開する。draft は `releases/latest` に出ない。

### 手動実行モード

`workflow_dispatch` の入力にタグ名を受け取り、検証とビルドと install.ps1 の確認だけを行う。Release は作らない。成果物は Actions の一時保存に残す。workflow の初回の調整や、本番のタグを打つ前の確認に使う。手動実行では「同じタグの Release がまだ無い」という確認は行わない。

### workflow でやらないこと

- 本体のテストは実行しない（手元でテスト済みの commit を使う前提）。
- 本体 repo への書き込みはしない（タグ、ブランチ、ファイルとも）。
- 既存の Release の上書きや削除はしない。

## install.sh（Linux）

```bash
curl -fsSL https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.sh | sh
curl -fsSL https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.sh | JFS_VERSION=v1.4.1-kaz.1 sh -s /opt/bin
```

- POSIX sh で書く。配布するスクリプト（install.sh / install.ps1）は、文字コードの問題を避けるため ASCII だけで書く（メッセージとコメントは英語）。`set -eu` で実行し、一時ディレクトリは `trap` で必ず削除する。
- **対応環境の判定**：`uname -s` が Linux 以外なら終了する。`uname -m` が `x86_64` なら amd64、`aarch64` / `arm64` なら arm64 とする。それ以外は、対応している組み合わせを表示して終了する。
- **入力**
  - 第1引数：インストール先のディレクトリ（既定は `/usr/local/bin`）
  - `JFS_VERSION`：タグ（既定は latest）
  - `JFS_INSTALL_NAME`：インストールするファイル名（既定は `juicefs`）
  - `JFS_DOWNLOAD_BASE`：ダウンロード元の URL の土台（テスト用。既定は `https://github.com/tongsama/juicefs_inspection/releases`）
- **取得先**：版の指定が無ければ `<base>/latest/download/<file>`、指定があれば `<base>/download/<tag>/<file>`。`curl -fsSL --retry 3` で取得する。404（版または添付ファイルが無い）と、それ以外のネットワークの失敗は、メッセージを分ける。
- **照合**：`checksums.txt` の該当する行と sha256 を比べる。`sha256sum` が無ければ `shasum -a 256` を使い、どちらも無ければ終了する。不一致や該当行が無い場合も終了する。
- **配置**
  - 展開して得た `juicefs` を、インストール先と同じディレクトリに一時ファイルとして置き、`chmod 0755` を付けてから `mv` で入れ替える。
  - インストール先に書き込めなければ sudo を使う。sudo も無い場合は終了する。
  - **それより前の工程で失敗した場合、既存のファイルには触れない。**
  - 既存のファイルがあれば、入れ替える前に `version` を実行して旧版として表示し、入れ替えた後に新版を表示する。
- 最後のメッセージで、稼働中の mount は再起動しないこと、ただし mount の supervisor が子プロセスの異常終了後に自動で再起動した場合（juicefs の `cmd/mount_unix.go` は起動時に取得した実行ファイルのパスで子プロセスを起動し直す）と、新しく mount した場合は新版が使われることを伝える。

## install.ps1（Windows）

```powershell
irm https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.ps1 | iex
```

- `juicefs-windows-amd64.zip` と `checksums.txt` を取得し、`Get-FileHash -Algorithm SHA256` で照合する。不一致なら何も配置せずに終了する。
- 展開先は既定で `%LOCALAPPDATA%\Programs\juicefs` とする。`irm | iex` では引数を渡せないため、展開先の変更、版の指定、テストでのダウンロード元の差し替えは、環境変数 `JFS_INSTALL_DIR`、`JFS_VERSION`、`JFS_DOWNLOAD_BASE` で行う（後の2つは install.sh と同じ意味）。
- `Invoke-Expression` で実行されるため、エラーは `exit` ではなく `throw` で返す（`exit` は利用者の PowerShell セッションごと閉じてしまう）。
- 実行中の `juicefs.exe` は置き換えられないため、置き換えに失敗したときは、juicefs を止めてから再実行するよう案内して失敗させる。
- **PATH には追加しない。** インストール先のパスを表示する。
- **WinFsp は自動でインストールしない。** 見つからない場合は、mount に必要であることと入手先を警告として表示する（インストール自体は失敗にしない）。
- エラーで止まるように `$ErrorActionPreference = 'Stop'` を設定し、一時ファイルは `finally` で削除する。

## テスト

- **install.sh（手元）**：`release/tests/test_install.sh` を用意する。一時ディレクトリに偽の tar.gz と `checksums.txt` を置き、`python3 -m http.server` で配信して、`JFS_DOWNLOAD_BASE` を向ける。次のケースを確認する。
  - 正常なインストール（latest）
  - 版の固定（`download/<tag>/` の URL を使うこと）
  - インストール先と名前の指定
  - sha256 の不一致で中止し、既存のファイルが変わらないこと
  - 404 で中止すること
  - 対応していない OS やアーキテクチャ（`uname` を差し替える PATH の細工で確認）
  - あわせて `shellcheck` をかける。
- **install.ps1**：上記の workflow の Windows job で確認する（手元に Windows が無いため）。
- **workflow**：手元に `actionlint` があれば事前にかける。初回は手動実行モードで3種類のビルドと確認を通し、その後に本番のタグを打つ。

## ファイル構成（inspection repo）

```
release/
├─ install.sh
├─ install.ps1
├─ versions.json
└─ tests/test_install.sh
docs/release-notes/<tag>.md
.github/workflows/release.yml
```

## 対象外

- armv7、darwin
- 本体のテストを CI で実行すること
- コードへの署名（Windows の Authenticode、cosign など）と、パッケージマネージャー向けの配布（deb/rpm、winget など）
- WinFsp の自動インストールと、Windows での PATH への追加
