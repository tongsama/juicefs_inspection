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

case "$PRODUCT" in
    juicefs|rclone) ;;
    "") die "usage: install.sh <juicefs|rclone> [install-dir]" ;;
    *) die "unknown product: $PRODUCT (expected juicefs or rclone)" ;;
esac
NAME="${KAZ_INSTALL_NAME:-$PRODUCT}"

need curl
need tar
need mktemp
need awk
need id
need sed
need tr
need sort
need tail

WORK=$(mktemp -d)
if [ -z "$VERSION" ]; then
    # latest_tag runs in a subshell, so its die cannot stop this script.
    if ! VERSION=$(latest_tag); then exit 1; fi
    log "newest $PRODUCT release: $VERSION"
fi
TARGET=$(detect_target)
ASSET="$PRODUCT-$TARGET.tar.gz"
URL="$BASE/download/$VERSION"
log "downloading $ASSET ($VERSION) from $URL"
fetch "$URL/$ASSET" "$WORK/$ASSET" asset
fetch "$URL/checksums.txt" "$WORK/checksums.txt" asset

expected=$(awk -v f="$ASSET" '$2 == f || $2 == ("*" f) { print $1; exit }' "$WORK/checksums.txt")
[ -n "$expected" ] || die "no checksum entry for $ASSET in checksums.txt"
actual=$(sha256_of "$WORK/$ASSET")
[ "$expected" = "$actual" ] || die "checksum mismatch for $ASSET (expected $expected, got $actual)"
log "checksum ok"

mkdir "$WORK/x"
tar -xzf "$WORK/$ASSET" -C "$WORK/x" "$PRODUCT" 2>/dev/null || die "archive does not contain $PRODUCT"
[ -f "$WORK/x/$PRODUCT" ] || die "archive does not contain $PRODUCT"

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
$SUDO cp "$WORK/x/$PRODUCT" "$STAGED"
$SUDO chmod 0755 "$STAGED"
$SUDO mv -f "$STAGED" "$DEST"
STAGED=""

new=$("$DEST" version 2>/dev/null | head -n 1 || true)
log "installed: $DEST"
log "version:   ${new:-unknown}"
# Another copy of the same name elsewhere keeps being used by scripts
# calling it by full path, or by PATH lookups that find it first.
seen=""
for other in $(command -v "$NAME" 2>/dev/null || true) /usr/bin/"$NAME" /usr/local/bin/"$NAME" /bin/"$NAME"; do
    [ -x "$other" ] && [ "$other" != "$DEST" ] || continue
    case " $seen " in *" $other "*) continue ;; esac
    seen="$seen $other"
    log "warning: another $NAME exists at $other and is not replaced; scripts or PATH lookups using it keep the old binary"
done
case "$PRODUCT" in
    juicefs) log "running mounts are not restarted, but a supervisor auto-restart after a crash (or any new mount) uses the new binary" ;;
    rclone) log "a running rclone keeps using the old binary until it is restarted" ;;
esac
