# 配布を JuiceFS と rclone の2製品に対応させる（Phase 3）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** このリポジトリの GitHub Releases から、JuiceFS と rclone の改修版を、製品ごとに独立した版で配布し、共通の install スクリプトで各ホスト（Linux・Windows）に入れられるようにする。

**Architecture:** タグを製品ごとに分け（`juicefs-v…-kaz.N`、`rclone-v…-kaz.N`）、workflow も製品ごとに分ける。install スクリプトは製品名を受け取り、GitHub API で製品ごとの最新を探す（GitHub の latest は使わない）。rclone は公式と同じ `bin/cross-compile.go -tags cmount` でビルドする。

**Tech Stack:** POSIX sh・awk（install.sh）、PowerShell 5.1／7（install.ps1）、bash（テスト）、GitHub Actions（ubuntu-24.04、ubuntu-24.04-arm、windows-latest）、Go（rclone は 1.26.0、JuiceFS は 1.25.11）、gh CLI。

**Spec:** [docs/superpowers/specs/2026-10-08-release-multi-product-design.md](../specs/2026-10-08-release-multi-product-design.md)

## Global Constraints

- 作業はこのリポジトリ（`tongsama/juicefs_inspection`、ルート）で、ブランチ `feat/release-multi-product`（`main` から）。本体のリポジトリ（`juicefs/`、`rclone/`）は読むだけで変更しない。
- commit はタスクごとに行ってよい（計画の承認時にユーザーに確認する）。push・タグ・workflow の手動実行・Release の作成は、Task 8・9 でユーザーの確認を取ってから行う。commit メッセージに attribution 行を付けない。commit メッセージは日本語（このリポジトリの慣習）。
- タグの形: JuiceFS は `juicefs-v<x.y.z>-kaz.<n>`、rclone は `rclone-v<x.y.z>-kaz.<n>`。JuiceFS の古い形 `v<x.y.z>-kaz.<n>` は残し、install スクリプトは JuiceFS として扱う。
- asset の名前: `<製品>-linux-amd64.tar.gz`、`<製品>-linux-arm64.tar.gz`（中身は `<製品>` の1ファイル）、`<製品>-windows-amd64.zip`（中身は `<製品>.exe`）、`checksums.txt`、`install.sh`、`install.ps1`。
- install スクリプトの環境変数は `KAZ_PRODUCT`（PowerShell）、`KAZ_VERSION`、`KAZ_INSTALL_NAME`、`KAZ_INSTALL_DIR`（PowerShell）、`KAZ_DOWNLOAD_BASE`、`KAZ_API_BASE`。既定のインストール先は Linux が `/usr/local/bin`、Windows が `%LOCALAPPDATA%\Programs\<製品>`。製品名は必須。
- SHA-256 の照合に通ったときだけ、一時ファイルから1回の `mv`（Move-Item）で置き換える。失敗したら既存のファイルに触れない。
- Release は draft、`--latest=false`、同じタグの Release があれば止める。公開はユーザー。
- install スクリプトは ASCII のみ（今のテストと同じ）。新規・変更する関数にはコメントを付ける。workflow とテストのコメントは日本語でよい（今と同じ）。

## Review Focus

1. API の一覧で `kaz.10` と `kaz.2` が並ぶ → 数値として比べて `kaz.10` を最新に選ぶこと（Task 2 のテスト 1）。
2. `rclone-v…` と `juicefs-v…`・古い形 `v…` が混ざった一覧 → 製品を取り違えないこと（Task 2 のテスト 1・2）。
3. API が 403（回数制限）を返す → `KAZ_VERSION` を案内して止まり、何も書き換えないこと（Task 2 のテスト 5）。
4. `/usr/bin/rclone` など別の場所に同名のファイルがある → 警告が出ること（Task 2 のテスト 14）。
5. Windows で実行中の rclone.exe を置き換えようとする → 分かりやすいエラーで止まり、既存のファイルが残ること（Task 3 のコード。Windows runner では再現しないので、レビューで確かめる）。

## ファイル構成

| ファイル | 種別 | 責務 |
|---|---|---|
| `release/juicefs/versions.json` | 移動・変更 | 今の `release/versions.json`。項目名を `repo`・`commit` に |
| `release/juicefs/notes/*.md` | 移動 | 今の `docs/release-notes/*.md` |
| `release/rclone/versions.json`、`release/rclone/notes/rclone-v1.75.1-kaz.1.md` | 新規 | rclone の版の対応表と最初のリリースノート |
| `release/install.sh` | 書き直し | 共通の install スクリプト（Linux） |
| `release/install.ps1` | 書き直し | 共通の install スクリプト（Windows） |
| `release/tests/test_install.sh` | 書き直し | install.sh のテスト |
| `release/tests/server.py` | 新規 | テスト用の HTTP サーバー（ファイル配信と、指定パスでの 403） |
| `.github/workflows/release-juicefs.yml` | 移動・変更 | 今の `release.yml` |
| `.github/workflows/release-rclone.yml` | 新規 | rclone のビルドと draft Release |
| `.github/workflows/install-tests.yml` | 新規 | install スクリプトのテストを流す |
| `README.md`、`docs/superpowers/specs/2026-10-04-release-distribution.md`、`docs/superpowers/README.md` | 変更 | 使い方・手順・置き換えの注記 |

---

### Task 0: ブランチと基準

- [ ] **Step 1:** `git switch main && git status --short && git switch -c feat/release-multi-product`。Expected: 作業ツリーが空。
- [ ] **Step 2:** `bash release/tests/test_install.sh`（root 以外で）。Expected: `passed=13 failed=0` 前後（今のテストがすべて通る）。
- [ ] **Step 3:** actionlint が動くか確かめる: `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 .github/workflows/release.yml`。Expected: 指摘なし（または既存の指摘を記録）。ネットワークで取得できなければユーザーに報告し、以降の YAML の確認は `python3 -c 'import json,sys'` ではなく目視とする。

---

### Task 1: 配置の移動と rclone の対応表

**Files:** 移動 `release/versions.json` → `release/juicefs/versions.json`、`docs/release-notes/*.md` → `release/juicefs/notes/`。新規 `release/rclone/versions.json`、`release/rclone/notes/rclone-v1.75.1-kaz.1.md`。

- [ ] **Step 1: 移動**

```bash
mkdir -p release/juicefs release/rclone/notes
git mv release/versions.json release/juicefs/versions.json
git mv docs/release-notes release/juicefs/notes
```

