# 改修版 JuiceFS バイナリ配布 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** inspection repo（`tongsama/juicefs_inspection`）の GitHub Actions で、改修版 JuiceFS の linux-amd64 / linux-arm64 / windows-amd64 バイナリを draft Release として作り、`curl -fsSL … | sh` と `irm … | iex` でインストールできるようにする。

**Architecture:** 版ごとの本体 commit を `release/versions.json` で管理する。タグを push すると、workflow が本体（`tongsama/juicefs`）をその SHA で checkout し、3 種類を並行してビルド・確認して、draft Release に添付する。install スクリプトは Release の添付ファイルとして配り、sha256 を照合してから既存のバイナリを1回の `mv` で置き換える。

**Tech Stack:** GitHub Actions（`ubuntu-24.04`、`ubuntu-24.04-arm`、`windows-latest`）、Go 1.25.11、musl-gcc、mingw-w64、POSIX sh、PowerShell、bash（テスト）、python3 `http.server`（テスト用の配信）、jq、gh CLI

**Spec:** `docs/superpowers/specs/2026-10-04-release-distribution.md`

## Global Constraints

- 作業する repo は inspection repo のルート（`/home/kwatanabe/tmp_local/juicefs_inspection`）だけ。本体 `juicefs/` のファイル、ブランチ、タグには一切触れない。
- 配布対象は `linux-amd64`、`linux-arm64`、`windows-amd64` の3つ。添付ファイル名は `juicefs-linux-amd64.tar.gz`、`juicefs-linux-arm64.tar.gz`、`juicefs-windows-amd64.zip`、`checksums.txt`、`install.sh`、`install.ps1`（版を含めない）。
- tar.gz は先頭階層に `juicefs` を1つだけ、zip は `juicefs.exe` を1つだけ含める。
- タグの形式は `^v[0-9]+\.[0-9]+\.[0-9]+-kaz\.[0-9]+$`。
- バージョン表示は `juicefs version <base>+<本体commitの日付>.<SHAの先頭8桁>-kaz.<n>`。ldflags は `-X github.com/juicedata/juicefs/pkg/version.revision=<sha8>-kaz.<n> -X github.com/juicedata/juicefs/pkg/version.revisionDate=<YYYY-MM-DD>`。
- 既定のダウンロード元は `https://github.com/tongsama/juicefs_inspection/releases`。latest は `<base>/latest/download/<file>`、版を指定した場合は `<base>/download/<tag>/<file>`。
- `install.sh` と `install.ps1` は ASCII のみで書く（メッセージとコメントは英語）。`install.ps1` は `exit` を使わず `throw` で失敗を返す。
- Release は必ず draft で作る。既存の Release を上書きや削除しない。公開はユーザーが行う。
- **commit・push・タグの push・Release の作成は、ユーザーの明示的な指示があるまで行わない**（AGENTS.md：文書の commit はユーザーが行う。agent は勝手に add・commit・push しない）。各 Task の最後は「ユーザーに commit を依頼する」で止める。
- commit メッセージに Co-Authored-By などの attribution を入れない。

## Review Focus

- **Windows ビルドが今回の改修コードで通るか**：本体の改修（`pkg/meta/compaction_*.go`、`pkg/vfs/writer_trace.go` など）は Linux でしかビルドしていない。mingw で失敗した場合、本体側の修正が必要になる。その修正は本計画の範囲外として、止めてユーザーに報告する（Task 5）。
- **WinFsp が無い Windows での `juicefs.exe version`**：`windows-latest` には WinFsp が無い。`version` の実行が WinFsp の DLL を要求して失敗する場合は、verify-windows job に `choco install winfsp -y` を追加する。install.ps1 の警告は、WinFsp の有無に関わらず出し分けが正しいことだけを確認する（Task 4、Task 5）。
- **インストール先が存在しない、または書き込めない**：存在しなければ作る（必要なら sudo）。書き込めず、sudo も無ければ、既存のファイルに触れずに失敗する（Task 1 のテストケース 3 と 10）。
- **途中で失敗したときに既存のバイナリが壊れない**：照合の不一致、404、checksum の行が無い場合に、既存のファイルが変わらないこと（Task 1 のテストケース 5・6・11、Task 4 の Windows 確認）。
- **`irm | iex` で実行したときの挙動**：throw が呼び出し元に伝わり、利用者のセッションが閉じないこと（Task 4 で Invoke-Expression の形で実行して確認）。

---

### Task 1: install.sh とそのテスト

**Files:**
- Create: `release/tests/test_install.sh`
- Create: `release/install.sh`

**Interfaces:**
- Consumes: なし
- Produces: `release/install.sh`（引数1：インストール先。環境変数：`JFS_VERSION`、`JFS_INSTALL_NAME`、`JFS_DOWNLOAD_BASE`）。Task 4 の workflow がこのファイルを Release に添付する。

- [ ] **Step 1: 失敗するテストを書く**

`release/tests/test_install.sh` を次の内容で作る。

