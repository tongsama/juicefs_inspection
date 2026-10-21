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

    def test_play_many_threads_no_busy_wait(self):
        """163 pid の記録（初回の本番の trace と同じ規模）でも、待つ thread は眠って GIL を離すので、
        1 操作の待ち時間（一時ファイル）は小さく、予定からの遅れも ~20 ms に収まる
        （WSL の scheduler の揺れの分、上限は 25 ms とする）。"""
        ops = []
        for b in range(30):  # 50 ms ごとの山に、160 pid が 0.1 ms ずつずれて 1 操作ずつ（山の中では多くの thread が同時に待つ）
            for p in range(160):
                i = b * 160 + p
                kind = ("write", "read")[i % 2]
                ops.append(_op(b * 50_000 + p * 100, 1000 + p, kind, (i * 4096) % (4 * MiB), 4096))
        r = player.play(self.path, ops, time.monotonic() + 1.0)
        self.assertEqual(r.procs, 16)  # 160 pid を 16 の worker の process に分ける
        self.assertEqual(r.errors, [])
        self.assertEqual(r.ops_done, len(ops))
        self.assertLess(r.latency["write"]["p50_ms"], 1.0, r.latency)
        self.assertLess(r.latency["read"]["p50_ms"], 1.0, r.latency)
        self.assertLessEqual(r.max_lag_ms, 25.0)

    def test_play_simultaneous_bursts_across_processes(self):
        """160 pid が 20 µs 以内にほぼ同時に動く山（1 process の thread では GIL の取り直しで遅れた形）でも、
        worker の process に分けると、待ち時間の p50 は小さく、予定からの遅れは ~20 ms に収まる。"""
        ops = []
        for b in range(30):
            for p in range(160):
                i = b * 160 + p
                ops.append(_op(b * 50_000 + p * 20 // 160, 2000 + p, ("write", "read")[i % 2], (i * 4096) % (4 * MiB), 4096))
        r = player.play(self.path, ops, time.monotonic() + 1.0)
        self.assertEqual((r.errors, r.ops_done), ([], len(ops)))
        self.assertLess(r.latency["write"]["p50_ms"], 1.0, r.latency)
        self.assertLess(r.latency["read"]["p50_ms"], 1.0, r.latency)
        self.assertLess(r.latency["write"]["p99_ms"], 5.0, r.latency)  # 1 process の thread では ~10 ms（GIL の取り直し）
        self.assertLessEqual(r.lag["p99_ms"], 10.0, r.lag)
        self.assertLessEqual(r.max_lag_ms, 50.0)  # 160 操作が 20 µs に重なる山なので、最大は WSL の揺れで ~25 ms まで出る

    def test_error_in_one_process_stops_all(self):
        """1 つの worker の process のエラー（不正な fallocate の mode）で、ほかの process の pid もすぐ止まる。"""
        ops = [_op(i * 10_000, 1 + i % 8, "write", i * 4096, 4096) for i in range(300)]  # 3 秒、8 pid
        ops.append(_op(100_000, 99, "fallocate", 0, 4096, mode=0x7FFF))
        ops.sort(key=lambda o: o.start_us)
        t = time.monotonic()
        r = player.play(self.path, ops, time.monotonic() + 0.5, procs=4)
        self.assertTrue(r.failed)
        self.assertTrue(r.stopped)
        self.assertEqual(r.errors[0]["kind"], "fallocate")
        self.assertLess(r.ops_done, 100)  # 3 秒分（300 操作）を流し切らずに止まる
        self.assertLess(time.monotonic() - t, 2.5)

    def test_pid_groups_stable(self):
        """pid → process は安定な hash で決まり、同じ pid の Op は同じ組に順のまま入る。"""
        by_pid = {p: [p * 10 + i for i in range(3)] for p in range(50)}
        g1, g2 = player._groups(by_pid, 7), player._groups(by_pid, 7)
        self.assertEqual(g1, g2)
        self.assertEqual(sorted(p for g in g1 for p in g), list(range(50)))
        for g in g1:
            for p, seq in g.items():
                self.assertEqual(seq, by_pid[p])

    def test_wait_never_spins(self):
        """予定の時刻までは timeout 付きの wait で眠るだけで、timeout 0 の wait を繰り返して回らない
        （旧版は最後の 2 ms を stop.wait(0) で回し、163 thread が GIL を取り合った）。"""
        import threading
        calls = []

        class Counting(threading.Event):
            """wait の timeout を控える Event。"""
            def wait(self, timeout=None):
                calls.append(timeout)
                return super().wait(timeout)
        ops = [_op(i * 3_000, 1 + i % 20, "write", 0, 10) for i in range(200)]
        r = player.play(self.path, ops, time.monotonic() + 0.2, stop=Counting(), procs=0)  # 待ち方は worker でも同じ _sleep_until
        self.assertEqual(r.ops_done, 200)
        self.assertTrue(calls)
        self.assertGreaterEqual(min(calls), player.ISSUE_EARLY_S)  # 0 や極小の timeout で回さない
        self.assertLessEqual(len(calls), 3 * len(ops))  # 1 操作あたり数回（起き遅れの見直し）まで

    def test_stop_wakes_sleeping_threads(self):
        """遠い予定の操作を待っている thread も、stop が立てばすぐ止まる（眠っていても反応が遅れない）。"""
        import threading
        stop = threading.Event()
        ops = [_op(i * 1_000_000, 1 + i % 50, "write", 0, 10) for i in range(100)]  # 100 秒先まで
        threading.Timer(0.2, stop.set).start()
        t = time.monotonic()
        r = player.play(self.path, ops, time.monotonic() + 0.5, stop=stop)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertTrue(r.stopped)

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
            r = player.play(self.path, ops, time.monotonic() + 0.05, procs=0)  # mock はこの process だけに効く
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
            r = player.play(self.path, ops, time.monotonic(), procs=0)  # mock はこの process だけに効く
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
        g = runmod.Guard(lambda: dict(vals), log, mounted=lambda: True, unlogged_grace_s=0)
        vals["juicefs_object_request_errors"] = 3.0
        self.assertIn("object_request_errors", g.check())  # ログに無い誤りは説明できないので止める
        g = runmod.Guard(lambda: dict(vals), log, mounted=lambda: False)
        self.assertIn("mount", g.check())

    T0 = 1_800_000_000.0  # 試験の時計の基準（ログの時刻もこの時刻から作る）

    @staticmethod
    def _ts(t):
        """UNIX 秒 t を、juicefs のログの行頭の時刻（現地時刻、マイクロ秒）にする。"""
        return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(t)) + ".%06d" % round((t % 1) * 1e6)

    def _put_err(self, t, key):
        """retire で打ち切られた PUT の誤りの行（初回の実走の形。URL は含めない）。"""
        return ('%s juicefs[1] <WARNING>: slow request: PUT %s payload_bytes=26 (req_id: "", err: operation error S3: '
                'PutObject, exceeded maximum number of attempts, 1, https response error StatusCode: 0, RequestID: , '
                'HostID: , request send failed, cost: 30.0017s) [logRequest@cached_store.go:769]\n' % (self._ts(t), key))

    def _abandoned(self, t, key):
        """finishStagingUpload の abandoned の行。"""
        return ("%s juicefs[1] <INFO>: Key %s is not needed, abandoned, err: <nil> "
                "[finishStagingUpload@cached_store.go:1457]\n" % (self._ts(t), key))

    def _guard_env(self, errors=0.0, **kw):
        """一時のログ、差し替えた metrics と時計で Guard を作る。戻り値は (guard, log の path, metrics の dict, 時計の list)。"""
        d = tempfile.mkdtemp(prefix="jfs-guard-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        log = os.path.join(d, "juicefs.log")
        open(log, "w").close()
        vals = {"juicefs_object_request_errors": errors}
        now = [self.T0]
        g = runmod.Guard(lambda: dict(vals), log, mounted=lambda: True, clock=lambda: now[0], **kw)
        return g, log, vals, now

    def test_classify_errors(self):
        """abandoned で説明できる PUT の誤り、待ち中、説明できない誤り（abandoned が無い・遅すぎる・PUT 以外）を分ける。"""
        t = self.T0
        errs = [("PUT", "chunks/0/9/9604_0_4096", t), ("PUT", "chunks/0/8/8777_0_4096", t),
                ("PUT", "chunks/0/1/1_0_4096", t - 100), ("GET", "chunks/0/9/9604_0_4096", t - 100),
                ("PUT", "chunks/0/2/2_0_4096", t - 100)]
        ab = {"chunks/0/9/9604_0_4096": [t + 5.3], "chunks/0/2/2_0_4096": [t - 100 + 31]}
        c = runmod.classify_errors(errs, ab, now=t + 10, window_s=30)
        self.assertEqual((c["explained"], c["pending"], c["unexplained"]), (1, 1, 3))
        self.assertIn("chunks/0/1/1_0_4096", c["first_unexplained"])
        c = runmod.classify_errors(errs, ab, now=t + 31, window_s=30)  # 窓を過ぎても abandoned が来なければ説明できない
        self.assertEqual((c["explained"], c["pending"], c["unexplained"]), (1, 0, 4))

    def test_guard_ignores_abandoned_put_errors(self):
        """初回の実走の形: PUT の誤りで object_request_errors が 0 → 1 になり、5 秒後に同じ key が abandoned。止めない。"""
        g, log, vals, now = self._guard_env()
        self.assertIsNone(g.check())
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 1, "chunks/0/9/9604_0_4096"))
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 2
        self.assertIsNone(g.check())  # abandoned を待つ間は止めない
        with open(log, "a") as f:
            f.write(self._abandoned(self.T0 + 6.3, "chunks/0/9/9604_0_4096"))
        for dt in (7, 40, 100):
            now[0] = self.T0 + dt
            self.assertIsNone(g.check())
        c = g.counts()
        self.assertEqual((c["errors_total"], c["abandoned_explained"], c["unexplained"]), (1, 1, 0))

    def test_guard_stops_on_unexplained_put_error(self):
        """abandoned が窓（30 秒）の中に来ない PUT の誤りは、窓を過ぎたら止める。"""
        g, log, vals, now = self._guard_env()
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 1, "chunks/0/1/1_0_4096"))
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 20
        self.assertIsNone(g.check())
        now[0] = self.T0 + 32
        r = g.check()
        self.assertIn("説明できない", r)
        self.assertIn("chunks/0/1/1_0_4096", r)
        self.assertEqual(g.counts()["unexplained"], 1)

    def test_guard_tolerance(self):
        """説明できない誤りが許容（profiles.toml の object_error_tolerance）以下なら止めない。"""
        g, log, vals, now = self._guard_env(tolerance=1)
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 1, "chunks/0/1/1_0_4096"))
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 40
        self.assertIsNone(g.check())
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 41, "chunks/0/2/2_0_4096"))
        vals["juicefs_object_request_errors"] = 2.0
        now[0] = self.T0 + 80
        self.assertIn("説明できない", g.check())

    def test_guard_unlogged_metric_errors_stop(self):
        """metrics の誤りがログに出ない（遅くない誤りは slow request の行が無い）なら、猶予の後に説明できないとして止める。"""
        g, log, vals, now = self._guard_env(unlogged_grace_s=5)
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 1
        self.assertIsNone(g.check())
        now[0] = self.T0 + 7
        r = g.check()
        self.assertIn("説明できない", r)
        self.assertEqual(g.counts()["unlogged"], 1)

    def test_guard_rate_limit_still_stops_with_abandoned(self):
        """abandoned の行があっても、429／403 の rate limit の行が出たらすぐ止める。"""
        g, log, vals, now = self._guard_env()
        with open(log, "a") as f:
            f.write(self._abandoned(self.T0 + 1, "chunks/0/1/1_0_4096"))
            f.write("%s juicefs[1] <WARNING>: upload: 429 Too Many Requests\n" % self._ts(self.T0 + 1))
        self.assertIn("rate limit", g.check())

    def test_finalize_turns_pending_into_unexplained(self):
        """終わりの直前の PUT の誤り（abandoned が来ない）は、finalize が窓の終わりまで待ってから説明できない誤りにする。"""
        g, log, vals, now = self._guard_env()
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 1, "chunks/0/1/1_0_4096"))
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 2
        self.assertIsNone(g.check())
        self.assertEqual(g.counts()["pending"], 1)
        slept = []

        def sleep(d):
            slept.append(d)
            now[0] += d
        r = g.finalize(poll_s=1.0, sleep=sleep)
        self.assertIn("説明できない", r)
        self.assertGreaterEqual(now[0], self.T0 + 1 + runmod.ABANDON_WINDOW_S)  # 窓が過ぎるまで待った
        self.assertLessEqual(now[0], self.T0 + 2 + runmod.ABANDON_WINDOW_S + runmod.UNLOGGED_GRACE_S + 1.0)  # 上限あり
        c = g.counts()
        self.assertEqual((c["pending"], c["unexplained"], c["finalized"]), (0, 1, True))
        self.assertIsNone(g.finalize())  # 2 回目は何もしない

    def test_finalize_waits_for_late_abandoned(self):
        """窓の中に abandoned が来れば、finalize は説明できる誤りとして止めない（その時点で待ちを終える）。"""
        g, log, vals, now = self._guard_env()
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 1, "chunks/0/9/9604_0_4096"))
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 2

        def sleep(d):
            now[0] += d
            if now[0] >= self.T0 + 8:
                with open(log, "a") as f:
                    f.write(self._abandoned(self.T0 + 7, "chunks/0/9/9604_0_4096"))
        self.assertIsNone(g.finalize(poll_s=1.0, sleep=sleep))
        self.assertLess(now[0], self.T0 + 20)
        c = g.counts()
        self.assertEqual((c["abandoned_explained"], c["unexplained"]), (1, 0))

    def test_finalize_without_wait_and_unlogged(self):
        """待たない finalize（失敗の後）でも、待ち中とログに行の無い最後の誤りを説明できない誤りに数える。許容以内なら理由は無い。"""
        g, log, vals, now = self._guard_env(tolerance=2)
        with open(log, "a") as f:
            f.write(self._put_err(self.T0 + 1, "chunks/0/1/1_0_4096"))
        vals["juicefs_object_request_errors"] = 2.0  # 1 件はログに行が無い
        now[0] = self.T0 + 2
        self.assertIsNone(g.finalize(wait=False))
        c = g.counts()
        self.assertEqual((c["unexplained"], c["unlogged"], c["pending"]), (2, 1, 0))

    def _err_line(self, t, method, key, code=500):
        """PUT／GET 以外も含む、誤りの slow request の行（実の確かめの DELETE の HTTP 500 の形。URL は含めない）。"""
        return ('%s juicefs[1] <WARNING>: slow request: %s %s (req_id: "2313DB8D03EB12F4", err: operation error S3: '
                'DeleteObject, exceeded maximum number of attempts, 1, https response error StatusCode: %d, cost: 12.1s) '
                '[logRequest@cached_store.go:769]\n' % (self._ts(t), method, key, code))

    def test_classify_delete_is_warned(self):
        """PUT／GET 以外（DELETE など）の誤りは warned に数え、説明できない誤りに入れない。GET は説明できない。"""
        t = self.T0
        c = runmod.classify_errors([("DELETE", "chunks/0/12/12497_0_8192", t), ("HEAD", "x", t), ("GET", "y", t)], {},
                                   now=t + 100)
        self.assertEqual((c["warned"], c["unexplained"]), (2, 1))
        self.assertIn("DELETE chunks/0/12/12497_0_8192", c["first_warned"])

    def test_guard_warns_delete_errors(self):
        """DELETE の HTTP 500 で object_request_errors が増えても止めない（finalize でも）。数は counts の warned。"""
        g, log, vals, now = self._guard_env()
        with open(log, "a") as f:
            f.write(self._err_line(self.T0 + 1, "DELETE", "chunks/0/12/12497_0_8192"))
        vals["juicefs_object_request_errors"] = 1.0
        for dt in (2, 40):
            now[0] = self.T0 + dt
            self.assertIsNone(g.check())
        self.assertIsNone(g.finalize(wait=False))
        c = g.counts()
        self.assertEqual((c["warned"], c["unexplained"], c["unlogged"]), (1, 0, 0))

    def test_guard_get_error_still_stops(self):
        """GET の誤りは（abandoned で説明できないので）止める。"""
        g, log, vals, now = self._guard_env()
        with open(log, "a") as f:
            f.write(self._err_line(self.T0 + 1, "GET", "chunks/0/1/1_0_4096"))
        vals["juicefs_object_request_errors"] = 1.0
        now[0] = self.T0 + 2
        self.assertIn("GET chunks/0/1/1_0_4096", g.check())

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

    def _run(self, warm=None, subproc=None, head_s=None, poll_s=5.0):
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
              mock.patch("replay.run.ensure_warm", return_value=({"uuid": UUID, "warm": {"ranges": 0}}, False)),
              mock.patch("replay.warmcache.restore",
                         return_value={"uuid": UUID, "files": 0, "bytes": 0, "skipped": 0, "seconds": 0}),
              mock.patch("os.path.ismount", side_effect=lambda p: p == mp)]
        if warm:  # 複製を作った後（計測のプロファイルを書くところ）で失敗させる
            ps.append(mock.patch("replay.run.write_measure_profiles", side_effect=warm))
        if subproc:
            ps.append(mock.patch("replay.run.subprocess.run", side_effect=subproc))
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return runmod.run(self.trace, "default", self.out, self.test, spec=self.spec,
                          up_kw=dict(prod=self.prod, prod_redis=tv.PROD_REDIS), log=lambda m: None,
                          prod_scrape=lambda: {}, head_s=head_s, poll_s=poll_s)

    def _results(self):
        """run.json と play.json の status と reasons。"""
        with open(os.path.join(self.out, "run.json")) as f:
            rj = json.load(f)["replay"]
        with open(os.path.join(self.out, "play.json")) as f:
            pj = json.load(f)
        return rj, pj

    def test_keyerror_midway_failed_and_cleaned(self):
        def boom(*a, **k):
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
        def intr(*a, **k):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self._run(warm=intr)
        self.assertFalse(os.path.exists(self.clone))
        self.assertEqual(self.calls[-1], "umount")
        rj, pj = self._results()
        self.assertEqual(rj["status"], "failed")
        self.assertTrue(any("中断" in r for r in rj["reasons"]))

    def test_run_records_counts_head_and_warm(self):
        """run.json に、誤りの内訳（object_errors）、--head-seconds と流した操作の数、温めたキャッシュの写しの情報が残る。"""
        import subprocess

        def sp(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with open(self.trace, "a") as f:
            f.write(trace.format_op(_op(5_000_000, 1, "write", 0, 10)))  # 5 秒後（--head-seconds 1 では流さない）
        self._run(subproc=sp, head_s=1)
        rj = self._results()[0]
        self.assertEqual((rj["head_seconds"], rj["ops"], rj["ops_total"]), (1, 1, 2))
        self.assertEqual(rj["warm_cache"]["restore"]["uuid"], UUID)
        self.assertFalse(rj["warm_cache"]["created"])

    def test_warm_copy_for_other_volume_fails(self):
        """戻した写しの UUID が mount した volume と違えば（volume を作り直した）、作り直しを促して失敗にする。"""
        self.uuid = "73e74f7b-0000-4155-adea-d9ef1e669c99"
        res = self._run()
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any("warm --force" in r for r in res["reasons"]), res["reasons"])
        self.assertEqual(self.calls[-1], "umount")

    def _run_with_late_put_error(self, tolerance, method="PUT"):
        """流し直しの終わりの直前に PUT の誤りが出て abandoned が来ない回を、計測・待ち・metrics を偽にして流す。"""
        log = self.spec.path("juicefs.log")
        os.makedirs(self.spec.state_dir, exist_ok=True)
        open(log, "w").close()
        errs = {"juicefs_object_request_errors": 0.0}
        real_play = player.play

        def play(path, ops, t0, stop=None):
            r = real_play(path, ops, t0, stop=stop)
            ts = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime()) + ".000000"
            with open(log, "a") as f:  # 終わりの直前の PUT の誤り（abandoned の行は来ない）
                f.write('%s juicefs[1] <WARNING>: slow request: %s chunks/0/1/1_0_4096 payload_bytes=26 '
                        '(req_id: "", err: operation error S3: PutObject, cost: 30.0s) [logRequest@cached_store.go:769]\n'
                        % (ts, method))
            errs["juicefs_object_request_errors"] = 1.0
            return r
        common = dict(config.load_common(), abandon_window_seconds=0.5, object_error_tolerance=tolerance)
        ps = [mock.patch("replay.run.player.play", side_effect=play),
              mock.patch("replay.run.config.load_common", return_value=common),
              mock.patch("replay.run.UNLOGGED_GRACE_S", 0.2),
              mock.patch("replay.run.metrics_io.scrape", side_effect=lambda *a, **k: dict(errs)),
              mock.patch("replay.run._mcli", return_value=""),
              mock.patch("replay.run.settle", return_value={"state": "未収束", "seconds": 1}),
              mock.patch("replay.run.mcap.capture", return_value=[])]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return self._run(poll_s=0.1)

    def test_late_put_error_without_abandoned_fails_run(self):
        """流し直しの終わりの直前の PUT の誤り（abandoned なし）は、待ちが未収束で終わっても確定して、その回を失敗にする。"""
        res = self._run_with_late_put_error(tolerance=0)
        self.assertEqual(res["status"], "failed", res)
        self.assertTrue(any("説明できない" in r for r in res["reasons"]), res["reasons"])
        rj = self._results()[0]
        self.assertEqual((rj["object_errors"]["unexplained"], rj["object_errors"]["pending"]), (1, 0))
        self.assertTrue(rj["object_errors"]["finalized"])

    def test_late_put_error_within_tolerance_is_warned(self):
        """許容（object_error_tolerance）以内なら失敗にしないが、warnings に残す。"""
        res = self._run_with_late_put_error(tolerance=1)
        self.assertEqual(res["status"], "ok", res)
        self.assertTrue(any("説明できない誤りが 1 件" in w for w in res["warnings"]), res["warnings"])

    def test_late_delete_error_is_warned_not_failed(self):
        """流し直しの終わりの直前の DELETE の誤りは、その回を失敗にせず warnings に残す（Ruling 13）。"""
        res = self._run_with_late_put_error(tolerance=0, method="DELETE")
        self.assertEqual(res["status"], "ok", res)
        self.assertTrue(any("PUT／GET 以外の誤り" in w and "1 件" in w for w in res["warnings"]), res["warnings"])
        rj = self._results()[0]
        self.assertEqual((rj["object_errors"]["warned"], rj["object_errors"]["unexplained"]), (1, 0))

    def test_clone_timeout_is_runerror(self):
        import subprocess
        with mock.patch("replay.run.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 600)), \
                self.assertRaises(runmod.RunError):
            runmod.clone("/bin/true", "/a", os.path.join(self.d, "b", "c"))


class TestRunIntegration(unittest.TestCase):
    def test_run_local_storage(self):
        common = config.load_common()
        if not os.access(common["test_binary"], os.X_OK):
            self.skipTest("試験のバイナリが無い")
        if not tv._have_docker():
            self.skipTest("docker compose が使えない")
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
            state_dir=os.path.join(root, "state"), storage="file", bucket=os.path.join(root, "bucket") + "/",
            compose_project=tv._unique_project())
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
            # 1 回目は温めたキャッシュの写しを作り（温めの数が残る）、2 回目はそれを戻すだけ（温め直さない）
            self.assertTrue(rj["replay"]["warm_cache"]["created"])
            self.assertGreaterEqual(rj["replay"]["warm"]["ranges"], 1)
            self.assertGreaterEqual(rj["replay"]["warm_cache"]["restore"]["files"], 1)
            self.assertEqual(rj["replay"]["object_errors"]["unexplained"], 0)
            out2 = os.path.join(root, "out2")
            with mock.patch("replay.run.warm", side_effect=AssertionError("2 回目は温め直さない")):
                res2 = runmod.run(t, "default", out2, test, spec=spec, up_kw=up_kw, settle_s=2, settle_max_s=60,
                                  delete_quiet_s=1, poll_s=0.5, intervals={"metrics": 1, "llen": 5, "keys": 5},
                                  log=lambda m: None, prod_scrape=lambda: {"juicefs_object_request_errors": 0.0},
                                  head_s=0.055)
            self.assertEqual(res2["status"], "ok", res2)
            self.assertFalse(res2["warm_cache"]["created"])
            self.assertGreaterEqual(res2["warm_cache"]["restore"]["files"], 1)
            with open(os.path.join(out2, "play.json")) as f:
                self.assertEqual(json.load(f)["ops_done"], 2)  # --head-seconds 0.055: 最初の 2 操作だけ
            snap = [n for n in os.listdir(out2) if n.startswith("keys-snapshot-")]
            with open(os.path.join(out2, snap[0])) as f:  # 戻した block が volume のキャッシュに見える
                self.assertGreaterEqual(len(f.read().splitlines()), 2)
        finally:
            testvol.cleanup(spec)


if __name__ == "__main__":
    unittest.main()