- [ ] **Step 2: 項目名とノートのパスを変える** — `release/juicefs/versions.json` の各項目で `juicefs_repo` → `repo`、`juicefs_commit` → `commit`、`notes` を `release/juicefs/notes/<タグ>.md` にする。キー（`v1.4.1-kaz.1` など）は変えない。`jq . release/juicefs/versions.json` で形を確かめる。

- [ ] **Step 3: rclone の対応表** `release/rclone/versions.json`:

```json
{
  "rclone-v1.75.1-kaz.1": {
    "repo": "tongsama/rclone",
    "commit": "dab33da31190b350cf75214080b362e0a50584d0",
    "go_version": "1.26.0",
    "notes": "release/rclone/notes/rclone-v1.75.1-kaz.1.md"
  }
}
```

- [ ] **Step 4: リリースノート** `release/rclone/notes/rclone-v1.75.1-kaz.1.md`（日本語）。内容: rclone v1.75.1 ベースの改修版の最初の版。Phase 1（`--kaz-vfs-lookup-by-path`、404 は「無い」ときだけ、DELETE が他ホストの object も消す、`--no-cleanup` の配線）と Phase 2（`--kaz-s3-persist-metadata` と `--drive-kaz-properties` は必ず一緒に、`X-Amz-Meta-*` を Drive の properties `s3m-*` に保存）の要点、本番での使い方（起動オプションの例）、既知の制約（他ホストが同じ key を上書きしたときのキャッシュ、Drive の同名フォルダの重複は範囲外）、仕様と手順書へのリンク（`docs/superpowers/specs/2026-10-08-rclone-lookup-by-path-design.md`、`…-s3-persist-metadata-design.md`、`rclone_dir_cache/2026-10-08/deploy-runbook-ja.md`）。Windows 版は `rclone mount` に WinFsp が必要。

- [ ] **Step 5: commit** — `git add -A release docs/release-notes && git commit -m "release: 版の対応表とリリースノートを製品ごとの配置に移し、rclone の最初の版を登録"`

---

### Task 2: 共通の install.sh とテスト

**Files:** 書き直し `release/install.sh`、`release/tests/test_install.sh`。新規 `release/tests/server.py`。

- [ ] **Step 1: テスト用サーバー** `release/tests/server.py`:

```python
#!/usr/bin/env python3
"""Serve a directory over HTTP for the install.sh tests.

Paths starting with /ratelimit/ answer 403 like the GitHub API when its
unauthenticated rate limit is used up; query strings are ignored.
"""
import functools
import http.server
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, plus a forced 403 under /ratelimit/."""

    def do_GET(self):
        if self.path.startswith("/ratelimit/"):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"message":"API rate limit exceeded"}')
            return
        self.path = self.path.split("?", 1)[0]
        super().do_GET()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port, root = int(sys.argv[1]), sys.argv[2]
    handler = functools.partial(Handler, directory=root)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
```

- [ ] **Step 2: 失敗するテスト** — `release/tests/test_install.sh` を書き直す。今のファイルの枠組み（`ok`/`ng`、`cleanup`、偽の `uname`、`FAKEBIN`、root では一部をスキップ）を残し、次のように変える。

偽の配布物と API の応答:

```bash
# make_release は、ディレクトリ $1 に製品 $2・版 $3 の偽配布物（amd64/arm64）と checksums.txt を作る。
# 偽のバイナリは「<製品> version <版> <target>」を表示するだけのシェルスクリプト。
make_release() {
  local dir=$1 prod=$2 ver=$3 target b
  mkdir -p "$dir"
  for target in linux-amd64 linux-arm64; do
    b="$T/build/$prod/$ver/$target"
    mkdir -p "$b"
    printf '#!/bin/sh\necho "%s version %s %s"\n' "$prod" "$ver" "$target" > "$b/$prod"
    chmod 0755 "$b/$prod"
    tar -czf "$dir/$prod-$target.tar.gz" -C "$b" "$prod"
  done
  (cd "$dir" && sha256sum "$prod-linux-amd64.tar.gz" "$prod-linux-arm64.tar.gz" > checksums.txt)
}

# make_api は、パス $1（サーバーのルートからの相対）に、タグ $2... を持つ Release 一覧の JSON を置く。
make_api() {
  local p="$SRV/$1"; shift
  mkdir -p "$(dirname "$p")"
  { printf '['; local first=1 t
    for t in "$@"; do
      [ $first -eq 1 ] || printf ','
      first=0
      printf '{"url":"x","tag_name":"%s","draft":false,"prerelease":false,"assets":[{"name":"a"}]}' "$t"
    done
    printf ']\n'; } > "$p"
}
```

配信するもの:

```bash
for t in rclone-v1.75.1-kaz.2 rclone-v1.75.1-kaz.10; do make_release "$SRV/download/$t" rclone "$t"; done
for t in juicefs-v1.4.1-kaz.4 v1.4.1-kaz.3; do make_release "$SRV/download/$t" juicefs "$t"; done
make_release "$SRV/download/rclone-v1.75.1-kaz.bad" rclone rclone-v1.75.1-kaz.bad
sed -i '1s/^[0-9a-f]\{64\}/0000000000000000000000000000000000000000000000000000000000000000/' "$SRV/download/rclone-v1.75.1-kaz.bad/checksums.txt"
make_release "$SRV/download/rclone-v1.75.1-kaz.nosum" rclone rclone-v1.75.1-kaz.nosum
: > "$SRV/download/rclone-v1.75.1-kaz.nosum/checksums.txt"
# 新旧が混ざった一覧（API の並びは新しい順とは限らない）
make_api api/releases rclone-v1.75.1-kaz.2 juicefs-v1.4.1-kaz.4 v1.4.1-kaz.3 rclone-v1.75.1-kaz.10 v1.4.1-kaz.1
# 古い形だけの一覧
make_api legacy/releases v1.4.1-kaz.1 v1.4.1-kaz.3 v1.4.1-kaz.2
python3 "$ROOT/release/tests/server.py" "$PORT" "$SRV" > "$T/server.log" 2>&1 &
SERVER_PID=$!
```

`run_install` は製品とインストール先を受け取る:

