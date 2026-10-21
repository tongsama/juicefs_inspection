"""計測の記録（metrics.tsv、llen.tsv、config.json）から表を作る。progress／compare の本体。
counter の差は lib.metrics_io.delta（mount のし直しで 0 に戻る分は足し合わせ、窓の始まりは a 以後の最初のサンプル）で取る。"""
import collections
import json
import os
import re
import time

from lib import metrics_io

GiB = 1 << 30
ISO = "%Y-%m-%dT%H:%M:%S%z"
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
# compare の区間指定の時刻の部分（: を含む日時があるので、全体を正規表現で分ける）
_T = r"(?:first|now|\d{9,}|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)"
_SPEC_RE = re.compile(r"^(?P<label>[^:]+):(?P<path>.+):(?P<s>%s):(?P<e>%s)$" % (_T, _T))


class NoData(ValueError):
    """指定した区間に記録が無いときの例外。"""


def parse_time(text, first, last):
    """時刻の指定を UNIX 秒にする。first／now は記録の最初／最後のサンプル、数字は UNIX 秒、
    それ以外は 'YYYY-MM-DD HH:MM[:SS]'（T 区切りも可、ローカル時刻）。"""
    t = str(text).strip()
    if t == "first":
        return first
    if t == "now":
        return last
    if t.isdigit():
        return int(t)
    for f in _TIME_FORMATS:
        try:
            return int(time.mktime(time.strptime(t, f)))
        except ValueError:
            pass
    raise ValueError("時刻を読めない: %r（UNIX 秒、first、now、'YYYY-MM-DD HH:MM[:SS]' のいずれか）" % text)


def metrics_path(path):
    """出力ディレクトリなら metrics.tsv、TSV（.gz 可）ならそのまま。"""
    return os.path.join(path, "metrics.tsv") if os.path.isdir(path) else path


def sample_times(m):
    """全系列のサンプル時刻（昇順・重複なし）。"""
    return sorted({t for v in m.values() for t, _ in v})


def _D(m, name, a, b):
    """系列 name の窓 [a, b] の増分（無ければ 0）。"""
    return metrics_io.delta(m.get(name, []), a, b)


def _Dsum(m, base, a, b):
    """name が base、または base{...} の系列すべての増分の合計。"""
    return sum(metrics_io.delta(v, a, b) for k, v in m.items() if k == base or k.startswith(base + "{"))


def _last(m, name, a, b):
    """窓の中の最後の値（無ければ 0）。"""
    pts = [v for t, v in m.get(name, []) if a <= t <= b]
    return pts[-1] if pts else 0.0


def _labels(m, prefix, label):
    """prefix{label="X"} の系列を {X: 系列名} にする。"""
    out = {}
    pat = re.compile(r'^%s\{%s="([^"]*)"\}$' % (re.escape(prefix), label))
    for k in m:
        mo = pat.match(k)
        if mo:
            out[mo.group(1)] = k
    return out


# compaction の event（出す順）と日本語の意味
EVENTS = [("partial", "一部"), ("full", "全部"), ("noop", "何もしない"), ("wasted", "無駄"), ("error", "失敗"),
          ("fallback_upstream", "upstream へ"), ("fallback_block", "block へ"), ("delta_noop", "delta 何もしない"),
          ("delta_tier", "段"), ("delta_merge", "まとめ直し"), ("delta_fold", "畳み込み"), ("delta_prune", "外した"),
          ("delta_block_fallback", "delta から block へ"), ("delta_hidden_full", "隠れた全部")]
# 書き直しの理由ごとのブロックの数
REASONS = [("delta_blocks_whole", "丸ごと"), ("delta_blocks_fallback", "fallback"), ("delta_blocks_nobase", "元なし"),
           ("delta_blocks_ratio", "比が大きい"), ("delta_fold_blocks", "畳み込み")]
EV_PREFIX = "juicefs_kaz_compaction_block_events_total"
AM_PREFIX = "juicefs_kaz_compaction_block_amount_total"
GC_PREFIX = "juicefs_compaction_gc_events_total"


def compaction_counts(m, a, b):
    """kaz compaction の event と amount の増分。{'events': {名前: 値}, 'amounts': {名前: 値}}（系列がある分だけ）。"""
    ev = {e: _D(m, k, a, b) for e, k in _labels(m, EV_PREFIX, "event").items()}
    am = {e: _D(m, k, a, b) for e, k in _labels(m, AM_PREFIX, "kind").items()}
    return {"events": ev, "amounts": am}


def _ordered(d, known):
    """known の順に並べ、知らない名前は後ろに名前順で足す。"""
    keys = [k for k, _ in known if k in d] + sorted(k for k in d if k not in dict(known))
    return keys


