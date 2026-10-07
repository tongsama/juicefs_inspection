#!/usr/bin/env python3
"""--meta-write-batch の計測ログから、batch commit の件数・大きさ・所要時間を集計する。

入力は debug ログ（固定 prefix で読む）。対象 inode と時間窓で絞る。
- 単発の commit: `metadata write inode=<ino> ... phase=done`
- batch の commit: `metadata write batch inode=<ino> ... count=<k> phase=done ... doWrite=<d> ... errno=<e>`
"""
import argparse
import collections
import json
import re
from datetime import datetime

TS = re.compile(r'^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\.\d+) ')
KV = re.compile(r'(\w+)=(\S+)')
UNIT = {'ns': 1e-9, 'µs': 1e-6, 'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'm': 60.0, 'h': 3600.0}


def go_duration(s):
    """Go の time.Duration 文字列（例: 41.2ms、1m2.5s）を秒に変換する。"""
    total = 0.0
    for num, unit in re.findall(r'([\d.]+)(ns|µs|us|ms|s|m|h)', s):
        total += float(num) * UNIT[unit]
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log', required=True)
    ap.add_argument('--inode', required=True)
    ap.add_argument('--start', required=True, help='YYYY/MM/DD HH:MM:SS')
    ap.add_argument('--end', required=True, help='YYYY/MM/DD HH:MM:SS')
    ap.add_argument('--prefix-bytes', type=int, required=True)
    a = ap.parse_args()
    start = datetime.strptime(a.start, '%Y/%m/%d %H:%M:%S').timestamp()
    end = datetime.strptime(a.end, '%Y/%m/%d %H:%M:%S').timestamp()
    single, batches, batch_slices, batch_time, errors = 0, 0, 0, 0.0, collections.Counter()
    sizes = collections.Counter()
    read = 0
    with open(a.log, 'r', errors='replace') as f:
        for line in f:
            read += len(line.encode('utf-8', 'replace'))
            if read > a.prefix_bytes:
                break
            if 'metadata write' not in line or 'phase=done' not in line:
                continue
            m = TS.match(line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), '%Y/%m/%d %H:%M:%S.%f').timestamp()
            if t < start or t > end:
                continue
            kv = dict(KV.findall(line))
            if kv.get('inode') != a.inode:
                continue
            if 'metadata write batch ' in line:
                k = int(kv['count'])
                batches += 1
                batch_slices += k
                sizes[k if k < 16 else (16 if k < 32 else (32 if k < 64 else 64))] += 1
                batch_time += go_duration(kv.get('doWrite', '0s'))
                if kv.get('errno', '0') not in ('0', 'errno'):
                    errors[kv['errno']] += 1
            else:
                single += 1
    out = {
        'single_commits': single, 'batch_commits': batches, 'slices_in_batches': batch_slices,
        'transactions': single + batches, 'slices_committed': single + batch_slices,
        'batch_size_hist(>=16:16..31, >=32:32..63, 64)': dict(sorted(sizes.items())),
        'batch_doWrite_sum_s': round(batch_time, 1),
        'batch_doWrite_mean_ms': round(batch_time / batches * 1000, 2) if batches else None,
        'batch_errors': dict(errors),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