```bash
#!/usr/bin/env bash
# release/install.sh の動作を、手元の HTTP サーバーと偽の配布物で確認するテスト。
# 実行: bash release/tests/test_install.sh   （root 以外で実行すること）
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
INSTALL_SH="$ROOT/release/install.sh"
T=$(mktemp -d)
SRV="$T/srv"
FAKEBIN="$T/fakebin"
REAL_UNAME=$(command -v uname)
PORT=$(( 20000 + RANDOM % 20000 ))
BASE="http://127.0.0.1:$PORT"
PASS=0
FAIL=0
SERVER_PID=""

# cleanup はテスト用 HTTP サーバーと一時ディレクトリを片付ける。
cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
  chmod -R u+w "$T" 2>/dev/null || true
  rm -rf "$T"
}
trap cleanup EXIT

# ok / ng はテスト結果を記録して表示する。
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
ng() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }

# make_release は、ディレクトリ $1 に版 $2 の偽配布物（amd64/arm64）と checksums.txt を作る。
# 偽の juicefs は「juicefs version <版> <target>」を表示するだけのシェルスクリプト。
make_release() {
  local dir=$1 ver=$2 target b
  mkdir -p "$dir"
  for target in linux-amd64 linux-arm64; do
    b="$T/build/$ver/$target"
    mkdir -p "$b"
    printf '#!/bin/sh\necho "juicefs version %s %s"\n' "$ver" "$target" > "$b/juicefs"
    chmod 0755 "$b/juicefs"
    tar -czf "$dir/juicefs-$target.tar.gz" -C "$b" juicefs
  done
  (cd "$dir" && sha256sum juicefs-linux-amd64.tar.gz juicefs-linux-arm64.tar.gz > checksums.txt)
}

# run_install は、偽の uname とテスト用の配信元を使って install.sh を実行する。
# 使い方: run_install <ログファイル> [VAR=値 ...] [--] <インストール先>
run_install() {
  local log=$1; shift
  local envs=()
  while [ $# -gt 1 ]; do envs+=("$1"); shift; done
  env PATH="$FAKEBIN:$PATH" JFS_DOWNLOAD_BASE="$BASE" "${envs[@]}" sh "$INSTALL_SH" "$1" > "$log" 2>&1
}

# 偽の uname（FAKE_UNAME_S / FAKE_UNAME_M で OS とアーキテクチャを差し替える）。
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/uname" <<EOF
#!/bin/sh
case "\$1" in
  -s) echo "\${FAKE_UNAME_S:-Linux}" ;;
  -m) echo "\${FAKE_UNAME_M:-x86_64}" ;;
  *) exec "$REAL_UNAME" "\$@" ;;
esac
EOF
chmod 0755 "$FAKEBIN/uname"

# 配信する偽の Release。
make_release "$SRV/latest/download" v1.0.0-kaz.2
make_release "$SRV/download/v1.0.0-kaz.1" v1.0.0-kaz.1
make_release "$SRV/download/v1.0.0-kaz.bad" v1.0.0-kaz.bad
sed -i '1s/^[0-9a-f]\{64\}/0000000000000000000000000000000000000000000000000000000000000000/' "$SRV/download/v1.0.0-kaz.bad/checksums.txt"
make_release "$SRV/download/v1.0.0-kaz.nosum" v1.0.0-kaz.nosum
: > "$SRV/download/v1.0.0-kaz.nosum/checksums.txt"

python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$SRV" > "$T/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  if curl -s -o /dev/null "$BASE/"; then break; fi
  sleep 0.1
done

# 1. latest を既定の名前でインストールできる。
d="$T/c1"; mkdir -p "$d"
if run_install "$T/c1.log" "$d" && "$d/juicefs" | grep -q 'v1.0.0-kaz.2 linux-amd64'; then ok "latest install"; else ng "latest install"; cat "$T/c1.log"; fi

# 2. 版を固定すると download/<tag>/ から取得する。
d="$T/c2"; mkdir -p "$d"
if run_install "$T/c2.log" JFS_VERSION=v1.0.0-kaz.1 "$d" && "$d/juicefs" | grep -q 'v1.0.0-kaz.1 linux-amd64'; then ok "pinned version"; else ng "pinned version"; cat "$T/c2.log"; fi

# 3. 名前の指定と、存在しないインストール先の作成。
d="$T/c3/sub/bin"
if run_install "$T/c3.log" JFS_INSTALL_NAME=juicefs-kaz "$d" && [ -x "$d/juicefs-kaz" ] && [ ! -e "$d/juicefs" ]; then ok "custom name and new dir"; else ng "custom name and new dir"; cat "$T/c3.log"; fi

# 4. aarch64 では arm64 版を選ぶ。
d="$T/c4"; mkdir -p "$d"
if run_install "$T/c4.log" FAKE_UNAME_M=aarch64 "$d" && "$d/juicefs" | grep -q 'linux-arm64'; then ok "arm64 detection"; else ng "arm64 detection"; cat "$T/c4.log"; fi

# 5. sha256 の不一致で中止し、既存のファイルを変えない。
d="$T/c5"; mkdir -p "$d"; printf 'old\n' > "$d/juicefs"; cp "$d/juicefs" "$T/c5.orig"
if ! run_install "$T/c5.log" JFS_VERSION=v1.0.0-kaz.bad "$d" && grep -q 'checksum mismatch' "$T/c5.log" && cmp -s "$d/juicefs" "$T/c5.orig"; then ok "checksum mismatch keeps existing"; else ng "checksum mismatch keeps existing"; cat "$T/c5.log"; fi

# 6. 存在しない版は 404 として中止し、何も作らない。
d="$T/c6"; mkdir -p "$d"
if ! run_install "$T/c6.log" JFS_VERSION=v9.9.9-kaz.9 "$d" && grep -q 'not found (404)' "$T/c6.log" && [ ! -e "$d/juicefs" ]; then ok "404"; else ng "404"; cat "$T/c6.log"; fi

# 7. 対応していないアーキテクチャ。
d="$T/c7"; mkdir -p "$d"
if ! run_install "$T/c7.log" FAKE_UNAME_M=armv7l "$d" && grep -q 'unsupported architecture: armv7l' "$T/c7.log" && [ ! -e "$d/juicefs" ]; then ok "unsupported arch"; else ng "unsupported arch"; cat "$T/c7.log"; fi

# 8. 対応していない OS。
d="$T/c8"; mkdir -p "$d"
if ! run_install "$T/c8.log" FAKE_UNAME_S=Darwin "$d" && grep -q 'unsupported OS: Darwin' "$T/c8.log"; then ok "unsupported OS"; else ng "unsupported OS"; cat "$T/c8.log"; fi

# 9. 既存のバイナリを置き換え、旧版と新版を表示し、一時ファイルを残さない。
d="$T/c9"; mkdir -p "$d"
printf '#!/bin/sh\necho "juicefs version OLD"\n' > "$d/juicefs"; chmod 0755 "$d/juicefs"
if run_install "$T/c9.log" "$d" && grep -q 'OLD' "$T/c9.log" && grep -q 'v1.0.0-kaz.2' "$T/c9.log" \
   && "$d/juicefs" | grep -q 'v1.0.0-kaz.2' && [ "$(ls -A "$d")" = "juicefs" ]; then ok "replace existing"; else ng "replace existing"; cat "$T/c9.log"; ls -A "$d"; fi

# 10. 書き込めず sudo も無ければ、既存のファイルに触れずに中止する。
if [ "$(id -u)" -eq 0 ]; then
  echo "skip - not writable without sudo (running as root)"
else
  TOOLS="$T/tools"; mkdir -p "$TOOLS"
  for c in curl tar gzip mktemp awk sha256sum id cp chmod mv rm mkdir head cat; do ln -s "$(command -v "$c")" "$TOOLS/$c"; done
  d="$T/c10"; mkdir -p "$d"; printf 'old\n' > "$d/juicefs"; cp "$d/juicefs" "$T/c10.orig"; chmod 0555 "$d"
  if ! env PATH="$FAKEBIN:$TOOLS" JFS_DOWNLOAD_BASE="$BASE" /bin/sh "$INSTALL_SH" "$d" > "$T/c10.log" 2>&1 \
     && grep -q 'sudo is not available' "$T/c10.log" && cmp -s "$d/juicefs" "$T/c10.orig"; then ok "not writable without sudo"; else ng "not writable without sudo"; cat "$T/c10.log"; fi
  chmod 0755 "$d"
fi

# 11. checksums.txt に該当する行が無ければ中止する。
d="$T/c11"; mkdir -p "$d"
if ! run_install "$T/c11.log" JFS_VERSION=v1.0.0-kaz.nosum "$d" && grep -q 'no checksum entry' "$T/c11.log" && [ ! -e "$d/juicefs" ]; then ok "missing checksum entry"; else ng "missing checksum entry"; cat "$T/c11.log"; fi

# 12. 配布スクリプトは ASCII のみ。
if [ -f "$INSTALL_SH" ] && ! LC_ALL=C grep -n '[^[:print:][:space:]]' "$INSTALL_SH" >/dev/null; then ok "install.sh is ASCII"; else ng "install.sh is ASCII"; fi

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `bash release/tests/test_install.sh`
Expected: install.sh がまだ無いので、12 件すべてが `FAIL` になり、最後に `passed=0 failed=12` が出て終了コードは 1。

- [ ] **Step 3: install.sh を実装する**

`release/install.sh` を次の内容で作り、`chmod 0755` を付ける。

```sh
#!/bin/sh
# install.sh - download, verify and install the patched JuiceFS build
# published on the tongsama/juicefs_inspection GitHub Releases.
#
# Usage:
#   curl -fsSL https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.sh | sh
#   curl -fsSL .../install.sh | JFS_VERSION=v1.4.1-kaz.1 sh -s /opt/bin
#
# Arguments:
#   $1                 install directory (default: /usr/local/bin)
# Environment:
#   JFS_VERSION        release tag to install (default: latest release)
#   JFS_INSTALL_NAME   installed file name (default: juicefs)
#   JFS_DOWNLOAD_BASE  releases base URL (default: this repository; used by tests)
#
# The existing binary is replaced by a single mv only after the download and
# the SHA-256 verification succeed. Running mounts keep using the old binary
# until they are remounted.
set -eu

