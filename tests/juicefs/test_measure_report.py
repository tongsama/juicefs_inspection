"""progress／compare／boot の表のテスト。作り物の記録と、前回の記録（metrics.tsv.gz）を使う。"""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest

os.environ["TZ"] = "Asia/Tokyo"
time.tzset()

from lib import metrics_io  # noqa: E402
from measure import boot, cli, report  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OLD = os.path.join(REPO, "docs/inspection/cache_churn/2026-10-19")
G = 1 << 30
T0 = int(time.mktime(time.strptime("2026-10-21 10:00:00", "%Y-%m-%d %H:%M:%S")))
EV = 'juicefs_kaz_compaction_block_events_total{event="%s"}'
AM = 'juicefs_kaz_compaction_block_amount_total{kind="%s"}'
OBJ_C = 'juicefs_object_request_durations_histogram_seconds_count{method="%s"}'
OBJ_S = 'juicefs_object_request_durations_histogram_seconds_sum{method="%s"}'


def sample(i, f):
    """i 番目（0 または 1、10 分あけて 2 点）の標本。f は 0 か 1 の係数で、counter は f 倍の量になる。"""
    s = {
        "juicefs_blockcache_write_bytes": 2 * G * f, "juicefs_staging_write_bytes": 1 * G * f,
        "juicefs_compact_size_histogram_bytes_sum": 0.56 * G * f, "juicefs_compact_size_histogram_bytes_count": 100 * f,
        "juicefs_blockcache_miss_bytes": 0.5 * G * f, "juicefs_blockcache_hit_bytes": 4.5 * G * f,
        OBJ_C % "GET": 200 * f, OBJ_S % "GET": 50 * f, 'juicefs_object_request_data_bytes{method="GET"}': 1 * G * f,
        OBJ_C % "PUT": 3000 * f, OBJ_S % "PUT": 60 * f, 'juicefs_object_request_data_bytes{method="PUT"}': 0.8 * G * f,
        OBJ_C % "DELETE": 500 * f, OBJ_S % "DELETE": 5 * f,
        'juicefs_kaz_cache_2q_events_total{event="promote"}': 10 * f,
        'juicefs_kaz_cache_2q_events_total{event="ghost_hit"}': 7 * f,
        'juicefs_kaz_cache_2q_events_total{event="ghost_hit_demoted"}': 3 * f,
        'juicefs_kaz_cache_2q_bytes{segment="protected"}': 16e9, 'juicefs_kaz_cache_2q_ghost_bytes': 28e9,
        EV % "delta_tier": 4 * f, EV % "delta_merge": 2 * f, EV % "partial": 6 * f,
        AM % "delta_blocks_whole": 11 * f, AM % "delta_fold_blocks": 5 * f, AM % "delta_bytes": 0.25 * G * f,
        AM % "delta_merge_bytes": 0.1 * G * f, AM % "removed": 42 * f,
        'juicefs_compaction_gc_events_total{event="local_retire_success"}': 123 * f,
        "juicefs_meta_ops_durations_histogram_seconds_count": 1000 * f,
        "juicefs_meta_ops_durations_histogram_seconds_sum": 2 * f,
        "juicefs_transaction_durations_histogram_seconds_count": 400 * f,
        "juicefs_transaction_durations_histogram_seconds_sum": 4 * f,
        "juicefs_uptime": 100 + 600 * i,
    }
    return s


def make_dir(d, cfg=None):
    """作り物の出力ディレクトリ（metrics.tsv、llen.tsv、任意で config.json）を作る。"""
    for i, f in enumerate((0, 1)):
        metrics_io.append_sample(os.path.join(d, "metrics.tsv"), T0 + 600 * i, sample(i, f))
    with open(os.path.join(d, "llen.tsv"), "w") as fh:
        fh.write("%d\t%s\t4761\t54825\t11.5\t8\t18\t691\t2\n" % (T0 + 600, "x"))
    if cfg is not None:
        with open(os.path.join(d, "config.json"), "w") as fh:
            json.dump(cfg, fh)


