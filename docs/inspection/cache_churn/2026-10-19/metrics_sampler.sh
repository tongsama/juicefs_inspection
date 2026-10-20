#!/usr/bin/env bash
# A-1: JuiceFS mount の /metrics を一定間隔で読み取り、キャッシュの流入・追い出し・
# ヒット／ミス・compaction・object 要求・kaz-2q（kaz_cache_2q_*）に関する値だけを TSV に追記する（読み取りのみ）。
# 使い方: metrics_sampler.sh <出力TSV> [間隔秒(既定300)] [metrics URL]
# 出力列: 取得時刻(UNIX秒) / 取得時刻(ISO) / metric 名と区別用ラベル / 値
set -u
out="${1:?usage: metrics_sampler.sh OUT.tsv [interval] [url]}"
interval="${2:-300}"
url="${3:-http://127.0.0.1:9567/metrics}"

# sample_once: 1 回分の metrics を取得し、対象の系列を共通ラベルを除いて追記する。
sample_once() {
  local now iso body
  now=$(date +%s)
  iso=$(date -d "@$now" +%Y-%m-%dT%H:%M:%S%z)
  body=$(curl -s --max-time 20 "$url") || { printf '%s\t%s\t_scrape_error\t1\n' "$now" "$iso" >>"$out"; return; }
  printf '%s\n' "$body" \
    | grep -E '^juicefs_(blockcache_|staging_|compact_size_histogram_bytes_(sum|count)|compaction_gc_events_total|object_request_data_bytes|object_request_durations_histogram_seconds_(sum|count)|uptime|fuse_ops_durations_histogram_seconds_count|kaz_cache_2q_)' \
    | grep -v '_bucket{' \
    | sed -E 's/(instance|juicefs_version|mp|vol_name|storage_class)="[^"]*",?//g; s/,\}/}/; s/\{\}//' \
    | awk -v t="$now" -v i="$iso" '{v=$NF; $NF=""; sub(/ +$/,""); printf "%s\t%s\t%s\t%s\n", t, i, $0, v}' >>"$out"
}

while true; do
  sample_once
  sleep "$interval"
done