DEFAULT_BASE="https://github.com/tongsama/juicefs_inspection/releases"
INSTALL_DIR="${1:-/usr/local/bin}"
NAME="${JFS_INSTALL_NAME:-juicefs}"
VERSION="${JFS_VERSION:-}"
BASE="${JFS_DOWNLOAD_BASE:-$DEFAULT_BASE}"
BASE="${BASE%/}"
WORK=""
STAGED=""
SUDO=""

# log prints a progress message to stderr.
log() { printf '%s\n' "$*" >&2; }

# die prints an error message to stderr and exits with status 1.
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# cleanup removes the temporary work directory and a half-staged binary.
cleanup() {
    if [ -n "$STAGED" ]; then $SUDO rm -f "$STAGED" 2>/dev/null || true; fi
    if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# need exits unless the given command is available.
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

# detect_target prints the release target name for this host.
detect_target() {
    os=$(uname -s)
    arch=$(uname -m)
    [ "$os" = "Linux" ] || die "unsupported OS: $os (supported: linux-amd64, linux-arm64; use install.ps1 on Windows)"
    case "$arch" in
        x86_64|amd64) echo linux-amd64 ;;
        aarch64|arm64) echo linux-arm64 ;;
        *) die "unsupported architecture: $arch (supported: linux-amd64, linux-arm64)" ;;
    esac
}

# fetch downloads URL $1 into file $2 and tells a 404 apart from other failures.
fetch() {
    if ! code=$(curl -sSL --retry 3 -o "$2" -w '%{http_code}' "$1"); then
        die "download failed (network error): $1"
    fi
    case "$code" in
        200) ;;
        404) die "not found (404): $1 -- check that release '${VERSION:-latest}' and its assets exist" ;;
        *) die "download failed (HTTP $code): $1" ;;
    esac
}

# sha256_of prints the SHA-256 hex digest of file $1.
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "neither sha256sum nor shasum is available"
    fi
}

# use_sudo selects sudo for writing into INSTALL_DIR, or exits when it cannot.
use_sudo() {
    if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        SUDO=sudo
    else
        die "cannot write to $INSTALL_DIR and sudo is not available"
    fi
}

