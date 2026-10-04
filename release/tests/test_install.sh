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

# 13. 置き換え後のメッセージが、mount の supervisor による自動再起動で新版が使われることを伝える。
if grep -q 'auto-restart' "$T/c1.log"; then ok "restart notice"; else ng "restart notice"; cat "$T/c1.log"; fi

# 12. 配布スクリプトは ASCII のみ。
if [ -f "$INSTALL_SH" ] && ! LC_ALL=C grep -n '[^[:print:][:space:]]' "$INSTALL_SH" >/dev/null; then ok "install.sh is ASCII"; else ng "install.sh is ASCII"; fi

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
