"""温めたキャッシュの写し（replay.warmcache と run.ensure_warm）の試験。

一時のディレクトリだけを使い、本番の mount・キャッシュ・Redis・バケットと、既存の試験の volume には触れない。
ensure_warm の試験は mount・clone・温めを偽にする（Drive にも FUSE にも触れない）。"""
import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from replay import accesslog, run as runmod, testvol, trace, warmcache
import test_testvol as tv

UUID = "73e74f7b-0000-4155-adea-d9ef1e669c21"


class WarmCacheTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="jfs-warm-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.d = d
        self.prod, self.test = tv._profiles()
        self.spec = dataclasses.replace(
            testvol.spec_from_profile(self.test), mountpoint=os.path.join(d, "mnt"),
            cache_dir=os.path.join(d, "cache"), state_dir=os.path.join(d, "state"), storage="file",
            bucket=os.path.join(d, "bucket") + "/")
        self.vol = os.path.join(self.spec.cache_dir, UUID)
        self.root = warmcache.warm_root(self.spec)
        self.dest = os.path.join(self.root, "t")
        self.kw = dict(mountpoint=self.spec.mountpoint, prod=self.prod, state_dir=self.spec.state_dir, root=self.root)

    def _fill(self):
        """温まった volume のキャッシュの形を作る（raw の block、kaz-2q の状態、空の rawstaging）。"""
        os.makedirs(os.path.join(self.vol, "raw", "chunks", "0", "0"))
        os.makedirs(os.path.join(self.vol, "rawstaging", "chunks", "0"))
        with open(os.path.join(self.vol, "raw", "chunks", "0", "0", "1_0_4096"), "wb") as f:
            f.write(b"a" * 4096)
        with open(os.path.join(self.vol, "kaz-cache-state.v1"), "wb") as f:
            f.write(b"state")

    def test_save_and_restore_plain_copy(self):
        """写して戻すと中身が同じで、写しとは別のファイル（hardlink でない）。戻した先を書き換えても写しは変わらない。"""
        self._fill()
        meta = warmcache.save(self.spec.cache_dir, UUID, self.dest, {"seed": {"inode": 3}}, **self.kw)
        self.assertEqual((meta["files"], meta["uuid"], meta["seed"]), (2, UUID, {"inode": 3}))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "cache", "rawstaging")))  # rawstaging は写さない
        self.assertEqual(warmcache.load_meta(self.dest)["bytes"], 4096 + 5)
        shutil.rmtree(os.path.join(self.vol, "raw"))  # clear_cache の後の形（rawstaging だけ残る）
        os.unlink(os.path.join(self.vol, "kaz-cache-state.v1"))
        rs = warmcache.restore(self.dest, self.spec.cache_dir, **self.kw)
        self.assertEqual((rs["uuid"], rs["files"]), (UUID, 2))
        blk = os.path.join(self.vol, "raw", "chunks", "0", "0", "1_0_4096")
        saved = os.path.join(self.dest, "cache", "raw", "chunks", "0", "0", "1_0_4096")
        with open(blk, "rb") as f:
            self.assertEqual(f.read(), b"a" * 4096)
        self.assertNotEqual(os.stat(blk).st_ino, os.stat(saved).st_ino)
        self.assertEqual(os.stat(blk).st_nlink, 1)
        with open(blk, "wb") as f:
            f.write(b"changed")
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"a" * 4096)
        with open(os.path.join(self.vol, "kaz-cache-state.v1"), "rb") as f:  # kaz-2q の状態も戻る
            self.assertEqual(f.read(), b"state")

    def test_save_replaces_old_copy(self):
        """2 回目の写しは前の写しを置き換え、一時のディレクトリを残さない。"""
        self._fill()
        warmcache.save(self.spec.cache_dir, UUID, self.dest, {"seed": 1}, **self.kw)
        os.unlink(os.path.join(self.vol, "kaz-cache-state.v1"))
        meta = warmcache.save(self.spec.cache_dir, UUID, self.dest, {"seed": 2}, **self.kw)
        self.assertEqual((meta["files"], warmcache.load_meta(self.dest)["seed"]), (1, 2))
        self.assertEqual(os.listdir(self.root), ["t"])

    def test_save_refuses_staging_and_mount(self):
        """rawstaging にファイルがあるか、試験の mount が上がっていれば写さない（写しも作らない）。"""
        self._fill()
        with open(os.path.join(self.vol, "rawstaging", "chunks", "0", "9_0_4096"), "wb") as f:
            f.write(b"x")
        with self.assertRaises(runmod.RunError):
            warmcache.save(self.spec.cache_dir, UUID, self.dest, {}, **self.kw)
        self.assertFalse(os.path.exists(self.dest))
        os.unlink(os.path.join(self.vol, "rawstaging", "chunks", "0", "9_0_4096"))
        mp = self.spec.mountpoint
        with mock.patch("os.path.ismount", side_effect=lambda p: p == mp), self.assertRaises(runmod.RunError):
            warmcache.save(self.spec.cache_dir, UUID, self.dest, {}, **self.kw)
        with self.assertRaises(testvol.SafetyError):  # UUID の形でない名前は扱わない
            warmcache.save(self.spec.cache_dir, "../x", self.dest, {}, **self.kw)

    def test_restore_refuses_nonempty_and_mount(self):
        """戻す先に rawstaging 以外があるか、mount が上がっていれば戻さない。"""
        self._fill()
        warmcache.save(self.spec.cache_dir, UUID, self.dest, {}, **self.kw)
        with self.assertRaises(runmod.RunError):  # 空にしていない
            warmcache.restore(self.dest, self.spec.cache_dir, **self.kw)
        shutil.rmtree(self.vol)
        mp = self.spec.mountpoint
        with mock.patch("os.path.ismount", side_effect=lambda p: p == mp), self.assertRaises(runmod.RunError):
            warmcache.restore(self.dest, self.spec.cache_dir, **self.kw)
        self.assertEqual(warmcache.restore(self.dest, self.spec.cache_dir, **self.kw)["files"], 2)  # 無ければ作って戻す

    def test_symlinks_not_followed(self):
        """キャッシュの中の symlink はたどらず写さない（外のファイルを写しに入れない）。"""
        self._fill()
        outside = os.path.join(self.d, "secret")
        with open(outside, "w") as f:
            f.write("x")
        os.symlink(outside, os.path.join(self.vol, "raw", "link"))
        os.symlink(self.d, os.path.join(self.vol, "dirlink"))
        meta = warmcache.save(self.spec.cache_dir, UUID, self.dest, {}, **self.kw)
        self.assertEqual((meta["files"], meta["skipped"]), (2, 2))
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "cache", "raw", "link")))

    def test_check_warm_dir_refuses_bad_places(self):
        """写しの場所は warm-cache の下だけ。根そのもの、外、試験のキャッシュ・本番のキャッシュとの重なりは止める。"""
        c = dict(cache_dir=self.spec.cache_dir, mountpoint=self.spec.mountpoint, prod=self.prod)
        self.assertTrue(warmcache.check_warm_dir(self.dest, self.root, **c).endswith("/t"))
        for bad in (self.root, os.path.join(self.d, "elsewhere"), os.path.join(self.root, "..", "x"), "rel/x", ""):
            with self.assertRaises(testvol.SafetyError, msg=bad):
                warmcache.check_warm_dir(bad, self.root, **c)
        with self.assertRaises(testvol.SafetyError):  # 試験のキャッシュの下に写しの根がある
            warmcache.check_warm_dir(os.path.join(self.spec.cache_dir, "w", "t"), os.path.join(self.spec.cache_dir, "w"), **c)
        prod = dataclasses.replace(self.prod, cache_dir=self.root)
        with self.assertRaises(testvol.SafetyError):  # 本番のキャッシュと重なる
            warmcache.check_warm_dir(self.dest, self.root, cache_dir=self.spec.cache_dir,
                                     mountpoint=self.spec.mountpoint, prod=prod)
        os.makedirs(self.root)
        os.symlink(self.spec.cache_dir, os.path.join(self.root, "evil"))
        with self.assertRaises(testvol.SafetyError):  # symlink で試験のキャッシュを指す
            warmcache.check_warm_dir(os.path.join(self.root, "evil"), self.root, **c)

    def _seed_files(self, inode=3):
        """trace と seed.json を作り、(trace の path, seed の info) を返す。"""
        t = os.path.join(self.d, "t.tsv")
        if not os.path.exists(t):
            with open(t, "w") as f:
                f.write(trace.format_op(accesslog.Op(0, 1, 1, "read", 0, 4096, 0, 1, "OK")))
        info = {"name": "t", "inode": inode, "path": "/m/seed/t.img", "mountpoint": "/m", "length": 1 << 26}
        with open(os.path.join(self.d, "seed.json"), "w") as f:
            json.dump(info, f)
        return t, info

    def test_identity_invalidated_when_seed_recreated(self):
        """種を作り直す（seed.json の書き直し、inode が変わる）と写しは使えない。同じなら使える。"""
        t, info = self._seed_files()
        ident = warmcache.seed_identity(t, info)
        self._fill()
        warmcache.save(self.spec.cache_dir, UUID, self.dest, {"seed": ident}, **self.kw)
        meta = warmcache.load_meta(self.dest)
        self.assertEqual(warmcache.is_valid(meta, warmcache.seed_identity(t, info), self.dest), (True, "使える"))
        time.sleep(0.01)
        t, info = self._seed_files(inode=3)  # 同じ中身で作り直しても（mtime が変わる）使わない
        ok, why = warmcache.is_valid(meta, warmcache.seed_identity(t, info), self.dest)
        self.assertFalse(ok)
        self.assertIn("種", why)
        t, info = self._seed_files(inode=9)
        self.assertFalse(warmcache.is_valid(meta, warmcache.seed_identity(t, info), self.dest)[0])
        self.assertFalse(warmcache.is_valid(None, ident, self.dest)[0])

    def test_space_shortage_is_error(self):
        """空きが足りなければ RunError（容量不足を隠さない）。"""
        self._fill()
        fake = shutil._ntuple_diskusage(100, 100, 10)
        with mock.patch("replay.warmcache.shutil.disk_usage", return_value=fake), self.assertRaises(runmod.RunError) as cm:
            warmcache.save(self.spec.cache_dir, UUID, self.dest, {}, **self.kw)
        self.assertIn("容量", str(cm.exception))
        self.assertFalse(os.path.exists(self.dest))


    def test_save_failure_cleans_tmp_and_keeps_old(self):
        """写しの途中の失敗では、作りかけ（.tmp-<pid>）を消し、前の写しはそのまま残す。"""
        self._fill()
        warmcache.save(self.spec.cache_dir, UUID, self.dest, {"seed": 1}, **self.kw)

        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        with mock.patch("replay.warmcache.shutil.copy2", side_effect=boom), self.assertRaises(OSError):
            warmcache.save(self.spec.cache_dir, UUID, self.dest, {"seed": 2}, **self.kw)
        self.assertEqual(os.listdir(self.root), ["t"])
        self.assertEqual(warmcache.load_meta(self.dest)["seed"], 1)


