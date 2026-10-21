"""replay の accesslog 解析、trace、record、verify の試験。testdata と一時ファイルだけを使い、本番の mount には触れない。"""
import http.server
import json
import os
import threading
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


class _FakeMetrics:
    """/metrics を返す偽の HTTP server。呼ぶたびに values の次の (count, sum) を返す。"""

    def __init__(self, values):
        self.values = list(values)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                c, s = outer.values.pop(0)
                body = ("juicefs_fuse_written_size_bytes_count %r\njuicefs_fuse_written_size_bytes_sum %r\n" % (c, s)).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/metrics" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


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
    def _setup(self, d, count, size):
        """記録（全 inode の write 100 回・100000 バイト）と、区間の外側を含む metrics を作る。metrics の差は count／size。"""
        base = int(time.time()) - 1000
        t = os.path.join(d, "t.tsv")
        with open(t, "w") as f:
            s = (base + 10) * 1000000
            f.write(trace.format_op(accesslog.Op(s, s + 1000, 1, "write", 0, 1000, 0, 1000)))
        with open(t + ".meta.json", "w") as f:
            json.dump({"inode": INO, "lines_total": 1, "unknown": 0, "keepalive": 0, "all_write_ops": 100, "all_write_bytes": 100000,
                       "started": base + 10, "stopped": base + 20}, f)
        out = os.path.join(d, "m")
        os.mkdir(out)
        for ts, c, b in ((base + 0, 5, 50), (base + 30, 5 + count * 3, 50 + size * 3)):
            # 区間 [10, 20] は全体 [0, 30] の中央 1/3 なので、線形補間で count／size の差になる
            metrics_io.append_sample(os.path.join(out, "metrics.tsv"), ts, {
                "juicefs_fuse_written_size_bytes_count": float(c), "juicefs_fuse_written_size_bytes_sum": float(b)})
        return t, out

    def test_verify_threshold(self):
        # (metrics の回数の差, バイトの差, 期待する終了コード)。記録は 100 回・100000 バイト
        for c, b, code in ((100, 100000, 0), (100, 100500, 0), (100.5, 100000, 0), (102, 100000, 1),
                           (100, 102000, 1), (98, 98000, 1)):
            with tempfile.TemporaryDirectory() as d:
                t, out = self._setup(d, c, b)
                self.assertEqual(cli.main(["verify", t, out]), code, (c, b))

    def _record_with_metrics(self, d, stop_vals):
        """write 2 回（全 inode で 4196 バイト）を記録し、開始・終了の metrics を偽 server から取る。"""
        src = os.path.join(d, "src")
        with open(src, "w") as f:
            f.write(_line("13:00:10.500000", 5, "write (627902,4096,0,9)") + _line("13:00:11.500000", 5, "write (1,100,0,9)"))
        fake = _FakeMetrics([(1000.0, 500000.0), stop_vals])
        try:
            cli.record(os.path.join(d, "o.tsv"), INO, src, exit_at_eof=True, metrics_url=fake.url)
        finally:
            fake.close()
        return os.path.join(d, "o.tsv")

    def test_record_stores_exact_metrics_and_verify_uses_them(self):
        for stop, code in (((1002.0, 504196.0), 0), ((1002.0, 504196.0 + 4200), 1), ((1010.0, 504196.0), 1)):
            with tempfile.TemporaryDirectory() as d:
                t = self._record_with_metrics(d, stop)
                with open(t + ".meta.json") as f:
                    meta = json.load(f)
                self.assertEqual(meta["metrics_start"]["count"], 1000.0)
                self.assertEqual(meta["metrics_stop"]["sum"], stop[1])
                out = os.path.join(d, "m")
                os.mkdir(out)  # metrics.tsv なし: meta の値だけで判定できる
                self.assertEqual(cli.main(["verify", t, out]), code, stop)

    def test_verify_falls_back_to_interpolation_without_meta_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            t, out = self._setup(d, 100, 100000)
            ratio, lines = cli.verify(t, out, 0.01)
            self.assertLess(ratio, 0.01)
            self.assertIn("線形補間", lines[0])

    def test_keepalive_excluded(self):
        """idle 時の "#" 行は、行の総数にも Unknown にも入れず keepalive として数える。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            with open(src, "w") as f:
                f.write("#\n" + _line("13:00:10.500000", 5, "write (627902,4096,0,9)") + "#\n#\n"
                        + _line("13:00:11.500000", 5, "write (1,100,0,9)") + "garbage\n")
            meta = cli.record(os.path.join(d, "o.tsv"), INO, src, exit_at_eof=True)
        self.assertEqual((meta["keepalive"], meta["lines_total"], meta["unknown"]), (3, 3, 1))
        self.assertEqual(meta["unknown_samples"], ["garbage"])
        self.assertEqual((meta["all_write_ops"], meta["all_write_bytes"], meta["ops"]), (2, 4196, 1))

    def test_all_write_excludes_internal_and_failed(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            with open(src, "w") as f:
                f.write(_line("13:00:10.500000", 5, "write (9223372032559808514,10,0,9)")
                        + "2026.10.14 13:00:11.000000 [uid:0,gid:0,pid:5] write (5,10,0,9) - input/output error <0.001000>\n"
                        + _line("13:00:12.000000", 5, "write (5,10,0,9)"))
            meta = cli.record(os.path.join(d, "o.tsv"), INO, src, exit_at_eof=True)
        self.assertEqual((meta["all_write_ops"], meta["all_write_bytes"]), (1, 10))


if __name__ == "__main__":
    unittest.main()