need curl
need tar
need mktemp
need awk
need id

TARGET=$(detect_target)
ASSET="juicefs-$TARGET.tar.gz"
if [ -n "$VERSION" ]; then
    URL="$BASE/download/$VERSION"
else
    URL="$BASE/latest/download"
fi

WORK=$(mktemp -d)
log "downloading $ASSET (${VERSION:-latest}) from $URL"
fetch "$URL/$ASSET" "$WORK/$ASSET"
fetch "$URL/checksums.txt" "$WORK/checksums.txt"

expected=$(awk -v f="$ASSET" '$2 == f || $2 == ("*" f) { print $1; exit }' "$WORK/checksums.txt")
[ -n "$expected" ] || die "no checksum entry for $ASSET in checksums.txt"
actual=$(sha256_of "$WORK/$ASSET")
[ "$expected" = "$actual" ] || die "checksum mismatch for $ASSET (expected $expected, got $actual)"
log "checksum ok"

mkdir "$WORK/x"
tar -xzf "$WORK/$ASSET" -C "$WORK/x" juicefs 2>/dev/null || die "archive does not contain juicefs"
[ -f "$WORK/x/juicefs" ] || die "archive does not contain juicefs"

if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR" 2>/dev/null || { use_sudo; $SUDO mkdir -p "$INSTALL_DIR"; }
fi
if [ -z "$SUDO" ] && [ ! -w "$INSTALL_DIR" ]; then
    use_sudo
fi

DEST="$INSTALL_DIR/$NAME"
if [ -x "$DEST" ]; then
    old=$("$DEST" version 2>/dev/null | head -n 1 || true)
    log "current: ${old:-unknown}"
fi

STAGED="$INSTALL_DIR/.$NAME.tmp.$$"
$SUDO cp "$WORK/x/juicefs" "$STAGED"
$SUDO chmod 0755 "$STAGED"
$SUDO mv -f "$STAGED" "$DEST"
STAGED=""

new=$("$DEST" version 2>/dev/null | head -n 1 || true)
log "installed: $DEST"
log "version:   ${new:-unknown}"
log "running mounts keep the old binary until they are remounted"
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `chmod 0755 release/install.sh release/tests/test_install.sh && bash release/tests/test_install.sh`
Expected: 12 件すべてが `ok`、最後に `passed=12 failed=0`、終了コード 0。

- [ ] **Step 5: shellcheck をかける**

手元に shellcheck が無いので、docker のイメージ `koalaman/shellcheck:stable` を使う。**イメージの取得はダウンロードになるので、実行前にユーザーに確認する。** 許可が出なければ、このステップは「未実施」と記録して次へ進む。

Run: `docker run --rm -v "$PWD/release:/mnt" koalaman/shellcheck:stable -s sh /mnt/install.sh && docker run --rm -v "$PWD/release:/mnt" koalaman/shellcheck:stable -s bash /mnt/tests/test_install.sh`
Expected: 指摘なしで終了コード 0。指摘があれば直して、Step 4 から再実行する。

- [ ] **Step 6: ユーザーに commit を依頼する**

変更ファイル：`release/install.sh`、`release/tests/test_install.sh`。commit メッセージの案：`release: add install.sh with checksum verification and tests`。agent は add・commit をしない。

---

### Task 2: install.ps1

**Files:**
- Create: `release/install.ps1`

**Interfaces:**
- Consumes: なし
- Produces: `release/install.ps1`（環境変数：`JFS_VERSION`、`JFS_INSTALL_DIR`、`JFS_DOWNLOAD_BASE`）。Task 4 の verify-windows job が `Get-Content -Raw | Invoke-Expression` の形で実行し、Release にも添付する。

手元に PowerShell と Windows が無いため、動作確認は Task 4 の workflow（`windows-latest`）で行う。このタスクでは ASCII のみであることだけを手元で確認する。

- [ ] **Step 1: install.ps1 を作る**

