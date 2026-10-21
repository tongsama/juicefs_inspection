"""種のファイル（jfsreplay seed）の試験。結合の試験は local の file storage と一時の port だけを使う。"""
import dataclasses
import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock
import zlib

from lib import config, resp
from replay import accesslog, seed, testvol, trace
import test_testvol as tv

MiB = 1 << 20


def _op(kind, off, size):
    """試験用の Op。"""
    return accesslog.Op(0, 1, 1, kind, off, size, 0, 1, "OK")


class TestSeed(unittest.TestCase):
    def test_chunks_from_trace(self):
        ops = [_op("write", 64 * MiB - 4096, 8192), _op("read", 5 * 64 * MiB + 10, 4096), _op("fsync", 900 * MiB, 0)]
        self.assertEqual(seed.chunks(ops), [0, 1, 5])

    def test_content_deterministic_compressible(self):
        a = seed.content(10 * MiB, 4 * MiB)
        self.assertEqual(a, seed.content(10 * MiB, 4 * MiB))
        self.assertEqual(seed.content(10 * MiB + 100, 50), a[100:150])
        self.assertLess(len(zlib.compress(a)) * 50, len(a))

    def test_disk_length_and_parse_size(self):
        self.assertEqual(seed.disk_length([_op("write", 64 * MiB, 1)]), 128 * MiB)
        self.assertEqual(seed.disk_length([_op("write", 0, 1)], 1 << 30), 1 << 30)
        self.assertEqual(seed.parse_size("32G"), 32 << 30)

    def test_seed_limit_refuses(self):
        with self.assertRaises(seed.SeedError):
            seed.check_limit([0, 1, 2], 2 * 64 * MiB)
        seed.check_limit([0, 1], 2 * 64 * MiB)
        # seed() は mount を立てる前に止まる（存在しない binary でも SafetyError／RuntimeError でなく SeedError）
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        t = os.path.join(d, "t.tsv")
        with open(t, "w") as f:
            f.write(trace.format_op(_op("write", 0, 4096)) + trace.format_op(_op("write", 64 * MiB, 4096)))
        spec = dataclasses.replace(testvol.spec_from_profile(config.load_profile("test")), binary="/nonexistent")
        with self.assertRaises(seed.SeedError):
            seed.seed(t, config.load_profile("test"), spec=spec, limit=64 * MiB)

    def test_slice_records_and_shape(self):
        raw = [struct.pack(">IQIII", 0, 1, 4 * MiB, 0, 4 * MiB), struct.pack(">IQIII", 0, 2, 5 * MiB, 0, 1)]
        self.assertEqual(seed.slice_records(raw)[1], (0, 2, 5 * MiB, 0, 1))
        with self.assertRaises(ValueError):
            seed.slice_records([b"x"])
        ok = [(i * 4 * MiB, 1, 4 * MiB, 0, 4 * MiB) for i in range(16)]
        self.assertIsNone(seed.shape_problem(ok, 0, 64 * MiB))
        self.assertIsNone(seed.shape_problem(ok[:2], 0, 8 * MiB))  # 長さで切れる chunk
        self.assertIsNotNone(seed.shape_problem(ok[:15], 0, 64 * MiB))  # 個数が足りない
        self.assertIsNotNone(seed.shape_problem([(0, 1, 64 * MiB, 0, 64 * MiB)], 0, 64 * MiB))
        self.assertIsNotNone(seed.shape_problem([(0, 1, 4 * MiB, 5, 4 * MiB)] + ok[1:], 0, 64 * MiB))  # off != 0
        self.assertIsNotNone(seed.shape_problem([(MiB, 1, 4 * MiB, 0, 4 * MiB)] + ok[1:], 0, 64 * MiB))  # pos がずれ

    def test_seed_safety_check_even_if_mounted(self):
        """本番の mountpoint を指す Spec は、mount 済みに見えても何も書く前に SafetyError。"""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        t = os.path.join(d, "t.tsv")
        with open(t, "w") as f:
            f.write(trace.format_op(_op("write", 0, 4096)))
        prod, test = tv._profiles()
        spec = dataclasses.replace(testvol.spec_from_profile(test), mountpoint=prod.mountpoint)
        with mock.patch("os.path.ismount", return_value=True), mock.patch.object(seed, "write_seed") as w:
            with self.assertRaises(testvol.SafetyError):
                seed.seed(t, test, spec=spec, up_kw=dict(prod=prod, prod_redis=tv.PROD_REDIS))
            w.assert_not_called()

    def test_seed_rejects_chunk_beyond_length(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        t = os.path.join(d, "t.tsv")
        with open(t, "w") as f:
            f.write(trace.format_op(_op("write", 200 * MiB, 4096)))
        prod, test = tv._profiles()
        with self.assertRaises(seed.SeedError):
            seed.seed(t, test, length=64 * MiB, up_kw=dict(prod=prod, prod_redis=tv.PROD_REDIS))


    def _seed_with_fakes(self, scrape, flush_max_s):
        """mount・書き込み・compact・LRANGE を偽にして seed を呼び、(結果か例外, log, seed.json の path) を返す。"""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        t = os.path.join(d, "t.tsv")
        with open(t, "w") as f:
            f.write(trace.format_op(_op("write", 0, 4096)))
        prod, test = tv._profiles()
        spec = dataclasses.replace(testvol.spec_from_profile(test), mountpoint=os.path.join(d, "mnt"))

        def fake_write(path, cl, length):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        logs = []
        ps = [mock.patch("replay.testvol.safety_check"), mock.patch("replay.testvol.up"),
              mock.patch("os.path.ismount", return_value=False), mock.patch.object(seed, "write_seed", fake_write),
              mock.patch("replay.seed.subprocess.run", return_value=mock.Mock(returncode=0)),
              mock.patch.object(seed, "verify_block_shape", return_value=[])]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        try:
            r = seed.seed(t, test, spec=spec, up_kw=dict(prod=prod, prod_redis=tv.PROD_REDIS), log=logs.append,
                          scrape=scrape, flush_max_s=flush_max_s, poll_s=0)
        except seed.SeedError as e:
            r = e
        return r, logs, os.path.join(d, "seed.json")

    def test_seed_waits_for_upload(self):
        vals = iter([{"juicefs_staging_blocks": 5, "juicefs_object_request_uploading": 2}] * 3 +
                    [{"juicefs_staging_blocks": 0, "juicefs_object_request_uploading": 0}] * 2)
        r, logs, sj = self._seed_with_fakes(lambda: next(vals), 3600)
        self.assertIsInstance(r, dict)
        self.assertTrue(any("staging_blocks=5" in m for m in logs), logs)  # 進み具合を出す
        self.assertTrue(any("書き出しが終わった" in m for m in logs), logs)
        self.assertTrue(os.path.exists(sj))

    def test_seed_flush_timeout_is_clear_error(self):
        r, logs, sj = self._seed_with_fakes(lambda: {"juicefs_staging_blocks": 1, "juicefs_object_request_uploading": 0}, 0)
        self.assertIsInstance(r, seed.SeedError)
        self.assertIn("書き出し", str(r))
        self.assertTrue(os.path.exists(sj))  # 種はできている（書き出しを待ってから run する）


class TestSeedIntegration(unittest.TestCase):
    def test_seed_local_storage(self):
        common = config.load_common()
        if not (os.access(common["test_binary"], os.X_OK) and os.access(common["redis_server"], os.X_OK)):
            self.skipTest("試験のバイナリか redis-server が無い")
        if not tv._have_fuse():
            self.skipTest("FUSE が使えない")
        prod, test = tv._profiles()
        root = tempfile.mkdtemp(prefix="jfs-seed-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        spec = dataclasses.replace(
            testvol.spec_from_profile(test, common),
            redis_addr=("127.0.0.1", tv._free_port()), proxy_addr=("127.0.0.1", tv._free_port()),
            metrics_addr=("127.0.0.1", tv._free_port()),
            mountpoint=os.path.join(root, "mnt"), cache_dir=os.path.join(root, "cache"),
            state_dir=os.path.join(root, "state"), storage="file", bucket=os.path.join(root, "bucket") + "/")
        t = os.path.join(root, "small.tsv")
        with open(t, "w") as f:
            for o in (_op("write", 64 * MiB - 4096, 8192), _op("read", 3 * 64 * MiB + 5, 100)):
                f.write(trace.format_op(o))
        up_kw = dict(prod=prod, prod_redis=tv.PROD_REDIS, prod_format=(4096, "zstd"))
        try:
            info = seed.seed(t, test, spec=spec, up_kw=up_kw, log=lambda m: None)
            self.assertEqual(info["chunks"], [0, 1, 3])
            self.assertEqual(info["length"], 4 * 64 * MiB)
            self.assertEqual(os.stat(info["path"]).st_size, 4 * 64 * MiB)
            with open(os.path.join(root, "seed.json")) as f:
                self.assertEqual(json.load(f)["inode"], os.stat(info["path"]).st_ino)
            with open(info["path"], "rb") as f:
                f.seek(64 * MiB - 4096)
                self.assertEqual(f.read(8192), seed.content(64 * MiB - 4096, 8192))
            self.assertEqual(testvol.read_mount_config(spec.mountpoint)["Meta"]["KazCompactionMode"], "block")
            with resp.Resp(*spec.redis_addr, db=spec.db) as r:  # 各 chunk は 4 MiB の block ごとに 1 record
                for c in info["chunks"]:
                    recs = sorted(seed.slice_records(r.call("LRANGE", "c%d_%d" % (info["inode"], c), 0, -1)))
                    self.assertEqual([x[0] for x in recs], [i * 4 * MiB for i in range(16)])
                    self.assertTrue(all(x[2] == x[4] == 4 * MiB and x[3] == 0 for x in recs))
        finally:
            testvol.cleanup(spec)


if __name__ == "__main__":
    unittest.main()
