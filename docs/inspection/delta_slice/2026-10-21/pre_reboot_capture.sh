#!/usr/bin/env bash
# VM を再起動する直前に、前回の判定（docs/inspection/cache_churn/2026-10-19/report-ja.md）と同じ材料をまとめて取る（読み取りのみ）。
#   - /metrics の全体（metrics-pre-reboot-<時刻>.txt）
#   - キャッシュの内訳と、ファイルごとの一覧（breakdown-pre-reboot-<時刻>.txt、files-pre-reboot-<時刻>.csv）
#   - kaz-2q の管理ファイルの写し（state-pre-reboot-<時刻>.v1）
# 起動の後は、key_tracker の記録と boot_analysis.py で、GET で補充したブロックが以前キャッシュにあって消えたものかを判定する。
# 使い方: pre_reboot_capture.sh [出力ディレクトリ（既定はこのスクリプトの場所）]
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here}
cache=/home/kwatanabe/.juicefs/cache/73e74f7b-6f95-4155-adea-d9ef1e669c20
tools=$here/../../cache_churn/2026-10-19
ts=$(date +%m%d%H%M%S)

curl -s --max-time 20 http://127.0.0.1:9567/metrics > "$out/metrics-pre-reboot-$ts.txt"
cp "$cache/kaz-cache-state.v1" "$out/state-pre-reboot-$ts.v1"
python3 -I "$tools/cache_breakdown.py" --cache "$cache" --files-csv "$out/files-pre-reboot-$ts.csv" > "$out/breakdown-pre-reboot-$ts.txt" 2>&1
echo "captured: $out/*-pre-reboot-$ts.*"
