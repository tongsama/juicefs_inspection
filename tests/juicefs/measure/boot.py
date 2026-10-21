"""起動の区間の解析（boot_analysis.py と同じ判定）。
GET で補充したブロックが、以前キャッシュにあって消えたもの（追い出し）かを、keys-snapshot-*.tsv と keys-events.tsv から分ける。
補充は、区間内に現れた key のうち、区間開始の直前の snapshot の最大 slice id 以下で、直前の snapshot に無いもの。"""
import collections
import os
import re
import time

from lib import metrics_io
from measure import report

G = 1 << 30
MARK_KEYS = ("起動開始", "ログイン画面", "ログイン完了", "アプリ")


def load_snap(path):
    """snapshot を {key: (atime, ctime, size)} で読む（1 行目は見出し）。"""
    d = {}
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            p = line.rstrip("\n").split("\t")
            if i == 0 or len(p) != 4:
                continue
            try:
                d[p[0]] = (int(p[1]), int(p[2]), int(p[3]))
            except ValueError:
                pass
    return d


def snapshots(outdir):
    """keys-snapshot-<UNIX>.tsv を (UNIX 秒, path) の昇順で返す。"""
    out = []
    for n in os.listdir(outdir):
        mo = re.fullmatch(r"keys-snapshot-(\d+)\.tsv", n)
        if mo:
            out.append((int(mo.group(1)), os.path.join(outdir, n)))
    return sorted(out)


def load_events(path):
    """keys-events.tsv を (時刻, 種類, key, atime, size) のリストにする。形の違う行は読み飛ばす。"""
    ev = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) != 6:
                    continue
                try:
                    ev.append((int(p[0]), p[1], p[2], int(p[3]), int(p[5])))
                except ValueError:
                    pass
    except OSError:
        pass
    return ev


def classify_fills(outdir, t0, t1):
    """区間の GET の補充を evicted／evicted-before-tracking／never-seen に分ける。
    返り値は (fills, 分類ごとの量, 追い出された時間帯, 最後のアクセスの時間帯, 分ごと, 直前の snapshot の最大 slice id)。
    直前の snapshot が無ければ None。"""
    snaps = [s for s in snapshots(outdir) if s[0] <= t0]
    if not snaps:
        return None
    pre = load_snap(snaps[-1][1])
    ids = [int(k.split("_")[0]) for k in pre if k.split("_")[0].isdigit()]
    max_id = max(ids) if ids else 0
    seen = set()
    for ts, p in snaps[:-1]:
        seen.update(load_snap(p))
    gone, fills = {}, []
    for t, kind, key, a, s in load_events(os.path.join(outdir, "keys-events.tsv")):
        if kind == "del":
            gone[key] = (t, a)
        elif t0 <= t <= t1 + 60:
            head = key.split("_")[0]
            if head.isdigit() and int(head) <= max_id and key not in pre:
                fills.append((t, key, s))
        if kind == "add":
            seen.add(key)
    cls, evict_at, last_acc = collections.Counter(), collections.Counter(), collections.Counter()
    per_min = collections.defaultdict(collections.Counter)
    for t, key, s in fills:
        if key in gone and gone[key][0] < t:
            c = "evicted"
            dt, a = gone[key]
            evict_at[time.strftime("%H:00", time.localtime(dt))] += s
            last_acc[time.strftime("%m/%d %H:00", time.localtime(a))] += s
        elif key in seen:
            c = "evicted-before-tracking"  # 古い snapshot にあり、直前の snapshot には無い
        else:
            c = "never-seen"
        cls[c] += s
        per_min[time.strftime("%H:%M", time.localtime(t))][c] += s
    return fills, cls, evict_at, last_acc, per_min, max_id


def parse_marks(marks, t0):
    """marks（名前 → 'YYYY-MM-DD HH:MM[:SS]' か 'HH:MM[:SS]'）を {名前: UNIX 秒} にする。HH:MM は区間開始の日付。"""
    out = {}
    day = time.strftime("%Y-%m-%d ", time.localtime(t0))
    for k, v in (marks or {}).items():
        v = v.strip()
        out[k] = report.parse_time(day + v if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v) else v, t0, t0)
    return out


