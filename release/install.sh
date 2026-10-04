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
# the SHA-256 verification succeed. Running mounts are not restarted, but the
# mount supervisor re-executes this path when its child crashes, so an
# auto-restart (or any new mount) picks up the new binary.
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
log "running mounts are not restarted, but a supervisor auto-restart after a crash (or any new mount) uses the new binary"
