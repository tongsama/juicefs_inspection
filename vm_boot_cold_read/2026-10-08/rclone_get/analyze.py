"""rclone serve s3 の DEBUG ログ（切り出し済み）から GET の段階別所要時間と
Drive API 呼び出し数（推定）を集計し、JuiceFS 側 GET ログと突き合わせる。

使い方: python3 analyze.py rclone_proc.log debug.part
  rclone_proc.log: rclone プロセス起動 (20:30:27) から窓の終わりまでの切り出し。
                   窓 (21:21:00 以降) より前は VFS キャッシュ状態の推定にだけ使う。
  debug.part     : JuiceFS --debug ログの同時間帯切り出し。

rclone ログは秒精度のため、すべての所要時間は ±1s の誤差を持つ。
"""
import re, sys, collections, datetime as dt, statistics, json

WIN_START = dt.datetime(2026, 10, 8, 21, 21, 0).timestamp()

LINE = re.compile(r'^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) (\w+)\s*: (.*)$')
K = r'juicefs-data/(chunks/\S+?)'


def ts(s):
    """rclone ログの時刻文字列を epoch 秒に変換する。"""
    return dt.datetime.strptime(s, '%Y/%m/%d %H:%M:%S').timestamp()


def hms(t):
    """epoch 秒を HH:MM:SS 表記にする。"""
    return dt.datetime.fromtimestamp(t).strftime('%H:%M:%S')


def pct(xs, p):
    """単純なパーセンタイル（最近傍）。"""
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def parse_rclone(path):
    """rclone ログを走査し、GET インスタンスと API 呼び出し推定イベントを返す。"""
    gets = []                      # GET インスタンス
    pend = collections.defaultdict(list)   # key -> 未完の GET (Open 待ち)
    pend_or = collections.defaultdict(list)  # key -> openRange 待ち
    pend_rd = collections.defaultdict(list)  # key -> 最初の Read 待ち
    open_rd = collections.defaultdict(list)  # key -> Read 中
    cached = set()                 # VFS に node がある（と推定される）key
    api = []                       # (t, 種別) Drive API 呼び出し推定
    s3ops = []                     # (t, 種別) S3 リクエスト
    errors = []
    last_create_upload = {}        # key -> 'small' or 'stream'
    pending_get_lookup = {}
    for ln, line in enumerate(open(path, errors='replace'), 1):
        m = LINE.match(line)
        if not m:
            continue
        t = ts(m.group(1)); lvl = m.group(2); msg = m.group(3).rstrip('\n')
        if msg.startswith('serve s3: GET OBJECT'):
            key = re.search(r'Object: juicefs-data/(\S+)', msg).group(1)
            g = dict(key=key, t_req=t, ln=ln, cached=key in cached)
            gets.append(g); pend[key].append(g)
            s3ops.append((t, 'GET'))
            if not g['cached']:
                api.append((t, 'get_lookup'))
                cached.add(key)
            continue
        if msg.startswith('serve s3: CREATE OBJECT'):
            key = re.search(r'juicefs-data/(\S+)', msg).group(1)
            s3ops.append((t, 'PUT'))
            api.append((t, 'put_lookup'))   # drive Put -> NewObject (files.list)
            cached.add(key)
            continue
        if msg.startswith('serve s3: DELETE'):
            key = re.search(r'juicefs-data/(\S+)', msg).group(1)
            s3ops.append((t, 'DELETE'))
            if key not in cached:
                api.append((t, 'del_lookup'))
            continue
        if lvl in ('ERROR', 'NOTICE') and t >= WIN_START:
            errors.append((t, ln, msg[:200]))
        if 'File to upload is small' in msg:
            api.append((t, 'put_upload_small'))
            continue
        mm = re.match(r'buckets/' + K + r': (.*)$', msg)
        if not mm:
            continue
        key, body = mm.group(1), mm.group(2)
        if body.startswith('Sending chunk'):
            # resumable: セッション作成 1 + chunk 送信 1（chunk 0 の時に作成分を加算）
            if body.startswith('Sending chunk 0 '):
                api.append((t, 'put_upload_init'))
            api.append((t, 'put_upload_chunk'))
        elif body == 'Remove: ':
            api.append((t, 'del_remove'))
        elif body.startswith('>Remove'):
            cached.discard(key)
        elif body == 'Open: flags=O_RDONLY':
            if pend[key]:
                g = pend[key].pop(0); g['t_open'] = t; pend_or[key].append(g)
        elif body.startswith('ChunkedReader.openRange'):
            api.append((t, 'get_download'))
            if pend_or[key]:
                g = pend_or[key].pop(0); g['t_or'] = t; g['ln_or'] = ln
                pend_rd[key].append(g)
        elif body.startswith('ChunkedReader.Read at '):
            at = int(body.split()[2])
            if at == 0 and pend_rd[key]:
                # 新しい Read 系列の開始。どの GET のものかは同一 key 同時実行時に曖昧
                g = pend_rd[key].pop(0); g['t_r0'] = t; g['t_rl'] = t
                g['ambig'] = len(pend_rd[key]) > 0 or len(open_rd[key]) > 0
                open_rd[key] = [g]
            elif open_rd[key]:
                open_rd[key][-1]['t_rl'] = t
    return gets, api, s3ops, errors


