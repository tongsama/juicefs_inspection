"""jfsreplay suite: settings.toml の設定の組を順に `run` し、設定ごと（--repeat なら回ごと）の列を持つ
1 枚の比較の表 results/<日時>/summary.md を作る（設計書 §5.5）。

- 表の数字は measure.report.compare と同じ行（config.json の違い、staging 1 GiB あたりの PUT と
  compaction の出力など）に、play の待ち時間の行を足したもの。各行の出どころの系列は脚注に書く。
- 失敗（EIO、rate limit、mount の停止、安全の確かめ）はその列を「失敗」として表に出し、そこで止める。
  「未収束」は失敗ではなく、表に印を付けて続ける。"""
import json
import os
import time
import tomllib

from measure import report
from replay import run as runmod

SUITE_DIR = runmod.SUITE_DIR
FAILED, NOT_SETTLED = "失敗", runmod.NOT_SETTLED


def load_all_settings(path=runmod.SETTINGS_PATH):
    """settings.toml の [[settings]] を、書かれた順に {name, mode, flags} の list で返す。"""
    with open(path, "rb") as f:
        items = tomllib.load(f).get("settings", [])
    return [{"name": s["name"], "mode": s.get("mode"), "flags": dict(s.get("flags") or {})} for s in items]


def select(path=runmod.SETTINGS_PATH, only=None):
    """--only（カンマ区切りの名前）で絞った設定の list。順序は settings.toml の順。知らない名前は KeyError。"""
    allset = load_all_settings(path)
    if not only:
        return allset
    want = [n.strip() for n in only.split(",") if n.strip()]
    known = {s["name"] for s in allset}
    bad = [n for n in want if n not in known]
    if bad:
        raise KeyError("未知の設定: %s（%s にある名前: %s）" % (", ".join(bad), path, ", ".join(sorted(known))))
    return [s for s in allset if s["name"] in want]


def make_runner(create_bucket=False, settings_path=None):
    """本物の run を呼ぶ runner（試験の volume、Drive を使う）を作る。create_bucket は testvol.up に渡す
    （volume が無いときだけ Drive に作ってよいという、ユーザーの明示の許可）。"""
    def runner(trace, name, out, age):
        from lib import config
        kw = {"settings_path": settings_path} if settings_path else {}
        return runmod.run(trace, name, out, config.load_profile("test"), age=age,
                          up_kw={"create_bucket": True} if create_bucket else None, **kw)
    return runner