def analyze(outdir, start, end, marks=None, label=None):
    """起動の区間の解析（GET、ミス、ヒット率、ghost_hit の内訳、GET で補充した量の分類、4 つの時刻の経過の分）。"""
    m = metrics_io.load(report.metrics_path(outdir))
    ts = report.sample_times(m)
    if not ts:
        raise report.NoData("metrics.tsv に記録がありません")
    t0, t1 = report.parse_time(start, ts[0], ts[-1]), report.parse_time(end, ts[0], ts[-1])
    hm = lambda x: time.strftime("%m/%d %H:%M", time.localtime(x))
    D = lambda n: report._D(m, n, t0, t1)
    L = ["起動の区間%s: %s - %s" % ("（%s）" % label if label else "", hm(t0), hm(t1)), ""]
    hit, miss = D("juicefs_blockcache_hit_bytes"), D("juicefs_blockcache_miss_bytes")
    get_n = D('juicefs_object_request_durations_histogram_seconds_count{method="GET"}')
    get_b = D('juicefs_object_request_data_bytes{method="GET"}')
    L.append("GET %d 回、%.2f GiB" % (get_n, get_b / G))
    L.append("ヒット %.2f GiB / ミス %.2f GiB（ヒット率 %.1f%%）" % (
        hit / G, miss / G, 100 * hit / (hit + miss) if hit + miss > 0 else 0))
    gh = {e: D('juicefs_kaz_cache_2q_events_total{event="%s"}' % e) for e in (
        "ghost_hit", "ghost_hit_demoted", "ghost_hit_probation", "ghost_hit_compaction", "ghost_hit_protected")}
    L.append("ghost_hit %d（降格を経た %d、常連用に上がらなかった %d、compaction の出力 %d、常連用から %d）" % (
        gh["ghost_hit"], gh["ghost_hit_demoted"], gh["ghost_hit_probation"], gh["ghost_hit_compaction"],
        gh["ghost_hit_protected"]))
    res = classify_fills(outdir, t0, t1) if os.path.isdir(outdir) else None
    fills = None
    if res is None:
        L += ["", "GET で補充した量の分類: 区間の開始以前の keys-snapshot-*.tsv が無いので出せない"]
    else:
        fills, cls, evict_at, last_acc, per_min, max_id = res
        tot = sum(cls.values())
        L += ["", "区間開始の直前の snapshot の最大 slice id: %d" % max_id,
              "GET の補充: %d ファイル、%.2f GiB" % (len(fills), tot / G)]
        for c in ("evicted", "evicted-before-tracking", "never-seen"):
            L.append("  %-24s %6.2f GiB  %5.1f%%" % (c, cls[c] / G, 100 * cls[c] / max(tot, 1)))
        L.append("追い出されたものが消された時間帯: %s" % {k: round(v / G, 2) for k, v in sorted(evict_at.items())})
        L.append("消される前の最後のアクセス: %s" % {k: round(v / G, 2) for k, v in sorted(last_acc.items())})
        L += ["", "分ごと（GiB）: evicted / before-tracking / never-seen"]
        for mn in sorted(per_min):
            c = per_min[mn]
            L.append("%s %6.2f %6.2f %6.2f" % (mn, c["evicted"] / G, c["evicted-before-tracking"] / G, c["never-seen"] / G))
    mk = parse_marks(marks, t0)
    if mk:
        order = [k for k in MARK_KEYS if k in mk] + [k for k in mk if k not in MARK_KEYS]
        base = mk.get("起動開始", mk[order[0]])
        L += ["", "| 時点 | 時刻 | 起動の開始からの分 | 直前の時点からの補充 GiB |", "|---|---|---|---|"]
        prev = None
        for k in order:
            seg = "-"
            if fills is not None and prev is not None:
                seg = "%.2f" % (sum(s for t, _, s in fills if mk[prev] < t <= mk[k]) / G)
            L.append("| %s | %s | %.1f | %s |" % (k, time.strftime("%H:%M:%S", time.localtime(mk[k])), (mk[k] - base) / 60, seg))
            prev = k
    return "\n".join(L)