def numbers_block(text):
    """[流入] から [期間の終わりの状態] の末尾までの本文行（脚注・空行を除く）。"""
    out, on = [], False
    for l in text.splitlines():
        if l.startswith("[流入]"):
            on = True
        if l.startswith("[compaction]"):
            on = False
        if on and l.strip() and not l.startswith("  ※"):
            out.append(l)
    return out


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        make_dir(self.d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_progress_sections(self):
        out = report.progress(self.d, str(T0), str(T0 + 600))
        for h in ("[流入]", "[読み込み]", "[2Q]", "[期間の終わりの状態]", "[compaction]", "[object storage]",
                  "[片付け]", "[メタデータ]", "[record の数]"):
            self.assertIn(h, out)
        self.assertIn("staging 1.00 GiB に対し compaction の出力 0.56 GiB（staging 1 GiB あたり 0.56）", out)
        self.assertIn("delta_tier 4", out)
        self.assertIn("丸ごと 11", out)
        self.assertIn("外した record 42", out)
        self.assertIn("local_retire_success 123", out)
        self.assertIn("PUT 3000 回、0.80 GiB、平均 20.0 ms", out)
        self.assertIn("staging 1 GiB あたり: PUT 3000.0 回", out)
        self.assertIn("meta ops 1000 回、平均 2.00 ms", out)
        self.assertIn("transaction 400 回、平均 10.00 ms", out)
        self.assertIn("54825", out)
        self.assertIn("※ 系列:", out)
        self.assertNotIn("fallback_block", out)  # 系列が無い event は出さない

    def test_progress_default_two_periods(self):
        out = report.progress(self.d)
        self.assertIn("開始から今まで", out)
        self.assertIn("直近 1 時間", out)

    def test_progress_no_data(self):
        with self.assertRaises(report.NoData):
            report.progress(self.d, str(T0 - 5000), str(T0 - 4000))

    def test_progress_matches_old_script(self):
        script = os.path.join(OLD, "progress_kaz.sh")
        old = subprocess.run(["bash", script, "-f", os.path.join(self.d, "metrics.tsv"), "@%d" % T0, "@%d" % (T0 + 600)],
                             capture_output=True, text=True, env=dict(os.environ, TZ="Asia/Tokyo"))
        self.assertEqual(old.returncode, 0, old.stderr)
        new = report.progress(self.d, str(T0), str(T0 + 600))
        want = [l for l in old.stdout.splitlines() if l.strip()]
        want = want[want.index("[流入]"):]
        self.assertEqual(numbers_block(new), want)
        self.assertEqual(new.splitlines()[0], old.stdout.splitlines()[0])  # 期間の行


    def test_progress_counter_reset_matches_old_script(self):
        """3 点目で counter が戻った（mount のし直し）入力でも、旧スクリプトと共通の項目が行単位で一致する。"""
        os.remove(os.path.join(self.d, "metrics.tsv"))
        for i, f in enumerate((0, 1, 0.5)):
            metrics_io.append_sample(os.path.join(self.d, "metrics.tsv"), T0 + 600 * i, sample(i, f))
        a, b = T0, T0 + 1200
        old = subprocess.run(["bash", os.path.join(OLD, "progress_kaz.sh"), "-f", os.path.join(self.d, "metrics.tsv"),
                              "@%d" % a, "@%d" % b], capture_output=True, text=True, env=dict(os.environ, TZ="Asia/Tokyo"))
        self.assertEqual(old.returncode, 0, old.stderr)
        new = report.progress(self.d, str(a), str(b))
        want = [l for l in old.stdout.splitlines() if l.strip()]
        want = want[want.index("[流入]"):]
        self.assertEqual(numbers_block(new), want)
        # 戻った分は足し合わせ: staging は 1 + 0.5 = 1.5 GiB
        self.assertIn("書き込み（staging）    1.5 GiB", new)
        self.assertEqual(new.splitlines()[0], old.stdout.splitlines()[0])


class CompareTest(unittest.TestCase):
    def test_compare_reads_old_gz(self):
        gz = os.path.join(OLD, "metrics.tsv.gz")
        out = report.compare(["old:%s:2026-10-19 21:38:2026-10-20 06:05" % gz])
        row = lambda n: re.search(r"\| %s \| ([^|]+) \|" % re.escape(n), out).group(1).strip()
        self.assertEqual(row("staging 書込 GiB"), "5.00")
        self.assertEqual(row("compaction 出力 GiB（非圧縮）"), "32.65")
        self.assertEqual(row("PUT GiB（圧縮後）"), "7.69")
        self.assertEqual(row("PUT 回数"), "138,163")
        self.assertEqual(row("DELETE 回数"), "193,226")

    def test_compare_config_diff_at_top(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            make_dir(a, {"Format": {"Name": "v", "BlockSize": 4096}, "Meta": {"Retries": 10}})
            make_dir(b, {"Format": {"Name": "v", "BlockSize": 8192}, "Meta": {"Retries": 10}})
            out = report.compare(["A:%s:first:now" % a, "B:%s:first:now" % b]).splitlines()
        self.assertIn("設定 Format.BlockSize", out[2])
        self.assertTrue(out[2].endswith("| 4096 | 8192 |"))
        self.assertFalse(any("Retries" in l or "Format.Name" in l for l in out))
        self.assertTrue(out[3].startswith("| 窓の長さ"))

    def test_compare_rows_for_new_items(self):
        with tempfile.TemporaryDirectory() as a:
            make_dir(a)
            out = report.compare(["A:%s:first:now" % a])
        for s in ("compaction 結果: delta_tier | 4 |", "片付け: local_retire_success | 123 |", "meta ops 平均 ms | 2.00 |",
                  "transaction 回数 | 400 |", "compaction 量: removed | 42 |"):
            self.assertIn(s, out)

    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            report.compare(["nonsense"])


class BootTest(unittest.TestCase):
    def test_boot_marks_minutes(self):
        t = lambda hms: int(time.mktime(time.strptime("2026-10-21 " + hms, "%Y-%m-%d %H:%M:%S")))
        with tempfile.TemporaryDirectory() as d:
            make_dir(d)
            hdr = "key\tatime\tctime\tsize\n"
            with open(os.path.join(d, "keys-snapshot-%d.tsv" % (T0 - 1000)), "w") as f:  # 古い snapshot
                f.write(hdr + "70_0_4096\t1\t1\t4100\n")
            with open(os.path.join(d, "keys-snapshot-%d.tsv" % (T0 - 10)), "w") as f:  # 直前の snapshot（最大 id 100）
                f.write(hdr + "100_0_4096\t1\t1\t4100\n")
            with open(os.path.join(d, "keys-events.tsv"), "w") as f:
                f.write("%d\tdel\t50_0_4096\t5\t5\t%d\n" % (T0 + 50, G))
                f.write("%d\tadd\t50_0_4096\t5\t5\t%d\n" % (T0 + 100, G))        # 消えた後に補充: evicted 1 GiB
                f.write("%d\tadd\t70_0_4096\t5\t5\t%d\n" % (T0 + 200, 2 * G))    # 古い snapshot にだけある: before-tracking 2 GiB
                f.write("%d\tadd\t60_0_4096\t5\t5\t%d\n" % (T0 + 250, G // 2))   # 旧スクリプトの規則では add は seen: before-tracking 0.5 GiB
                f.write("%d\tadd\t100_0_4096\t5\t5\t%d\n" % (T0 + 260, 7 * G))   # 直前の snapshot にある: 除外
                f.write("%d\tadd\t900_0_4096\t5\t5\t%d\n" % (T0 + 300, 9 * G))   # 最大 id より新しい: 除外
                f.write("broken line\n")
            marks = {"起動開始": "10:00:00", "ログイン画面": "10:01:18", "ログイン完了": "10:01:48", "アプリ": "10:04:30"}
            out = boot.analyze(d, "2026-10-21 10:00:00", "2026-10-21 10:10:00", marks)
        self.assertIn("| 起動開始 | 10:00:00 | 0.0 |", out)
        self.assertIn("| ログイン画面 | 10:01:18 | 1.3 |", out)
        self.assertIn("| ログイン完了 | 10:01:48 | 1.8 |", out)
        self.assertIn("| アプリ | 10:04:30 | 4.5 |", out)
        self.assertIn("最大 slice id: 100", out)
        self.assertIn("GET の補充: 3 ファイル、3.50 GiB", out)
        self.assertIn("  evicted                    1.00 GiB   28.6%", out)
        self.assertIn("  evicted-before-tracking    2.50 GiB   71.4%", out)
        self.assertIn("  never-seen                 0.00 GiB    0.0%", out)
        self.assertIn("{'%s': 1.0}" % time.strftime("%H:00", time.localtime(T0 + 50)), out)  # 消された時間帯
        # 補充の分ごと: T0+100 と T0+200 と T0+250 は 10:01 と 10:03 と 10:04
        self.assertIn("10:01   1.00   0.00   0.00", out)
        self.assertIn("10:03   0.00   2.00   0.00", out)
        self.assertIn("10:04   0.00   0.50   0.00", out)

    def test_boot_without_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            make_dir(d)
            out = boot.analyze(d, "first", "now")
        self.assertIn("分類", out)
        self.assertIn("出せない", out)


class CliTest(unittest.TestCase):
    def test_cli_progress_and_missing(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            make_dir(d)
            self.assertEqual(cli.main(["progress", d]), 0)
            self.assertEqual(cli.main(["compare", "A:%s:first:now" % d]), 0)
            self.assertEqual(cli.main(["progress", d, "2000-01-01 00:00", "2000-01-02 00:00"]), 1)


if __name__ == "__main__":
    unittest.main()