def _llen_load(path):
    """llen.tsv（UNIX 秒、ISO、chunk 数、合計、平均、中央、90%、最大、350 超）を行のリストにする。error の行は除く。"""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 9 and p[2] != "error":
                    try:
                        rows.append((int(p[0]), p))
                    except ValueError:
                        pass
    except OSError:
        pass
    return sorted(rows, key=lambda r: r[0])


def _foot(*names):
    """項目の下に付ける系列名の脚注。"""
    return "  ※ 系列: " + "、".join(names)


def progress_period(m, llen_rows, a, b):
    """窓 [a, b] の progress の本文。[流入]〜[期間の終わりの状態] は progress_kaz.sh と同じ並び・同じ言葉、
    その後ろに compaction、object storage、片付け、メタデータ、record の数を足す。"""
    pts = sorted({t for v in m.values() for t, x in v if a <= t <= b})
    if not pts:
        raise NoData("この期間の記録がありません")
    first, last = pts[0], pts[-1]
    h = (last - first) / 3600
    if h <= 0:
        h = 1 / 60
    g = lambda x: x / GiB
    iso = lambda t: time.strftime(ISO, time.localtime(t))
    D = lambda n: _D(m, n, a, b)
    inn, stage, comp = D("juicefs_blockcache_write_bytes"), D("juicefs_staging_write_bytes"), D("juicefs_compact_size_histogram_bytes_sum")
    miss, hit = D("juicefs_blockcache_miss_bytes"), D("juicefs_blockcache_hit_bytes")
    get_n = D('juicefs_object_request_durations_histogram_seconds_count{method="GET"}')
    get_s = D('juicefs_object_request_durations_histogram_seconds_sum{method="GET"}')
    ev = {e: D('juicefs_kaz_cache_2q_events_total{event="%s"}' % e) for e in (
        "promote", "demote", "demote_unread", "demote_read", "ghost_hit", "ghost_hit_demoted", "ghost_hit_probation",
        "ghost_hit_compaction", "ghost_hit_protected", "evict_probation", "evict_protected",
        "compaction_output_cached", "compaction_output_hot", "compaction_output_skipped")}
    L = ["期間: %s 〜 %s（%.1f 時間）" % (iso(first), iso(last), h), "", "[流入]"]
    L.append("  キャッシュへの流入   %7.1f GiB（%.1f GiB/h）" % (g(inn), g(inn) / h))
    L.append("    書き込み（staging）%7.1f GiB" % g(stage))
    L.append("    compaction の出力  %7.1f GiB（キャッシュに入らなかった分を含む）" % g(comp))
    L.append("    ミスの補充（目安） %7.1f GiB" % g(miss))
    L.append(_foot("blockcache_write_bytes", "staging_write_bytes", "compact_size_histogram_bytes_sum", "blockcache_miss_bytes"))
    L += ["", "[読み込み]"]
    tot = hit + miss
    L.append("  ヒット %.1f GiB / ミス %.1f GiB（ヒット率 %.1f%%）" % (g(hit), g(miss), 100 * hit / tot if tot > 0 else 0))
    L.append("  GET %d 件、合計 %.0f 秒" % (get_n, get_s))
    L.append(_foot("blockcache_hit_bytes", "blockcache_miss_bytes", "object_request_durations_histogram_seconds{method=GET}"))
    L += ["", "[2Q]"]
    L.append("  昇格 %d、降格 %d（読まれていない %d、読まれた %d）、ghost_hit %d" % (
        ev["promote"], ev["demote"], ev["demote_unread"], ev["demote_read"], ev["ghost_hit"]))
    L.append("    ghost_hit の内訳: 降格を経た %d、常連用に上がらなかった %d、compaction の出力 %d、常連用から %d" % (
        ev["ghost_hit_demoted"], ev["ghost_hit_probation"], ev["ghost_hit_compaction"], ev["ghost_hit_protected"]))
    L.append("  追い出し: 新入り用 %d、常連用 %d" % (ev["evict_probation"], ev["evict_protected"]))
    L.append("  compaction の出力: キャッシュ %d（若い元を除いて新入り用にしたもの %d）、見送り %d" % (
        ev["compaction_output_cached"], ev["compaction_output_hot"], ev["compaction_output_skipped"]))
    L.append(_foot("kaz_cache_2q_events_total{event=...}"))
    L += ["", "[期間の終わりの状態]"]
    L.append("  常連用 %.1f GB（うち読まれた実績なし %.1f GB）、新入り用 %.1f GB、追い出し済みリスト %.1f GB" % (
        _last(m, 'juicefs_kaz_cache_2q_bytes{segment="protected"}', a, b) / 1e9,
        _last(m, "juicefs_kaz_cache_2q_protected_unread_bytes", a, b) / 1e9,
        _last(m, 'juicefs_kaz_cache_2q_bytes{segment="probation"}', a, b) / 1e9,
        _last(m, "juicefs_kaz_cache_2q_ghost_bytes", a, b) / 1e9))
    L.append(_foot("kaz_cache_2q_bytes{segment}", "kaz_cache_2q_protected_unread_bytes", "kaz_cache_2q_ghost_bytes"))
    # --- ここから足した項目 ---
    cc = compaction_counts(m, a, b)
    L += ["", "[compaction]"]
    if not cc["events"] and not cc["amounts"]:
        L.append("  （kaz compaction の系列が記録に無い）")
    if cc["events"]:
        L.append("  結果（回数）: " + "、".join("%s %d" % (e, cc["events"][e]) for e in _ordered(cc["events"], EVENTS)))
    am = cc["amounts"]
    why = [(k, n) for k, n in REASONS if k in am]
    if why:
        L.append("  書き直しの理由（ブロック数）: " + "、".join("%s %d" % (n, am[k]) for k, n in why))
    parts = []
    for k, n, fmt in (("delta_bytes", "段のバイト", "%.2f GiB"), ("delta_merge_bytes", "まとめ直しのバイト", "%.2f GiB")):
        if k in am:
            parts.append(("%s " + fmt) % (n, am[k] / GiB))
    if "removed" in am:
        parts.append("外した record %d" % am["removed"])
    if parts:
        L.append("  " + "、".join(parts))
    others = [k for k in _ordered(am, []) if k not in dict(REASONS) and k not in ("delta_bytes", "delta_merge_bytes", "removed")]
    if others:
        L.append("  その他の量: " + "、".join("%s %d" % (k, am[k]) for k in others))
    sg = g(stage)
    L.append("  staging %.2f GiB に対し compaction の出力 %.2f GiB（staging 1 GiB あたり %s）" % (
        sg, g(comp), "%.2f" % (g(comp) / sg) if sg else "-"))
    for k, n in (("delta_bytes", "段のバイト"), ("delta_merge_bytes", "まとめ直しのバイト")):
        if k in am and sg:
            L.append("    %s は staging 1 GiB あたり %.3f GiB" % (n, am[k] / GiB / sg))
    L.append(_foot("kaz_compaction_block_events_total{event}", "kaz_compaction_block_amount_total{kind}"))
    L += ["", "[object storage]"]
    for meth in ("PUT", "GET", "DELETE"):
        c = D('juicefs_object_request_durations_histogram_seconds_count{method="%s"}' % meth)
        s = D('juicefs_object_request_durations_histogram_seconds_sum{method="%s"}' % meth)
        by = D('juicefs_object_request_data_bytes{method="%s"}' % meth)
        txt = "  %s %d 回" % (meth, c)
        if meth != "DELETE":
            txt += "、%.2f GiB" % g(by)
        txt += "、平均 %s" % ("%.1f ms" % (1000 * s / c) if c else "-")
        L.append(txt)
    put_n = D('juicefs_object_request_durations_histogram_seconds_count{method="PUT"}')
    put_b = D('juicefs_object_request_data_bytes{method="PUT"}')
    if sg:
        L.append("  staging 1 GiB あたり: PUT %.1f 回、PUT %.2f GiB" % (put_n / sg, g(put_b) / sg))
    L.append(_foot("object_request_durations_histogram_seconds_{count,sum}{method}", "object_request_data_bytes{method}"))
    gc = {e: D(k) for e, k in _labels(m, GC_PREFIX, "event").items()}
    L += ["", "[片付け]"]
    L.append("  " + ("、".join("%s %d" % (e, gc[e]) for e in sorted(gc)) if gc else "（compaction_gc_events_total の系列が無い）"))
    L.append(_foot("compaction_gc_events_total{event}（local_retire_success は片付けた slice の数）"))
    L += ["", "[メタデータ]"]
    for lab, base in (("meta ops", "juicefs_meta_ops_durations_histogram_seconds"),
                      ("transaction", "juicefs_transaction_durations_histogram_seconds")):
        c, s = _Dsum(m, base + "_count", a, b), _Dsum(m, base + "_sum", a, b)
        L.append("  %s %d 回、平均 %s" % (lab, c, "%.2f ms" % (1000 * s / c) if c else "-"))
    L.append(_foot("meta_ops_durations_histogram_seconds_{count,sum}", "transaction_durations_histogram_seconds_{count,sum}"))
    L += ["", "[record の数]"]
    cand = [p for t, p in llen_rows if a <= t <= b]
    if cand:
        p = cand[-1]
        L.append("  %s: chunk %s、合計 %s、平均 %s、中央 %s、90%% %s、最大 %s、350 超 %s" % ((p[1],) + tuple(p[2:9])))
    else:
        L.append("  （この期間の llen.tsv の記録が無い）")
    L.append(_foot("llen.tsv の期間の最後の行"))
    return "\n".join(L)