```powershell
# install.ps1 - download, verify and install the patched JuiceFS build
# (windows-amd64) published on the tongsama/juicefs_inspection GitHub Releases.
#
# Usage:
#   irm https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.ps1 | iex
#
# Environment:
#   JFS_VERSION        release tag to install (default: latest release)
#   JFS_INSTALL_DIR    install directory (default: %LOCALAPPDATA%\Programs\juicefs)
#   JFS_DOWNLOAD_BASE  releases base URL (default: this repository; used by tests)
#
# This script is run through Invoke-Expression, so it reports failures with
# `throw` and never calls `exit` (which would close the caller's session).
# It does not modify PATH and does not install WinFsp.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $base = 'https://github.com/tongsama/juicefs_inspection/releases'
    if ($env:JFS_DOWNLOAD_BASE) { $base = $env:JFS_DOWNLOAD_BASE.TrimEnd('/') }
    $version = $env:JFS_VERSION
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\juicefs'
    if ($env:JFS_INSTALL_DIR) { $installDir = $env:JFS_INSTALL_DIR }
    $asset = 'juicefs-windows-amd64.zip'

    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if ($arch -ne 'AMD64') { throw "unsupported architecture: $arch (supported: windows-amd64)" }

    $label = 'latest'
    $url = "$base/latest/download"
    if ($version) { $label = $version; $url = "$base/download/$version" }

    # Save-Asset downloads one release asset into $dir and tells a 404 apart
    # from other failures.
    function Save-Asset([string]$name, [string]$dir) {
        $out = Join-Path $dir $name
        try {
            Invoke-WebRequest -Uri "$url/$name" -OutFile $out -UseBasicParsing
        } catch {
            $resp = $_.Exception.Response
            if ($resp -and [int]$resp.StatusCode -eq 404) {
                throw "not found (404): $url/$name -- check that release '$label' and its assets exist"
            }
            throw "download failed: $url/$name : $($_.Exception.Message)"
        }
        return $out
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('jfs-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Write-Host "downloading $asset ($label) from $url"
        $zip = Save-Asset $asset $tmp
        $sums = Save-Asset 'checksums.txt' $tmp

        $expected = $null
        foreach ($line in Get-Content $sums) {
            $parts = $line.Trim() -split '\s+', 2
            if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $asset) {
                $expected = $parts[0].ToLower()
                break
            }
        }
        if (-not $expected) { throw "no checksum entry for $asset in checksums.txt" }
        $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
        if ($actual -ne $expected) { throw "checksum mismatch for $asset (expected $expected, got $actual)" }
        Write-Host 'checksum ok'

        $x = Join-Path $tmp 'x'
        Expand-Archive -Path $zip -DestinationPath $x
        $exe = Join-Path $x 'juicefs.exe'
        if (-not (Test-Path $exe)) { throw 'archive does not contain juicefs.exe' }

        New-Item -ItemType Directory -Force -Path $installDir | Out-Null
        $dest = Join-Path $installDir 'juicefs.exe'
        if (Test-Path $dest) {
            $old = 'unknown'
            try { $old = (& $dest version | Select-Object -First 1) } catch { }
            Write-Host "current: $old"
        }

        $staged = "$dest.new"
        Copy-Item -Force -Path $exe -Destination $staged
        try {
            Move-Item -Force -Path $staged -Destination $dest
        } catch {
            Remove-Item -Force -ErrorAction SilentlyContinue -Path $staged
            throw "cannot replace $dest (is juicefs running? stop it and retry): $($_.Exception.Message)"
        }

        $new = (& $dest version | Select-Object -First 1)
        Write-Host "installed: $dest"
        Write-Host "version:   $new"
        Write-Host "PATH was not modified; run it by full path or add $installDir to PATH yourself."

        $winfsp = @(
            "${env:ProgramFiles(x86)}\WinFsp\bin\winfsp-x64.dll",
            "$env:ProgramFiles\WinFsp\bin\winfsp-x64.dll"
        ) | Where-Object { Test-Path $_ }
        if (-not $winfsp) {
            Write-Warning 'WinFsp was not found. It is required to mount JuiceFS on Windows: https://winfsp.dev/rel/'
        }
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -Path $tmp
    }
}
```

- [ ] **Step 2: ASCII のみであり、`exit` を使っていないことを確認する**

Run: `LC_ALL=C grep -n '[^[:print:][:space:]]' release/install.ps1; echo "nonascii_exit=$?"; grep -nw 'exit' release/install.ps1 | grep -v '^\s*#' | grep -v 'calls `exit`' ; echo done`
Expected: 1 行目の grep は何も出さず `nonascii_exit=1`。2 つ目の grep は、コメント行以外に `exit` を含む行を出さない。

- [ ] **Step 3: ユーザーに commit を依頼する**

変更ファイル：`release/install.ps1`。commit メッセージの案：`release: add install.ps1 for windows-amd64`。

---

### Task 3: versions.json と最初のリリースノート

**Files:**
- Create: `release/versions.json`
- Create: `docs/release-notes/v1.4.1-kaz.1.md`

**Interfaces:**
- Consumes: なし
- Produces: `release/versions.json`。キーはタグ。値は `juicefs_repo`、`juicefs_commit`（40 桁）、`go_version`、`notes`（inspection repo のルートからの相対パス）。Task 4 の resolve job が `jq` で読む。

- [ ] **Step 1: versions.json を作る**

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

- [ ] **Step 2: リリースノートを作る**

`docs/release-notes/v1.4.1-kaz.1.md`：

```markdown
# v1.4.1-kaz.1

JuiceFS v1.4.1 をもとに、VM イメージ（qcow2）を JuiceFS に置いて動かすときの停止・データ破損を避けるための改修を加えた版です。

## 既定で変わる動作

- `--writer-flush-timeout`（既定 `0s`）：保存が終わるまで期限なしで待ちます。遅延だけを理由に EIO を返しません。旧来の期限は `auto` で選べます。
- FUSE の固定 15 分 watchdog を既定で無効にしました（遅い fsync が EINTR で打ち切られないようにするため）。
- Read の前の flush が失敗したときに、古いデータを返さずエラーを返します。
- Redis のメタデータでの copy/clone と compaction の競合を WATCH で防ぎます。

## 追加した設定（既定は従来どおり）

- `--compaction-gc-mode legacy|deferred`（既定 `legacy`）
- `--compaction-scheduler legacy|priority`（既定 `legacy`）
- `--writer-reuse-window 1..64`（既定 `4`）
- `deferred` のときは、不要になった slice のローカル staging ファイルを、リモートの DELETE を待たずに回収します。

## 注意

- 新しいモードを使うときは、同じボリュームを使うすべてのクライアントをこの版にそろえてください。
- 32bit ARM（armv7）と macOS 向けのバイナリはありません。
- 調査の経緯と既知の制約は、このリポジトリの `docs/findings.md` と `TODO.md` を参照してください。
```

- [ ] **Step 3: 内容を確認する**

Run: `jq -e '."v1.4.1-kaz.1" | (.juicefs_commit | test("^[0-9a-f]{40}$")) and (.go_version == "1.25.11")' release/versions.json && test -f docs/release-notes/v1.4.1-kaz.1.md && git -C juicefs cat-file -e 84f19ca43f77c8e52eaffc2955a42bd9ac1d4c9b^{commit} && echo ok`
Expected: `true` と `ok`。

- [ ] **Step 4: ユーザーに commit を依頼する**

変更ファイル：`release/versions.json`、`docs/release-notes/v1.4.1-kaz.1.md`。commit メッセージの案：`release: register v1.4.1-kaz.1`。

---

### Task 4: release workflow

