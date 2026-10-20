#!/usr/bin/env bash
# 次の再起動の目安を見る（読み取りのみ）。
# 1) metrics.tsv から、記録開始以降にキャッシュへ入った量（blockcache_write_bytes の増分）
# 2) keys-events.tsv から、消えたブロックのうち、最終アクセスが指定時刻（既定は 10/19 の起動）以降のもの
#    （今回の起動で読んだブロックが追い出され始めたかの目安。retire で消えたものも含む）
# 使い方: progress.sh [起動時刻 'YYYY-MM-DD HH:MM'(既定 2026-10-19 09:55)]
cd "$(dirname "$0")"
since=$(date -d "${1:-2026-10-19 09:55}" +%s)
awk -F'\t' '$3=="juicefs_blockcache_write_bytes"{if(!f){f=$4;ft=$2} l=$4;lt=$2} END{printf "inflow %s -> %s: %.2f GiB\n", ft, lt, (l-f)/2^30}' metrics.tsv
awk -F'\t' -v s="$since" '$2=="del"{n++; b+=$6; if($4>=s){m++; c+=$6}} END{printf "deleted blocks: %d (%.2f GiB), of which last access after boot: %d (%.2f GiB)\n", n, b/2^30, m, c/2^30}' keys-events.tsv
