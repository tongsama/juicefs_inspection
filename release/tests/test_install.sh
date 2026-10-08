#!/usr/bin/env bash
# release/install.sh の動作を、手元の HTTP サーバーと偽の配布物・偽の GitHub API で確認するテスト。
# 実行: bash release/tests/test_install.sh   （root 以外で実行すること）
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
INSTALL_SH="$ROOT/release/install.sh"
T=$(mktemp -d)
SRV="$T/srv"
FAKEBIN="$T/fakebin"
FAKEBIN2="$T/fakebin2"
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

# run_install は、偽の uname とテスト用の配信元・API を使って install.sh を実行する。
# 使い方: run_install <ログ> [VAR=値 ...] -- <製品> <インストール先>
run_install() {
  local log=$1; shift
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done
  shift
  env PATH="$FAKEBIN:$PATH" KAZ_DOWNLOAD_BASE="$BASE" KAZ_API_BASE="$BASE/api" "${envs[@]}" sh "$INSTALL_SH" "$@" > "$log" 2>&1
}

# 偽の uname（FAKE_UNAME_S / FAKE_UNAME_M で OS とアーキテクチャを差し替える）。
mkdir -p "$FAKEBIN" "$FAKEBIN2"
cat > "$FAKEBIN/uname" <<EOF
#!/bin/sh
case "\$1" in
  -s) echo "\${FAKE_UNAME_S:-Linux}" ;;
  -m) echo "\${FAKE_UNAME_M:-x86_64}" ;;
  *) exec "$REAL_UNAME" "\$@" ;;
esac
EOF
chmod 0755 "$FAKEBIN/uname"

# 配信する偽の Release と API の応答。
for t in rclone-v1.75.1-kaz.2 rclone-v1.75.1-kaz.10; do make_release "$SRV/download/$t" rclone "$t"; done
for t in juicefs-v1.4.1-kaz.4 v1.4.1-kaz.3; do make_release "$SRV/download/$t" juicefs "$t"; done
make_release "$SRV/download/rclone-v1.75.1-kaz.91" rclone rclone-v1.75.1-kaz.91
sed -i '1s/^[0-9a-f]\{64\}/0000000000000000000000000000000000000000000000000000000000000000/' "$SRV/download/rclone-v1.75.1-kaz.91/checksums.txt"
make_release "$SRV/download/rclone-v1.75.1-kaz.92" rclone rclone-v1.75.1-kaz.92
: > "$SRV/download/rclone-v1.75.1-kaz.92/checksums.txt"
# 新旧が混ざった一覧（API の並びは新しい順とは限らない）
make_api api/releases rclone-v1.75.1-kaz.2 juicefs-v1.4.1-kaz.4 v1.4.1-kaz.3 rclone-v1.75.1-kaz.10 v1.4.1-kaz.1
# 古い形だけの一覧
make_api legacy/releases v1.4.1-kaz.1 v1.4.1-kaz.3 v1.4.1-kaz.2
python3 "$ROOT/release/tests/server.py" "$PORT" "$SRV" > "$T/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  if curl -s -o /dev/null "$BASE/"; then break; fi
  sleep 0.1
done

# 1. rclone の最新（kaz.10 を数値として最大に選ぶ）。
d="$T/c1"; mkdir -p "$d"
if run_install "$T/c1.log" -- rclone "$d" && "$d/rclone" | grep -q 'rclone-v1.75.1-kaz.10 linux-amd64'; then ok "rclone newest"; else ng "rclone newest"; cat "$T/c1.log"; fi

# 2. juicefs の最新（新旧の形が混在）。
d="$T/c2"; mkdir -p "$d"
if run_install "$T/c2.log" -- juicefs "$d" && "$d/juicefs" | grep -q 'juicefs-v1.4.1-kaz.4 linux-amd64'; then ok "juicefs newest (mixed)"; else ng "juicefs newest (mixed)"; cat "$T/c2.log"; fi

# 3. juicefs の最新（古い形だけ）。
d="$T/c3"; mkdir -p "$d"
if run_install "$T/c3.log" KAZ_API_BASE="$BASE/legacy" -- juicefs "$d" && "$d/juicefs" | grep -q 'v1.4.1-kaz.3 linux-amd64'; then ok "juicefs newest (legacy only)"; else ng "juicefs newest (legacy only)"; cat "$T/c3.log"; fi