**Files:**
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: Task 1 の `release/install.sh`、Task 2 の `release/install.ps1`、Task 3 の `release/versions.json` とリリースノート
- Produces: タグ `v*-kaz.*` の push で draft Release を作る workflow と、`workflow_dispatch`（入力 `tag`）でビルドと確認だけを行う workflow

- [ ] **Step 1: workflow を作る**

```yaml
# 改修版 JuiceFS のバイナリをビルドし、draft Release を作る。
# - タグ v<base>-kaz.<n> の push：検証 → ビルド → install.ps1 の確認 → draft Release
# - 手動実行（workflow_dispatch）：Release は作らず、検証・ビルド・確認だけを行う
# 本体 repo（versions.json の juicefs_repo）は読み取るだけで、書き込まない。
name: release

on:
  push:
    tags: ['v*-kaz.*']
  workflow_dispatch:
    inputs:
      tag:
        description: 'versions.json に登録済みのタグ（例: v1.4.1-kaz.1）'
        required: true

permissions:
  contents: read

env:
  VERSION_PKG: github.com/juicedata/juicefs/pkg/version

jobs:
  # タグと versions.json を検証し、ビルドに必要な値を後続の job へ渡す。
  resolve:
    runs-on: ubuntu-24.04
    outputs:
      tag: ${{ steps.r.outputs.tag }}
      base: ${{ steps.r.outputs.base }}
      kaz: ${{ steps.r.outputs.kaz }}
      repo: ${{ steps.r.outputs.repo }}
      commit: ${{ steps.r.outputs.commit }}
      sha8: ${{ steps.r.outputs.sha8 }}
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
          [[ "$TAG" =~ ^v([0-9]+\.[0-9]+\.[0-9]+)-(kaz\.[0-9]+)$ ]] || fail "タグの形式が不正です: $TAG"
          base=${BASH_REMATCH[1]}
          kaz=${BASH_REMATCH[2]}
          entry=$(jq -e --arg t "$TAG" '.[$t]' release/versions.json) || fail "release/versions.json に $TAG がありません"
          repo=$(jq -r '.juicefs_repo // ""' <<<"$entry")
          commit=$(jq -r '.juicefs_commit // ""' <<<"$entry")
          go_version=$(jq -r '.go_version // ""' <<<"$entry")
          notes=$(jq -r '.notes // ""' <<<"$entry")
          [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "juicefs_repo が不正です: $repo"
          [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "juicefs_commit は 40 桁の SHA にしてください: $commit"
          [[ "$go_version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || fail "go_version が不正です: $go_version"
          [ -n "$notes" ] && [ -f "$notes" ] || fail "リリースノートがありません: $notes"
          if ! got=$(gh api "repos/$repo/commits/$commit" --jq .sha 2>/dev/null) || [ "$got" != "$commit" ]; then
            fail "$repo に commit $commit がありません"
          fi
          {
            echo "tag=$TAG"; echo "base=$base"; echo "kaz=$kaz"; echo "repo=$repo"
            echo "commit=$commit"; echo "sha8=${commit:0:8}"
            echo "go_version=$go_version"; echo "notes=$notes"
          } >> "$GITHUB_OUTPUT"

  # Linux 向けを各アーキテクチャのネイティブ runner で、musl の静的リンクでビルドする。
  build-linux:
    needs: resolve
    strategy:
      fail-fast: true
      matrix:
        include:
          - target: linux-amd64
            runner: ubuntu-24.04
          - target: linux-arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: actions/checkout@v4
        with:
          repository: ${{ needs.resolve.outputs.repo }}
          ref: ${{ needs.resolve.outputs.commit }}
          path: juicefs
      - uses: actions/setup-go@v5
        with:
          go-version: ${{ needs.resolve.outputs.go_version }}
          cache-dependency-path: juicefs/go.sum
      - name: install toolchain
        run: sudo apt-get update && sudo apt-get install -y musl-tools file
      - name: build
        working-directory: juicefs
        env:
          CC: musl-gcc
          CGO_ENABLED: '1'
          REV: ${{ needs.resolve.outputs.sha8 }}-${{ needs.resolve.outputs.kaz }}
        run: |
          set -euo pipefail
          revdate=$(git log -1 --format=%cd --date=short)
          echo "REVDATE=$revdate" >> "$GITHUB_ENV"
          go build -ldflags "-s -w -X $VERSION_PKG.revision=$REV -X $VERSION_PKG.revisionDate=$revdate -linkmode external -extldflags '-static'" -o juicefs .
      - name: verify and package
        working-directory: juicefs
        env:
          WANT_PREFIX: juicefs version ${{ needs.resolve.outputs.base }}+
          WANT_SUFFIX: .${{ needs.resolve.outputs.sha8 }}-${{ needs.resolve.outputs.kaz }}
          TARGET: ${{ matrix.target }}
        run: |
          set -euo pipefail
          file juicefs
          file juicefs | grep -Eq 'statically linked|static-pie linked' || { echo "::error::静的リンクになっていません"; exit 1; }
          got=$(./juicefs version)
          want="${WANT_PREFIX}${REVDATE}${WANT_SUFFIX}"
          [ "$got" = "$want" ] || { echo "::error::version が一致しません: got '$got' want '$want'"; exit 1; }
          tar -czf "juicefs-$TARGET.tar.gz" juicefs
      - uses: actions/upload-artifact@v4
        with:
          name: juicefs-${{ matrix.target }}
          path: juicefs/juicefs-${{ matrix.target }}.tar.gz
          if-no-files-found: error

  # Windows 向けを mingw-w64 と本体同梱の WinFsp ヘッダーでクロスビルドする。
  build-windows:
    needs: resolve
    runs-on: ubuntu-24.04
    outputs:
      expected: ${{ steps.b.outputs.expected }}
    steps:
      - uses: actions/checkout@v4
        with:
          repository: ${{ needs.resolve.outputs.repo }}
          ref: ${{ needs.resolve.outputs.commit }}
          path: juicefs
      - uses: actions/setup-go@v5
        with:
          go-version: ${{ needs.resolve.outputs.go_version }}
          cache-dependency-path: juicefs/go.sum
      - name: install toolchain
        run: |
          sudo apt-get update && sudo apt-get install -y gcc-mingw-w64-x86-64 file zip
          sudo mkdir -p /usr/local/include/winfsp
          sudo cp juicefs/hack/winfsp_headers/* /usr/local/include/winfsp/
      - id: b
        name: build, verify and package
        working-directory: juicefs
        env:
          GOOS: windows
          GOARCH: amd64
          CGO_ENABLED: '1'
          CC: x86_64-w64-mingw32-gcc
          REV: ${{ needs.resolve.outputs.sha8 }}-${{ needs.resolve.outputs.kaz }}
          BASE: ${{ needs.resolve.outputs.base }}
        run: |
          set -euo pipefail
          revdate=$(git log -1 --format=%cd --date=short)
          go build -buildmode exe -ldflags "-s -w -X $VERSION_PKG.revision=$REV -X $VERSION_PKG.revisionDate=$revdate" -o juicefs.exe .
          file juicefs.exe
          file juicefs.exe | grep -q 'PE32+ executable' || { echo "::error::PE32+ ではありません"; exit 1; }
          file juicefs.exe | grep -q 'x86-64' || { echo "::error::x86-64 ではありません"; exit 1; }
          zip -j juicefs-windows-amd64.zip juicefs.exe
          echo "expected=juicefs version ${BASE}+${revdate}.${REV}" >> "$GITHUB_OUTPUT"
      - uses: actions/upload-artifact@v4
        with:
          name: juicefs-windows-amd64
          path: juicefs/juicefs-windows-amd64.zip
          if-no-files-found: error

  # ビルドした zip を手元で配信し、install.ps1 を iex の形で実行して確認する。
  verify-windows:
    needs: [resolve, build-windows]
    runs-on: windows-latest
    defaults:
      run:
        shell: pwsh
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: juicefs-windows-amd64
          path: srv/download/${{ needs.resolve.outputs.tag }}
      - name: install.ps1 (success and checksum mismatch)
        env:
          TAG: ${{ needs.resolve.outputs.tag }}
          JFS_VERSION: ${{ needs.resolve.outputs.tag }}
          JFS_DOWNLOAD_BASE: http://127.0.0.1:8765
          JFS_INSTALL_DIR: ${{ runner.temp }}\jfs
          EXPECTED: ${{ needs.build-windows.outputs.expected }}
        run: |
          $dir = "srv/download/$env:TAG"
          $h = (Get-FileHash "$dir/juicefs-windows-amd64.zip" -Algorithm SHA256).Hash.ToLower()
          "$h  juicefs-windows-amd64.zip" | Set-Content -Encoding ascii "$dir/checksums.txt"
          $srv = Start-Process python -ArgumentList '-m','http.server','8765','--bind','127.0.0.1','--directory','srv' -PassThru -WindowStyle Hidden
          try {
            for ($i = 0; $i -lt 50; $i++) {
              try { Invoke-WebRequest http://127.0.0.1:8765/ -UseBasicParsing | Out-Null; break } catch { Start-Sleep -Milliseconds 200 }
            }
            # 1. 正常にインストールでき、期待した版が表示される
            Get-Content release/install.ps1 -Raw | Invoke-Expression
            $got = & "$env:JFS_INSTALL_DIR\juicefs.exe" version | Select-Object -First 1
            if ($got -ne $env:EXPECTED) { throw "version mismatch: got '$got' want '$env:EXPECTED'" }
            # 2. sha256 が合わなければ失敗し、既存の exe は変わらない
            $before = (Get-FileHash "$env:JFS_INSTALL_DIR\juicefs.exe").Hash
            ('0' * 64) + '  juicefs-windows-amd64.zip' | Set-Content -Encoding ascii "$dir/checksums.txt"
            $failed = $false
            try { Get-Content release/install.ps1 -Raw | Invoke-Expression } catch { $failed = $true; Write-Host "expected failure: $_" }
            if (-not $failed) { throw 'checksum mismatch was not detected' }
            if ((Get-FileHash "$env:JFS_INSTALL_DIR\juicefs.exe").Hash -ne $before) { throw 'existing juicefs.exe was modified' }
            Write-Host 'install.ps1 checks passed'
          } finally {
            Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
          }

  # すべて成功したときだけ、タグの push で draft Release を作る。
  release:
    if: github.event_name == 'push'
    needs: [resolve, build-linux, build-windows, verify-windows]
    runs-on: ubuntu-24.04
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          path: dist
          pattern: juicefs-*
          merge-multiple: true
      - name: assemble
        run: |
          set -euo pipefail
          cp release/install.sh release/install.ps1 dist/
          cd dist
          for f in juicefs-linux-amd64.tar.gz juicefs-linux-arm64.tar.gz juicefs-windows-amd64.zip install.sh install.ps1; do
            [ -f "$f" ] || { echo "::error::$f がありません"; exit 1; }
          done
          sha256sum juicefs-linux-amd64.tar.gz juicefs-linux-arm64.tar.gz juicefs-windows-amd64.zip install.sh install.ps1 > checksums.txt
          cat checksums.txt
      - name: create draft release
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ needs.resolve.outputs.tag }}
          NOTES: ${{ needs.resolve.outputs.notes }}
          JREPO: ${{ needs.resolve.outputs.repo }}
          COMMIT: ${{ needs.resolve.outputs.commit }}
          GOV: ${{ needs.resolve.outputs.go_version }}
        run: |
          set -euo pipefail
          if gh release list --repo "$GITHUB_REPOSITORY" --limit 1000 --json tagName --jq '.[].tagName' | grep -qxF "$TAG"; then
            echo "::error::$TAG の Release はすでにあります（draft を含む）。上書きしません"; exit 1
          fi
          { cat "$NOTES"; printf '\n\n---\nビルド元: https://github.com/%s/commit/%s（Go %s）\n' "$JREPO" "$COMMIT" "$GOV"; } > notes.md
          gh release create "$TAG" --repo "$GITHUB_REPOSITORY" --draft --verify-tag --title "$TAG" --notes-file notes.md dist/*
```