class EnsureWarmTest(unittest.TestCase):
    """ensure_warm: 使える写しは使い、無いか種が変わっていれば、既定の設定で温めてから写す（mount 等は偽）。"""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="jfs-ensure-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.d = d
        self.prod, self.test = tv._profiles()
        self.spec = dataclasses.replace(
            testvol.spec_from_profile(self.test), mountpoint=os.path.join(d, "mnt"),
            cache_dir=os.path.join(d, "cache"), state_dir=os.path.join(d, "state"), storage="file",
            bucket=os.path.join(d, "bucket") + "/")
        self.trace = os.path.join(d, "t.tsv")
        self.ops = [accesslog.Op(0, 1, 1, "read", 0, 4096, 0, 1, "OK")]
        with open(self.trace, "w") as f:
            f.write(trace.format_op(self.ops[0]))
        self.write_seed(3)
        self.clone = os.path.join(d, "mnt", "replay", "warm.img")
        self.calls = []
        self.mounted = False

    def write_seed(self, inode):
        """seed.json を書く（種の作り直しの代わり）。"""
        with open(os.path.join(self.d, "seed.json"), "w") as f:
            json.dump({"name": "t", "inode": inode, "path": "/m/seed/t.img", "mountpoint": "/m", "length": 1 << 26}, f)

    def _up(self, *a, **k):
        """偽の mount: volume のキャッシュに温まった block を置く。"""
        self.calls.append("up")
        self.mounted = True
        os.makedirs(os.path.join(self.spec.cache_dir, UUID, "raw"), exist_ok=True)
        with open(os.path.join(self.spec.cache_dir, UUID, "raw", "b"), "w") as f:
            f.write("blk")
        os.makedirs(os.path.dirname(self.clone), exist_ok=True)
        with open(self.clone, "w") as f:
            f.write("c")
        return {"mount_args": []}

    def _umount(self, *a, **k):
        """偽の umount。"""
        self.calls.append("umount")
        self.mounted = False

    def _ensure(self, warm=None, force=False):
        """外の資源を偽にして ensure_warm を呼ぶ。"""
        ps = [mock.patch("replay.testvol.up", side_effect=self._up),
              mock.patch("replay.testvol.umount", side_effect=self._umount),
              mock.patch("os.path.ismount", side_effect=lambda p: self.mounted and p == self.spec.mountpoint),
              mock.patch("replay.run._make_clone", side_effect=lambda *a: (self.calls.append("clone") or (self.clone, 7))),
              mock.patch("replay.run._volume_uuid", return_value=UUID),
              mock.patch("replay.run.wait_deleted", side_effect=lambda *a, **k: self.calls.append("wait") or {"state": "落ち着いた"}),
              mock.patch("replay.run.warm", side_effect=warm or (lambda p, ops: self.calls.append("warm") or
                                                                 {"ranges": 1, "bytes": 4096, "seconds": 0.1}))]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return runmod.ensure_warm(self.trace, self.ops, self.spec, self.test, self.prod, {}, {"name": "default"},
                                  lambda: {}, force=force, delete_quiet_s=0, max_s=1, poll_s=0, log=lambda m: None)

    def test_create_then_reuse_then_invalidate(self):
        meta, created = self._ensure()
        self.assertTrue(created)
        self.assertEqual(self.calls, ["up", "clone", "warm", "wait", "umount"])
        self.assertFalse(os.path.exists(self.clone))  # 温め用の複製は消す
        self.assertEqual((meta["uuid"], meta["files"], meta["warm"]["ranges"]), (UUID, 1, 1))
        self.calls.clear()
        meta2, created = self._ensure()
        self.assertFalse(created)
        self.assertEqual(self.calls, [])  # 使える写しがあれば mount も温めもしない
        self.assertEqual(meta2["saved_at"], meta["saved_at"])
        time.sleep(0.01)
        self.write_seed(4)  # 種を作り直した
        shutil.rmtree(os.path.join(self.spec.cache_dir, UUID, "raw"))
        _, created = self._ensure()
        self.assertTrue(created)
        self.assertIn("warm", self.calls)
        self.calls.clear()
        _, created = self._ensure(force=True)  # --force は必ず作り直す
        self.assertTrue(created)

    def test_failure_cleans_clone_and_mount(self):
        """温めの途中の失敗でも、複製を消して mount を外し、写しは作らない。"""
        def boom(p, ops):
            raise OSError(5, "EIO")
        with self.assertRaises(OSError):
            self._ensure(warm=boom)
        self.assertFalse(os.path.exists(self.clone))
        self.assertEqual(self.calls[-2:], ["wait", "umount"])
        self.assertIsNone(warmcache.load_meta(warmcache.warm_dir(self.spec, self.trace)))


if __name__ == "__main__":
    unittest.main()