# 4. 版の固定は API を使わない（/noapi は 404 なので、使えば失敗する）。
d="$T/c4"; mkdir -p "$d"
if run_install "$T/c4.log" KAZ_VERSION=rclone-v1.75.1-kaz.2 KAZ_API_BASE="$BASE/noapi" -- rclone "$d" && "$d/rclone" | grep -q 'kaz.2 linux-amd64'; then ok "pinned version skips API"; else ng "pinned version skips API"; cat "$T/c4.log"; fi

# 5. API の回数制限。既存のファイルは変わらない。
d="$T/c5"; mkdir -p "$d"; printf 'old\n' > "$d/rclone"; cp "$d/rclone" "$T/c5.orig"
if ! run_install "$T/c5.log" KAZ_API_BASE="$BASE/ratelimit" -- rclone "$d" && grep -q 'rate limit' "$T/c5.log" && grep -q 'KAZ_VERSION' "$T/c5.log" && cmp -s "$d/rclone" "$T/c5.orig"; then ok "API rate limit"; else ng "API rate limit"; cat "$T/c5.log"; fi

# 6. API の他の失敗。
d="$T/c6"; mkdir -p "$d"
if ! run_install "$T/c6.log" KAZ_API_BASE="$BASE/noapi" -- rclone "$d" && grep -q 'GitHub API request failed (HTTP 404)' "$T/c6.log" && [ ! -e "$d/rclone" ]; then ok "API failure"; else ng "API failure"; cat "$T/c6.log"; fi

# 7. 製品名なし・不明な製品。
d="$T/c7"; mkdir -p "$d"
if ! run_install "$T/c7a.log" -- && grep -q 'usage:' "$T/c7a.log" \
   && ! run_install "$T/c7b.log" -- s3fs "$d" && grep -q 'unknown product: s3fs' "$T/c7b.log"; then ok "product argument"; else ng "product argument"; cat "$T/c7a.log" "$T/c7b.log"; fi

# 8. 名前の指定と、存在しないインストール先の作成。
d="$T/c8/sub/bin"
if run_install "$T/c8.log" KAZ_INSTALL_NAME=rclone-kaz -- rclone "$d" && [ -x "$d/rclone-kaz" ] && [ ! -e "$d/rclone" ]; then ok "custom name and new dir"; else ng "custom name and new dir"; cat "$T/c8.log"; fi

# 9. aarch64 では arm64 版を選ぶ。
d="$T/c9"; mkdir -p "$d"
if run_install "$T/c9.log" FAKE_UNAME_M=aarch64 -- rclone "$d" && "$d/rclone" | grep -q 'linux-arm64'; then ok "arm64 detection"; else ng "arm64 detection"; cat "$T/c9.log"; fi

# 10. sha256 の不一致で中止し、既存のファイルを変えない。
d="$T/c10"; mkdir -p "$d"; printf 'old\n' > "$d/rclone"; cp "$d/rclone" "$T/c10.orig"
if ! run_install "$T/c10.log" KAZ_VERSION=rclone-v1.75.1-kaz.91 -- rclone "$d" && grep -q 'checksum mismatch' "$T/c10.log" && cmp -s "$d/rclone" "$T/c10.orig"; then ok "checksum mismatch keeps existing"; else ng "checksum mismatch keeps existing"; cat "$T/c10.log"; fi

# 11. 存在しない版は 404 として中止し、何も作らない。
d="$T/c11"; mkdir -p "$d"
if ! run_install "$T/c11.log" KAZ_VERSION=rclone-v9.9.9-kaz.9 -- rclone "$d" && grep -q 'not found (404)' "$T/c11.log" && [ ! -e "$d/rclone" ]; then ok "404"; else ng "404"; cat "$T/c11.log"; fi

# 12. 対応していないアーキテクチャ・OS。
d="$T/c12"; mkdir -p "$d"
if ! run_install "$T/c12a.log" FAKE_UNAME_M=armv7l -- rclone "$d" && grep -q 'unsupported architecture: armv7l' "$T/c12a.log" && [ ! -e "$d/rclone" ] \
   && ! run_install "$T/c12b.log" FAKE_UNAME_S=Darwin -- rclone "$d" && grep -q 'unsupported OS: Darwin' "$T/c12b.log"; then ok "unsupported arch and OS"; else ng "unsupported arch and OS"; cat "$T/c12a.log" "$T/c12b.log"; fi