def progress(outdir, start="first", end="now"):
    """出力ディレクトリの progress。start／end を省くと「開始から今まで」と「直近 1 時間」を続けて出す。
    指定があれば、その 1 区間だけ出す。"""
    m = metrics_io.load(metrics_path(outdir))
    ts = sample_times(m)
    if not ts:
        raise NoData("metrics.tsv に記録がありません")
    llen = _llen_load(os.path.join(outdir, "llen.tsv")) if os.path.isdir(outdir) else []
    b = parse_time(end, ts[0], ts[-1])
    a = parse_time(start, ts[0], ts[-1])
    if a >= b:
        raise ValueError("開始 (%s) が終了 (%s) より後です" % (time.strftime(ISO, time.localtime(a)), time.strftime(ISO, time.localtime(b))))
    if (start, end) != ("first", "now"):
        return progress_period(m, llen, a, b)
    return ("=== 開始から今まで ===\n" + progress_period(m, llen, a, b) + "\n\n=== 直近 1 時間 ===\n" +
            progress_period(m, llen, max(a, b - 3600), b))


# compare で並べる系列（系列名、割る数、表示名、種類 g=GiB の小数、n=回数）
KEYS = [
    ("juicefs_staging_write_bytes", GiB, "staging 書込 GiB", "g"),
    ("juicefs_compact_size_histogram_bytes_sum", GiB, "compaction 出力 GiB（非圧縮）", "g"),
    ("juicefs_compact_size_histogram_bytes_count", 1, "compaction 出力 slice 数", "n"),
    ('juicefs_object_request_data_bytes{method="PUT"}', GiB, "PUT GiB（圧縮後）", "g"),
    ('juicefs_object_request_durations_histogram_seconds_count{method="PUT"}', 1, "PUT 回数", "n"),
    ('juicefs_object_request_data_bytes{method="GET"}', GiB, "GET GiB", "g"),
    ('juicefs_object_request_durations_histogram_seconds_count{method="GET"}', 1, "GET 回数", "n"),
    ('juicefs_object_request_durations_histogram_seconds_count{method="DELETE"}', 1, "DELETE 回数", "n"),
    ("juicefs_blockcache_write_bytes", GiB, "cache 書込 GiB", "g"),
    ("juicefs_fuse_ops_durations_histogram_seconds_count", 1, "FUSE ops", "n"),
    ("juicefs_meta_ops_durations_histogram_seconds_count", 1, "meta ops", "n"),
]