def _read_json(path):
    """JSON を読む。無い・壊れていれば None。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cell_split(line, n):
    """表の 1 行を n+1 個のセルにする。数が合わなければ余りを最後へ寄せ、足りなければ "-" で埋める。"""
    parts = line.strip()[1:-1].split(" | ")
    parts[0] = parts[0].strip()
    if len(parts) > n + 1:
        parts = parts[:n] + [" | ".join(parts[n:])]
    return parts + ["-"] * (n + 1 - len(parts))


def _merge_compare(text, labels, have):
    """report.compare の表（have に含まれる列だけ）を、全ての列（labels）に広げる。
    データの無い列は "-"。表の外の行（警告）は別に返す。戻り値は (表の行の [セルの list], 表の外の行)。"""
    rows, extra = [], []
    for line in text.splitlines():
        if not line.startswith("|"):
            if line.strip():
                extra.append(line)
            continue
        if line.startswith("|---"):
            continue
        cells = _cell_split(line, len(have))
        it = iter(cells[1:])
        rows.append([cells[0]] + [next(it) if l in have else "-" for l in labels])
    return rows, extra


def _play_rows(plays, labels):
    """play.json から待ち時間などの行 [(行の名前, [セル])] と、出どころの脚注を作る。"""
    kinds = sorted({k for p in plays.values() if p for k in (p.get("latency") or {})})
    rows = []
    for k in kinds:
        for key, nm in (("p50_ms", "p50"), ("p99_ms", "p99"), ("max_ms", "max")):
            cells = []
            for l in labels:
                v = ((plays.get(l) or {}).get("latency") or {}).get(k, {}).get(key)
                cells.append("-" if v is None else "%.2f" % v)
            rows.append(("%s %s ms" % (k, nm), cells))
    for name, f in (("予定からの遅れの最大 ms", lambda p: p.get("max_lag_ms")),
                    ("play のエラー数", lambda p: len(p.get("errors") or []))):
        rows.append((name, ["-" if not plays.get(l) or f(plays[l]) is None else
                            ("%.2f" % f(plays[l]) if isinstance(f(plays[l]), float) else str(f(plays[l]))) for l in labels]))
    return rows


def _row(cells):
    return "| " + " | ".join(cells) + " |"


def build_summary(entries, trace, age, stamp):
    """entries（label、setting、dir、status、reasons、settle）から summary.md の本文を作る。"""
    labels = [e["label"] for e in entries]
    plays = {e["label"]: _read_json(os.path.join(e["dir"], "play.json")) for e in entries}
    have, specs = [], []
    for e in entries:
        if e["status"] == FAILED:
            continue
        sp = "%s:%s:first:now" % (e["label"], e["dir"])
        try:
            report.compare([sp])
        except (report.NoData, OSError, ValueError, KeyError) as ex:
            e["status"], e["reasons"] = FAILED, e["reasons"] + ["表を作れない（%s: %s）" % (type(ex).__name__, ex)]
            continue
        have.append(e["label"])
        specs.append(sp)
    body, extra = [], []
    if specs:
        body, extra = _merge_compare(report.compare(specs), labels, have)
    status = []
    for e in entries:
        s = e["status"]
        status.append("失敗" if s == FAILED else "未収束" if s == NOT_SETTLED else "成功")
    head = ["項目"] + labels
    out = ["# suite の結果 %s" % stamp, "",
           "- trace: `%s`、age: %d、設定: %s" % (trace, age, ", ".join(e["setting"] for e in entries)), "",
           _row(head), "|---|" + "---|" * len(labels), _row(["結果"] + status)]
    if body:
        out += [_row(r) for r in body[1:]]  # body[0] は compare のヘッダー行
    pr = _play_rows(plays, labels)
    out += [_row([n] + c) for n, c in pr]
    out.append("")
    if extra:
        out += extra + [""]
    bad = [e for e in entries if e["status"] != "ok" and e["reasons"]]
    if bad:
        out.append("## 失敗・注意")
        for e in bad:
            out.append("- %s（%s）: %s" % (e["label"], "失敗" if e["status"] == FAILED else e["status"], "; ".join(e["reasons"])))
        out.append("")
    out += ["## 出どころ（各行の系列）", "",
            "- 各列の区間は、その回の `<dir>/metrics.tsv` の最初のサンプルから最後のサンプルまで（`report.compare` の `first`〜`now`）。",
            "- 「設定 …」の行: 各回の `config.json`（mount の `.config`）で、列の間で値が違う項目だけ。"]
    for k, d, n, _ in report.KEYS:
        out.append("- %s: `%s` の増分" % (n, k))
    out += ["- compaction 結果／量、片付け、meta ops、transaction: `juicefs_compact_*`、`juicefs_*gc*`、`juicefs_meta_ops_*`、"
            "`juicefs_transaction_*`（`measure/report.py` の compaction_counts／compare を参照）。",
            "- ÷ staging GiB の行: 上の行を `juicefs_staging_write_bytes`（staging 書込 GiB）で割った値。",
            "- 待ち時間の行（write p50 ms など）: 各回の `play.json` の latency（流し直しのクライアント側の測定、nearest-rank）。",
            "- 結果の行: 各回の `run.json`／play の status と落ち着きの判定（未収束 = 上限まで待っても staging／uploading／compaction が止まらなかった）。",
            "", "## 各列のディレクトリ", ""]
    out += ["- %s: `%s`" % (e["label"], e["dir"]) for e in entries]
    return "\n".join(out) + "\n"


def run_suite(trace, names, repeat=1, age=0, results_root=None, runner=None, stamp=None, log=print):
    """names の設定を順に（各 repeat 回）流す。失敗で止め、summary.md を書く。
    戻り値は {status（ok／failed）、summary（summary.md の path）、dir、entries}。"""
    results_root = results_root or os.path.join(SUITE_DIR, "results")
    runner = runner or make_runner()
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(results_root, stamp)
    os.makedirs(base, exist_ok=True)
    entries, stop = [], False
    for name in names:
        for i in range(1, repeat + 1):
            label = name if repeat == 1 or i == 1 else "%s#%d" % (name, i)
            out = os.path.join(base, label.replace("#", "-"))
            log("== %s を流す ==" % label)
            try:
                res = runner(trace, name, out, age)
            except Exception as e:  # 安全の確かめ・続けられないエラーも、その列の失敗として表に出して止める
                res = {"status": "failed", "reasons": ["%s: %s" % (type(e).__name__, e)], "settle": None}
            settle = res.get("settle") or {}
            st = FAILED if res["status"] != "ok" else NOT_SETTLED if settle.get("state") == NOT_SETTLED else "ok"
            entries.append({"label": label, "setting": name, "dir": out, "status": st,
                            "reasons": list(res.get("reasons") or []), "settle": settle})
            if st == FAILED:
                stop = True
                break
        if stop:
            break
    summary = os.path.join(base, "summary.md")
    with open(summary, "w", encoding="utf-8") as f:
        f.write(build_summary(entries, trace, age, stamp))
    return {"status": "failed" if stop else "ok", "summary": summary, "dir": base, "entries": entries}