# 13. 既存のバイナリを置き換え、旧版と新版を表示し、一時ファイルを残さない。
d="$T/c13"; mkdir -p "$d"
printf '#!/bin/sh\necho "rclone version OLD"\n' > "$d/rclone"; chmod 0755 "$d/rclone"
if run_install "$T/c13.log" -- rclone "$d" && grep -q 'OLD' "$T/c13.log" && grep -q 'kaz.10' "$T/c13.log" \
   && "$d/rclone" | grep -q 'kaz.10' && [ "$(ls -A "$d")" = "rclone" ]; then ok "replace existing"; else ng "replace existing"; cat "$T/c13.log"; ls -A "$d"; fi

# 14. 別の場所に同名ファイルがあれば警告する。
d="$T/c14"; mkdir -p "$d"
printf '#!/bin/sh\necho other\n' > "$FAKEBIN2/rclone"; chmod 0755 "$FAKEBIN2/rclone"
if run_install "$T/c14.log" PATH="$FAKEBIN2:$FAKEBIN:$PATH" -- rclone "$d" && grep -q 'another rclone' "$T/c14.log" && grep -q "$FAKEBIN2/rclone" "$T/c14.log"; then ok "warn about another copy"; else ng "warn about another copy"; cat "$T/c14.log"; fi

# 14b. 同じディレクトリを別名（symlink）で PATH に置いても、同じファイルなので警告しない。
mkdir -p "$T/real"; ln -s "$T/real" "$T/link"
if run_install "$T/c14b.log" PATH="$T/link:$FAKEBIN:$PATH" -- rclone "$T/real" && ! grep -q "another rclone exists at $T/" "$T/c14b.log"; then ok "no warning for symlinked same file"; else ng "no warning for symlinked same file"; cat "$T/c14b.log"; fi

# 14c. 他製品の版を KAZ_VERSION に指定したら、何も作らず中止する。
d="$T/c14c"; mkdir -p "$d"
if ! run_install "$T/c14c.log" KAZ_VERSION=v1.4.1-kaz.3 -- rclone "$d" && grep -q 'is not a rclone release tag' "$T/c14c.log" && [ ! -e "$d/rclone" ] && [ -z "$(ls -A "$d")" ]; then ok "pinned version of another product"; else ng "pinned version of another product"; cat "$T/c14c.log"; fi

# 15. 書き込めず sudo も無ければ、既存のファイルに触れずに中止する。
if [ "$(id -u)" -eq 0 ]; then
  echo "skip - not writable without sudo (running as root)"
else
  TOOLS="$T/tools"; mkdir -p "$TOOLS"
  for c in curl tar gzip mktemp awk sed tr sort tail sha256sum id cp chmod mv rm mkdir head cat; do ln -s "$(command -v "$c")" "$TOOLS/$c"; done
  d="$T/c15"; mkdir -p "$d"; printf 'old\n' > "$d/rclone"; cp "$d/rclone" "$T/c15.orig"; chmod 0555 "$d"
  if ! env PATH="$FAKEBIN:$TOOLS" KAZ_DOWNLOAD_BASE="$BASE" KAZ_API_BASE="$BASE/api" /bin/sh "$INSTALL_SH" rclone "$d" > "$T/c15.log" 2>&1 \
     && grep -q 'sudo is not available' "$T/c15.log" && cmp -s "$d/rclone" "$T/c15.orig"; then ok "not writable without sudo"; else ng "not writable without sudo"; cat "$T/c15.log"; fi
  chmod 0755 "$d"
fi

# 16. checksums.txt に該当する行が無ければ中止する。
d="$T/c16"; mkdir -p "$d"
if ! run_install "$T/c16.log" KAZ_VERSION=rclone-v1.75.1-kaz.92 -- rclone "$d" && grep -q 'no checksum entry' "$T/c16.log" && [ ! -e "$d/rclone" ]; then ok "missing checksum entry"; else ng "missing checksum entry"; cat "$T/c16.log"; fi

# 17. 再起動の案内（rclone は restart、juicefs は supervisor の auto-restart）。
if grep -q 'restart' "$T/c1.log" && grep -q 'auto-restart' "$T/c2.log"; then ok "restart notice"; else ng "restart notice"; cat "$T/c1.log" "$T/c2.log"; fi

# 18. 配布スクリプトは ASCII のみ。
if [ -f "$INSTALL_SH" ] && ! LC_ALL=C grep -n '[^[:print:][:space:]]' "$INSTALL_SH" >/dev/null; then ok "install.sh is ASCII"; else ng "install.sh is ASCII"; fi

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
