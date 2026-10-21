"""計測の CLI（jfsmeasure の start／check／stop／capture）と常駐の試験。
本番には繋がない。偽の metrics の HTTP サーバー、偽の Redis（読み取りの命令だけ答える）、一時ディレクトリを使う。"""
import http.server
import json
import os
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JFSMEASURE = os.path.join(HERE, "jfsmeasure")

METRICS_TEXT = (
    'juicefs_uptime{instance="h",mp="/m"} 12\n'
    'juicefs_blockcache_hits{mp="/m"} 5\n'
    'juicefs_blockcache_miss{mp="/m"} 2\n'
    'juicefs_unrelated 1\n')

SLICE = struct.pack(">IQIII", 0, 100, 4 << 20, 0, 4 << 20)  # slice id 100 が chunk の全体を占める


class _MetricsHandler(http.server.BaseHTTPRequestHandler):
    """偽の /metrics。"""

    def do_GET(self):
        body = METRICS_TEXT.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _RedisHandler(socketserver.StreamRequestHandler):
    """偽の Redis。受けた命令名を server.seen に残し、読み取りの命令に決め打ちで答える。"""

    def _read_cmd(self):
        line = self.rfile.readline()
        if not line:
            return None
        n = int(line[1:])
        cmd = []
        for _ in range(n):
            ln = int(self.rfile.readline()[1:])
            cmd.append(self.rfile.read(ln + 2)[:-2])
        return cmd

    def handle(self):
        while True:
            cmd = self._read_cmd()
            if cmd is None:
                return
            name = cmd[0].decode().upper()
            self.server.seen.append(name)
            if name in ("AUTH", "SELECT"):
                out = b"+OK\r\n"
            elif name == "SCAN":
                out = b"*2\r\n$1\r\n0\r\n*1\r\n$4\r\nc7_0\r\n"
            elif name == "LRANGE":
                out = b"*1\r\n$24\r\n" + SLICE + b"\r\n"
            elif name == "LLEN":
                out = b":3\r\n"
            elif name == "DBSIZE":
                out = b":1\r\n"
            else:
                out = b"-ERR unknown\r\n"
            self.wfile.write(out)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MeasureTestBase(unittest.TestCase):
    """偽の metrics・Redis・キャッシュ・mountpoint・プロファイルを用意する共通の土台。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.msrv = _Server(("127.0.0.1", 0), _MetricsHandler)
        self.rsrv = _Server(("127.0.0.1", 0), _RedisHandler)
        self.rsrv.seen = []
        for s in (self.msrv, self.rsrv):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        # キャッシュ: ブロックファイルと kaz-2q の管理ファイル
        self.cache = os.path.join(t, "cache")
        os.makedirs(os.path.join(self.cache, "raw", "chunks", "0", "0"))
        with open(os.path.join(self.cache, "raw", "chunks", "0", "0", "100_0_4194304"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(self.cache, "kaz-cache-state.v1"), "wb") as f:
            f.write(b"STATE")
        # mount の .config（接続先・鍵らしい項目を伏せるかの確認用）
        self.mp = os.path.join(t, "mnt")
        os.makedirs(self.mp)
        with open(os.path.join(self.mp, ".config"), "w") as f:
            json.dump({"Meta": "redis://:hunter2@db.example:6379/1", "SecretKey": "s3cr3t", "Bucket": "b1",
                       "BlockSize": 4096, "CacheSize": 1024, "Format": {"Name": "vol", "AccessKey": "AKIA",
                                                                      "Storage": "s3"}}, f)
        # 偽のバイナリ（version だけ答える）
        self.binary = os.path.join(t, "fakejfs")
        with open(self.binary, "w") as f:
            f.write('#!/bin/sh\necho "juicefs version 1.4.1-fake"\n')
        os.chmod(self.binary, os.stat(self.binary).st_mode | stat.S_IXUSR)
        with open(os.path.join(t, "vm-io-1.log"), "w") as f:
            f.write("boot\nkaz-2q enabled\nkaz meta redis://u:pw@db.example:6379/1\nunrelated\n")
        self.profiles = os.path.join(t, "profiles.toml")
        self.write_profiles(60, 600, 300)
        self.out = os.path.join(t, "out")
        self.addCleanup(self.stop_quietly)

    def write_profiles(self, im, il, ic):
        """試験用のプロファイル（間隔を引数で変える）。"""
        t = self.tmp.name
        with open(self.profiles, "w") as f:
            f.write('''[test]
metrics_url = "http://127.0.0.1:%d/metrics"
inode = 7
cache_dir = "%s"
mountpoint = "%s"
redis_source = "test"
redis_addr = "127.0.0.1:%d"
log_glob = "%s/vm-io-*.log"
bucket = "x"

[common]
redis_server = "/nonexistent"
test_binary = "%s"
rtt_proxy_addr = "127.0.0.1:1"
rtt_one_way_ms = 5
seed_limit_gib = 32
settle_seconds = 60
settle_max_seconds = 600
loss_tolerance = 0.01
interval_metrics_s = %d
interval_llen_s = %d
interval_cache_churn_s = %d
''' % (self.msrv.server_address[1], self.cache, self.mp, self.rsrv.server_address[1], t, self.binary,
            im, il, ic))

    def jfs(self, *args):
        """jfsmeasure を子プロセスで実行して (rc, 標準出力+標準エラー) を返す。"""
        p = subprocess.run([sys.executable, JFSMEASURE, "--profiles", self.profiles] + list(args),
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def stop_quietly(self):
        if os.path.exists(os.path.join(self.out, "run.json")):
            self.jfs("stop", self.out)

    def run_json(self):
        with open(os.path.join(self.out, "run.json"), encoding="utf-8") as f:
            return json.load(f)

    def wait_for(self, path, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
            time.sleep(0.05)
        return False


def alive(pid):
    """pid が生きている（ゾンビは除く）か。"""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class TestStartStop(MeasureTestBase):
    def test_start_writes_run_json_and_pids(self):
        rc, out = self.jfs("start", "test", self.out)
        self.assertEqual(rc, 0, out)
        r = self.run_json()
        self.assertEqual(r["profile"], "test")
        self.assertIsInstance(r["started"], int)
        self.assertEqual(set(r["pids"]), {"metrics", "llen", "keys"})
        self.assertEqual(set(r["intervals"]), {"metrics", "llen", "keys"})
        self.assertEqual(r["binary"]["path"], self.binary)
        self.assertIn("1.4.1-fake", r["binary"]["version"])
        self.assertEqual(len(r["binary"]["sha256"]), 64)
        self.assertEqual(r["kaz_log_lines"], ["kaz-2q enabled"])
        for p in r["pids"].values():
            self.assertTrue(alive(p))
        # 最初の記録が出る
        self.assertTrue(self.wait_for(os.path.join(self.out, "metrics.tsv")))
        self.assertTrue(self.wait_for(os.path.join(self.out, "llen.tsv")))
        with open(os.path.join(self.out, "metrics.tsv")) as f:
            rows = [l.split("\t") for l in f.read().splitlines()]
        names = {x[2] for x in rows}
        self.assertEqual(names, {"juicefs_uptime", "juicefs_blockcache_hits", "juicefs_blockcache_miss"})
        self.assertTrue(all(len(x) == 4 for x in rows))
        with open(os.path.join(self.out, "llen.tsv")) as f:
            cols = f.readline().rstrip("\n").split("\t")
        self.assertEqual(cols[2:], ["4800", "14400", "3.0", "3", "3", "3", "0"])  # 偽の Redis は全 key が LLEN 3
        self.assertTrue(any(n.startswith("keys-snapshot-") for n in os.listdir(self.out)))
        self.assertTrue(os.path.exists(os.path.join(self.out, "keys-events.tsv")))
        # 開始時の材料
        self.assertTrue(any(n.startswith("metrics-start-") for n in os.listdir(self.out)))
        self.assertTrue(any(n.startswith("breakdown-start-") for n in os.listdir(self.out)))
        # 偽の Redis には読み取りの命令だけが届く
        self.assertLessEqual(set(self.rsrv.seen), {"AUTH", "SELECT", "SCAN", "LRANGE", "LLEN", "DBSIZE"})
        # 全部止まる
        pids = list(r["pids"].values())
        rc, out = self.jfs("stop", self.out)
        self.assertEqual(rc, 0, out)
        for p in pids:
            self.assertFalse(alive(p))

    def test_start_copies_config_redacted(self):
        rc, out = self.jfs("start", "test", self.out)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.out, "config.json"), encoding="utf-8") as f:
            raw = f.read()
        c = json.loads(raw)
        self.assertEqual(c["Meta"], "<redacted>")        # 値に :// を含む
        self.assertEqual(c["SecretKey"], "<redacted>")   # 名前に secret／key
        self.assertEqual(c["Bucket"], "<redacted>")      # 名前に bucket
        self.assertEqual(c["Format"]["AccessKey"], "<redacted>")  # 入れ子も
        self.assertEqual(c["BlockSize"], 4096)           # 残す値
        self.assertEqual(c["CacheSize"], 1024)
        self.assertEqual(c["Format"]["Name"], "vol")
        self.assertEqual(c["Format"]["Storage"], "s3")
        for secret in ("hunter2", "s3cr3t", "AKIA", "db.example"):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, json.dumps(self.run_json()))

    def test_start_refuses_existing_run(self):
        rc, out = self.jfs("start", "test", self.out)
        self.assertEqual(rc, 0, out)
        before = self.run_json()
        rc, out = self.jfs("start", "test", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("動いている", out)
        self.assertEqual(self.run_json()["pids"], before["pids"])
        for p in before["pids"].values():
            self.assertTrue(alive(p))

    def test_start_after_stop_is_allowed(self):
        self.assertEqual(self.jfs("start", "test", self.out)[0], 0)
        self.assertEqual(self.jfs("stop", self.out)[0], 0)
        rc, out = self.jfs("start", "test", self.out)
        self.assertEqual(rc, 0, out)


class TestProdOutputRefused(MeasureTestBase):
    """start と capture は、出力ディレクトリが本番の mountpoint・キャッシュ（偽の一時の path）と重なれば止まる。"""

    def setUp(self):
        super().setUp()
        t = self.tmp.name
        self.prod_mp, self.prod_cache = os.path.join(t, "prodmnt"), os.path.join(t, "prodcache")
        for d in (self.prod_mp, self.prod_cache):
            os.makedirs(d)
        with open(self.profiles, "a") as f:
            f.write('''
[prod]
metrics_url = "http://127.0.0.1:1/metrics"
inode = 1
cache_dir = "%s"
mountpoint = "%s"
redis_source = "mount_script"
redis_addr = ""
log_glob = "%s/none-*.log"
bucket = "buckets"
''' % (self.prod_cache, self.prod_mp, t))

    def test_start_refuses_under_prod_cache_and_mount(self):
        for out in (os.path.join(self.prod_cache, "out"), self.prod_mp, os.path.join(self.prod_mp, "a", "b")):
            rc, msg = self.jfs("start", "test", out)
            self.assertEqual(rc, 3, msg)
            self.assertIn("本番", msg)
            self.assertFalse(os.path.exists(os.path.join(out, "run.json")))
        self.assertEqual(os.listdir(self.prod_cache), [])

    def test_symlink_into_prod_is_refused(self):
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.prod_cache, link)
        rc, msg = self.jfs("capture", os.path.join(link, "o"), "x", "--profile", "test")
        self.assertEqual(rc, 3, msg)
        self.assertEqual(os.listdir(self.prod_cache), [])
        link2 = os.path.join(self.tmp.name, "link2")
        os.symlink(self.prod_mp, link2)
        rc, msg = self.jfs("start", "test", link2)
        self.assertEqual(rc, 3, msg)
        self.assertEqual(os.listdir(self.prod_mp), [])


class TestCheck(MeasureTestBase):
    def test_check_ok_then_reports_missing_sampler(self):
        self.assertEqual(self.jfs("start", "test", self.out)[0], 0)
        self.assertTrue(self.wait_for(os.path.join(self.out, "metrics.tsv")))
        self.assertTrue(self.wait_for(os.path.join(self.out, "llen.tsv")))
        rc, out = self.jfs("check", self.out)
        self.assertEqual(rc, 0, out)
        pid = self.run_json()["pids"]["llen"]
        os.kill(pid, 9)
        end = time.time() + 5
        while alive(pid) and time.time() < end:
            time.sleep(0.05)
        rc, out = self.jfs("check", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("llen", out)
        self.assertIn("止まっている", out)

    def test_check_stale_record(self):
        self.assertEqual(self.jfs("start", "test", self.out)[0], 0)
        self.assertTrue(self.wait_for(os.path.join(self.out, "metrics.tsv")))
        old = time.time() - 4 * 60  # 間隔 60 秒の 3 倍より古い
        os.utime(os.path.join(self.out, "metrics.tsv"), (old, old))
        rc, out = self.jfs("check", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("metrics.tsv", out)
        self.assertIn("古い", out)

    def _start_and_wait(self):
        self.assertEqual(self.jfs("start", "test", self.out)[0], 0)
        self.assertTrue(self.wait_for(os.path.join(self.out, "metrics.tsv")))
        self.assertTrue(self.wait_for(os.path.join(self.out, "llen.tsv")))

    def test_check_latest_scrape_error(self):
        self._start_and_wait()
        now = int(time.time())
        with open(os.path.join(self.out, "metrics.tsv"), "a") as f:
            f.write("%d\tiso\t_scrape_error\t1\n" % (now + 1))
        rc, out = self.jfs("check", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("_scrape_error", out)

    def test_check_latest_llen_error(self):
        self._start_and_wait()
        with open(os.path.join(self.out, "llen.tsv"), "a") as f:
            f.write("%d\tiso\terror\tConnectionError\n" % int(time.time()))
        rc, out = self.jfs("check", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("llen.tsv の最新の記録が取得の失敗", out)

    def test_check_missing_run_json(self):
        os.makedirs(self.out)
        rc, out = self.jfs("check", self.out)
        self.assertNotEqual(rc, 0)
        self.assertIn("run.json", out)


class TestCapture(MeasureTestBase):
    def test_capture_files(self):
        os.makedirs(self.out)
        rc, out = self.jfs("capture", self.out, "pre-reboot", "--profile", "test")
        self.assertEqual(rc, 0, out)
        names = os.listdir(self.out)
        for pre, suf in (("metrics-pre-reboot-", ".txt"), ("breakdown-pre-reboot-", ".txt"),
                         ("files-pre-reboot-", ".csv"), ("state-pre-reboot-", ".v1")):
            hit = [n for n in names if n.startswith(pre) and n.endswith(suf)]
            self.assertEqual(len(hit), 1, (pre, names))
        g = lambda pre: os.path.join(self.out, [n for n in names if n.startswith(pre)][0])
        with open(g("metrics-pre-reboot-")) as f:
            self.assertIn("juicefs_unrelated 1", f.read())  # 全体（絞り込まない）
        with open(g("files-pre-reboot-")) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "id,indx,size,real,atime,ctime,cat,inodes")
        self.assertTrue(lines[1].startswith("100,0,4194304,1000,"))
        self.assertTrue(lines[1].endswith('"7"'))
        with open(g("breakdown-pre-reboot-")) as f:
            self.assertIn("== category (bytes, files) ==", f.read())
        with open(g("state-pre-reboot-"), "rb") as f:
            self.assertEqual(f.read(), b"STATE")

    def test_capture_reports_missing_state_but_keeps_going(self):
        os.makedirs(self.out)
        os.remove(os.path.join(self.cache, "kaz-cache-state.v1"))
        rc, out = self.jfs("capture", self.out, "x", "--profile", "test")
        self.assertNotEqual(rc, 0)
        self.assertIn("kaz-cache-state.v1", out)
        self.assertTrue(any(n.startswith("breakdown-x-") for n in os.listdir(self.out)))


class TestRedact(unittest.TestCase):
    def test_redact_rules(self):
        from measure.capture import redact_config
        got = redact_config({"a": "http://x", "token_ttl": 5, "k": [{"URL": "z"}, "plain"], "ok": "fine"})
        self.assertEqual(got, {"a": "<redacted>", "token_ttl": "<redacted>",
                               "k": [{"URL": "<redacted>"}, "plain"], "ok": "fine"})
        # 敏感な名前の下は、dict や list でも丸ごと伏せる
        got = redact_config({"Bucket": {"name": "x"}, "Keys": ["a", {"b": 1}], "keep": {"n": 1}})
        self.assertEqual(got, {"Bucket": "<redacted>", "Keys": "<redacted>", "keep": {"n": 1}})
        # 名前に :// を含む項目は、名前も値も伏せる
        got = redact_config({"redis://h:1/0": {"x": 1}, "ok": 2})
        self.assertEqual(got, {"<redacted:0>": "<redacted>", "ok": 2})
        self.assertNotIn("redis", json.dumps(got))


class TestSamplerFormats(unittest.TestCase):
    """今の llen_sampler.py／key_tracker.py と同じ記録の形。"""

    def test_llen_row_format(self):
        from measure.samplers import llen_row
        row = llen_row(1000, [5, 400, 10, 360, 1, 2, 3, 4, 6, 7])
        self.assertEqual(row[0], 1000)
        self.assertEqual(row[2:], [10, 798, "79.8", 6, 400, 400, 2])
        self.assertEqual(llen_row(1000, [])[2:], [0, 0, 0, 0, 0, 0, 0])

    def test_keys_snapshot_and_events(self):
        from measure import samplers
        with tempfile.TemporaryDirectory() as d:
            cache, out = os.path.join(d, "c"), os.path.join(d, "o")
            os.makedirs(os.path.join(cache, "raw", "chunks", "0"))
            os.makedirs(out)
            f1 = os.path.join(cache, "raw", "chunks", "0", "1_0_10")
            open(f1, "wb").write(b"a" * 10)
            open(os.path.join(cache, "raw", "chunks", "0", "not-a-block"), "wb").write(b"z")
            samplers.run_keys(cache, out, 0, once=True)
            snap = [n for n in os.listdir(out) if n.startswith("keys-snapshot-")][0]
            with open(os.path.join(out, snap)) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0], "key\tatime\tctime\tsize")
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[1].startswith("1_0_10\t") and lines[1].endswith("\t10"))
            self.assertEqual(samplers.scan_cache(cache).keys(), {"1_0_10"})

    def test_keys_scan_oserror_logged_and_continues(self):
        from unittest import mock
        from measure import samplers
        calls = []

        def fake_scan(root):
            calls.append(1)
            if len(calls) == 2:
                raise PermissionError("denied")
            return {"1_0_1": (1, 1, 1)} if len(calls) == 1 else {"1_0_1": (1, 1, 1), "2_0_1": (2, 2, 2)}

        sleeps = []

        def fake_sleep(n):
            sleeps.append(n)
            if len(sleeps) == 3:
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as d, mock.patch.object(samplers, "scan_cache", fake_scan), \
                mock.patch.object(samplers.time, "sleep", fake_sleep), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            with self.assertRaises(KeyboardInterrupt):
                samplers.run_keys("x", d, 1)
            with open(os.path.join(d, "keys-events.tsv")) as f:
                ev = f.read().splitlines()
        self.assertIn("PermissionError", err.getvalue())
        self.assertEqual(len(ev), 1)  # 失敗の回は偽の del を出さず、次の回の add だけ
        self.assertIn("\tadd\t2_0_1\t", ev[0])


if __name__ == "__main__":
    unittest.main()