def parse_juicefs(path):
    """JuiceFS DEBUG ログから GET/PUT/DELETE の (開始, 終了, cost, err) を取り出す。"""
    pat = re.compile(r'^(\S+ \S+) juicefs\[\d+\] <(?:DEBUG|WARNING)>: (?:slow request: )?(GET|PUT|DELETE) (chunks/\S+) .*?\(req_id: "[^"]*", err: (.*), cost: ([\d.]+)(ms|s|µs)\) \[logRequest')
    out = []
    for line in open(path, errors='replace'):
        if ' GET chunks' not in line and ' PUT chunks' not in line and ' DELETE chunks' not in line:
            continue
        m = pat.match(line)
        if not m:
            continue
        end = dt.datetime.strptime(m.group(1), '%Y/%m/%d %H:%M:%S.%f').timestamp()
        c = float(m.group(5)) * {'ms': 1e-3, 's': 1, 'µs': 1e-6}[m.group(6)]
        out.append(dict(op=m.group(2), key=m.group(3), end=end, start=end - c, cost=c, err=m.group(4)))
    return out


def main():
    gets, api, s3ops, errors = parse_rclone(sys.argv[1])
    jfs = parse_juicefs(sys.argv[2])
    W = [g for g in gets if g['t_req'] >= WIN_START]
    print('== rclone GET (窓内)', len(W), ' 窓開始以前キャッシュ済み推定:', sum(g['cached'] for g in W))
    big = [g for g in W if g['key'].endswith('_4194304')]
    print('   うち 4MiB object', len(big))
    # 段階別
    def stage(gs, name):
        lk = [g['t_open'] - g['t_req'] for g in gs if 't_open' in g]
        dl = [g['t_r0'] - g['t_or'] for g in gs if 't_r0' in g and 't_or' in g]
        bd = [g['t_rl'] - g['t_r0'] for g in gs if 't_r0' in g]
        print(f'-- {name}: n={len(gs)}')
        for nm, xs in (('lookup(GET→Open)', lk), ('download open(openRange→first Read)', dl), ('body(first→last Read)', bd)):
            if xs:
                c = collections.Counter(int(x) for x in xs)
                print(f'   {nm}: n={len(xs)} mean={statistics.mean(xs):.2f}s p50={pct(xs,.5):.0f} p90={pct(xs,.9):.0f} p99={pct(xs,.99):.0f} max={max(xs):.0f}  >=5s:{sum(x>=5 for x in xs)} >=15s:{sum(x>=15 for x in xs)} >=25s:{sum(x>=25 for x in xs)}')
                print('      hist', sorted(c.items())[:40])
    stage(big, '4MiB GET')
    stage([g for g in big if not g['cached']], '4MiB GET lookup あり推定')
    stage([g for g in big if g['cached']], '4MiB GET lookup なし推定')
    noread = [g for g in W if 't_r0' not in g]
    print('   最初の Read が無い GET', len(noread))

    # JuiceFS 側との突き合わせ
    jg = [j for j in jfs if j['op'] == 'GET']
    byk = collections.defaultdict(list)
    for g in W:
        byk[g['key']].append(g)
    match = []
    for j in jg:
        cands = [g for g in byk.get(j['key'], []) if abs(g['t_req'] - j['start']) <= 2.0 and not g.get('_m')]
        if cands:
            g = min(cands, key=lambda g: abs(g['t_req'] - j['start']))
            g['_m'] = True; match.append((j, g))
    print(f'\n== JuiceFS GET {len(jg)}、rclone GET と照合できた数 {len(match)}')
    slow = [(j, g) for j, g in match if j['cost'] > 15]
    print(f'-- JuiceFS cost>15s の GET: {len([j for j in jg if j["cost"]>15])}（照合済み {len(slow)}）')
    rows = []
    for j, g in sorted(slow, key=lambda x: x[0]['start']):
        r = dict(key=j['key'], jfs_start=hms(j['start']), cost=round(j['cost'], 1), err=j['err'][:60],
                 req=hms(g['t_req']), open=hms(g['t_open']) if 't_open' in g else None,
                 openRange=hms(g['t_or']) if 't_or' in g else None,
                 firstRead=hms(g['t_r0']) if 't_r0' in g else None,
                 lastRead=hms(g['t_rl']) if 't_rl' in g else None,
                 lookup=(g.get('t_open', 0) - g['t_req']) if 't_open' in g else None,
                 dlopen=(g['t_r0'] - g['t_or']) if 't_r0' in g and 't_or' in g else None,
                 ambig=g.get('ambig'), cached=g['cached'], ln_or=g.get('ln_or'))
        rows.append(r)
        print('  ', json.dumps(r, ensure_ascii=False))
    # 遅い GET の段階分類
    cls = collections.Counter()
    for r in rows:
        if r['dlopen'] is None:
            cls['firstRead 無し(Drive 応答前に打ち切り/エラー)'] += 1
        elif r['dlopen'] >= 10:
            cls['download open 待ち>=10s'] += 1
        elif r['lookup'] is not None and r['lookup'] >= 10:
            cls['lookup>=10s'] += 1
        else:
            cls['その他(body/曖昧)'] += 1
    print('   分類', dict(cls))
    json.dump(rows, open('slow_gets.json', 'w'), ensure_ascii=False, indent=1)

    # JuiceFS 側 cost と rclone 側 download open の相関
    pairs = [(j['cost'], g['t_r0'] - g['t_or'], g['t_open'] - g['t_req']) for j, g in match
             if 't_r0' in g and 't_or' in g and 't_open' in g and j['key'].endswith('_4194304') and not g.get('ambig')]
    if pairs:
        print(f'\n-- 4MiB GET (照合済み・非曖昧 n={len(pairs)}): JuiceFS cost p50={pct([p[0] for p in pairs],.5):.2f}s;'
              f' rclone dlopen p50={pct([p[1] for p in pairs],.5)}s mean={statistics.mean(p[1] for p in pairs):.2f}s;'
              f' lookup mean={statistics.mean(p[2] for p in pairs):.2f}s')
        rest = [p[0] - p[1] - p[2] for p in pairs]
        print(f'   cost - dlopen - lookup の残差 mean={statistics.mean(rest):.2f}s p50={pct(rest,.5):.2f}s')

    # API 呼び出し推定 (窓内、秒毎)
    Aw = [(t, k) for t, k in api if t >= WIN_START]
    Sw = [(t, k) for t, k in s3ops if t >= WIN_START]
    print('\n== Drive API 呼び出し推定 (窓内)', len(Aw), dict(collections.Counter(k for _, k in Aw)))
    print('   S3 リクエスト', dict(collections.Counter(k for _, k in Sw)))
    t0 = int(min(t for t, _ in Aw)); t1 = int(max(t for t, _ in Aw))
    per = collections.Counter(int(t) for t, _ in Aw)
    secs = list(range(t0, t1 + 1))
    vals = [per.get(s, 0) for s in secs]
    active = [v for v in vals if v > 0]
    print(f'   期間 {hms(t0)}-{hms(t1)} {len(secs)}s, 平均 {len(Aw)/len(secs):.2f}/s, 活動秒平均 {statistics.mean(active):.2f}/s, max {max(vals)}/s')
    print('   秒あたり件数の分布', sorted(collections.Counter(vals).items()))
    print('   >=20/s の秒数', sum(v >= 20 for v in vals), ' >=15', sum(v >= 15 for v in vals), ' >=10', sum(v >= 10 for v in vals))
    # 10 秒窓 / 60 秒窓の平均
    for w in (10, 60):
        mx = max(sum(vals[i:i + w]) / w for i in range(0, len(vals) - w + 1))
        print(f'   {w}s 移動平均の最大 {mx:.2f}/s')
    # 分ごとの内訳
    print('\n   分ごと: total/s | 内訳')
    bym = collections.defaultdict(collections.Counter)
    for t, k in Aw:
        bym[hms(t)[:5]][k] += 1
    s3m = collections.defaultdict(collections.Counter)
    for t, k in Sw:
        s3m[hms(t)[:5]][k] += 1
    with open('api_per_minute.tsv', 'w') as f:
        cols = ['get_lookup', 'get_download', 'put_lookup', 'put_upload_small', 'put_upload_init', 'put_upload_chunk', 'del_lookup', 'del_remove']
        f.write('minute\ttotal\tper_s\t' + '\t'.join(cols) + '\tS3_GET\tS3_PUT\tS3_DELETE\n')
        for mi in sorted(bym):
            c = bym[mi]; tot = sum(c.values())
            f.write(f'{mi}\t{tot}\t{tot/60:.2f}\t' + '\t'.join(str(c.get(x, 0)) for x in cols) +
                    f"\t{s3m[mi].get('GET',0)}\t{s3m[mi].get('PUT',0)}\t{s3m[mi].get('DELETE',0)}\n")
            print(f'   {mi} {tot/60:5.2f}/s  ' + ' '.join(f'{x}={c.get(x,0)}' for x in cols if c.get(x, 0)))
    with open('api_per_second.tsv', 'w') as f:
        for s, v in zip(secs, vals):
            f.write(f'{hms(s)}\t{v}\n')
    print('\n== ERROR/NOTICE (窓内)', len(errors))
    for e in collections.Counter(re.sub(r'\d+', 'N', m)[:90] for _, _, m in errors).most_common():
        print('  ', e)


if __name__ == '__main__':
    main()
