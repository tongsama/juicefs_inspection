#!/usr/bin/env bash
# kaz-2q のキャッシュの使われ方を、指定した期間で集計する（読み取りのみ）。
# metrics_sampler.sh が書いた metrics.tsv を読み、キャッシュの容量や probation-ratio を決める材料を出す。
#   - キャッシュへの流入（書き込み・compaction の出力・GET の補充）と、1 時間あたりの量
#   - ミスと GET（キャッシュが足りないと増える）
#   - 2Q の昇格・降格・追い出し・追い出し済みリストのヒット（ghost_hit が多いほど、追い出した後にまた読まれている）。
#     ghost_hit は追い出す前の区分ごとにも出す（降格を経た／常連用に上がらなかった／compaction の出力／常連用から）
#   - 期間の終わりの区分ごとの容量と、追い出し済みリストの容量
# カウンタは mount し直すと 0 に戻るので、増えた分だけを足して扱う。
#
# 使い方: progress_kaz.sh [-f metrics.tsv] [開始] [終了]
#   開始・終了は date -d が読める日時（例 '2026-10-19 16:45'、'3 hours ago'、'today 09:00'）。
#   終了の既定は実行した時刻、開始の既定は終了の 24 時間前。
set -euo pipefail

file="$(dirname "$0")/metrics.tsv"
if [ "${1:-}" = "-f" ]; then
  file=$2
  shift 2
fi
end=$(date -d "${2:-now}" +%s)
start=$(date -d "${1:-@$((end - 86400))}" +%s)
if [ "$start" -ge "$end" ]; then
  echo "開始 ($(date -d "@$start" '+%F %T')) が終了 ($(date -d "@$end" '+%F %T')) より後です" >&2
  exit 2
fi

awk -F'\t' -v s="$start" -v e="$end" '
  # add_delta: 単調増加のカウンタの増分を足す（巻き戻りは新しい値をそのまま増分とみなす）
  function add_delta(name, v) {
    if (name in last) { d = v - last[name]; sum[name] += (d >= 0 ? d : v) }
    last[name] = v
  }
  # gib: バイトを GiB に直す
  function gib(b) { return b / 2^30 }
  $1 < s || $1 > e { next }
  {
    if (first == "") first = $2
    lastT = $2
    n = $3
  }
  n == "juicefs_blockcache_write_bytes"                 { add_delta("in", $4) }
  n == "juicefs_staging_write_bytes"                    { add_delta("stage", $4) }
  n == "juicefs_compact_size_histogram_bytes_sum"       { add_delta("compact", $4) }
  n == "juicefs_blockcache_miss_bytes"                  { add_delta("miss", $4) }
  n == "juicefs_blockcache_hit_bytes"                   { add_delta("hit", $4) }
  n == "juicefs_object_request_durations_histogram_seconds_count{method=\"GET\"}" { add_delta("get", $4) }
  n == "juicefs_object_request_durations_histogram_seconds_sum{method=\"GET\"}"   { add_delta("gets", $4) }
  n ~ /^juicefs_kaz_cache_2q_events_total\{event="/ {
    ev = n; sub(/.*event="/, "", ev); sub(/".*/, "", ev); add_delta("ev_" ev, $4)
  }
  n == "juicefs_kaz_cache_2q_bytes{segment=\"protected\"}" { prot = $4 }
  n == "juicefs_kaz_cache_2q_bytes{segment=\"probation\"}" { prob = $4 }
  n == "juicefs_kaz_cache_2q_ghost_bytes"                  { ghost = $4 }
  n == "juicefs_kaz_cache_2q_protected_unread_bytes"       { unread = $4 }
  END {
    if (first == "") { print "この期間の記録がありません"; exit 1 }
    # 期間の長さは記録のある範囲で測る
    cmd = "date -d \"" first "\" +%s"; cmd | getline t0; close(cmd)
    cmd = "date -d \"" lastT "\" +%s"; cmd | getline t1; close(cmd)
    h = (t1 - t0) / 3600; if (h <= 0) h = 1 / 60
    printf "期間: %s 〜 %s（%.1f 時間）\n", first, lastT, h
    printf "\n[流入]\n"
    printf "  キャッシュへの流入   %7.1f GiB（%.1f GiB/h）\n", gib(sum["in"]), gib(sum["in"]) / h
    printf "    書き込み（staging）%7.1f GiB\n", gib(sum["stage"])
    printf "    compaction の出力  %7.1f GiB（キャッシュに入らなかった分を含む）\n", gib(sum["compact"])
    printf "    ミスの補充（目安） %7.1f GiB\n", gib(sum["miss"])
    printf "\n[読み込み]\n"
    tot = sum["hit"] + sum["miss"]
    printf "  ヒット %.1f GiB / ミス %.1f GiB（ヒット率 %.1f%%）\n", gib(sum["hit"]), gib(sum["miss"]), (tot > 0 ? 100 * sum["hit"] / tot : 0)
    printf "  GET %d 件、合計 %.0f 秒\n", sum["get"], sum["gets"]
    printf "\n[2Q]\n"
    printf "  昇格 %d、降格 %d（読まれていない %d、読まれた %d）、ghost_hit %d\n", sum["ev_promote"], sum["ev_demote"], sum["ev_demote_unread"], sum["ev_demote_read"], sum["ev_ghost_hit"]
    printf "    ghost_hit の内訳: 降格を経た %d、常連用に上がらなかった %d、compaction の出力 %d、常連用から %d\n", sum["ev_ghost_hit_demoted"], sum["ev_ghost_hit_probation"], sum["ev_ghost_hit_compaction"], sum["ev_ghost_hit_protected"]
    printf "  追い出し: 新入り用 %d、常連用 %d\n", sum["ev_evict_probation"], sum["ev_evict_protected"]
    printf "  compaction の出力: キャッシュ %d（若い元を除いて新入り用にしたもの %d）、見送り %d\n", sum["ev_compaction_output_cached"], sum["ev_compaction_output_hot"], sum["ev_compaction_output_skipped"]
    printf "\n[期間の終わりの状態]\n"
    printf "  常連用 %.1f GB（うち読まれた実績なし %.1f GB）、新入り用 %.1f GB、追い出し済みリスト %.1f GB\n", prot / 1e9, unread / 1e9, prob / 1e9, ghost / 1e9
  }' "$file"