```bash
# run_install は、偽の uname とテスト用の配信元・API を使って install.sh を実行する。
# 使い方: run_install <ログ> [VAR=値 ...] -- <製品> <インストール先>
run_install() {
  local log=$1; shift
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  env PATH="$FAKEBIN:$PATH" KAZ_DOWNLOAD_BASE="$BASE" KAZ_API_BASE="$BASE/api" "${envs[@]}" sh "$INSTALL_SH" "$@" > "$log" 2>&1
}
```

（`envs` が空のとき `set -u` の bash で `"${envs[@]}"` が問題にならないことを確かめる。bash 4.4 以降なら問題ない。）

テストケース（それぞれ `ok`/`ng` で記録し、失敗時はログを表示する）:

1. rclone の最新: `run_install c1.log -- rclone "$d"` → `"$d/rclone"` の出力に `rclone-v1.75.1-kaz.10 linux-amd64`（`kaz.10` を数値として最大に選ぶ）。
2. juicefs の最新（新旧が混在）: `-- juicefs "$d"` → `juicefs-v1.4.1-kaz.4`。
3. juicefs の最新（古い形だけ）: `KAZ_API_BASE="$BASE/legacy" -- juicefs "$d"` → `v1.4.1-kaz.3`。
4. 版の固定は API を使わない: `KAZ_VERSION=rclone-v1.75.1-kaz.2 KAZ_API_BASE="$BASE/noapi" -- rclone "$d"` → 成功し `kaz.2`（`/noapi` は 404 なので、使えば失敗する）。
5. API の回数制限: `KAZ_API_BASE="$BASE/ratelimit" -- rclone "$d"`（`$d` に既存のファイル）→ 失敗、ログに `rate limit` と `KAZ_VERSION` を含み、既存のファイルは変わらない。
6. API の他の失敗: `KAZ_API_BASE="$BASE/noapi" -- rclone "$d"` → 失敗、ログに `GitHub API request failed (HTTP 404)`。
7. 製品名なし: `-- ` のあとに何も渡さない → 失敗、ログに `usage:`。不明な製品 `-- s3fs "$d"` → 失敗、ログに `unknown product: s3fs`。
8. 名前の指定と新しいディレクトリ: `KAZ_INSTALL_NAME=rclone-kaz -- rclone "$T/c8/sub/bin"` → `rclone-kaz` があり `rclone` は無い。
9. arm64: `FAKE_UNAME_M=aarch64 -- rclone "$d"` → `linux-arm64`。
10. チェックサムの不一致: `KAZ_VERSION=rclone-v1.75.1-kaz.bad -- rclone "$d"`（既存のファイルあり）→ 失敗、`checksum mismatch`、既存のファイルは同じ。
11. 存在しない版: `KAZ_VERSION=rclone-v9.9.9-kaz.9 -- rclone "$d"` → 失敗、`not found (404)`、何も作らない。
12. 対応していないアーキテクチャ・OS（`armv7l`、`Darwin`）→ 今と同じメッセージ。
13. 既存の置き換え: 旧版を表示する偽の `rclone` を置いてから最新を入れる → ログに `OLD` と新しい版、ディレクトリには `rclone` だけ（一時ファイルが残らない）。
14. 別の場所の同名ファイル: `FAKEBIN2` に偽の `rclone` を置き、`PATH="$FAKEBIN2:…"` で `-- rclone "$d"` → ログに `another rclone` と `$FAKEBIN2/rclone` を含む。
15. 書き込めず sudo も無い（root 以外のときだけ）→ 今と同じ（`sudo is not available`、既存のファイルは同じ）。
16. checksums.txt に行が無い: `KAZ_VERSION=rclone-v1.75.1-kaz.nosum` → `no checksum entry`。
17. 再起動の案内: rclone のログに `restart`、juicefs のログに `auto-restart`。
18. install.sh は ASCII のみ。

- [ ] **Step 3: 失敗を確かめる** — `bash release/tests/test_install.sh`。Expected: ほとんどが FAIL（今の install.sh は製品名を受け取らない）。

- [ ] **Step 4: install.sh を書き直す** — 今のファイルの構造（`log`、`die`、`cleanup`、`need`、`detect_target`、`sha256_of`、`use_sudo`、置き換え）を保ち、次を変える。

先頭のコメントと引数・環境変数:

```sh
#!/bin/sh
# install.sh - download, verify and install a patched build (juicefs or
# rclone) published on the tongsama/juicefs_inspection GitHub Releases.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/tongsama/juicefs_inspection/main/release/install.sh | sh -s -- <product> [install-dir]
#   curl -fsSL .../install.sh | KAZ_VERSION=rclone-v1.75.1-kaz.1 sh -s -- rclone
#
# Arguments:
#   $1  product: juicefs or rclone (required)
#   $2  install directory (default: /usr/local/bin)
# Environment:
#   KAZ_VERSION        release tag to install (default: the newest release of the product)
#   KAZ_INSTALL_NAME   installed file name (default: the product name)
#   KAZ_DOWNLOAD_BASE  releases base URL (default: this repository; used by tests)
#   KAZ_API_BASE       GitHub API URL of this repository (default; used by tests)
#
# The newest release is looked up per product with the GitHub API (the
# repository-wide "latest" release is not used). The existing binary is
# replaced by a single mv only after the download and the SHA-256
# verification succeed.
set -eu

REPO="tongsama/juicefs_inspection"
PRODUCT="${1:-}"
INSTALL_DIR="${2:-/usr/local/bin}"
VERSION="${KAZ_VERSION:-}"
BASE="${KAZ_DOWNLOAD_BASE:-https://github.com/$REPO/releases}"
BASE="${BASE%/}"
API="${KAZ_API_BASE:-https://api.github.com/repos/$REPO}"
API="${API%/}"
```

製品の確認（`need curl` などの前）:

```sh
case "$PRODUCT" in
    juicefs|rclone) ;;
    "") die "usage: install.sh <juicefs|rclone> [install-dir]" ;;
    *) die "unknown product: $PRODUCT (expected juicefs or rclone)" ;;
esac
NAME="${KAZ_INSTALL_NAME:-$PRODUCT}"
```

`fetch` は用途で 404 の文言を分ける:

```sh
# fetch downloads URL $1 into file $2. $3 is "asset" or "api" and selects
# the error message for a 404 or other HTTP failure.
fetch() {
    if ! code=$(curl -sSL --retry 3 -o "$2" -w '%{http_code}' "$1"); then
        die "download failed (network error): $1"
    fi
    case "$3:$code" in
        *:200) ;;
        asset:404) die "not found (404): $1 -- check that release '$VERSION' and its assets exist" ;;
        api:403|api:429) die "GitHub API rate limit reached ($1). Set KAZ_VERSION=<tag> to install a given release without the API" ;;
        api:*) die "GitHub API request failed (HTTP $code): $1. Set KAZ_VERSION=<tag> to install a given release without the API" ;;
        *) die "download failed (HTTP $code): $1" ;;
    esac
}
```

製品ごとの最新:

```sh
# version_key prints "<major> <minor> <patch> <kaz>" for a release tag of
# PRODUCT ($1), or nothing for any other tag. JuiceFS also accepts the
# older form v<x.y.z>-kaz.<n>.
version_key() {
    printf '%s\n' "$1" | awk -v p="$PRODUCT" '
        {
            t = $0
            if (index(t, p "-v") == 1) t = substr(t, length(p) + 3)
            else if (p == "juicefs" && t ~ /^v[0-9]/) t = substr(t, 2)
            else exit
            if (t !~ /^[0-9]+\.[0-9]+\.[0-9]+-kaz\.[0-9]+$/) exit
            split(t, a, "-kaz.")
            split(a[1], v, ".")
            printf "%d %d %d %d\n", v[1], v[2], v[3], a[2]
        }'
}

# latest_tag prints the newest release tag of PRODUCT found with the API.
latest_tag() {
    fetch "$API/releases?per_page=100" "$WORK/releases.json" api
    best=""
    bestkey=""
    for t in $(tr ',' '\n' < "$WORK/releases.json" | sed -n 's/^[[:space:][{]*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'); do
        k=$(version_key "$t")
        [ -n "$k" ] || continue
        if [ -z "$bestkey" ] || [ "$(printf '%s\n%s\n' "$bestkey" "$k" | sort -k1,1n -k2,2n -k3,3n -k4,4n | tail -n 1)" != "$bestkey" ]; then
            best=$t
            bestkey=$k
        fi
    done
    [ -n "$best" ] || die "no $PRODUCT release found ($API/releases)"
    printf '%s\n' "$best"
}
```

本体の流れ（`WORK=$(mktemp -d)` の後）:

```sh
if [ -z "$VERSION" ]; then
    VERSION=$(latest_tag)
    log "newest $PRODUCT release: $VERSION"
fi
TARGET=$(detect_target)
ASSET="$PRODUCT-$TARGET.tar.gz"
URL="$BASE/download/$VERSION"
log "downloading $ASSET ($VERSION) from $URL"
fetch "$URL/$ASSET" "$WORK/$ASSET" asset
fetch "$URL/checksums.txt" "$WORK/checksums.txt" asset
```

（`latest_tag` は `$(...)` の中で動くので、その中の `die` はサブシェルだけを終わらせる。`VERSION=$(latest_tag)` を `if ! VERSION=$(latest_tag); then exit 1; fi` にして、エラー時に止まるようにする。メッセージは stderr に出ているのでそのまま見える。）

展開は `tar -xzf "$WORK/$ASSET" -C "$WORK/x" "$PRODUCT"`、存在確認は `$WORK/x/$PRODUCT`。置き換えのあと:

```sh
new=$("$DEST" version 2>/dev/null | head -n 1 || true)
log "installed: $DEST"
log "version:   ${new:-unknown}"
# Another copy of the same name elsewhere keeps being used by scripts
# calling it by full path, or by PATH lookups that find it first.
for other in $(command -v "$NAME" 2>/dev/null || true) /usr/bin/"$NAME" /usr/local/bin/"$NAME" /bin/"$NAME"; do
    [ -x "$other" ] && [ "$other" != "$DEST" ] || continue
    log "warning: another $NAME exists at $other and is not replaced; scripts or PATH lookups using it keep the old binary"
done
case "$PRODUCT" in
    juicefs) log "running mounts are not restarted, but a supervisor auto-restart after a crash (or any new mount) uses the new binary" ;;
    rclone) log "a running rclone keeps using the old binary until it is restarted" ;;
esac
```

（同じパスに対する警告が重複しないように、表示済みのパスを覚えておく。`$DEST` 自身は除く。）

- [ ] **Step 5: 通ることを確かめる** — `bash release/tests/test_install.sh`、`sh -n release/install.sh`、`dash -n release/install.sh`（dash があれば）。Expected: `failed=0`。

- [ ] **Step 6: commit** — `git add release/install.sh release/tests && git commit -m "release: install.sh を製品名を受け取る共通スクリプトにし、製品ごとの最新を API で探す"`

---

### Task 3: 共通の install.ps1

**Files:** 書き直し `release/install.ps1`。

- [ ] **Step 1: 書き直す** — 今のファイルの構造（`& { ... }`、`throw` だけで `exit` しない、`Save-Asset`、SHA-256 の照合、`.new` から Move-Item、WinFsp の警告）を保ち、次を変える。

```powershell
# install.ps1 - download, verify and install a patched build (juicefs or
# rclone, windows-amd64) published on the tongsama/juicefs_inspection
# GitHub Releases.
#
# Usage:
#   $env:KAZ_PRODUCT='rclone'; irm https://raw.githubusercontent.com/tongsama/juicefs_inspection/main/release/install.ps1 | iex
#
# Environment:
#   KAZ_PRODUCT        juicefs or rclone (required)
#   KAZ_VERSION        release tag to install (default: the newest release of the product)
#   KAZ_INSTALL_DIR    install directory (default: %LOCALAPPDATA%\Programs\<product>)
#   KAZ_DOWNLOAD_BASE  releases base URL (default: this repository; used by tests)
#   KAZ_API_BASE       GitHub API URL of this repository (default; used by tests)
#
# This script is run through Invoke-Expression, so it reports failures with
# `throw` and never calls `exit` (which would close the caller's session).
# It does not modify PATH and does not install WinFsp.
```

本体の先頭:

```powershell
    $repo = 'tongsama/juicefs_inspection'
    $product = $env:KAZ_PRODUCT
    if (-not $product) { throw "set `$env:KAZ_PRODUCT to juicefs or rclone before running install.ps1" }
    if (@('juicefs', 'rclone') -notcontains $product) { throw "unknown product: $product (expected juicefs or rclone)" }
    $base = "https://github.com/$repo/releases"
    if ($env:KAZ_DOWNLOAD_BASE) { $base = $env:KAZ_DOWNLOAD_BASE.TrimEnd('/') }
    $api = "https://api.github.com/repos/$repo"
    if ($env:KAZ_API_BASE) { $api = $env:KAZ_API_BASE.TrimEnd('/') }
    $version = $env:KAZ_VERSION
    $installDir = Join-Path $env:LOCALAPPDATA "Programs\$product"
    if ($env:KAZ_INSTALL_DIR) { $installDir = $env:KAZ_INSTALL_DIR }
    $exeName = "$product.exe"
    $asset = "$product-windows-amd64.zip"
```

最新の検索:

```powershell
    # Get-VersionKey returns @(major, minor, patch, kaz) for a release tag of
    # the product, or $null for any other tag. JuiceFS also accepts the older
    # form v<x.y.z>-kaz.<n>.
    function Get-VersionKey([string]$tag) {
        $t = $tag
        if ($t.StartsWith("$product-v")) { $t = $t.Substring($product.Length + 2) }
        elseif ($product -eq 'juicefs' -and $t -match '^v\d') { $t = $t.Substring(1) }
        else { return $null }
        if ($t -notmatch '^(\d+)\.(\d+)\.(\d+)-kaz\.(\d+)$') { return $null }
        return , @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3], [int]$Matches[4])
    }

    # Compare-VersionKey returns a positive number when key $a is newer than $b.
    function Compare-VersionKey($a, $b) {
        for ($i = 0; $i -lt 4; $i++) { if ($a[$i] -ne $b[$i]) { return $a[$i] - $b[$i] } }
        return 0
    }

    if (-not $version) {
        try {
            $releases = Invoke-RestMethod -Uri "$api/releases?per_page=100" -UseBasicParsing
        } catch {
            $resp = $_.Exception.Response
            $code = if ($resp) { [int]$resp.StatusCode } else { 0 }
            if ($code -eq 403 -or $code -eq 429) {
                throw "GitHub API rate limit reached. Set `$env:KAZ_VERSION to a release tag to install it without the API"
            }
            throw "GitHub API request failed: $($_.Exception.Message). Set `$env:KAZ_VERSION to a release tag to install it without the API"
        }
        $bestKey = $null
        foreach ($r in $releases) {
            $k = Get-VersionKey $r.tag_name
            if ($null -eq $k) { continue }
            if ($null -eq $bestKey -or (Compare-VersionKey $k $bestKey) -gt 0) { $bestKey = $k; $version = $r.tag_name }
        }
        if (-not $version) { throw "no $product release found ($api/releases)" }
        Write-Host "newest $product release: $version"
    }
    $label = $version
    $url = "$base/download/$version"
```

（`Save-Asset` の 404 の文言は `release '$label'` のまま。展開後の確認とインストール先は `$exeName` を使う。置き換えに失敗したときの文言は `"cannot replace $dest (is $product running? stop it and retry): ..."`。）

最後の案内:

```powershell
        if ($product -eq 'rclone') { Write-Host 'a running rclone keeps using the old binary until it is restarted' }
        $winfsp = @(
            "${env:ProgramFiles(x86)}\WinFsp\bin\winfsp-x64.dll",
            "$env:ProgramFiles\WinFsp\bin\winfsp-x64.dll"
        ) | Where-Object { Test-Path $_ }
        if (-not $winfsp) {
            Write-Warning "WinFsp was not found. It is required for '$product mount' on Windows: https://winfsp.dev/rel/"
        }
```

一時ディレクトリの接頭辞は `kaz-install-`。

- [ ] **Step 2: 静的な確認** — ASCII のみか: `LC_ALL=C grep -n '[^[:print:][:space:]]' release/install.ps1`（出力なし）。`$product`・`$exeName` の使い忘れが無いかを `grep -n 'juicefs' release/install.ps1` で確かめる（残るのはコメントと製品名の一覧だけのはず）。pwsh は手元に無いので、実行の確認は Task 4・5 の Windows runner で行う。

- [ ] **Step 3: commit** — `git add release/install.ps1 && git commit -m "release: install.ps1 を製品名を受け取る共通スクリプトにし、製品ごとの最新を API で探す"`

---

### Task 4: `release-juicefs.yml`

**Files:** 移動 `.github/workflows/release.yml` → `.github/workflows/release-juicefs.yml`（`git mv`）。

- [ ] **Step 1:** 先頭のコメントを「改修版 JuiceFS」用に書き直し、起動を次にする:

```yaml
on:
  push:
    tags: ['juicefs-v*']
  workflow_dispatch:
    inputs:
      tag:
        description: 'release/juicefs/versions.json に登録済みのタグ（例: juicefs-v1.4.1-kaz.4。手動実行では古い形 v1.4.1-kaz.3 も可）'
        required: true
```

- [ ] **Step 2:** resolve のタグの検証と読み込みを次にする（`EVENT: ${{ github.event_name }}` を env に足す）:

```bash
if [[ "$TAG" =~ ^juicefs-v([0-9]+\.[0-9]+\.[0-9]+)-(kaz\.[0-9]+)$ ]]; then
  :
elif [[ "$EVENT" == workflow_dispatch && "$TAG" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)-(kaz\.[0-9]+)$ ]]; then
  :  # 古い形は手動実行（Release を作らない）だけで受け付ける
else
  fail "タグの形式が不正です: $TAG"
fi
base=${BASH_REMATCH[1]}
kaz=${BASH_REMATCH[2]}
entry=$(jq -e --arg t "$TAG" '.[$t]' release/juicefs/versions.json) || fail "release/juicefs/versions.json に $TAG がありません"
repo=$(jq -r '.repo // ""' <<<"$entry")
commit=$(jq -r '.commit // ""' <<<"$entry")
```

（以降の検証と出力は今のまま。）

- [ ] **Step 3:** verify-windows の環境変数を新しい名前にする: `KAZ_PRODUCT: juicefs`、`KAZ_VERSION: ${{ needs.resolve.outputs.tag }}`、`KAZ_DOWNLOAD_BASE: http://127.0.0.1:8765`、`KAZ_INSTALL_DIR: ${{ runner.temp }}\jfs`（`JFS_*` を置き換える。PowerShell 内の `$env:JFS_INSTALL_DIR` も `$env:KAZ_INSTALL_DIR` に）。

- [ ] **Step 4:** release の job の `gh release create` を次にする:

```bash
gh release create "$TAG" --repo "$GITHUB_REPOSITORY" --draft --verify-tag --latest=false --title "JuiceFS v${BASE}-${KAZ}" --notes-file notes.md dist/*
```

（`BASE: ${{ needs.resolve.outputs.base }}`、`KAZ: ${{ needs.resolve.outputs.kaz }}` を env に足す。）

- [ ] **Step 5: 構文の確認** — `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 .github/workflows/release-juicefs.yml`。Expected: 指摘なし。

- [ ] **Step 6: commit** — `git add -A .github/workflows && git commit -m "release: JuiceFS の workflow を新しいタグの形と配置に合わせる"`

---

### Task 5: `release-rclone.yml`

**Files:** 新規 `.github/workflows/release-rclone.yml`。

- [ ] **Step 1:** 次の内容で作る（`release-juicefs.yml` と同じ書き方。コメントは日本語）。

```yaml
# 改修版 rclone のバイナリをビルドし、draft Release を作る。
# - タグ rclone-v<base>-kaz.<n> の push：検証 → ビルド → 確認 → draft Release
# - 手動実行（workflow_dispatch）：Release は作らず、検証・ビルド・確認だけを行う
# ビルドは公式と同じ bin/cross-compile.go（-tags cmount、CGO 無し）。本体 repo は読むだけ。
name: release-rclone

on:
  push:
    tags: ['rclone-v*']
  workflow_dispatch:
    inputs:
      tag:
        description: 'release/rclone/versions.json に登録済みのタグ（例: rclone-v1.75.1-kaz.1）'
        required: true

permissions:
  contents: read

jobs:
  # タグと versions.json を検証し、ビルドに必要な値を後続の job へ渡す。
  resolve:
    runs-on: ubuntu-24.04
    outputs:
      tag: ${{ steps.r.outputs.tag }}
      version: ${{ steps.r.outputs.version }}
      repo: ${{ steps.r.outputs.repo }}
      commit: ${{ steps.r.outputs.commit }}
      go_version: ${{ steps.r.outputs.go_version }}
      notes: ${{ steps.r.outputs.notes }}
    steps:
      - uses: actions/checkout@v4
      - id: r
        env:
          TAG: ${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name }}
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          fail() { echo "::error::$*"; exit 1; }
          [[ "$TAG" =~ ^rclone-(v[0-9]+\.[0-9]+\.[0-9]+-kaz\.[0-9]+)$ ]] || fail "タグの形式が不正です: $TAG"
          version=${BASH_REMATCH[1]}
          entry=$(jq -e --arg t "$TAG" '.[$t]' release/rclone/versions.json) || fail "release/rclone/versions.json に $TAG がありません"
          repo=$(jq -r '.repo // ""' <<<"$entry")
          commit=$(jq -r '.commit // ""' <<<"$entry")
          go_version=$(jq -r '.go_version // ""' <<<"$entry")
          notes=$(jq -r '.notes // ""' <<<"$entry")
          [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "repo が不正です: $repo"
          [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "commit は 40 桁の SHA にしてください: $commit"
          [[ "$go_version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail "go_version が不正です: $go_version"
          [ -n "$notes" ] && [ -f "$notes" ] || fail "リリースノートがありません: $notes"
          if ! got=$(gh api "repos/$repo/commits/$commit" --jq .sha 2>/dev/null) || [ "$got" != "$commit" ]; then
            fail "$repo に commit $commit がありません"
          fi
          {
            echo "tag=$TAG"; echo "version=$version"; echo "repo=$repo"; echo "commit=$commit"
            echo "go_version=$go_version"; echo "notes=$notes"
          } >> "$GITHUB_OUTPUT"

  # 公式と同じ cross-compile.go で 3 種類をビルドし、asset の形にまとめる。
  build:
    needs: resolve
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with:
          repository: ${{ needs.resolve.outputs.repo }}
          ref: ${{ needs.resolve.outputs.commit }}
          path: rclone
      - uses: actions/setup-go@v5
        with:
          go-version: ${{ needs.resolve.outputs.go_version }}
          cache-dependency-path: rclone/go.sum
      - name: install tools
        run: sudo apt-get update && sudo apt-get install -y file zip
      - name: build
        working-directory: rclone
        env:
          VERSION: ${{ needs.resolve.outputs.version }}
        run: |
          set -euo pipefail
          go run bin/cross-compile.go -compile-only -tags cmount -include '^(linux/(amd64|arm64)|windows/amd64)$' "$VERSION"
          ls -R build
      - name: verify and package
        working-directory: rclone/build
        env:
          VERSION: ${{ needs.resolve.outputs.version }}
        run: |
          set -euo pipefail
          mkdir -p ../../dist
          for t in linux-amd64 linux-arm64; do
            f="rclone-$VERSION-$t/rclone"
            file "$f"
            file "$f" | grep -q 'statically linked' || { echo "::error::$t が静的リンクではありません"; exit 1; }
            tar -czf "../../dist/rclone-$t.tar.gz" -C "rclone-$VERSION-$t" rclone
          done
          f="rclone-$VERSION-windows-amd64/rclone.exe"
          file "$f" | grep -q 'PE32+ executable' || { echo "::error::PE32+ ではありません"; exit 1; }
          zip -j ../../dist/rclone-windows-amd64.zip "$f"
          b="rclone-$VERSION-linux-amd64/rclone"
          got=$("$b" version | head -n 1)
          [ "$got" = "rclone $VERSION" ] || { echo "::error::version が一致しません: '$got'"; exit 1; }
          "$b" version | grep -q 'go/tags: cmount' || { echo "::error::cmount タグがありません"; exit 1; }
          for flag in --kaz-vfs-lookup-by-path --kaz-s3-persist-metadata; do
            "$b" serve s3 --help | grep -q -- "$flag" || { echo "::error::$flag がありません"; exit 1; }
          done
          "$b" help backend drive | grep -q -- '--drive-kaz-properties' || { echo "::error::--drive-kaz-properties がありません"; exit 1; }
      - uses: actions/upload-artifact@v4
        with:
          name: rclone-dist
          path: dist/
          if-no-files-found: error

  # arm64 版を arm の runner で起動して確かめる。
  verify-arm64:
    needs: [resolve, build]
    runs-on: ubuntu-24.04-arm
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: rclone-dist
          path: dist
      - env:
          VERSION: ${{ needs.resolve.outputs.version }}
        run: |
          set -euo pipefail
          tar -xzf dist/rclone-linux-arm64.tar.gz rclone
          got=$(./rclone version | head -n 1)
          [ "$got" = "rclone $VERSION" ] || { echo "::error::version が一致しません: '$got'"; exit 1; }

  # Windows 版を install.ps1 で入れて確かめる（成功とチェックサム不一致）。
  verify-windows:
    needs: [resolve, build]
    runs-on: windows-latest
    defaults:
      run:
        shell: pwsh
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: rclone-dist
          path: dist
      - name: install.ps1 (success and checksum mismatch)
        env:
          TAG: ${{ needs.resolve.outputs.tag }}
          VERSION: ${{ needs.resolve.outputs.version }}
          KAZ_PRODUCT: rclone
          KAZ_VERSION: ${{ needs.resolve.outputs.tag }}
          KAZ_DOWNLOAD_BASE: http://127.0.0.1:8765
          KAZ_INSTALL_DIR: ${{ runner.temp }}\rclone
        run: |
          $dir = "srv/download/$env:TAG"
          New-Item -ItemType Directory -Force -Path $dir | Out-Null
          Copy-Item dist/rclone-windows-amd64.zip $dir/
          $h = (Get-FileHash "$dir/rclone-windows-amd64.zip" -Algorithm SHA256).Hash.ToLower()
          "$h  rclone-windows-amd64.zip" | Set-Content -Encoding ascii "$dir/checksums.txt"
          $srv = Start-Process python -ArgumentList '-m','http.server','8765','--bind','127.0.0.1','--directory','srv' -PassThru -WindowStyle Hidden
          try {
            for ($i = 0; $i -lt 50; $i++) {
              try { Invoke-WebRequest http://127.0.0.1:8765/ -UseBasicParsing | Out-Null; break } catch { Start-Sleep -Milliseconds 200 }
            }
            Get-Content release/install.ps1 -Raw | Invoke-Expression
            $exe = "$env:KAZ_INSTALL_DIR\rclone.exe"
            $got = & $exe version | Select-Object -First 1
            if ($got -ne "rclone $env:VERSION") { throw "version mismatch: got '$got'" }
            if (-not ((& $exe version) -match 'go/tags: cmount')) { throw 'cmount tag missing' }
            & $exe mount --help | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'rclone mount --help failed' }
            $before = (Get-FileHash $exe).Hash
            ('0' * 64) + '  rclone-windows-amd64.zip' | Set-Content -Encoding ascii "$dir/checksums.txt"
            $failed = $false
            try { Get-Content release/install.ps1 -Raw | Invoke-Expression } catch { $failed = $true; Write-Host "expected failure: $_" }
            if (-not $failed) { throw 'checksum mismatch was not detected' }
            if ((Get-FileHash $exe).Hash -ne $before) { throw 'existing rclone.exe was modified' }
            Write-Host 'install.ps1 checks passed'
          } finally {
            Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
          }

  # すべて成功したときだけ、タグの push で draft Release を作る。
  release:
    if: github.event_name == 'push'
    needs: [resolve, build, verify-arm64, verify-windows]
    runs-on: ubuntu-24.04
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: rclone-dist
          path: dist
      - name: assemble
        run: |
          set -euo pipefail
          cp release/install.sh release/install.ps1 dist/
          cd dist
          files="rclone-linux-amd64.tar.gz rclone-linux-arm64.tar.gz rclone-windows-amd64.zip install.sh install.ps1"
          for f in $files; do [ -f "$f" ] || { echo "::error::$f がありません"; exit 1; }; done
          sha256sum $files > checksums.txt
          cat checksums.txt
      - name: create draft release
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ needs.resolve.outputs.tag }}
          VERSION: ${{ needs.resolve.outputs.version }}
          NOTES: ${{ needs.resolve.outputs.notes }}
          RREPO: ${{ needs.resolve.outputs.repo }}
          COMMIT: ${{ needs.resolve.outputs.commit }}
          GOV: ${{ needs.resolve.outputs.go_version }}
        run: |
          set -euo pipefail
          if gh release list --repo "$GITHUB_REPOSITORY" --limit 1000 --json tagName --jq '.[].tagName' | grep -qxF "$TAG"; then
            echo "::error::$TAG の Release はすでにあります（draft を含む）。上書きしません"; exit 1
          fi
          { cat "$NOTES"; printf '\n\n---\nビルド元: https://github.com/%s/commit/%s（Go %s、bin/cross-compile.go -tags cmount）\n' "$RREPO" "$COMMIT" "$GOV"; } > notes.md
          gh release create "$TAG" --repo "$GITHUB_REPOSITORY" --draft --verify-tag --latest=false --title "rclone $VERSION" --notes-file notes.md dist/*
```