- [ ] **Step 2: YAML の文法と actionlint を確認する**

Run: `python3 -c 'import yaml,sys; yaml.safe_load(open(".github/workflows/release.yml")); print("yaml ok")'`
Expected: `yaml ok`（PyYAML が無ければ `pip install --user pyyaml` をユーザーに確認してから行う。許可が無ければ次の actionlint だけにする）。

actionlint は docker イメージ `rhysd/actionlint:latest` を使う。**イメージの取得前にユーザーに確認する。**
Run: `docker run --rm -v "$PWD:/repo" --workdir /repo rhysd/actionlint:latest -no-color`
Expected: 指摘なしで終了コード 0。指摘があれば直して再実行する。

- [ ] **Step 3: ユーザーに commit を依頼する**

変更ファイル：`.github/workflows/release.yml`。commit メッセージの案：`ci: add release workflow for patched juicefs binaries`。

---

### Task 5: 手動実行モードでの試行（ユーザーの push が前提）

**Files:**
- Modify（失敗した場合のみ）：`.github/workflows/release.yml`、`release/install.ps1`

**Interfaces:**
- Consumes: Task 1〜4 の成果物が `original/main` に push されていること
- Produces: 3 種類のビルドと install.ps1 の確認が通った workflow の実行結果

- [ ] **Step 1: ユーザーに push と手動実行を依頼する**

