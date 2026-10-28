"""metrics の TSV（取得時刻 / ISO / 系列名 / 値）の追記・読み込み・窓の差分、と /metrics の取得。"""
import collections
import gzip
import re
import time
import urllib.request

# 共通ラベル（mount ごとに変わるので系列名から外す）
_LABEL_RE = re.compile(r'(instance|juicefs_version|mp|vol_name|storage_class)="[^"]*",?')


def append_sample(path, ts, series):
    """1 回分の系列 {名前: 値} を TSV に追記する。"""
    iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))
    with open(path, "a", encoding="utf-8") as f:
        for k, v in series.items():
            f.write("%d\t%s\t%s\t%r\n" % (ts, iso, k, float(v)))


def load(path):
    """TSV（gzip 可）を系列名 → [(UNIX 秒, 値)]（時刻順）の dict にする。"""
    op = gzip.open if path.endswith(".gz") else open
    m = collections.defaultdict(list)
    with op(path, "rt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 4:
                continue
            try:
                m[p[2]].append((int(p[0]), float(p[3])))
            except ValueError:
                pass
    for v in m.values():
        v.sort()  # 追記の順が時刻順とは限らない
    return m


def delta(series, a, b, born_after=None):
    """窓 [a, b] の counter の増分。始まりは a 以後の最初のサンプル、終わりは b 以前の最後のサンプル。
    値が前より減った（mount のし直しで 0 に戻った）ときは、減った後の値を増分として足す。
    born_after（記録の最初のサンプルの時刻）を渡すと、それより後に初めて現れた系列は、現れる前を 0 とみなす
    （label 付きの counter は最初の増加まで exposition に出ないため、最初の値を落とさない）。"""
    pts = [(t, v) for t, v in series if a <= t <= b]
    total = 0.0
    # kaz: 窓の中で生まれた系列は、最初のサンプルの値も増分に入れる
    if born_after is not None and pts and series[0][0] == pts[0][0] and pts[0][0] > born_after:
        total += pts[0][1]
    for (_, v0), (_, v1) in zip(pts, pts[1:]):
        total += v1 - v0 if v1 >= v0 else v1
    return total


def parse_exposition(text, include):
    """Prometheus のテキストから include に先頭一致する系列だけを、共通ラベルを外して dict にする。
    _bucket{ の系列は除く。"""
    out = {}
    for line in text.splitlines():
        if not include.match(line) or "_bucket{" in line:
            continue
        name = _LABEL_RE.sub("", line.rsplit(None, 1)[0])
        name = name.replace(",}", "}").replace("{}", "").strip()
        try:
            out[name] = float(line.rsplit(None, 1)[1])
        except (ValueError, IndexError):
            pass
    return out


def scrape(url, include, timeout=20):
    """url の /metrics を取得し、include に合う系列を {名前: 値} で返す。"""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return parse_exposition(r.read().decode(), include)


# 2026-10-21 版 metrics_sampler.sh と同じ対象の系列
DEFAULT_INCLUDE = re.compile(
    r"^juicefs_(blockcache_|staging_|compact_size_histogram_bytes_(sum|count)|compaction_gc_events_total|"
    r"object_request_data_bytes|object_request_durations_histogram_seconds_(sum|count)|uptime|"
    r"fuse_ops_durations_histogram_seconds_count|kaz_cache_2q_|kaz_compaction_|"
    r"meta_ops_durations_histogram_seconds_(sum|count)|transaction_durations_histogram_seconds_(sum|count)|"
    r"fuse_ops_durations_histogram_seconds_sum|fuse_read_size_bytes_(sum|count)|fuse_written_size_bytes_(sum|count))")