def _fmt(v, kind):
    """表のセルの数字。回数は 3 桁区切りの整数、GiB は小数 2 桁。"""
    return "{:,.0f}".format(v) if kind == "n" else "%.2f" % v


def _flatten(obj, pre=""):
    """JSON を {ドット区切りの項目名: 値} にする（リストは添字つき）。"""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, pre + "." + k if pre else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, "%s[%d]" % (pre, i)))
    else:
        out[pre] = obj
    return out


CONFIG_IGNORE = {"Pid", "PPid", "CommPath", "StatePath"}  # mount ごとに必ず違う項目（設定の違いではない）


def config_rows(specs):
    """区間ごとの config.json の違い。{項目: [各区間の値 or None]} と、config.json の無い区間のラベル。
    比べるのは config.json のある区間だけ。値がそろっている項目と、CONFIG_IGNORE（最後の名前で判定）は含めない。"""
    flats, missing = [], []
    for s in specs:
        p = os.path.join(s["path"], "config.json") if os.path.isdir(s["path"]) else None
        flat = None
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    flat = _flatten(json.load(f))
            except (OSError, ValueError):
                flat = None
        if flat is None:
            missing.append(s["label"])
        flats.append(flat)
    have = [f for f in flats if f is not None]
    rows = {}
    if len(have) >= 2:
        for k in sorted(set().union(*have)):
            if k.rsplit(".", 1)[-1] in CONFIG_IGNORE:
                continue
            vals = [None if f is None else f.get(k, "(なし)") for f in flats]
            if len({json.dumps(v, sort_keys=True) for v in vals if v is not None}) > 1:
                rows[k] = vals
    return rows, missing


