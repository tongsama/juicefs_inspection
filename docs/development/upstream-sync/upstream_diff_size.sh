#!/bin/sh
# 本家（upstream）のファイルに対する kaz の差分量を数える。
# kaz 専用のファイル（パスに _kaz を含む、ファイル名が kaz_ で始まる、_test.go、testdata/、
# base に無く追加されたファイル）は除き、残りの追加行・削除行を合計する。
#
# 使い方: upstream_diff_size.sh <本体リポジトリ> <base> <head>
#   例: upstream_diff_size.sh juicefs v1.4.2 HEAD
# 出力（1 行）: files=N add=A del=D
set -eu

repo=$1
base=$2
head=$3

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# pipefail が無いので、git の出力は先にファイルへ書いて終了コードを確かめる。
git -C "$repo" diff --no-renames --diff-filter=A --name-only "$base" "$head" >"$tmp/added"
git -C "$repo" diff --no-renames --numstat "$base" "$head" >"$tmp/numstat"

awk -F '\t' -v added="$tmp/added" '
	BEGIN { while ((getline line < added) > 0) isadded[line] = 1 }
	{
		f = $3
		n = split(f, parts, "/")
		name = parts[n]
		if (index(f, "_kaz") || substr(name, 1, 4) == "kaz_" || f ~ /_test\.go$/ || f ~ /(^|\/)testdata\// || (f in isadded)) next
		if ($1 == "-") next # binary
		files++; add += $1; del += $2
	}
	END { printf "files=%d add=%d del=%d\n", files, add, del }' "$tmp/numstat"