注意: `cross-compile.go` の `-include` は `os/arch` の文字列に対する正規表現。`osarches` に `linux/arm64` などがそのままの形で入っていることを `rclone/bin/cross-compile.go:49-` で確かめる（`linux/arm-v7` のような合成名は除外される）。Windows の `.syso` の生成（`go run ../bin/resource_windows.go`）が失敗すると警告だけで続くので、build のログで `Warning: Couldn't generate Windows` が出ていないかを Task 8 で確かめる。

- [ ] **Step 2: 構文の確認** — `go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 .github/workflows/release-rclone.yml`。Expected: 指摘なし。

- [ ] **Step 3: 手元でのビルドの試し** — `cd rclone && git status --short && go run bin/cross-compile.go -compile-only -tags cmount -include '^(linux/(amd64|arm64)|windows/amd64)$' v1.75.1-kaz.1 && ls -R build | head -20 && ./build/rclone-v1.75.1-kaz.1-linux-amd64/rclone version | head -3; rm -rf build`（`rclone/` の作業ツリーに `build/` を残さない。`bin/../resource_windows_amd64.syso` などの生成物が残ったら消す。`git status --short` で何も残っていないことを確かめる）。Expected: 3つのディレクトリができ、1行目が `rclone v1.75.1-kaz.1`、`go/tags: cmount`。

- [ ] **Step 4: commit** — `git add .github/workflows/release-rclone.yml && git commit -m "release: rclone の workflow を追加（cross-compile.go で linux-amd64・arm64・windows-amd64）"`

---

### Task 6: `install-tests.yml`

- [ ] **Step 1:** `.github/workflows/install-tests.yml`:

