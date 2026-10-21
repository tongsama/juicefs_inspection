"""種のファイル（jfsreplay seed）の試験。結合の試験は local の file storage と一時の port だけを使う。"""
import dataclasses
import json
import os
import shutil
import struct
import tempfile
import unittest
import zlib

from lib import config
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

    def test_slice_sizes(self):
        raw = [struct.pack(">IQIII", 0, 1, 4 * MiB, 0, 4 * MiB), struct.pack(">IQIII", 0, 2, 5 * MiB, 0, 1)]
        self.assertEqual(seed.slice_sizes(raw), [4 * MiB, 5 * MiB])
        with self.assertRaises(ValueError):
            seed.slice_sizes([b"x"])


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
        finally:
            testvol.cleanup(spec)


if __name__ == "__main__":
    unittest.main()
