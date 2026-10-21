"""lib/ の共通部品の試験。"""
import logging
import os
import re
import socket
import tempfile
import unittest

from lib import config, metrics_io, secrets
from lib.resp import Resp
from lib.secrets import Secret


class TestSecret(unittest.TestCase):
    def test_secret_never_prints(self):
        s = Secret("x")
        self.assertEqual(str(s), "<redacted>")
        self.assertEqual(repr(s), "<redacted>")
        self.assertEqual(f"{s}", "<redacted>")
        self.assertEqual("%s %r" % (s, s), "<redacted> <redacted>")
        self.assertEqual(s.value, "x")
        with self.assertLogs(level="INFO") as cm:
            logging.getLogger().info("v=%s", s)
        self.assertNotIn("x", cm.output[0].split("v=")[1].replace("<redacted>", ""))

    def test_parse_scripts(self):
        with tempfile.TemporaryDirectory() as d:
            m, r = os.path.join(d, "m.sh"), os.path.join(d, "r.sh")
            with open(m, "w") as f:
                f.write('MOUNT_FROM="redis://:pw@h:1/2${X}"\n')
            with open(r, "w") as f:
                f.write('export RCLONE_SERVE_S3_ACCESS_KEY="ak"\nRCLONE_SERVE_S3_SECRET=sk\n'
                        'export RCLONE_SERVE_S3_PORT=9000\n')
            self.assertEqual(secrets.redis_url_from_mount_script(m).value, "redis://:pw@h:1/2")
            ak, sk, port = secrets.rclone_keys(r)
            self.assertEqual((ak.value, sk.value, port), ("ak", "sk", 9000))
            self.assertEqual(str(ak), "<redacted>")
            with self.assertRaises(ValueError) as cm:
                secrets.rclone_keys(m)
            self.assertNotIn("pw", str(cm.exception))


class TestResp(unittest.TestCase):
    def test_resp_readonly_blocks_write(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        r = Resp(None, None, readonly=True, sock=a)
        with self.assertRaises(PermissionError):
            r.call("SET", "k", "v")
        b.settimeout(0.1)
        with self.assertRaises(TimeoutError):  # 何も送られていない
            b.recv(1)

    def test_readonly_allows_read_and_parses(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        r = Resp(None, None, sock=a)
        b.sendall(b":7\r\n")
        self.assertEqual(r.call("llen", "k"), 7)
        self.assertEqual(b.recv(100), b"*2\r\n$4\r\nllen\r\n$1\r\nk\r\n")


class TestMetrics(unittest.TestCase):
    def test_counter_reset_summed(self):
        s = [(0, 10), (60, 30), (120, 5), (180, 15)]
        self.assertEqual(metrics_io.delta(s, 0, 180), 35)

    def test_window_start_first_after(self):
        s = [(0, 1000), (50, 2000), (100, 3), (160, 13), (220, 33)]
        self.assertEqual(metrics_io.delta(s, 100, 220), 30)

    def test_load_sorts_unsorted_append(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.tsv")
            metrics_io.append_sample(p, 120, {"a": 3})
            metrics_io.append_sample(p, 60, {"a": 1})
            metrics_io.append_sample(p, 180, {"a": 5})
            self.assertEqual([t for t, _ in metrics_io.load(p)["a"]], [60, 120, 180])

    def test_parse_strips_labels(self):
        txt = ('juicefs_uptime{instance="h",mp="/m",vol_name="v"} 5\n'
               'juicefs_blockcache_hits{mp="/m",x="y"} 3\n'
               'juicefs_blockcache_foo_bucket{le="1"} 9\nother 1\n')
        got = metrics_io.parse_exposition(txt, metrics_io.DEFAULT_INCLUDE)
        self.assertEqual(got, {"juicefs_uptime": 5.0, 'juicefs_blockcache_hits{x="y"}': 3.0})


class TestConfig(unittest.TestCase):
    def test_profiles(self):
        p, t = config.load_profile("prod"), config.load_profile("test")
        self.assertEqual((p.inode, p.redis_source, p.bucket), (634040, "mount_script", "buckets"))
        self.assertEqual((t.redis_addr, t.metrics_url, t.bucket),
                         ("127.0.0.1:16379", "http://127.0.0.1:9568/metrics", "juicefs-kaz-test"))
        self.assertTrue(t.mountpoint.endswith("/mnt_juicefs_test"))
        self.assertTrue(config.load_common()["redis_server"].endswith("redis-8.0.5/redis-server"))
        with self.assertRaises(KeyError):
            config.load_profile("nope")


if __name__ == "__main__":
    unittest.main()
