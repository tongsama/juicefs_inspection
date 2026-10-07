#!/usr/bin/env python3
"""Ubuntu インストール計測の解析: JuiceFS の debug ログ・accesslog・metrics 差分から、
metadata commit 待ち、writer flush 待ち、FUSE 操作の内訳を集計する。

元ログは読み取りのみ。--start/--end で対象の時間帯を切り出す（"YYYY/MM/DD HH:MM:SS" 形式）。
出力は JSON（--out）と標準出力の要約。
"""
import argparse
import bisect
import collections
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime

DUR_RE = re.compile(r'(\d+(?:\.\d+)?)(ns|µs|us|ms|s|m|h)')
DUR_UNIT = {'ns': 1e-9, 'µs': 1e-6, 'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'm': 60.0, 'h': 3600.0}


def parse_go_duration(s):
    """Go の time.Duration 文字列（例 1m2.5s, 60.94µs）を秒に変換する。"""
    total = 0.0
    for num, unit in DUR_RE.findall(s):
        total += float(num) * DUR_UNIT[unit]
    return total


class Stat:
    """値を保持して件数・合計・分位点を出すための単純な集計器。"""

    def __init__(self):
        self.v = []

    def add(self, x):
        self.v.append(x)

    def summary(self):
        if not self.v:
            return {'count': 0}
        s = sorted(self.v)
        n = len(s)

        def q(p):
            return s[min(n - 1, int(math.ceil(p * n)) - 1)]
        return {'count': n, 'sum': round(sum(s), 6), 'mean': sum(s) / n,
                'p50': q(0.5), 'p95': q(0.95), 'p99': q(0.99), 'max': s[-1]}


def sha256_prefix(path, size):
    """解析した固定 prefix（先頭 size バイト）の SHA-256 を返す（証跡用）。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        left = size
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()


LOG_TS = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+) juicefs\[(\d+)\] <(\w+)>: (.*)$')
KV = re.compile(r'(\w+)=(\S+)')


def ts_log(s):
    return datetime.strptime(s, '%Y/%m/%d %H:%M:%S.%f').timestamp()


def analyze_debug_log(path, start, end, size):
    """debug ログから commit・flush・freeze・compaction・PUT・WARN を集計する。"""
    commits = {}          # (inode, chunk, slice) -> dict(t_lock, lock_wait, dowrite, t_done, slices)
    flush_by_origin = collections.defaultdict(Stat)
    flush_open = {}
    freeze_reason = collections.Counter()
    freeze_origin = collections.Counter()
    freeze_raw = Stat()
    put_cost = collections.defaultdict(Stat)
    warn = collections.Counter()
    compaction = collections.Counter()
    levels = collections.Counter()
    first_ts = last_ts = None
    with open(path, 'r', errors='replace') as f:
        read = 0
        for line in f:
            read += len(line.encode('utf-8', 'replace'))
            if read > size:
                break
            m = LOG_TS.match(line)
            if not m:
                continue
            t = ts_log(m.group(1))
            if (start and t < start) or (end and t > end):
                continue
            first_ts = first_ts or t
            last_ts = t
            lvl, msg = m.group(3), m.group(4)
            levels[lvl] += 1
            if lvl == 'WARNING':
                warn[re.sub(r'[=:]\s*\S+', '', msg)[:80]] += 1
            if msg.startswith('metadata write inode='):
                kv = dict(KV.findall(msg))
                key = (kv.get('inode'), kv.get('chunk'), kv.get('slice'))
                c = commits.setdefault(key, {})
                ph = kv.get('phase')
                if ph == 'lock_wait':
                    c['t_lock'] = t
                elif ph == 'doWrite':
                    c['lock_wait'] = parse_go_duration(kv.get('lock_wait', '0'))
                    c['t_dowrite'] = t
                elif ph == 'stat':
                    c['dowrite'] = parse_go_duration(kv.get('doWrite', '0'))
                    c['slices'] = int(kv.get('slices', '0'))
                elif ph == 'done':
                    c['t_done'] = t
                    c['errno'] = kv.get('errno')
            elif msg.startswith('writer flush inode='):
                kv = dict(KV.findall(msg))
                if kv.get('phase') == 'end':
                    flush_by_origin[kv.get('origin')].add(parse_go_duration(kv.get('elapsed', '0')))
            elif msg.startswith('slice freeze inode='):
                kv = dict(KV.findall(msg))
                freeze_reason[kv.get('reason')] += 1
                freeze_origin[kv.get('origin', '-')] += 1
                freeze_raw.add(int(kv.get('raw_length', '0')))
            elif msg.startswith('PUT chunks/'):
                mm = re.search(r'cost: ([\d.]+\w+)\)', msg)
                kind = 'compaction_or_large' if re.search(r'_\d+_4194304 ', msg) else 'other'
                if mm:
                    put_cost[kind].add(parse_go_duration(mm.group(1)))
            elif msg.startswith('compaction inode='):
                kv = dict(KV.findall(msg))
                compaction[kv.get('phase')] += 1
    # commit を inode ごとに集計し、直列区間（lock 取得〜done）の占有率を出す
    per_inode = collections.defaultdict(lambda: {'n': 0, 'busy': 0.0, 'lock_wait': Stat(), 'dowrite': Stat(),
                                                 'total': Stat(), 'slices': Stat(), 'chunks': set()})
    all_dowrite, all_lock, all_total = Stat(), Stat(), Stat()
    for (ino, chunk, _), c in commits.items():
        if 't_lock' not in c or 't_done' not in c:
            continue
        p = per_inode[ino]
        tot = c['t_done'] - c['t_lock']
        p['n'] += 1
        p['total'].add(tot)
        all_total.add(tot)
        p['chunks'].add(chunk)
        if 'dowrite' in c:
            p['dowrite'].add(c['dowrite'])
            all_dowrite.add(c['dowrite'])
            p['busy'] += c['dowrite']
        if 'lock_wait' in c:
            p['lock_wait'].add(c['lock_wait'])
            all_lock.add(c['lock_wait'])
        if 'slices' in c:
            p['slices'].add(c['slices'])
    span = (last_ts - first_ts) if first_ts else 0
    inodes = sorted(per_inode.items(), key=lambda kv: -kv[1]['n'])[:10]
    return {
        'span_seconds': span,
        'levels': dict(levels),
        'commits': {'count': all_total.summary()['count'], 'total': all_total.summary(),
                    'doWrite': all_dowrite.summary(), 'lock_wait': all_lock.summary()},
        'top_inodes': [{'inode': ino, 'commits': p['n'], 'chunks': len(p['chunks']),
                        'doWrite_busy_seconds': round(p['busy'], 3),
                        'doWrite_busy_ratio_of_span': round(p['busy'] / span, 4) if span else None,
                        'lock_wait': p['lock_wait'].summary(), 'doWrite': p['dowrite'].summary(),
                        'commit_total': p['total'].summary(), 'slices_at_commit': p['slices'].summary()}
                       for ino, p in inodes],
        'writer_flush_by_origin': {k: v.summary() for k, v in flush_by_origin.items()},
        'freeze_reason': dict(freeze_reason), 'freeze_origin': dict(freeze_origin),
        'freeze_raw_length': freeze_raw.summary(),
        'put_cost': {k: v.summary() for k, v in put_cost.items()},
        'compaction_phase_lines': dict(compaction),
        'warnings': dict(warn.most_common(30)),
    }


AL_RE = re.compile(r'^(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}\.\d+) \[uid:\d+,gid:\d+,pid:(\d+)\] (\w+) \((\d+)[,)](.*) - (\w+) <([\d.]+)>$')


def analyze_accesslog(path, start, end, size, target_name, target_inode=None):
    """accesslog から FUSE 操作別・inode 別の件数と所要時間を集計する。"""
    by_op = collections.defaultdict(Stat)
    target_ops = collections.defaultdict(Stat)
    write_sizes = collections.Counter()
    read_sizes = collections.Counter()
    errors = collections.Counter()
    per_minute = collections.defaultdict(lambda: collections.defaultdict(float))
    with open(path, 'r', errors='replace') as f:
        read = 0
        for line in f:
            read += len(line.encode('utf-8', 'replace'))
            if read > size:
                break
            line = line.rstrip('\n')
            if target_name and target_inode is None and f',{target_name})' in line and ' lookup ' in line:
                mm = re.search(r'\): \((\d+),', line)
                if mm:
                    target_inode = mm.group(1)
            m = AL_RE.match(line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), '%Y.%m.%d %H:%M:%S.%f').timestamp()
            if (start and t < start) or (end and t > end):
                continue
            op, ino, rest, res, dur = m.group(3), m.group(4), m.group(5), m.group(6), float(m.group(7))
            by_op[op].add(dur)
            if res != 'OK':
                errors[(op, res)] += 1
            if ino == target_inode:
                target_ops[op].add(dur)
                per_minute[int(t // 60) * 60][op] += dur
                if op in ('write', 'read'):
                    mm = re.match(r'(\d+),(\d+)', rest)
                    if mm:
                        (write_sizes if op == 'write' else read_sizes)[int(mm.group(1))] += 1
    timeline = [{'minute': datetime.fromtimestamp(k).strftime('%H:%M'),
                 **{op: round(v, 3) for op, v in sorted(d.items())}} for k, d in sorted(per_minute.items())]
    return {
        'target_inode': target_inode,
        'by_op': {k: v.summary() for k, v in sorted(by_op.items(), key=lambda kv: -sum(kv[1].v))},
        'target_by_op': {k: v.summary() for k, v in sorted(target_ops.items(), key=lambda kv: -sum(kv[1].v))},
        'target_write_sizes_top': write_sizes.most_common(10),
        'target_read_sizes_top': read_sizes.most_common(10),
        'errors': {f'{a}:{b}': n for (a, b), n in errors.items()},
        'target_seconds_per_minute': timeline,
    }


METRIC_RE = re.compile(r'^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-+\deE.naNIinf]+)$')


def load_metrics(path):
    """Prometheus テキスト形式を {name{labels}: value} に読む。"""
    out = {}
    with open(path) as f:
        for line in f:
            if line.startswith('#'):
                continue
            m = METRIC_RE.match(line.strip())
            if m:
                try:
                    out[m.group(1) + (m.group(2) or '')] = float(m.group(3))
                except ValueError:
                    pass
    return out


def metrics_diff(before, after):
    """counter／histogram の sum・count の差分で、計測期間の増分を出す。"""
    a, b = load_metrics(before), load_metrics(after)
    keep = re.compile(r'^juicefs_(meta_ops|transaction|fuse_ops|object_request|staging|compact|blockcache|'
                      r'compaction_gc|meta_ops_durations|fuse_written|fuse_read|cpu|memory)')
    out = {}
    for k, v in b.items():
        if not keep.match(k) or '_bucket' in k:
            continue
        d = v - a.get(k, 0.0)
        if d != 0:
            out[k] = d
    return dict(sorted(out.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--log', required=True)
    ap.add_argument('--accesslog')
    ap.add_argument('--metrics-before')
    ap.add_argument('--metrics-after')
    ap.add_argument('--start', help='YYYY/MM/DD HH:MM:SS')
    ap.add_argument('--end', help='YYYY/MM/DD HH:MM:SS')
    ap.add_argument('--target-name', default='ubuntu-install-test.qcow2')
    ap.add_argument('--target-inode', help='対象 qcow2 の inode（accesslog に lookup が無い場合に指定）')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    start = datetime.strptime(a.start, '%Y/%m/%d %H:%M:%S').timestamp() if a.start else None
    end = datetime.strptime(a.end, '%Y/%m/%d %H:%M:%S').timestamp() if a.end else None
    result = {'inputs': {}}
    for name, p in (('log', a.log), ('accesslog', a.accesslog)):
        if p:
            size = os.path.getsize(p)  # 追記中のファイルでも固定 prefix で解析する
            result['inputs'][name] = {'path': p, 'prefix_bytes': size, 'sha256': sha256_prefix(p, size)}
    result['window'] = {'start': a.start, 'end': a.end}
    result['debug_log'] = analyze_debug_log(a.log, start, end, result['inputs']['log']['prefix_bytes'])
    if a.accesslog:
        result['accesslog'] = analyze_accesslog(a.accesslog, start, end,
                                                result['inputs']['accesslog']['prefix_bytes'], a.target_name, a.target_inode)
    if a.metrics_before and a.metrics_after:
        result['metrics_diff'] = metrics_diff(a.metrics_before, a.metrics_after)
    with open(a.out, 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=str)
    d = result['debug_log']
    print('commits:', json.dumps(d['commits'], default=str))
    print('flush:', json.dumps(d['writer_flush_by_origin'], default=str))
    if 'accesslog' in result:
        print('target inode:', result['accesslog']['target_inode'])
        for op, s in list(result['accesslog']['target_by_op'].items())[:10]:
            print(' ', op, json.dumps(s))


if __name__ == '__main__':
    main()