def parse_spec(spec):
    """'ラベル:ディレクトリかTSV:開始:終了' を dict にする。"""
    mo = _SPEC_RE.match(spec)
    if not mo:
        raise ValueError("区間の指定を読めない: %r（ラベル:ディレクトリかTSV:開始:終了）" % spec)
    return {"label": mo.group("label"), "path": mo.group("path"), "s": mo.group("s"), "e": mo.group("e")}


def _short(v, n=60):
    """表に出す設定値（長ければ切る）。"""
    s = "-" if v is None else json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n - 1] + "…"


def compare(specs):
    """区間を同じ表（Markdown）で並べる。先頭に config.json の違い、続いて窓の量、compaction、片付け、メタデータ。"""
    runs = []
    for sp in specs:
        s = parse_spec(sp)
        m = metrics_io.load(metrics_path(s["path"]))
        ts = sample_times(m)
        if not ts:
            raise NoData("%s: 記録がありません" % s["label"])
        a, b = parse_time(s["s"], ts[0], ts[-1]), parse_time(s["e"], ts[0], ts[-1])
        s.update(m=m, a=a, b=b)
        runs.append(s)
    out = ["| 項目 | " + " | ".join(r["label"] for r in runs) + " |", "|---|" + "---|" * len(runs)]
    crow, missing = config_rows(runs)
    for k, vals in crow.items():
        out.append("| 設定 %s | %s |" % (k, " | ".join(_short(v) for v in vals)))
    if missing and len(missing) < len(runs):
        out.append("| 設定の比較 | " + " | ".join("config.json なし" if r["label"] in missing else "あり" for r in runs) + " |")
    out.append("| 窓の長さ h | " + " | ".join("%.2f" % ((r["b"] - r["a"]) / 3600) for r in runs) + " |")
    resets = []
    for r in runs:
        up = [v for t, v in r["m"].get("juicefs_uptime", []) if r["a"] <= t <= r["b"]]
        resets.append(None if not up else sum(1 for x, y in zip(up, up[1:]) if y < x))
    out.append("| mount のし直し（uptime の巻き戻り）回数 | " + " | ".join("-" if x is None else str(x) for x in resets) + " |")
    vals = [{n: _D(r["m"], k, r["a"], r["b"]) / d for k, d, n, _ in KEYS} for r in runs]
    for k, d, n, kind in KEYS:
        out.append("| %s | %s |" % (n, " | ".join(_fmt(v[n], kind) for v in vals)))
    st = "staging 書込 GiB"
    for n in ("compaction 出力 GiB（非圧縮）", "PUT GiB（圧縮後）", "PUT 回数"):
        out.append("| %s ÷ staging GiB | %s |" % (n, " | ".join("%.2f" % (v[n] / v[st]) if v[st] else "-" for v in vals)))
    # compaction
    ccs = [compaction_counts(r["m"], r["a"], r["b"]) for r in runs]
    for sect, known, title in (("events", EVENTS, "compaction 結果"), ("amounts", REASONS, "compaction 量")):
        names = set().union(*(c[sect].keys() for c in ccs))
        order = [k for k, _ in known if k in names] + sorted(names - {k for k, _ in known})
        for k in order:
            out.append("| %s: %s | %s |" % (title, k, " | ".join(
                "-" if k not in c[sect] else _fmt(c[sect][k], "n") for c in ccs)))
    gcs = [{e: _D(r["m"], k, r["a"], r["b"]) for e, k in _labels(r["m"], GC_PREFIX, "event").items()} for r in runs]
    for e in sorted(set().union(*(g.keys() for g in gcs))):
        out.append("| 片付け: %s | %s |" % (e, " | ".join("-" if e not in g else _fmt(g[e], "n") for g in gcs)))
    for lab, base in (("meta ops", "juicefs_meta_ops_durations_histogram_seconds"),
                      ("transaction", "juicefs_transaction_durations_histogram_seconds")):
        cs = [(_Dsum(r["m"], base + "_count", r["a"], r["b"]), _Dsum(r["m"], base + "_sum", r["a"], r["b"])) for r in runs]
        if lab == "transaction":
            out.append("| transaction 回数 | " + " | ".join(_fmt(c, "n") for c, _ in cs) + " |")
        out.append("| %s 平均 ms | %s |" % (lab, " | ".join("%.2f" % (1000 * s / c) if c else "-" for c, s in cs)))
    for r, x in zip(runs, resets):
        if x:
            out.append("\n警告: %s の窓の中で mount のし直し（counter のリセット）が %d 回あった。差は足し合わせで出している" % (r["label"], x))
    return "\n".join(out)