ユーザーが Task 1〜4 の commit を `git push original main` で送る。手動実行は、ユーザーの許可を得て `gh workflow run release.yml --repo tongsama/juicefs_inspection -f tag=v1.4.1-kaz.1` で起動する（GitHub の画面の「Run workflow」からでもよい）。

- [ ] **Step 2: 結果を確認する**

Run: `gh run list --repo tongsama/juicefs_inspection --workflow release.yml --limit 1` で run ID を確認し、`gh run view <ID> --repo tongsama/juicefs_inspection --log-failed`
Expected: `resolve`、`build-linux (linux-amd64)`、`build-linux (linux-arm64)`、`build-windows`、`verify-windows` が success。`release` は skipped。

- [ ] **Step 3: 失敗したときの対応**

- **build-windows が Go のコンパイルエラーで失敗した場合**：本体側の修正が必要になる。本計画の範囲外なので、エラーをそのままユーザーに報告して止める（本体の修正は別途の設計とテストで行う）。
- **verify-windows で `juicefs.exe version` が WinFsp の DLL 不足で失敗した場合**：verify-windows の checkout の直後に、`- run: choco install winfsp -y --no-progress` のステップを追加して再実行する。
- **musl の静的リンクで、特定ライブラリのリンクエラーが出た場合**：エラーをユーザーに報告し、対処方針を相談する（upstream の goreleaser と同じ構成なので、通常は起きない想定）。
- workflow を直した場合は、ユーザーに commit と push を依頼して Step 1 から再実行する。

- [ ] **Step 4: 成果物を手元で確かめる**

Run: `gh run download <ID> --repo tongsama/juicefs_inspection -D "$SCRATCH/run" && tar -xzf "$SCRATCH/run/juicefs-linux-amd64/juicefs-linux-amd64.tar.gz" -C "$SCRATCH/run" && "$SCRATCH/run/juicefs" version`（`$SCRATCH` はセッションの scratchpad ディレクトリ）
Expected: `juicefs version 1.4.1+2026-10-03.84f19ca4-kaz.1`

---

### Task 6: 最初の draft Release（ユーザーの指示が前提）

**Files:** なし（タグの作成と push だけ）

**Interfaces:**
- Consumes: Task 5 の成功
- Produces: inspection repo の draft Release `v1.4.1-kaz.1`

- [ ] **Step 1: ユーザーにタグの作成と push を依頼する**

ユーザーが `git tag v1.4.1-kaz.1 && git push original v1.4.1-kaz.1` を実行する（agent が行う場合は、その都度の明示的な許可が必要）。

- [ ] **Step 2: draft Release ができたことを確認する**

Run: `gh release view v1.4.1-kaz.1 --repo tongsama/juicefs_inspection --json isDraft,assets --jq '{isDraft, assets: [.assets[].name]}'`
Expected: `isDraft: true` と、6 ファイル（`checksums.txt`、`install.ps1`、`install.sh`、`juicefs-linux-amd64.tar.gz`、`juicefs-linux-arm64.tar.gz`、`juicefs-windows-amd64.zip`）。

- [ ] **Step 3: ユーザーが GitHub の画面で公開する**

公開はユーザーが行う。agent は公開しない。

- [ ] **Step 4: 公開後に、実際の URL から手元でインストールできることを確かめる**

Run: `curl -fsSL https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.sh | sh -s "$SCRATCH/bin" && "$SCRATCH/bin/juicefs" version`
Expected: `checksum ok` が出て、`juicefs version 1.4.1+2026-10-03.84f19ca4-kaz.1`。システムの `/usr/local/bin` には入れない。

- [ ] **Step 5: 記録を残す**

`agent_memo.md` と `TODO.md` に、Release の URL、使った本体の commit、確認結果を追記し、`docs/superpowers/README.md` の一覧の「実装計画」欄をこの計画へのリンクにする。commit はユーザーに依頼する。
