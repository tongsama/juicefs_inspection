"""jfsreplay suite の試験: settings.toml の読み取り、summary.md の列、失敗での停止。
作り物の runner を使い、Drive にも mount にも触れない。"""
import json
import os
import tempfile
import unittest

from replay import run as runmod
from replay import suite

HERE = os.path.dirname(os.path.abspath(__file__))
GiB = 1 << 30


def _fake_result(dirpath, put, comp, status="ok", config=None):
    """作り物の結果ディレクトリ（metrics.tsv、play.json、config.json、run.json）を作る。"""
    os.makedirs(dirpath, exist_ok=True)
    rows = []
    for t, f in ((1000, 0), (2000, 1)):
        rows.append("%d\tiso\tjuicefs_staging_write_bytes\t%d" % (t, int(f * GiB)))
        rows.append("%d\tiso\tjuicefs_compact_size_histogram_bytes_sum\t%d" % (t, int(f * comp * GiB)))
        rows.append('%d\tiso\tjuicefs_object_request_durations_histogram_seconds_count{method="PUT"}\t%d' % (t, f * put))
        rows.append("%d\tiso\tjuicefs_uptime\t%d" % (t, 10 + t))
    with open(os.path.join(dirpath, "metrics.tsv"), "w") as f:
        f.write("\n".join(rows) + "\n")
    with open(os.path.join(dirpath, "play.json"), "w") as f:
        json.dump({"latency": {"write": {"count": 5, "p50_ms": 1.5, "p99_ms": 9.0, "max_ms": 20.0}},
                   "max_lag_ms": 3.0, "errors": []}, f)
    with open(os.path.join(dirpath, "config.json"), "w") as f:
        json.dump(config or {"Compaction": {"trigger": 99}}, f)


class SuiteTest(unittest.TestCase):
    def test_settings_default_six(self):
        got = [(s["name"], s["mode"], s["flags"]) for s in suite.load_all_settings(os.path.join(HERE, "settings.toml"))]
        self.assertEqual([g[0] for g in got], ["default", "trigger30", "trigger50", "trigger200", "block-ratio75", "block"])
        d = dict((g[0], g) for g in got)
        self.assertEqual(d["default"][2], {"kaz-compaction-delta-trigger": 99, "kaz-compaction-delta-block-ratio": 0.5})
        self.assertEqual(d["trigger30"][2], {"kaz-compaction-delta-trigger": 30})
        self.assertEqual(d["trigger50"][2], {"kaz-compaction-delta-trigger": 50})
        self.assertEqual(d["trigger200"][2], {"kaz-compaction-delta-trigger": 200})
        self.assertEqual(d["block-ratio75"][2], {"kaz-compaction-delta-block-ratio": 0.75})
        self.assertEqual(d["block"][1], "block")
        # run.load_settings と同じ形で読める
        self.assertEqual(runmod.load_settings("trigger30", os.path.join(HERE, "settings.toml"))["flags"],
                         {"kaz-compaction-delta-trigger": 30})

    def test_summary_table_columns(self):
        with tempfile.TemporaryDirectory() as root:
            def runner(trace, name, out, age):
                _fake_result(out, put=100 if name == "default" else 40, comp=2.0,
                             config={"Compaction": {"trigger": 99 if name == "default" else 30}})
                return {"status": "ok", "reasons": [], "settle": {"state": "落ち着いた", "seconds": 60}}
            res = suite.run_suite("t.tsv", ["default", "trigger30"], results_root=root, runner=runner, stamp="X")
            with open(res["summary"], encoding="utf-8") as f:
                text = f.read()
            self.assertEqual(res["summary"], os.path.join(root, "X", "summary.md"))
            head = [l for l in text.splitlines() if l.startswith("| 項目")][0]
            self.assertEqual([c.strip() for c in head.strip("|").split("|")], ["項目", "default", "trigger30"])
            self.assertIn("PUT 回数 ÷ staging GiB", text)
            self.assertIn("compaction 出力 GiB（非圧縮） ÷ staging GiB", text)
            self.assertIn("write p99 ms", text)
            self.assertIn("設定 Compaction.trigger", text)
            self.assertIn("100.00", text)
            self.assertIn("40.00", text)
            self.assertIn("出どころ", text)
            self.assertIn("juicefs_staging_write_bytes", text)

    def test_suite_stops_on_failure(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            def runner(trace, name, out, age):
                calls.append(name)
                if name == "b":
                    os.makedirs(out, exist_ok=True)
                    return {"status": "failed", "reasons": ["EIO"], "settle": None}
                _fake_result(out, put=10, comp=1.0)
                return {"status": "ok", "reasons": [], "settle": {"state": "落ち着いた", "seconds": 60}}
            res = suite.run_suite("t.tsv", ["a", "b", "c"], results_root=root, runner=runner, stamp="Y")
            self.assertEqual(calls, ["a", "b"])
            self.assertEqual(res["status"], "failed")
            with open(res["summary"], encoding="utf-8") as f:
                text = f.read()
            self.assertIn("失敗", text)
            self.assertIn("EIO", text)
            self.assertNotIn("| c ", text)

    def test_not_settled_marked_and_continues(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            def runner(trace, name, out, age):
                calls.append(name)
                _fake_result(out, put=10, comp=1.0)
                st = "未収束" if name == "a" else "落ち着いた"
                return {"status": "ok", "reasons": [], "settle": {"state": st, "seconds": 600}}
            res = suite.run_suite("t.tsv", ["a", "b"], repeat=2, results_root=root, runner=runner, stamp="Z")
            self.assertEqual(calls, ["a", "a", "b", "b"])
            with open(res["summary"], encoding="utf-8") as f:
                text = f.read()
            self.assertIn("a#2", text)
            self.assertIn("未収束", text)

    def test_only_filter_and_unknown(self):
        self.assertEqual([s["name"] for s in suite.select(os.path.join(HERE, "settings.toml"), "block,trigger30")],
                         ["trigger30", "block"])
        with self.assertRaises(KeyError):
            suite.select(os.path.join(HERE, "settings.toml"), "nope")


if __name__ == "__main__":
    unittest.main()
