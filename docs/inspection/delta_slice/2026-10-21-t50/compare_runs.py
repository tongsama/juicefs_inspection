#!/usr/bin/env python3
"""metrics_sampler.sh の TSV から、2 つの計測の窓（前回の block モードと今回の delta モード）の
counter の差分を並べて比べる（読み取りのみ）。

使い方:
  compare_runs.py --run LABEL:TSV[.gz]:START:END [--run ...]
    START/END は UNIX 秒か "now"/"first"。END に "now" を書くと TSV の最後のサンプルを使う。
出力: 窓ごとの量（staging、compaction の出力、PUT／GET／DELETE、cache 書込、kaz の block/delta の結果）と、
  staging 1 GiB あたりの compaction 出力と PUT の比。counter の差は窓の両端の直前のサンプルで取る。
  uptime の差が窓の長さより大きく減っていれば、窓の中で再起動があったとして警告する。
"""
import argparse
import collections
import gzip

GiB = 1 << 30

# 比べる系列（metric 名とラベル、割る数、表示名）。
KEYS = [
    ("juicefs_staging_write_bytes", GiB, "staging 書込 GiB"),
    ("juicefs_compact_size_histogram_bytes_sum", GiB, "compaction 出力 GiB（非圧縮）"),
    ("juicefs_compact_size_histogram_bytes_count", 1, "compaction 出力 slice 数"),
    ('juicefs_object_request_data_bytes{method="PUT"}', GiB, "PUT GiB（圧縮後）"),
    ('juicefs_object_request_durations_histogram_seconds_count{method="PUT"}', 1, "PUT 回数"),
    ('juicefs_object_request_data_bytes{method="GET"}', GiB, "GET GiB"),
    ('juicefs_object_request_durations_histogram_seconds_count{method="GET"}', 1, "GET 回数"),
    ('juicefs_object_request_durations_histogram_seconds_count{method="DELETE"}', 1, "DELETE 回数"),
    ("juicefs_blockcache_write_bytes", GiB, "cache 書込 GiB"),
    ("juicefs_fuse_ops_durations_histogram_seconds_count", 1, "FUSE ops"),
    ("juicefs_meta_ops_durations_histogram_seconds_count", 1, "meta ops"),
]


def load(path):
    """TSV（gzip 可）を系列名 → [(UNIX 秒, 値)] の dict にする。"""
    op = gzip.open if path.endswith(".gz") else open
    m = collections.defaultdict(list)
    with op(path, "rt") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 4:
                continue
            try:
                m[p[2]].append((int(p[0]), float(p[3])))
            except ValueError:
                pass
    for v in m.values():
        v.sort()  # 追記の順が時刻順とは限らない（sampler を動かし直したときなど）
    return m


def at(series, ts):
    """ts 以前で最も新しいサンプル値（なければ None）。"""
    best = None
    for t, v in series:
        if t <= ts:
            best = v
        else:
            break
    return best


def first_after(series, ts):
    """ts 以後で最も古いサンプル値（なければ None）。窓の始まりの直前が前の mount の値になるのを避ける。"""
    for t, v in series:
        if t >= ts:
            return v
    return None


def diff(m, key, a, b):
    """系列 key の窓 [a, b] の差（始まりは a 以後の最初のサンプル、終わりは b 以前の最後のサンプル）。
    始まりに値が無い counter は、窓の中で初めて出てきたものとして 0 から数える。"""
    s = m.get(key, [])
    vb = at(s, b)
    if vb is None:
        return 0.0
    va = first_after(s, a)
    return vb - (va or 0.0)


def kaz_events(m, a, b):
    """kaz の block/delta compaction の結果と量（ラベルごとの差）。"""
    out = {}
    for k in m:
        if k.startswith("juicefs_kaz_compaction_block_") and "{" in k:
            d = diff(m, k, a, b)
            if d:
                out[k.replace("juicefs_kaz_compaction_block_", "")] = d
    return out


def main():
    """各窓の差分と比を Markdown の表で出す。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True)
    a = ap.parse_args()
    rows = []
    for spec in a.run:
        label, path, s, e = spec.rsplit(":", 3)
        m = load(path)
        ts = sorted({t for v in m.values() for t, _ in v})
        start = ts[0] if s == "first" else int(s)
        end = ts[-1] if e == "now" else int(e)
        up0, up1 = first_after(m["juicefs_uptime"], start), at(m["juicefs_uptime"], end)
        vals = {name: diff(m, key, start, end) / div for key, div, name in KEYS}
        rows.append((label, start, end, up0, up1, vals, kaz_events(m, start, end)))
    print("| 項目 | " + " | ".join(r[0] for r in rows) + " |")
    print("|---|" + "---|" * len(rows))
    print("| 窓の長さ h | " + " | ".join(f"{(r[2]-r[1])/3600:.2f}" for r in rows) + " |")
    print("| uptime の差 h | " + " | ".join("-" if r[3] is None or r[4] is None else f"{(r[4]-r[3])/3600:.2f}" for r in rows) + " |")
    for _, _, name in KEYS:
        print(f"| {name} | " + " | ".join(f"{r[5][name]:.2f}" if r[5][name] % 1 else f"{r[5][name]:.0f}" for r in rows) + " |")
    st = "staging 書込 GiB"
    for name in ("compaction 出力 GiB（非圧縮）", "PUT GiB（圧縮後）", "PUT 回数"):
        print(f"| {name} ÷ staging GiB | " + " | ".join(f"{r[5][name]/r[5][st]:.2f}" if r[5][st] else "-" for r in rows) + " |")
    for r in rows:
        # サンプルの間隔（最大 2 分ずつ両端）の分だけ短いのは正常。それより短ければ再起動を疑う。
        if r[3] is not None and r[4] is not None and r[4] - r[3] < (r[2] - r[1]) - 240:
            print(f"\n警告: {r[0]} の窓の中で再起動があった可能性（uptime の差が窓より短い）")
    for r in rows:
        if r[6]:
            print(f"\n{r[0]} の kaz compaction（差分）:")
            for k, v in sorted(r[6].items()):
                print(f"- {k}: {v:.0f}")


if __name__ == "__main__":
    main()
