"""試験の volume（RTT の中継、安全の確かめ、mount の引数、結合の format／mount）の試験。

本番の Redis、バケット、mount には触れない。結合の試験は local の file storage と一時の port だけを使う。"""
import asyncio
import dataclasses
import os
import shutil
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from lib import config
from replay import rtt_proxy, testvol

HERE = os.path.dirname(os.path.abspath(__file__))
FLAGS = os.path.join(HERE, "mount_flags.txt")
PROD_REDIS = ("10.1.2.3", 56379)  # 偽の本番の Redis（実際の値は試験で読まない）


def _free_port():
    """空いている TCP の port を 1 つ返す（試験用）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _echo_server():
    """受け取ったものをそのまま返す TCP サーバーを thread で立て、(port, 止める関数) を返す。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def handle(c):
        with c:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                c.sendall(d)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1], srv.close


def _rtt(port, payload, n_expect, rounds=15):
    """port へ payload を送り、n_expect バイトが返るまでの時間（秒）の中央値を返す。"""
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ts = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            s.sendall(payload)
            got = 0
            while got < n_expect:
                got += len(s.recv(65536))
            ts.append(time.perf_counter() - t0)
    return statistics.median(ts)


class TestRttProxy(unittest.TestCase):
    def test_rtt_proxy_adds_delay(self):
        eport, stop_echo = _echo_server()
        self.addCleanup(stop_echo)
        pport = _free_port()
        loop = asyncio.new_event_loop()
        started = threading.Event()

        async def run():
            srv = await rtt_proxy.start("127.0.0.1:%d" % pport, "127.0.0.1:%d" % eport, 5)
            started.set()
            async with srv:
                try:
                    await srv.serve_forever()
                except asyncio.CancelledError:  # 試験の終わりに止める
                    pass

        th = threading.Thread(target=lambda: loop.run_until_complete(run()), daemon=True)
        th.start()
        self.assertTrue(started.wait(5))
        self.addCleanup(lambda: loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(loop)]))
        one = b"PING\r\n"
        added = _rtt(pport, one, len(one)) - _rtt(eport, one, len(one))
        self.assertAlmostEqual(added * 1000, 10, delta=2)
        pipe = one * 50  # pipeline: 50 個の命令を 1 回で送る
        added_pipe = _rtt(pport, pipe, len(pipe)) - _rtt(eport, pipe, len(pipe))
        self.assertAlmostEqual(added_pipe * 1000, 10, delta=2)  # 往復 1 回分だけ増える

    def test_parse_addr(self):
        self.assertEqual(rtt_proxy.parse_addr("127.0.0.1:16380"), ("127.0.0.1", 16380))
        with self.assertRaises(ValueError):
            rtt_proxy.parse_addr("16380")


def _profiles():
    """本番と試験のプロファイル（profiles.toml の値）を返す。"""
    return config.load_profile("prod"), config.load_profile("test")


