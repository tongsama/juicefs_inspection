"""流し直し（replay.player）と 1 回の試験（jfsreplay run）の試験。

player の試験は一時ファイル（FUSE ではない）だけを使う。結合の試験は local の file storage と一時の port・path だけを使い、
本番の mount・キャッシュ・Redis・バケットと、既存の試験の volume（juicefs-kaz-test）には触れない。"""
import dataclasses
import errno
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from lib import config, resp
from replay import accesslog, player, run as runmod, seed, testvol, trace
import test_testvol as tv

MiB = 1 << 20
UUID = "73e74f7b-0000-4155-adea-d9ef1e669c21"  # 試験のキャッシュの 1 段目の名前（UUID の形）


def _op(start_us, pid, kind, off=0, size=0, mode=0, status="OK"):
    """試験用の Op（start_us は記録の上の開始の時刻）。"""
    return accesslog.Op(start_us, start_us + 100, pid, kind, off, size, mode, 100, status)


class TestPlayer(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="jfs-play-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.path = os.path.join(d, "disk.img")
        with open(self.path, "wb") as f:
            f.truncate(8 * MiB)

    def test_content_shifted_differs_from_seed(self):
        a = player.content(8192, 4096)
        self.assertNotEqual(a, seed.content(8192, 4096))
        self.assertEqual(a[:8], (8192 + 1).to_bytes(8, "big"))
        self.assertEqual(player.content(8192 + 10, 20), a[10:30])

    def test_play_timing(self):
        ops = []
        for i in range(100):  # 10 ms ごと、4 つの pid、write／read／fsync／flush を混ぜる
            kind = ("write", "read", "write", "fsync", "flush")[i % 5]
            ops.append(_op(1_000_000 + i * 10_000, 100 + i % 4, kind, (i * 4096) % (4 * MiB), 4096))
        t0 = time.monotonic() + 0.1
        r = player.play(self.path, ops, t0)
        self.assertEqual(r.errors, [])
        self.assertEqual(r.ops_done, 100)
        self.assertLessEqual(r.max_lag_ms, 20.0)
        self.assertGreaterEqual(time.monotonic() - t0, 0.99 * 0.99)
        self.assertEqual(set(r.latency), {"write", "read", "fsync", "flush"})
        for k in ("count", "p50_ms", "p99_ms", "max_ms"):
            self.assertIn(k, r.latency["write"])
        with open(self.path, "rb") as f:  # write の中身は種と違う（位置 + 1）
            self.assertEqual(f.read(4096), player.content(0, 4096))

    def test_play_kinds_truncate_fallocate(self):
        ops = [_op(0, 1, "fallocate", 8 * MiB, MiB, 0), _op(1000, 1, "truncate", 0, 10 * MiB),
               _op(2000, 1, "write", 9 * MiB, 100), _op(3000, 1, "read", 9 * MiB, 100),
               _op(4000, 1, "write", 0, 10, status="EIO")]
        r = player.play(self.path, ops, time.monotonic())
        self.assertEqual(r.errors, [])
        self.assertEqual(r.ops_done, 4)
        self.assertEqual(r.skipped, 1)  # 記録の上で失敗していた操作は流さない
        self.assertEqual(os.path.getsize(self.path), 10 * MiB)
        with open(self.path, "rb") as f:
            f.seek(9 * MiB)
            self.assertEqual(f.read(100), player.content(9 * MiB, 100))

    def test_play_pid_threads(self):
        # pid 1 の fsync が 0.5 秒かかる間も、pid 2 の操作は予定どおり出る
        ops = [_op(0, 1, "write", 0, 4096), _op(10_000, 1, "fsync")]
        ops += [_op(20_000 + i * 20_000, 2, "write", MiB + i * 4096, 4096) for i in range(20)]
        ops.sort(key=lambda o: o.start_us)
        real = os.fsync

        def slow_fsync(fd):
            time.sleep(0.5)
            real(fd)
        with mock.patch("os.fsync", side_effect=slow_fsync):
            r = player.play(self.path, ops, time.monotonic() + 0.05)
        self.assertEqual(r.errors, [])
        self.assertGreaterEqual(r.latency["fsync"]["max_ms"], 490)
        self.assertLessEqual(r.max_lag_ms, 20.0)

    def test_replay_stops_on_eio(self):
        ops = [_op(i * 10_000, 1 + i % 2, "write", i * 4096, 4096) for i in range(50)]
        calls = []

        def eio(fd, data, off):
            calls.append(off)
            if len(calls) >= 3:
                raise OSError(errno.EIO, "Input/output error")
            return len(data)
        with mock.patch("os.pwrite", side_effect=eio):
            r = player.play(self.path, ops, time.monotonic())
        self.assertTrue(r.failed)
        self.assertEqual(r.errors[0]["errno"], "EIO")
        self.assertEqual(r.errors[0]["kind"], "write")
        self.assertLess(r.ops_done, 10)  # その時点で止まり、残りは流さない
        d = r.to_dict()
        self.assertTrue(d["failed"])
        json.dumps(d)

    def test_external_stop(self):
        import threading
        stop = threading.Event()
        stop.set()
        r = player.play(self.path, [_op(i * 1000, 1, "write", 0, 10) for i in range(5)], time.monotonic(), stop=stop)
        self.assertEqual(r.ops_done, 0)
        self.assertTrue(r.stopped)

    def test_percentiles(self):
        s = player.summarize([i / 1000 for i in range(1, 101)])
        self.assertEqual(s["count"], 100)
        self.assertAlmostEqual(s["p50_ms"], 50.0)
        self.assertAlmostEqual(s["p99_ms"], 99.0)
        self.assertAlmostEqual(s["max_ms"], 100.0)


class TestRunParts(unittest.TestCase):
    def test_settled_logic(self):
        st = runmod.Settle(quiet_s=60)
        m = {"juicefs_staging_blocks": 0, "juicefs_object_request_uploading": 0,
             "juicefs_compact_size_histogram_bytes_count": 5}
        self.assertFalse(st.update(m, 0))
        self.assertFalse(st.update(m, 59))
        self.assertTrue(st.update(m, 60))
        self.assertFalse(st.update(dict(m, juicefs_compact_size_histogram_bytes_count=6), 61))
        self.assertFalse(st.update(dict(m, juicefs_compact_size_histogram_bytes_count=6), 100))
        self.assertFalse(st.update(dict(m, juicefs_staging_blocks=1, juicefs_compact_size_histogram_bytes_count=6), 130))
        self.assertFalse(st.update(dict(m, juicefs_compact_size_histogram_bytes_count=6), 131))  # staging の後は数え直す
        self.assertTrue(st.update(dict(m, juicefs_compact_size_histogram_bytes_count=6), 190))
        self.assertFalse(runmod.Settle(quiet_s=0).update({}, 0))  # 系列が無ければ落ち着いたとはみなさない

    def test_rate_limit_lines(self):
        self.assertTrue(runmod.RATE_LIMIT_RE.search("upload: 429 Too Many Requests"))
        self.assertTrue(runmod.RATE_LIMIT_RE.search("googleapi: Error 403: User Rate Limit Exceeded, rateLimitExceeded"))
        self.assertFalse(runmod.RATE_LIMIT_RE.search("put chunks/0/1/1_0_4096: 403 Forbidden"))
        self.assertFalse(runmod.RATE_LIMIT_RE.search("compaction done 4290 bytes"))

    def test_guard_detects_errors_and_log(self):
        d = tempfile.mkdtemp(prefix="jfs-guard-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        log = os.path.join(d, "juicefs.log")
        with open(log, "w") as f:
            f.write("old line 429 Too Many Requests\n")  # 始める前の行は数えない
        vals = {"juicefs_object_request_errors": 2.0}
        g = runmod.Guard(lambda: dict(vals), log, mounted=lambda: True)
        self.assertIsNone(g.check())
        with open(log, "a") as f:
            f.write("upload failed: 429 Too Many Requests\n")
        self.assertIn("429", g.check())
        g = runmod.Guard(lambda: dict(vals), log, mounted=lambda: True)
        vals["juicefs_object_request_errors"] = 3.0
        self.assertIn("object_request_errors", g.check())
        g = runmod.Guard(lambda: dict(vals), log, mounted=lambda: False)
        self.assertIn("mount", g.check())

    def test_guard_stops_on_prod_errors(self):
        d = tempfile.mkdtemp(prefix="jfs-guard-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        log = os.path.join(d, "juicefs.log")
        prod = {"juicefs_object_request_errors": 7.0}
        g = runmod.Guard(lambda: {"juicefs_object_request_errors": 0.0}, log, mounted=lambda: True,
                         prod_scrape=lambda: dict(prod))
        self.assertIsNone(g.check())
        self.assertTrue(g.prod_seen)
        prod["juicefs_object_request_errors"] = 8.0
        self.assertIn("本番の object_request_errors", g.check())

        def down():
            raise OSError("refused")
        g = runmod.Guard(lambda: {"juicefs_object_request_errors": 0.0}, log, mounted=lambda: True, prod_scrape=down)
        self.assertIsNone(g.check())  # 本番が読めないだけでは止めない（呼ぶ側が警告にする）
        self.assertFalse(g.prod_seen)

    def test_write_measure_profiles_uses_volume_cache_dir(self):
        d = tempfile.mkdtemp(prefix="jfs-mprof-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        prod, test = tv._profiles()
        spec = dataclasses.replace(testvol.spec_from_profile(test), cache_dir=os.path.join(d, "cache"),
                                   state_dir=os.path.join(d, "state"))
        vol = os.path.join(spec.cache_dir, UUID)
        p = runmod.write_measure_profiles(d, spec, 42, config.load_common(), cache_dir=vol)
        prof = config.load_profile("test", p)
        self.assertEqual(prof.cache_dir, vol)
        self.assertEqual(prof.log_glob, os.path.join(spec.state_dir, "juicefs.log"))  # Redis や中継のログは含めない
        self.assertEqual(prof.inode, 42)

    def test_seed_path_checks_trace_name(self):
        d = tempfile.mkdtemp(prefix="jfs-seedp-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        spec = dataclasses.replace(testvol.spec_from_profile(tv._profiles()[1]), mountpoint=os.path.join(d, "mnt"))
        with open(os.path.join(d, "seed.json"), "w") as f:
            json.dump({"name": "other", "path": "/m/seed/other.img", "mountpoint": "/m", "inode": 3}, f)
        with self.assertRaises(runmod.RunError):
            runmod._seed_path(os.path.join(d, "boot.tsv"), spec)
        path, info = runmod._seed_path(os.path.join(d, "other.tsv"), spec)
        self.assertEqual(path, os.path.join(d, "mnt", "seed", "other.img"))

    def test_wait_flushed(self):
        vals = iter([{}, {"juicefs_staging_blocks": 3, "juicefs_object_request_uploading": 1},
                     {"juicefs_staging_blocks": 0, "juicefs_object_request_uploading": 0},
                     {"juicefs_staging_blocks": 0, "juicefs_object_request_uploading": 1},
                     {"juicefs_staging_blocks": 0, "juicefs_object_request_uploading": 0},
                     {"juicefs_staging_blocks": 0, "juicefs_object_request_uploading": 0}])
        t = iter(range(100))
        logs = []
        r = runmod.wait_flushed(lambda: next(vals), max_s=50, poll_s=0, progress_s=2, log=logs.append,
                                clock=lambda: next(t))
        self.assertEqual(r["state"], "落ち着いた")
        self.assertEqual(r["seconds"], 6)  # 2 回続けての 0 まで待つ（6 回目の標本、時計は 1 回目の標本で 1）
        self.assertTrue(logs and "staging_blocks" in logs[0])
        r = runmod.wait_flushed(lambda: {"juicefs_staging_blocks": 1, "juicefs_object_request_uploading": 0},
                                max_s=3, poll_s=0, log=lambda m: None, clock=lambda: next(t))
        self.assertEqual(r["state"], "未収束")
        self.assertEqual(r["last"]["juicefs_staging_blocks"], 1)

    def test_load_settings(self):
        d = tempfile.mkdtemp(prefix="jfs-set-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "settings.toml")
        self.assertEqual(runmod.load_settings("default", p), {"name": "default", "mode": None, "flags": {}})
        with open(p, "w") as f:
            f.write('[[settings]]\nname = "t30"\nmode = "delta"\nflags = {"kaz-compaction-delta-trigger" = 30}\n')
        self.assertEqual(runmod.load_settings("t30", p)["flags"], {"kaz-compaction-delta-trigger": 30})
        with self.assertRaises(KeyError):
            runmod.load_settings("nope", p)

    def _cache(self):
        """一時の試験のキャッシュ（UUID の 1 段目、raw に 1 ファイル、空の rawstaging）と、その外の場所を作る。"""
        d = tempfile.mkdtemp(prefix="jfs-cache-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        cache = os.path.join(d, "cache")
        vol = os.path.join(cache, UUID)
        os.makedirs(os.path.join(vol, "raw", "chunks", "0"))
        os.makedirs(os.path.join(vol, "rawstaging"))
        with open(os.path.join(vol, "raw", "chunks", "0", "1_0_4"), "w") as f:
            f.write("x")
        state = os.path.join(d, "state")
        os.makedirs(state)
        return d, cache, vol, state

    def test_clear_cache_refuses_staging_and_prod(self):
        d, cache, vol, state = self._cache()
        prod = config.load_profile("prod")
        mnt = os.path.join(d, "mnt")
        self.assertEqual(runmod.clear_cache(cache, mnt, prod, state), 1)
        self.assertEqual(os.listdir(vol), ["rawstaging"])
        with open(os.path.join(vol, "rawstaging", "x"), "w") as f:
            f.write("x")
        with self.assertRaises(runmod.RunError):  # 上がっていない staging は消さない
            runmod.clear_cache(cache, mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):
            runmod.clear_cache(prod.cache_dir, mnt, prod, state)
        os.makedirs(os.path.join(d, "mnt2"))
        with mock.patch("os.path.ismount", return_value=True), self.assertRaises(runmod.RunError):
            runmod.clear_cache(cache, os.path.join(d, "mnt2"), prod, state)

    def test_clear_cache_refuses_bad_paths(self):
        d, cache, vol, state = self._cache()
        prod = config.load_profile("prod")
        mnt = os.path.join(d, "mnt")
        for bad in ("", None, "cache-test", "./x"):  # 空・None・相対
            with self.assertRaises(testvol.SafetyError, msg=repr(bad)):
                runmod.clear_cache(bad, mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):  # state_dir（Redis の AOF）と重なる
            runmod.clear_cache(d, mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):
            runmod.clear_cache(os.path.join(state, "c"), mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):  # $HOME 自体とその上
            runmod.clear_cache(os.path.expanduser("~"), mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):
            runmod.clear_cache("/", mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):  # suite のディレクトリ
            runmod.clear_cache(runmod.SUITE_DIR, mnt, prod, state)
        with self.assertRaises(testvol.SafetyError):  # 試験の mountpoint の下
            runmod.clear_cache(os.path.join(mnt, "c"), mnt, prod, state)
        link = os.path.join(d, "link-to-prod")  # symlink で本番のキャッシュを指しても realpath で止める
        os.symlink(prod.cache_dir, link)
        with self.assertRaises(testvol.SafetyError):
            runmod.clear_cache(link, mnt, prod, state)
        self.assertTrue(os.path.exists(os.path.join(vol, "raw", "chunks", "0", "1_0_4")))  # どれも何も消していない

    def test_clear_cache_skips_symlinks_and_other_names(self):
        d, cache, vol, state = self._cache()
        prod = config.load_profile("prod")
        outside = os.path.join(d, "outside")
        os.makedirs(os.path.join(outside, "sub"))
        for p in (os.path.join(outside, "keep"), os.path.join(outside, "sub", "keep2")):
            with open(p, "w") as f:
                f.write("keep")
        other = "11111111-2222-3333-4444-555555555555"
        os.symlink(outside, os.path.join(cache, other))  # 1 段目の UUID の名前の symlink: たどらない
        os.makedirs(os.path.join(cache, "not-a-uuid"))  # UUID の形でない名前: 触れない
        with open(os.path.join(cache, "not-a-uuid", "f"), "w") as f:
            f.write("x")
        os.symlink(outside, os.path.join(vol, "raw", "chunks", "lnk"))  # UUID の下の symlink: リンク自体だけ消す
        os.symlink(os.path.join(outside, "keep"), os.path.join(vol, "flink"))
        runmod.clear_cache(cache, os.path.join(d, "mnt"), prod, state)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))
        self.assertTrue(os.path.exists(os.path.join(outside, "sub", "keep2")))
        self.assertTrue(os.path.islink(os.path.join(cache, other)))
        self.assertTrue(os.path.exists(os.path.join(cache, "not-a-uuid", "f")))
        self.assertEqual(os.listdir(vol), ["rawstaging"])
        os.makedirs(os.path.join(vol, "raw"))
        with open(os.path.join(vol, "raw", "y"), "w") as f:
            f.write("y")
        runmod.clear_cache(cache, os.path.join(d, "mnt"), prod, state, volume_uuid=other)  # 別の UUID だけ: vol は残る
        self.assertTrue(os.path.exists(os.path.join(vol, "raw", "y")))


class TestRunFailures(unittest.TestCase):
    """run の途中の例外でも、失敗として結果に残り、後始末（複製の削除・umount）と結果の書き出しが行われること。"""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="jfs-runfail-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.d = d
        self.prod, self.test = tv._profiles()
        self.spec = dataclasses.replace(
            testvol.spec_from_profile(self.test), mountpoint=os.path.join(d, "mnt"),
            cache_dir=os.path.join(d, "cache"), state_dir=os.path.join(d, "state"), storage="file",
            bucket=os.path.join(d, "bucket") + "/")
        self.trace = os.path.join(d, "t.tsv")
        with open(self.trace, "w") as f:
            f.write(trace.format_op(_op(0, 1, "write", 0, 10)))
        self.clone = os.path.join(d, "mnt", "replay", "c.img")
        os.makedirs(os.path.dirname(self.clone))
        with open(self.clone, "w") as f:
            f.write("x")
        self.calls = []
        self.out = os.path.join(d, "out")
        self.uuid = UUID

    def _run(self, warm=None, subproc=None):
        """外の資源をすべて偽にして run を呼ぶ。"""
        mp = self.spec.mountpoint
        ps = [mock.patch("replay.testvol.safety_check"),
              mock.patch("replay.testvol.up", return_value={"mount_args": []}),
              mock.patch("replay.testvol.umount", side_effect=lambda *a, **k: self.calls.append("umount")),
              mock.patch("replay.run.clear_cache", return_value=0),
              mock.patch("replay.run._seed_path", return_value=("/seed", {"inode": 3})),
              mock.patch("replay.run._make_clone", return_value=(self.clone, 5)),
              mock.patch("replay.run._volume_uuid", side_effect=lambda spec: self.uuid),
              mock.patch("replay.run.wait_deleted", side_effect=lambda *a, **k: self.calls.append("wait") or {}),
              mock.patch("replay.run.warm", side_effect=warm or (lambda *a: {"ranges": 0, "bytes": 0, "seconds": 0})),
              mock.patch("os.path.ismount", side_effect=lambda p: p == mp)]
        if subproc:
            ps.append(mock.patch("replay.run.subprocess.run", side_effect=subproc))
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return runmod.run(self.trace, "default", self.out, self.test, spec=self.spec,
                          up_kw=dict(prod=self.prod, prod_redis=tv.PROD_REDIS), log=lambda m: None,
                          prod_scrape=lambda: {})

    def _results(self):
        """run.json と play.json の status と reasons。"""
        with open(os.path.join(self.out, "run.json")) as f:
            rj = json.load(f)["replay"]
        with open(os.path.join(self.out, "play.json")) as f:
            pj = json.load(f)
        return rj, pj

    def test_keyerror_midway_failed_and_cleaned(self):
        def boom(*a):
            raise KeyError("x")
        res = self._run(warm=boom)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any("KeyError" in r for r in res["reasons"]))
        self.assertFalse(os.path.exists(self.clone))  # 複製は消える
        self.assertEqual(self.calls[-2:], ["wait", "umount"])
        rj, pj = self._results()
        self.assertEqual((rj["status"], pj["status"]), ("failed", "failed"))
        self.assertIn("traceback", rj)

    def test_timeout_in_measure_start_failed_and_cleaned(self):
        import subprocess

        def sp(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        res = self._run(subproc=sp)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any("RunError" in r and "start" in r for r in res["reasons"]), res["reasons"])
        self.assertFalse(os.path.exists(self.clone))
        self.assertEqual(self.calls[-1], "umount")
        self.assertEqual(self._results()[0]["status"], "failed")

    def test_measure_profile_points_to_volume_cache(self):
        import subprocess

        def sp(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        self._run(subproc=sp)  # 計測の start で止める（profiles.toml は書かれた後）
        prof = config.load_profile("test", os.path.join(self.out, "profiles.toml"))
        self.assertEqual(prof.cache_dir, os.path.join(self.spec.cache_dir, UUID))
        self.assertEqual(self._results()[0]["volume_uuid"], UUID)

    def test_missing_volume_uuid_fails_before_measure(self):
        self.uuid = None
        res = self._run()
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any("UUID" in r for r in res["reasons"]), res["reasons"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "profiles.toml")))
        self.assertEqual(self.calls[-1], "umount")  # 後始末（umount）はする

    def test_interrupt_reraised_after_cleanup(self):
        def intr(*a):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self._run(warm=intr)
        self.assertFalse(os.path.exists(self.clone))
        self.assertEqual(self.calls[-1], "umount")
        rj, pj = self._results()
        self.assertEqual(rj["status"], "failed")
        self.assertTrue(any("中断" in r for r in rj["reasons"]))

    def test_clone_timeout_is_runerror(self):
        import subprocess
        with mock.patch("replay.run.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 600)), \
                self.assertRaises(runmod.RunError):
            runmod.clone("/bin/true", "/a", os.path.join(self.d, "b", "c"))


class TestRunIntegration(unittest.TestCase):
    def test_run_local_storage(self):
        common = config.load_common()
        if not (os.access(common["test_binary"], os.X_OK) and os.access(common["redis_server"], os.X_OK)):
            self.skipTest("試験のバイナリか redis-server が無い")
        if not tv._have_fuse():
            self.skipTest("FUSE が使えない")
        prod, test = tv._profiles()
        root = tempfile.mkdtemp(prefix="jfs-run-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        spec = dataclasses.replace(
            testvol.spec_from_profile(test, common),
            redis_addr=("127.0.0.1", tv._free_port()), proxy_addr=("127.0.0.1", tv._free_port()),
            metrics_addr=("127.0.0.1", tv._free_port()),
            mountpoint=os.path.join(root, "mnt"), cache_dir=os.path.join(root, "cache"),
            state_dir=os.path.join(root, "state"), storage="file", bucket=os.path.join(root, "bucket") + "/")
        t = os.path.join(root, "small.tsv")
        ops = [_op(0, 11, "read", 3 * 64 * MiB + 5, 100), _op(50_000, 11, "write", 64 * MiB - 4096, 8192),
               _op(60_000, 12, "write", 2 * MiB, 4096), _op(100_000, 11, "fsync"), _op(150_000, 12, "flush")]
        with open(t, "w") as f:
            for o in ops:
                f.write(trace.format_op(o))
        up_kw = dict(prod=prod, prod_redis=tv.PROD_REDIS, prod_format=(4096, "zstd"))
        out = os.path.join(root, "out")
        try:
            info = seed.seed(t, test, spec=spec, up_kw=up_kw, log=lambda m: None)
            res = runmod.run(t, "default", out, test, spec=spec, up_kw=up_kw, settle_s=2, settle_max_s=60,
                             delete_quiet_s=1, poll_s=0.5, intervals={"metrics": 1, "llen": 5, "keys": 5},
                             log=lambda m: None, prod_scrape=lambda: {"juicefs_object_request_errors": 0.0})
            self.assertEqual(res["status"], "ok", res)
            names = os.listdir(out)
            snap = [n for n in names if n.startswith("keys-snapshot-")]
            self.assertEqual(len(snap), 1, names)
            with open(os.path.join(out, snap[0])) as f:  # 温めた block が volume のキャッシュ（<cache>/<UUID>）に見える
                self.assertGreaterEqual(len(f.read().splitlines()), 2)
            self.assertTrue(any(n.startswith("breakdown-start-") for n in names), names)
            self.assertFalse(any("内訳" in w for w in res["warnings"]), res["warnings"])
            for fn in ("run.json", "metrics.tsv", "play.json", "config.json"):
                self.assertTrue(os.path.exists(os.path.join(out, fn)), fn)
            with open(os.path.join(out, "play.json")) as f:
                play = json.load(f)
            self.assertEqual(play["errors"], [])
            self.assertEqual(play["ops_done"], 5)
            self.assertIn("write", play["latency"])
            self.assertIn(play["settle"]["state"], ("落ち着いた", "未収束"))
            with open(os.path.join(out, "llen.tsv")) as f:  # 複製の inode の chunk を試験の Redis（db 1）で数えている
                rows = [l.split("\t") for l in f.read().splitlines()]
            self.assertTrue(rows and rows[0][2] != "error" and int(rows[0][2]) >= 1, rows)
            with open(os.path.join(out, "run.json")) as f:
                rj = json.load(f)
            self.assertEqual(rj["replay"]["settings"]["name"], "default")
            self.assertFalse(os.path.exists(res["clone"]))  # clone は消える
            self.assertTrue(os.path.exists(info["path"]) or not os.path.ismount(spec.mountpoint))
            self.assertFalse(os.path.ismount(spec.mountpoint))  # 最後に mount を外す
            with resp.Resp(*spec.redis_addr, db=spec.db) as r:  # clone の chunk は消えている
                self.assertEqual(r.call("SCAN", 0, "MATCH", "c%d_*" % res["clone_inode"], "COUNT", 10000)[1], [])
        finally:
            testvol.cleanup(spec)


if __name__ == "__main__":
    unittest.main()
