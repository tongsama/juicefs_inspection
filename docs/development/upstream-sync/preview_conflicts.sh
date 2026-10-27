#!/bin/sh
# 本家の新しい版（タグや origin/main）へ kaz を載せる前に、衝突を数える。
# 作業ツリーにもブランチにも触れない（git merge-tree --write-tree だけを使う）。
#
# 使い方: preview_conflicts.sh <本体リポジトリ> <kaz の ref> <本家の ref> [<比較の基準にする本家の ref>]
#   例: preview_conflicts.sh juicefs 1.4.2-improve-kaz origin/main v1.4.2
# 4 番目を与えると、その ref と本家の ref のあいだで元から起きる衝突（kaz と無関係）を
# 基準として並べ、kaz が原因で増えた衝突ブロックの数を出す。
set -eu

repo=$1
kaz=$2
upstream=$3
base=${4:-}

# conflict_blocks <ref>: 衝突したファイルごとに「ファイル名 ブロック数」を出す。
conflict_blocks() {
	tree=$(git -C "$repo" merge-tree --write-tree "$1" "$upstream" 2>/dev/null | head -n 1) || true
	git -C "$repo" merge-tree --write-tree --name-only "$1" "$upstream" 2>/dev/null |
		sed -n 's/^CONFLICT (content): Merge conflict in //p' |
		while read -r f; do
			printf '%s %s\n' "$f" "$(git -C "$repo" cat-file -p "$tree:$f" | grep -c '^<<<<<<<')"
		done | sort
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
conflict_blocks "$kaz" >"$tmp/kaz"
if [ -z "$base" ]; then
	printf 'file blocks\n'
	cat "$tmp/kaz"
	exit 0
fi
conflict_blocks "$base" >"$tmp/base"
join -a1 -a2 -e0 -o 0,1.2,2.2 "$tmp/base" "$tmp/kaz" |
	awk 'BEGIN { print "file base kaz added" }
	     { print $1, $2, $3, $3 - $2; b += $2; k += $3 }
	     END { print "TOTAL", b, k, k - b }' | column -t
# 注意: テキストの衝突だけを数える。改名などの意味の衝突（ビルドが壊れる）は出ない。
