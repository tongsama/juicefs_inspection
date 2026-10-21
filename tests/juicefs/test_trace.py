"""replay の accesslog 解析、trace、record、verify の試験。testdata と一時ファイルだけを使い、本番の mount には触れない。"""
import json
import os
import tempfile
import time
import unittest

from lib import metrics_io
from replay import accesslog, cli, trace

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "testdata", "accesslog-sample.txt")
INO = 627902


def _line(ts, pid, body, dur="0.001000"):
    return "2026.10.14 %s [uid:0,gid:0,pid:%d] %s - OK <%s>\n" % (ts, pid, body, dur)


class ParseTest(unittest.TestCase):
    def test_parse_write_read_fallocate_setattr(self):
        w = accesslog.parse_line(_line("13:00:10.500000", 5, "write (627902,4096,8192,77)", "0.002000"), INO)
        self.assertEqual((w.kind, w.size, w.off, w.pid, w.dur_us), ("write", 4096, 8192, 5, 2000))
        self.assertEqual(w.end_us - w.start_us, 2000)
        self.assertEqual(w.end_us % 1000000, 500000)
        r = accesslog.parse_line(_line("13:00:10.500000", 5, "read (627902,512,1024,77): 512"), INO)
        self.assertEqual((r.kind, r.size, r.off), ("read", 512, 1024))
        f = accesslog.parse_line(_line("13:00:10.500000", 5, "fallocate (627902,16,4096,196608)"), INO)
        self.assertEqual((f.kind, f.mode, f.off, f.size), ("fallocate", 16, 4096, 196608))
        t = accesslog.parse_line(_line("13:00:10.500000", 5, "setattr (627902[3],0x8,[size=1048576]):(627902,[x])"), INO)
        self.assertEqual((t.kind, t.size, t.mode), ("truncate", 1048576, 8))
        self.assertIsNone(accesslog.parse_line(_line("13:00:10.500000", 5, "setattr (627902[0],0x6,[uid=1,gid=2]): (627902,[x])"), INO))
        fs = accesslog.parse_line(_line("13:00:10.500000", 5, "fsync (627902,1)"), INO)
        self.assertEqual(fs.kind, "fsync")
        fl = accesslog.parse_line(_line("13:00:10.500000", 5, "flush (627902,77,54BCCBE3B2BDA064)"), INO)
        self.assertEqual(fl.kind, "flush")

    def test_parse_other_inode_and_methods_ignored(self):
        self.assertIsNone(accesslog.parse_line(_line("13:00:10.500000", 5, "write (1,4096,0,7)"), INO))
        self.assertIsNone(accesslog.parse_line(_line("13:00:10.500000", 5, "getattr (627902)"), INO))

    def test_parse_read_both_suffixes(self):
        for suf in (": 4096", ": (4096)"):
            r = accesslog.parse_line(_line("13:00:10.500000", 5, "read (627902,4096,0,9)" + suf), INO)
            self.assertEqual(r.kind, "read", suf)

    def test_parse_unknown_lines_counted(self):
        for bad in ("garbage line\n", _line("13:00:10.500000", 5, "write (627902,abc)"),
                    _line("13:00:10.500000", 5, "read (627902,1,2,3)")):
            self.assertIsInstance(accesslog.parse_line(bad, INO), accesslog.Unknown, bad)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.tsv")
            meta = cli.record(out, INO, SAMPLE, exit_at_eof=True)
            with open(out + ".meta.json") as f:
                disk = json.load(f)
            self.assertEqual(disk["unknown"], 2)  # 手で足した不正な write と、accesslog でない行
            self.assertEqual(meta["lines_total"], 209)
            self.assertEqual(len(trace.load_unsorted(out)), meta["ops"])
            kinds = {o.kind for o in trace.load(out)}
            self.assertTrue({"write", "read", "fallocate", "truncate", "fsync"} <= kinds)

    def test_mark_appends(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.tsv")
            cli.mark(out, "start", now=100.0)
            cli.mark(out, "end", now=200.0)
            with open(out + ".marks.tsv") as f:
                rows = [l.split("\t") for l in f.read().splitlines()]
            self.assertEqual([(r[0], r[2]) for r in rows], [("100000000", "start"), ("200000000", "end")])


class TraceTest(unittest.TestCase):
    def test_trace_sorted_by_start_keeps_pid_order(self):
        """終了時刻の順で記録された行を開始時刻で並べ直し、pid ごとの記録の順は変えない。"""
        mk = lambda s, pid, off: accesslog.Op(s, s + 10, pid, "write", off, 1, 0, 10)
        recorded = [mk(300, 1, 0), mk(100, 2, 1), mk(200, 1, 2),  # pid 1 は記録の順が 300, 200
                    mk(50, 3, 3), mk(400, 2, 4)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.tsv")
            with open(p, "w") as f:
                f.writelines(trace.format_op(o) for o in recorded)
            got = trace.load(p)
        self.assertEqual(len(got), 5)
        self.assertEqual([o.start_us for o in got if o.pid != 1], [50, 100, 400])
        self.assertEqual([o.off for o in got if o.pid == 1], [0, 2])  # 記録の順
        pos = [i for i, o in enumerate(got) if o.pid == 1]
        self.assertEqual(pos, [2, 3])  # 開始の順で並べた時に pid 1 が占める位置
        self.assertEqual(sorted(o.start_us for o in got), [50, 100, 200, 300, 400])


class VerifyTest(unittest.TestCase):
    def _setup(self, d, metric_bytes):
        base = int(time.time()) - 1000
        with open(os.path.join(d, "t.tsv"), "w") as f:
            for i in range(10):  # 10 回 × 1000 バイト、1 秒おき
                s = (base + 10 + i) * 1000000
                f.write(trace.format_op(accesslog.Op(s, s + 1000, 1, "write", i * 1000, 1000, 0, 1000)))
        out = os.path.join(d, "m")
        os.mkdir(out)
        metrics_io.append_sample(os.path.join(out, "metrics.tsv"), base + 5, {"juicefs_staging_write_bytes": 500.0})
        metrics_io.append_sample(os.path.join(out, "metrics.tsv"), base + 10, {"juicefs_staging_write_bytes": 500.0})
        metrics_io.append_sample(os.path.join(out, "metrics.tsv"), base + 20, {"juicefs_staging_write_bytes": 500.0 + metric_bytes})
        return os.path.join(d, "t.tsv"), out

    def test_verify_threshold(self):
        for metric, code in ((10050, 0), (9800, 1)):  # 差 0.5% と 2%（記録は 10000 バイト）
            with tempfile.TemporaryDirectory() as d:
                t, out = self._setup(d, metric)
                rc = cli.main(["verify", t, out])
                self.assertEqual(rc, code, metric)


if __name__ == "__main__":
    unittest.main()