class TestSafety(unittest.TestCase):
    def setUp(self):
        self.prod, self.test = _profiles()
        self.kw = dict(prod=self.prod, prod_redis=PROD_REDIS)

    def check(self, profile, spec=None):
        testvol.safety_check(profile, spec=spec, **self.kw)

    def test_safe_test_profile_passes(self):
        self.check(self.test)

    def test_safety_refuses_prod_redis(self):
        for addr in ("10.1.2.3:56379", "127.0.0.1:56379", "10.1.2.3:16379"):
            with self.subTest(addr=addr), self.assertRaises(testvol.SafetyError):
                self.check(dataclasses.replace(self.test, redis_addr=addr))
        with self.assertRaises(testvol.SafetyError):
            self.check(dataclasses.replace(self.test, redis_source="mount_script"))
        # spec 側（中継の先など）が本番を指しても止まる
        spec = testvol.spec_from_profile(self.test)
        with self.assertRaises(testvol.SafetyError):
            self.check(self.test, dataclasses.replace(spec, proxy_addr=("10.1.2.3", 56379)))
        # 本番の接続先を確かめられないときも止まる
        with self.assertRaises(testvol.SafetyError):
            testvol.safety_check(self.test, prod=self.prod, prod_redis=None,
                                 mount_script="/nonexistent/juicefs_mount.sh")

    def test_safety_refuses_prod_bucket(self):
        for b in ("buckets", " buckets", "BUCKETS"):
            with self.subTest(b=b), self.assertRaises(testvol.SafetyError):
                self.check(dataclasses.replace(self.test, bucket=b))
        spec = testvol.spec_from_profile(self.test)
        with self.assertRaises(testvol.SafetyError):
            self.check(self.test, dataclasses.replace(spec, bucket_name="buckets"))

    def test_safety_refuses_prod_mountpoint(self):
        mp = self.prod.mountpoint
        for v in (mp, mp + "/", mp + "/sub", mp + "/../" + os.path.basename(mp)):
            with self.subTest(v=v), self.assertRaises(testvol.SafetyError):
                self.check(dataclasses.replace(self.test, mountpoint=v))
        for v in (self.prod.cache_dir, os.path.dirname(self.prod.cache_dir)):
            with self.subTest(cache=v), self.assertRaises(testvol.SafetyError):
                self.check(dataclasses.replace(self.test, cache_dir=v))
        with self.assertRaises(testvol.SafetyError):
            self.check(dataclasses.replace(self.test, metrics_url="http://127.0.0.1:9567/metrics"))

    def test_up_stops_before_any_action(self):
        calls = []
        bad = dataclasses.replace(self.test, bucket="buckets")
        with self.assertRaises(testvol.SafetyError):
            testvol.up(bad, {"name": "default"}, run=lambda *a, **k: calls.append(a), **self.kw)
        self.assertEqual(calls, [])

    def test_up_rejects_bad_settings_before_any_action(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            spec = dataclasses.replace(testvol.spec_from_profile(self.test), state_dir=os.path.join(d, "state"),
                                       redis_addr=("127.0.0.1", _free_port()), proxy_addr=("127.0.0.1", _free_port()))
            with self.assertRaises(ValueError):
                testvol.up(self.test, {"flags": {"max-uploads": 50}}, spec=spec,
                           run=lambda *a, **k: calls.append(a), **self.kw)
            self.assertFalse(os.path.exists(spec.state_dir))  # Redis も中継も起動していない
        self.assertEqual(calls, [])


class TestFixRound1(unittest.TestCase):
    """Ruling 8（state の置き場所は 0700）と Ruling 9（s3 の新規 format は --create-bucket が要る）。"""

    def setUp(self):
        self.prod, self.test = _profiles()
        self.kw = dict(prod=self.prod, prod_redis=PROD_REDIS)
        self.tmp = tempfile.mkdtemp(prefix="jfs-fix1-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def spec(self, **kw):
        """一時の path と空いた port（Redis は動いていない）の Spec。"""
        base = dict(state_dir=os.path.join(self.tmp, "state"), mountpoint=os.path.join(self.tmp, "mnt"),
                    cache_dir=os.path.join(self.tmp, "cache"), redis_addr=("127.0.0.1", _free_port()),
                    proxy_addr=("127.0.0.1", _free_port()))
        base.update(kw)
        return dataclasses.replace(testvol.spec_from_profile(self.test), **base)

    def test_state_dir_private(self):
        spec = self.spec()
        os.makedirs(spec.path("redis"), mode=0o755)
        os.chmod(spec.state_dir, 0o755)
        os.chmod(spec.path("redis"), 0o755)
        testvol.prepare_state_dir(spec)
        for d in (spec.state_dir, spec.path("redis")):
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)
        spec2 = self.spec(state_dir=os.path.join(self.tmp, "new", "state"))
        testvol.prepare_state_dir(spec2)
        for d in (spec2.state_dir, spec2.path("redis")):
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)

    def test_up_s3_unformatted_requires_create_bucket(self):
        spec = self.spec(storage="s3")
        calls = []
        with self.assertRaises(testvol.CreateBucketRequired) as cm:
            testvol.up(self.test, {"name": "default"}, spec=spec, run=lambda *a, **k: calls.append(a), **self.kw)
        msg = str(cm.exception)
        self.assertIn("--create-bucket", msg)
        self.assertIn("Drive", msg)
        self.assertEqual(calls, [])
        for d in (spec.state_dir, spec.mountpoint, spec.cache_dir):  # mkdir もしていない
            self.assertFalse(os.path.exists(d), d)
        self.assertIsNone(testvol._redis_dbsize(spec.redis_addr, spec.db))  # Redis も起動していない
        # ensure_format 単独でも、フラグなしの s3 は鍵を読む前に止まる
        with self.assertRaises(testvol.CreateBucketRequired):
            testvol.ensure_format(spec, prod_format=(4096, "zstd"), run=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_create_bucket_passes_keys_by_env(self):
        script = os.path.join(self.tmp, "rclone.sh")
        with open(script, "w") as f:
            f.write('RCLONE_SERVE_S3_ACCESS_KEY="fake-ak"\nRCLONE_SERVE_S3_SECRET="fake-sk"\nRCLONE_SERVE_S3_PORT=18080\n')
        old = testvol.RCLONE_SCRIPT
        testvol.RCLONE_SCRIPT = script
        self.addCleanup(setattr, testvol, "RCLONE_SCRIPT", old)
        got = []
        spec = self.spec(storage="s3")
        self.assertTrue(testvol.ensure_format(spec, prod_format=(4096, "zstd"), create_bucket=True,
                                              run=lambda cmd, **k: got.append((cmd, k))))
        cmd, k = got[0]
        self.assertNotIn("fake-ak", " ".join(cmd))
        self.assertNotIn("fake-sk", " ".join(cmd))
        self.assertIn("http://127.0.0.1:18080/juicefs-kaz-test", cmd)
        self.assertEqual((k["env"]["ACCESS_KEY"], k["env"]["SECRET_KEY"]), ("fake-ak", "fake-sk"))

    def test_file_storage_needs_no_flag(self):
        got = []
        spec = self.spec(storage="file", bucket=os.path.join(self.tmp, "bucket") + "/")
        self.assertTrue(testvol.ensure_format(spec, prod_format=(4096, "zstd"), run=lambda cmd, **k: got.append(cmd)))
        self.assertEqual(got[0][:4], [spec.binary, "format", "--storage", "file"])


class TestMountFlags(unittest.TestCase):
    def test_mount_flags_override_compaction(self):
        base = testvol.read_mount_flags(FLAGS)
        comp = [e for e in base if e.mark == "compaction"]
        self.assertIn("kaz-compaction-delta-trigger", [e.name for e in comp])
        a0 = testvol.build_mount_args(FLAGS, {})
        a1 = testvol.build_mount_args(FLAGS, {"mode": "block", "flags": {"kaz-compaction-delta-trigger": 30,
                                                                          "kaz-compaction-delta-block-ratio": 0.75}})
        self.assertEqual(len(a0), len(a1))
        diff = [(x, y) for x, y in zip(a0, a1) if x != y]
        self.assertEqual(diff, [("delta", "block"), ("50", "30"), ("0.5", "0.75")])
        i = a1.index("--kaz-compaction-mode")
        self.assertEqual(a1[i + 1], "block")
        # 本番の mount に無い compaction の値は末尾に足す
        a2 = testvol.build_mount_args(FLAGS, {"flags": {"kaz-compaction-block-max-slices": 8}})
        self.assertEqual(a2[:len(a0)], a0)
        self.assertEqual(a2[len(a0):], ["--kaz-compaction-block-max-slices", "8"])
        # compaction 以外の値は settings で変えられない
        for bad in ({"flags": {"max-uploads": 50}}, {"flags": {"metrics": "127.0.0.1:9567"}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                testvol.build_mount_args(FLAGS, bad)
        # [test] の行は試験の値で置き換わる
        a3 = testvol.build_mount_args(FLAGS, {}, test_values={"metrics": "127.0.0.1:19568"})
        self.assertEqual(a3[a3.index("--metrics") + 1], "127.0.0.1:19568")
        with self.assertRaises(ValueError):
            testvol.build_mount_args(FLAGS, {}, test_values={"max-uploads": "1"})

    def test_mount_flags_no_stray_args(self):
        args = testvol.build_mount_args(FLAGS, {"mode": "delta", "flags": {"kaz-compaction-delta-trigger": 99}})
        for a in args:
            self.assertTrue(a.strip(), "空白だけの要素: %r" % a)
            self.assertEqual(a, a.strip(), "前後の空白: %r" % a)
            self.assertNotIn("\\", a)
            self.assertNotIn("#", a)
        self.assertNotIn("--log", args)
        self.assertNotIn("--cache-dir", args)
        self.assertIn("--metrics", args)
        # 試験のバイナリがあれば、全オプションが mount --help に載っていることも確かめる
        binary = config.load_common()["test_binary"]
        if os.access(binary, os.X_OK):
            h = subprocess.run([binary, "mount", "--help"], capture_output=True, text=True, timeout=30).stdout
            for a in args:
                if a.startswith("--"):
                    self.assertIn(a.split("=")[0] + " ", h, a)

    def test_bool_flags(self):
        a = testvol.build_mount_args(FLAGS, {"flags": {"kaz-compaction-delta-trigger": 30}})
        self.assertIn("--writeback", a)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.txt")
            with open(p, "w") as f:
                f.write("--x 1\n--kaz-compaction-foo   # [compaction]\n")
            self.assertEqual(testvol.build_mount_args(p, {"flags": {"kaz-compaction-foo": False}}), ["--x", "1"])
            self.assertEqual(testvol.build_mount_args(p, {"flags": {"kaz-compaction-foo": True}}),
                             ["--x", "1", "--kaz-compaction-foo"])
            with open(p, "w") as f:
                f.write("--x 1 \\\n")
            with self.assertRaises(ValueError):
                testvol.read_mount_flags(p)


def _have_fuse():
    """FUSE を user で使えるか（/dev/fuse と fusermount3）。"""
    return os.path.exists("/dev/fuse") and bool(shutil.which("fusermount3") or shutil.which("fusermount"))


def _have_docker():
    """docker compose を user で使えるか（試験用に Redis の container を立てられるか）。"""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _unique_project():
    """結合の試験用の compose project 名（実行ごとに別。固定の本番用の名前と衝突しない）。"""
    return "jfs-kaz-it-%d-%s" % (os.getpid(), os.urandom(3).hex())


class TestRedisCompose(unittest.TestCase):
    """試験の Redis を docker compose で起動する部分。docker の実行は偽の run で確かめる。"""

    def setUp(self):
        self.prod, self.test = _profiles()
        self.kw = dict(prod=self.prod, prod_redis=PROD_REDIS)
        self.tmp = tempfile.mkdtemp(prefix="jfs-compose-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.spec = dataclasses.replace(
            testvol.spec_from_profile(self.test), state_dir=os.path.join(self.tmp, "state"),
            mountpoint=os.path.join(self.tmp, "mnt"), cache_dir=os.path.join(self.tmp, "cache"),
            redis_addr=("127.0.0.1", 17999), proxy_addr=("127.0.0.1", _free_port()),
            compose_project="jfs-kaz-unit")

    def test_spec_from_profile_has_compose_settings(self):
        spec = testvol.spec_from_profile(self.test)
        self.assertEqual(spec.compose_project, "jfs-kaz-test")
        self.assertTrue(spec.compose_file.endswith("replay/docker-compose.yml"))
        self.assertTrue(os.path.exists(spec.compose_file))
        self.assertFalse(hasattr(spec, "redis_server"))

    def test_compose_file_contents(self):
        with open(self.spec.compose_file, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("image: redis:8.0.5", text)
        self.assertIn('127.0.0.1:${JFS_TEST_REDIS_PORT:-16379}:6379', text)
        self.assertIn("--appendonly", text)
        self.assertIn("--databases", text)
        self.assertNotIn("0.0.0.0", text)

    def test_ensure_redis_runs_compose_up_with_env(self):
        calls = []
        answers = iter([None, None, 0])  # 起動前、起動直後はまだ、その後に応答

        def fake_run(cmd, env=None, timeout=300, hidden=()):
            calls.append((cmd, env))
            return ""

        with mock.patch.object(testvol, "_redis_dbsize", side_effect=lambda *a, **k: next(answers)), \
                mock.patch.object(testvol.time, "sleep"):
            self.assertTrue(testvol.ensure_redis(self.spec, run=fake_run))
        self.assertEqual(len(calls), 1)
        cmd, env = calls[0]
        self.assertEqual(cmd, ["docker", "compose", "-p", "jfs-kaz-unit", "-f", self.spec.compose_file,
                               "up", "-d", "redis"])
        self.assertEqual(env["JFS_TEST_REDIS_PORT"], "17999")
        self.assertEqual(env["JFS_TEST_REDIS_DIR"], self.spec.path("redis"))
        self.assertEqual((env["UID"], env["GID"]), (str(os.getuid()), str(os.getgid())))
        self.assertEqual(os.stat(self.spec.path("redis")).st_mode & 0o777, 0o700)  # compose の前に 0700

    def test_ensure_redis_noop_when_running(self):
        calls = []
        with mock.patch.object(testvol, "_redis_dbsize", return_value=0):
            self.assertFalse(testvol.ensure_redis(self.spec, run=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])

    def test_ensure_redis_timeout_is_error(self):
        with mock.patch.object(testvol, "_redis_dbsize", return_value=None), \
                mock.patch.object(testvol.time, "sleep"), mock.patch.object(testvol, "REDIS_START_TIMEOUT", 0):
            with self.assertRaises(RuntimeError):
                testvol.ensure_redis(self.spec, run=lambda *a, **k: "")

    def test_no_redis_server_binary_is_spawned(self):
        with mock.patch.object(testvol.subprocess, "Popen") as popen, \
                mock.patch.object(testvol, "_redis_dbsize", side_effect=[None, 0, 0]):
            testvol.ensure_redis(self.spec, run=lambda *a, **k: "")
        popen.assert_not_called()

    def test_stop_redis_runs_compose_down(self):
        calls = []
        with mock.patch.object(testvol, "_redis_dbsize", return_value=None):
            testvol._stop_redis(self.spec, run=lambda cmd, **k: calls.append((cmd, k.get("env"))))
        self.assertEqual(calls[0][0], ["docker", "compose", "-p", "jfs-kaz-unit", "-f", self.spec.compose_file, "down"])
        self.assertEqual(calls[0][1]["JFS_TEST_REDIS_PORT"], "17999")

    def test_safety_failure_runs_no_docker(self):
        calls = []
        run = lambda *a, **k: calls.append(a)
        bad = dataclasses.replace(self.test, bucket="buckets")
        with self.assertRaises(testvol.SafetyError):
            testvol.up(bad, {"name": "default"}, spec=self.spec, run=run, **self.kw)
        with self.assertRaises(testvol.SafetyError):
            testvol.destroy(bad, spec=self.spec, confirm=lambda p: "destroy", run=run, **self.kw)
        with self.assertRaises(testvol.SafetyError):
            testvol.down(spec=self.spec, profile=bad, run=run, **self.kw)
        bad_spec = dataclasses.replace(self.spec, redis_addr=("127.0.0.1", PROD_REDIS[1]))
        with self.assertRaises(testvol.SafetyError):
            testvol.up(self.test, {"name": "default"}, spec=bad_spec, run=run, **self.kw)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.spec.state_dir))

    def test_destroy_stops_compose_project(self):
        calls = []
        # Redis は動いている体で、format 済みではない（DBSIZE 0）。FLUSHDB は偽の接続で受ける
        spec = self.spec

        class FakeResp:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def call(self, *a):
                return 0

        with mock.patch.object(testvol, "_redis_dbsize", return_value=0), mock.patch.object(testvol, "Resp", FakeResp), \
                mock.patch.object(testvol, "_wait", return_value=True):
            os.makedirs(spec.path("redis"))
            self.assertTrue(testvol.destroy(self.test, spec=spec, confirm=lambda p: "destroy",
                                            run=lambda cmd, **k: calls.append(cmd), **self.kw))
        self.assertIn(["docker", "compose", "-p", "jfs-kaz-unit", "-f", spec.compose_file, "down"], calls)
        self.assertFalse(os.path.exists(spec.path("redis")))  # 従来どおりデータも消す


class TestIntegration(unittest.TestCase):
    def test_up_down_local_storage(self):
        common = config.load_common()
        if not os.access(common["test_binary"], os.X_OK):
            self.skipTest("試験のバイナリが無い")
        if not _have_docker():
            self.skipTest("docker compose が使えない")
        if not _have_fuse():
            self.skipTest("FUSE が使えない")
        prod, test = _profiles()
        root = tempfile.mkdtemp(prefix="jfs-testvol-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        rport, pport, mport = _free_port(), _free_port(), _free_port()
        spec = dataclasses.replace(
            testvol.spec_from_profile(test, common),
            redis_addr=("127.0.0.1", rport), proxy_addr=("127.0.0.1", pport),
            metrics_addr=("127.0.0.1", mport),
            mountpoint=os.path.join(root, "mnt"), cache_dir=os.path.join(root, "cache"),
            state_dir=os.path.join(root, "state"), storage="file",
            bucket=os.path.join(root, "bucket") + "/", compose_project=_unique_project())
        settings = {"name": "t", "mode": "delta", "flags": {"kaz-compaction-delta-trigger": 30,
                                                           "kaz-compaction-delta-block-ratio": 0.75}}
        skw = dict(prod=prod, prod_redis=PROD_REDIS)
        kw = dict(skw, prod_format=(4096, "zstd"))
        try:
            info = testvol.up(test, settings, spec=spec, **kw)
            self.assertTrue(os.path.ismount(spec.mountpoint))
            for d in (spec.state_dir, spec.path("redis")):  # Ruling 8: 鍵が AOF に入るので 0700
                self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)
            self.assertNotIn("password", repr(info).lower())
            p = os.path.join(spec.mountpoint, "hello.bin")
            data = os.urandom(1 << 20)
            with open(p, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data)
            cfg = testvol.read_mount_config(spec.mountpoint)
            self.assertEqual(cfg["Meta"]["KazCompactionDeltaTrigger"], 30)
            self.assertEqual(cfg["Meta"]["KazCompactionDeltaBlockRatio"], 0.75)
            self.assertEqual(cfg["Meta"]["KazCompactionMode"], "delta")
            self.assertEqual(cfg["Port"]["PrometheusAgent"], "127.0.0.1:%d" % mport)
            self.assertEqual(cfg["Chunk"]["CacheEviction"], "kaz-2q")
            self.assertEqual(cfg["Chunk"]["CacheSize"], 20 << 30)
            self.assertEqual(cfg["Chunk"]["BufferSize"], 1 << 30)
            self.assertEqual(cfg["Chunk"]["WritebackThresholdSize"], 4194305)
            self.assertEqual(cfg["Format"]["BlockSize"], 4096)
            self.assertEqual(cfg["Format"]["Compression"], "zstd")
            self.assertEqual(cfg["Format"]["TrashDays"], 0)
            self.assertEqual(cfg["MetaWriteBatch"], 64)
            self.assertTrue(cfg["FuseOpts"]["AllowOther"])
            st = testvol.status(spec)
            self.assertTrue(st["redis"] and st["proxy"] and st["mounted"] and st["formatted"])
            testvol.down(spec, test, **skw)
            self.assertFalse(os.path.ismount(spec.mountpoint))
            st = testvol.status(spec)
            self.assertTrue(st["redis"])  # down は Redis を残す
            self.assertFalse(st["proxy"])
            # 2 回目の up は format を飛ばして mount だけする
            testvol.up(test, {"name": "default"}, spec=spec, **kw)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data)
            self.assertEqual(testvol.read_mount_config(spec.mountpoint)["Meta"]["KazCompactionDeltaTrigger"], 50)
            # destroy は確認の入力が違えば何もしない
            self.assertFalse(testvol.destroy(test, spec=spec, confirm=lambda prompt: "no", **skw))
            self.assertTrue(os.path.ismount(spec.mountpoint))
            self.assertTrue(testvol.destroy(test, spec=spec, confirm=lambda prompt: "destroy", **skw))
            st = testvol.status(spec)
            self.assertFalse(st["redis"] or st["proxy"] or st["mounted"])
            objs = [f for _, _, fs in os.walk(spec.bucket) for f in fs]
            self.assertEqual(objs, [])
        finally:
            testvol.cleanup(spec)
        self.assertFalse(os.path.ismount(spec.mountpoint))


if __name__ == "__main__":
    unittest.main()