```yaml
# install.sh のテストを、release/ や workflow の変更時に流す。
name: install-tests

on:
  push:
    paths: ['release/**', '.github/workflows/install-tests.yml']
  pull_request:
    paths: ['release/**', '.github/workflows/install-tests.yml']
  workflow_dispatch:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - run: bash release/tests/test_install.sh
```

- [ ] **Step 2:** actionlint で確認し、commit — `git add .github/workflows/install-tests.yml && git commit -m "release: install スクリプトのテストを GitHub Actions で流す"`

---

### Task 7: README と文書

- [ ] **Step 1:** README の「改修版バイナリの配布とインストール」の節を書き直す。
  - 製品ごとの Linux と Windows のコマンド（§4 の使い方）。
  - 版の固定、インストール先、名前の指定。
  - 製品ごとの Releases の絞り込みへのリンク（`https://github.com/tongsama/juicefs_inspection/releases?q=rclone`、`?q=juicefs`）。
  - 古い形のタグ（`v1.4.1-kaz.1`〜`.3`）は JuiceFS として扱われること。
  - 稼働中の mount・rclone への注意。
  - Windows の WinFsp。
- [ ] **Step 2:** 「新しい版を出す手順」を製品ごとに書き直す。
  1. 本体を直して push する。
  2. `release/<製品>/versions.json` とノートを追加する。
  3. 手動実行で試す（`gh workflow run release-<製品>.yml --repo tongsama/juicefs_inspection -f tag=<タグ>`）。
  4. タグを push する。
  5. draft を確かめて公開する。

  バイナリの表示（`juicefs version`・`rclone version`）の形も書く。
- [ ] **Step 3:** README のディレクトリ構成（`release/` の中身、workflow）と、資料の表の `docs/release-notes/` へのリンクを直す。
- [ ] **Step 4:** `docs/superpowers/specs/2026-10-04-release-distribution.md` の先頭に、「2026-10-08 の [2製品対応の設計](2026-10-08-release-multi-product-design.md) で、配置・タグ・install スクリプトの使い方を置き換えた」と追記する。`docs/superpowers/README.md` の一覧表の Phase 3 の行に、実装計画へのリンクを入れる。
- [ ] **Step 5:** `grep -rn "docs/release-notes\|releases/latest/download\|JFS_VERSION\|JFS_INSTALL" README.md docs/superpowers/README.md release .github` で、古い参照が残っていないことを確かめる（過去の記録である agent_memo・TODO の履歴と、古い設計書の本文は対象外）。
- [ ] **Step 6: commit** — `git add -A README.md docs/superpowers && git commit -m "docs: 2製品の配布とインストールの手順に README を書き直す"`

---

### Task 8: push と、workflow の手動実行（ユーザーの確認を取る）

workflow_dispatch は、既定のブランチ（`main`）にある workflow しか実行できない。そのため、先に `main` へ取り込む。

- [ ] **Step 1:** ユーザーの確認のうえで、`feat/release-multi-product` を `main` に取り込み（`--ff-only` か `--no-ff` はユーザーに確認）、`original` に push する。`install-tests` の workflow が push で動くので、結果を `gh run list --repo tongsama/juicefs_inspection --workflow install-tests.yml --limit 1` と `gh run watch` で確かめる。
- [ ] **Step 2:** ユーザーの確認のうえで、手動実行を2つ行う。
  - `gh workflow run release-juicefs.yml --repo tongsama/juicefs_inspection -f tag=v1.4.1-kaz.3`
  - `gh workflow run release-rclone.yml --repo tongsama/juicefs_inspection -f tag=rclone-v1.75.1-kaz.1`

  両方の結果を確かめる（全 job が成功し、release の job は skip）。rclone の build のログに `Warning: Couldn't generate Windows` が無いことも確かめる。失敗したら原因を調べ、修正してから同じ手順で試し直す。
- [ ] **Step 3:** 結果を `agent_memo.md` に記録する（run の ID、各 job の結果）。

---

### Task 9: 最初の rclone の Release（ユーザーの確認を取る）

- [ ] **Step 1:** ユーザーの確認のうえで、`main` の最新の commit にタグ `rclone-v1.75.1-kaz.1`（軽量タグ、今の JuiceFS と同じ形）を付けて push する。workflow の結果を確かめ、draft の Release ができたことを確かめる（asset 6つ: tar.gz 2つ、zip、checksums.txt、install.sh、install.ps1）。
- [ ] **Step 2:** draft の asset を手元にダウンロードして確かめる: `gh release download rclone-v1.75.1-kaz.1 --repo tongsama/juicefs_inspection --dir <scratchpad>`、`sha256sum -c checksums.txt`、amd64 の `rclone version`。
- [ ] **Step 3:** ユーザーが GitHub の画面で公開する。**公開するときは「Set as the latest release」のチェックを外す**（draft に付けた `--latest=false` が公開時まで効くかは未確認のため）。公開後、`gh api repos/tongsama/juicefs_inspection/releases/latest --jq .tag_name` が `v1.4.1-kaz.3` のままであることを確かめる。違っていれば、ユーザーの確認のうえで `gh release edit v1.4.1-kaz.3 --repo tongsama/juicefs_inspection --latest` で戻す（2026-10-08 最終レビューの指摘で追加）。
- [ ] **Step 3a:** タグは、最終レビューの修正を取り込んだ後の `main` に付ける（Release に添付する install スクリプトはタグの commit のもの）。merge 直後は raw.githubusercontent.com が最大5分ほど古い install.sh を返すことがあるので、実地の確認で古い挙動（usage エラーなど）が出たら少し待って試し直す。
公開後、このホストで install スクリプトを実際に使い、一時ディレクトリへ入れて確かめる: `curl -fsSL https://raw.githubusercontent.com/tongsama/juicefs_inspection/main/release/install.sh | sh -s -- rclone <scratchpad>/bin`（最新として `rclone-v1.75.1-kaz.1` が選ばれ、`rclone version` が `rclone v1.75.1-kaz.1`）。`… | sh -s -- juicefs <scratchpad>/bin` で `v1.4.1-kaz.3` が入ることも確かめる。Windows はユーザーに確認を依頼する（`$env:KAZ_PRODUCT='rclone'; irm …/install.ps1 | iex`）。
- [ ] **Step 4:** `TODO.md`、`agent_memo.md`、`docs/superpowers/README.md` を更新し、ユーザーの許可を得て commit・push する。
