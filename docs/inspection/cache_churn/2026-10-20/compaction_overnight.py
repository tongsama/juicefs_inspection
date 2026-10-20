#!/usr/bin/env python3
"""夜間 compaction 量の解析（読み取りのみ）。

入力:
  --log      診断ログ（先頭 --prefix バイトだけを読む。SHA256 も出力する）
  --metrics  1 分間隔 metrics.tsv（unix秒, ISO, 系列, 値）
  --start/--end  解析窓（unix 秒）
出力: 標準出力へ集計表（Markdown 断片）。--csv を与えると compaction 行を CSV 化する。
本番の metadata／cache／staging／元ログには一切書き込まない。
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import re
import statistics

JST = dt.timezone(dt.timedelta(hours=9))
MiB = 1 << 20
GiB = 1 << 30

# slow compaction 行（block mode と upstream の両方）を取り出す正規表現
RE_COMP = re.compile(
    r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\.\d+) .*slow compaction inode=(\d+) chunk=(\d+) once=(\w+) force=(\w+) "
    r"slices=(\d+) bytes=(\d+) total=(\S+) queue_wait=(\S+) object=(\S+) metadata=(\S+)(?: mode=(\w+))?")
# readSlice 失敗行（compaction 等の読み込みが中断されたもの）
RE_READFAIL = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)\.\d+ .*fail to read sliceId (\d+) .*inode: (\d+)\): (.*) \[readSlice")
# 書き込み側の slow metadata write（slices= は commit 後の record 数）
RE_SLOWWRITE = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)\.\d+ .*slow metadata write.* inode=(\d+) chunk=(\d+) .*slices=(\d+)")


def parse_ts(s):
    """ログの時刻文字列（JST）を unix 秒へ変換する。"""
    fmt = "%Y/%m/%d %H:%M:%S.%f" if "." in s else "%Y/%m/%d %H:%M:%S"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=JST).timestamp()


def dur(s):
    """Go の Duration 文字列（1m2.3s, 250ms, 2µs など）を秒へ変換する。"""
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(h|ms|µs|us|ns|m|s)", s):
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}[unit]
    return total


def hour_of(ts):
    """unix 秒を JST の『MM-DD HH時』ラベルにする。"""
    return dt.datetime.fromtimestamp(ts, JST).strftime("%m-%d %H")


def load_log(path, prefix):
    """ログ先頭 prefix バイトを読み、SHA256 と行リストを返す。"""
    with open(path, "rb") as f:
        data = f.read(prefix)
    sha = hashlib.sha256(data).hexdigest()
    return sha, data.decode("utf-8", "replace").splitlines()


def load_metrics(path):
    """metrics.tsv を {系列: [(ts, 値), ...]} に読む。"""
    m = collections.defaultdict(list)
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 4:
                continue
            try:
                m[p[2]].append((int(p[0]), float(p[3])))
            except ValueError:
                pass
    return m


def value_at(series, ts):
    """ts 以前で最も新しいサンプル値（なければ None）を返す。"""
    best = None
    for t, v in series:
        if t <= ts:
            best = v
        else:
            break
    return best


def metrics_section(m, start, end):
    """解析窓と 1 時間ごとの counter 差分を表にする（窓内に再起動がない前提を uptime で確認）。"""
    keys = [
        ("juicefs_compact_size_histogram_bytes_sum", GiB, "compaction 出力(GiB, 非圧縮)"),
        ("juicefs_compact_size_histogram_bytes_count", 1, "compaction 出力 slice 数"),
        ("juicefs_staging_write_bytes", GiB, "staging 書込(GiB)"),
        ('juicefs_object_request_data_bytes{method="PUT"}', GiB, "PUT(GiB)"),
        ('juicefs_object_request_data_bytes{method="GET"}', GiB, "GET(GiB)"),
        ("juicefs_blockcache_write_bytes", GiB, "cache 書込(GiB)"),
        ("juicefs_fuse_ops_durations_histogram_seconds_count", 1, "FUSE ops"),
    ]
    out = []
    up0, up1 = value_at(m["juicefs_uptime"], start), value_at(m["juicefs_uptime"], end)
    out.append(f"- uptime: start={up0:.0f}s end={up1:.0f}s（差 {up1-up0:.0f}s, 窓 {end-start}s。差が窓とほぼ等しければ窓内に再起動なし）")
    out.append("")
    out.append("| 区間 | " + " | ".join(k[2] for k in keys) + " |")
    out.append("|---|" + "---|" * len(keys))
    edges = [start]
    t = (start // 3600 + 1) * 3600
    while t < end:
        edges.append(t)
        t += 3600
    edges.append(end)
    rows = list(zip(edges[:-1], edges[1:])) + [(start, end)]
    for a, b in rows:
        cells = []
        for k, div, _ in keys:
            va, vb = value_at(m[k], a), value_at(m[k], b)
            cells.append("-" if va is None or vb is None else (f"{(vb-va)/div:.2f}" if div != 1 else f"{vb-va:.0f}"))
        label = "合計" if (a, b) == (start, end) else f"{dt.datetime.fromtimestamp(a, JST):%m-%d %H:%M}–{dt.datetime.fromtimestamp(b, JST):%H:%M}"
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main():
    """各集計を Markdown 断片として出力する。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--prefix", type=int, required=True)
    ap.add_argument("--metrics", required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--csv")
    a = ap.parse_args()

    sha, lines = load_log(a.log, a.prefix)
    print(f"## log prefix\n- bytes={a.prefix} sha256={sha} lines={len(lines)}\n")

    comps, readfail, slowwrites = [], [], []
    for ln in lines:
        mm = RE_COMP.match(ln)
        if mm:
            ts = parse_ts(mm.group(1))
            comps.append(dict(ts=ts, inode=int(mm.group(2)), chunk=int(mm.group(3)), once=mm.group(4),
                              force=mm.group(5), slices=int(mm.group(6)), bytes=int(mm.group(7)),
                              total=dur(mm.group(8)), object=dur(mm.group(10)), metadata=dur(mm.group(11)),
                              mode=mm.group(12) or "upstream"))
            continue
        mm = RE_READFAIL.match(ln)
        if mm:
            readfail.append((parse_ts(mm.group(1)), int(mm.group(2)), int(mm.group(3)), mm.group(4)))
            continue
        mm = RE_SLOWWRITE.match(ln)
        if mm:
            slowwrites.append((parse_ts(mm.group(1)), int(mm.group(2)), int(mm.group(3)), int(mm.group(4))))

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "iso", "inode", "chunk", "once", "force", "slices", "bytes", "total_s", "object_s", "metadata_s", "mode"])
            for c in comps:
                w.writerow([f"{c['ts']:.3f}", dt.datetime.fromtimestamp(c['ts'], JST).isoformat(), c["inode"], c["chunk"],
                            c["once"], c["force"], c["slices"], c["bytes"], f"{c['total']:.3f}", f"{c['object']:.3f}",
                            f"{c['metadata']:.3f}", c["mode"]])

    win = [c for c in comps if a.start <= c["ts"] < a.end]
    allb = sum(c["bytes"] for c in comps)
    wb = sum(c["bytes"] for c in win)
    print("## slow compaction 行（total>=1s のみ記録される）")
    print(f"- 全体: {len(comps)} 行, {allb/GiB:.2f} GiB; 窓内: {len(win)} 行, {wb/GiB:.2f} GiB")
    print(f"- mode: {dict(collections.Counter(c['mode'] for c in win))}, once: {dict(collections.Counter(c['once'] for c in win))}, force: {dict(collections.Counter(c['force'] for c in win))}")
    print(f"- inode: {dict(collections.Counter(c['inode'] for c in win))}")
    print(f"- 合計所要 {sum(c['total'] for c in win)/3600:.2f} h（窓 {(a.end-a.start)/3600:.2f} h; inode ごとに 1 chunk ずつ直列）\n")

    # 時間帯別
    print("### 時間帯別（窓内）\n| 時 | 件数 | GiB | 中央 slices | 中央 MiB/件 | 所要 h |\n|---|---|---|---|---|---|")
    byh = collections.defaultdict(list)
    for c in win:
        byh[hour_of(c["ts"])].append(c)
    for h in sorted(byh):
        cs = byh[h]
        print(f"| {h} | {len(cs)} | {sum(c['bytes'] for c in cs)/GiB:.2f} | {statistics.median(c['slices'] for c in cs):.0f} | "
              f"{statistics.median(c['bytes'] for c in cs)/MiB:.0f} | {sum(c['total'] for c in cs)/3600:.2f} |")

    # bytes（=書いた 4MiB block 数）の分布
    print("\n### 1 回あたりの出力 block 数（bytes/4MiB）分布（窓内）\n| blocks | 件数 | GiB |\n|---|---|---|")
    bd = collections.defaultdict(lambda: [0, 0])
    for c in win:
        k = round(c["bytes"] / (4 * MiB))
        bd[k][0] += 1
        bd[k][1] += c["bytes"]
    for k in sorted(bd):
        print(f"| {k} | {bd[k][0]} | {bd[k][1]/GiB:.2f} |")

    # slices の分布（commit trigger は 99/199/.. または >350、read trigger は >=100）
    print("\n### compaction 開始時の records 数（slices=）分布（窓内）\n| 範囲 | 件数 | GiB |\n|---|---|---|")
    bins = [(0, 49), (50, 98), (99, 99), (100, 149), (150, 198), (199, 199), (200, 298), (299, 299), (300, 350), (351, 999), (1000, 10**9)]
    for lo, hi in bins:
        cs = [c for c in win if lo <= c["slices"] <= hi]
        print(f"| {lo}–{hi} | {len(cs)} | {sum(c['bytes'] for c in cs)/GiB:.2f} |")

    # chunk 別の繰り返し
    byc = collections.defaultdict(list)
    for c in win:
        byc[(c["inode"], c["chunk"])].append(c)
    rep = collections.Counter(len(v) for v in byc.values())
    print(f"\n### chunk 別（窓内）\n- 対象 chunk 数: {len(byc)}（同一 chunk の回数分布: {dict(sorted(rep.items()))}）")
    multi = [v for v in byc.values() if len(v) >= 2]
    print(f"- 2 回以上 compaction された chunk: {len(multi)} 個, その出力 {sum(c['bytes'] for v in multi for c in v)/GiB:.2f} GiB"
          f"（うち 2 回目以降 {sum(c['bytes'] for v in multi for c in v[1:])/GiB:.2f} GiB）")
    gaps = [v[i]["ts"] - v[i-1]["ts"] for v in multi for i in range(1, len(v))]
    if gaps:
        q = statistics.quantiles(gaps, n=4)
        print(f"- 同一 chunk の再 compaction 間隔: 中央 {statistics.median(gaps)/60:.1f} 分, 四分位 {q[0]/60:.1f}/{q[2]/60:.1f} 分, 最小 {min(gaps):.0f}s")
    print("\n| inode | chunk | 回数 | GiB | slices（各回） | 時刻(最初–最後) |\n|---|---|---|---|---|---|")
    for (ino, ch), v in sorted(byc.items(), key=lambda kv: -sum(c["bytes"] for c in kv[1]))[:25]:
        sl = ",".join(str(c["slices"]) for c in v[:12]) + ("…" if len(v) > 12 else "")
        print(f"| {ino} | {ch} | {len(v)} | {sum(c['bytes'] for c in v)/GiB:.2f} | {sl} | "
              f"{dt.datetime.fromtimestamp(v[0]['ts'], JST):%H:%M}–{dt.datetime.fromtimestamp(v[-1]['ts'], JST):%H:%M} |")

    # chunk index の範囲（ディスク上のオフセット = chunk*64MiB）
    print("\n### chunk index 帯（64 MiB 単位 = ディスク内オフセット GiB 帯, 窓内）\n| オフセット帯(GiB) | chunk 数 | 回数 | GiB |\n|---|---|---|---|")
    band = collections.defaultdict(lambda: [set(), 0, 0])
    for c in win:
        g = (c["chunk"] * 64) // 1024  # GiB 単位のオフセット
        b = band[(g // 8) * 8]
        b[0].add(c["chunk"]); b[1] += 1; b[2] += c["bytes"]
    for k in sorted(band):
        print(f"| {k}–{k+8} | {len(band[k][0])} | {band[k][1]} | {band[k][2]/GiB:.2f} |")

    # 読み込み失敗
    rw = [r for r in readfail if a.start <= r[0] < a.end]
    print(f"\n### readSlice 失敗（窓内）: {len(rw)} 行, 理由 {dict(collections.Counter(r[3][:40] for r in rw).most_common(5))}")
    print(f"- slice 種類 {len(set(r[1] for r in rw))}, 時間帯 {dict(collections.Counter(hour_of(r[0]) for r in rw))}")
    sw = [s for s in slowwrites if a.start <= s[0] < a.end]
    print(f"\n### slow metadata write（窓内）: {len(sw)} 行, chunk {sorted(set(s[2] for s in sw))[:20]}")

    m = load_metrics(a.metrics)
    print("\n## metrics 差分\n")
    print(metrics_section(m, a.start, a.end))


if __name__ == "__main__":
    main()
