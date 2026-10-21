"""温めたキャッシュの写し（warm copy）: 1 回だけ Drive から温め、各回はその写しを空のキャッシュへ戻す。

初回の実走（2026-10-21）では、毎回キャッシュを空にして記録の read の範囲（85,409 範囲、6.9 GB）を Drive から
読み直し、温めるだけで 787 秒かかった。種の object は全ての複製（juicefs clone）で共有されるので、温めた後の
キャッシュ（<cache-dir>/<UUID>/ の rawstaging 以外。kaz-2q の状態のファイル kaz-cache-state.v1 を含む）を、
試験の mount を外した状態（キャッシュと状態のファイルが揃った状態）で写しておけば、各回はそれを戻すだけでよい。

- 写しの置き場所: <state_dir>/warm-cache/<trace の名前>/ の cache/（中身）と meta.json（種の印、UUID、数）。
- 写しは普通のファイルの複製（shutil.copy2）。hardlink は使わないので、各回の mount が写しを書き換えることはない。
- 種を作り直す（seed.json が書き直される）と、写しは使えない（seed_identity が変わる）。呼ぶ側が作り直す。
- 写す元・戻す先とも、キャッシュの安全の確かめ（run.check_cache_dir の realpath・本番との重なり、UUID の名前の
  ディレクトリだけ、symlink をたどらない）を通す。写しの置き場所も warm-cache の下だけに限る（check_warm_dir）。
- 容量が足りなければ RunError（容量不足を隠さない）。"""
import hashlib
import json
import os
import shutil
import stat
import time

from replay import testvol

VERSION = 1
META = "meta.json"
CONTENT = "cache"
SPACE_MARGIN = 1 << 30  # 写す前に残しておく空きの余裕（1 GiB）


def warm_root(spec):
    """温めたキャッシュの写しの置き場所の根（<state_dir>/warm-cache）。"""
    return spec.path("warm-cache")


def warm_dir(spec, trace_path):
    """trace の写しの置き場所（<state_dir>/warm-cache/<trace の名前（拡張子を除く）>）。"""
    return os.path.join(warm_root(spec), os.path.splitext(os.path.basename(trace_path))[0])


def _sha256(path):
    """ファイルの sha256（16 進）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def seed_identity(trace_path, seed_info):
    """写しが使えるかを決める種の印。seed.json の name／inode／path／length と、seed.json の sha256 と mtime、
    trace の sha256。種を作り直すと seed.json が書き直されるので、印が変わる。"""
    sj = os.path.join(os.path.dirname(os.path.abspath(trace_path)), "seed.json")
    return {"name": seed_info.get("name"), "inode": seed_info.get("inode"), "path": seed_info.get("path"),
            "length": seed_info.get("length"), "seed_json_sha256": _sha256(sj),
            "seed_json_mtime_ns": os.stat(sj).st_mtime_ns, "trace_sha256": _sha256(trace_path)}


def load_meta(d):
    """写しの meta.json を読む。無い・壊れていれば None。"""
    try:
        with open(os.path.join(d, META), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def is_valid(meta, ident, d):
    """写しが今の種で使えるか。戻り値は (真偽, 理由の文字列)。"""
    if meta is None:
        return False, "写しが無い"
    if meta.get("version") != VERSION:
        return False, "写しの形式が古い（version %r）" % meta.get("version")
    if meta.get("seed") != ident:
        return False, "種が写しを作った時と違う（種を作り直した）"
    if not os.path.isdir(os.path.join(d, CONTENT)) or os.path.islink(os.path.join(d, CONTENT)):
        return False, "写しの中身（%s）が無い" % CONTENT
    return True, "使える"


def _under(a, b):
    """a が b と同じか、b の下にあるか（どちらも解決済みの絶対 path）。"""
    return a == b or a.startswith(b.rstrip("/") + "/")


def check_warm_dir(path, root, *, cache_dir, mountpoint, prod):
    """写しの置き場所 path が、写しの根 root の下（root 自体は不可）にあり、本番の mountpoint・本番のキャッシュ・
    試験のキャッシュ・試験の mountpoint・$HOME 自体と重ならないことを確かめ、解決した絶対 path を返す。
    だめなら testvol.SafetyError。"""
    if not path or not os.path.isabs(path) or not root or not os.path.isabs(root):
        raise testvol.SafetyError("温めたキャッシュの写しの場所が空か相対 path: %r（根 %r）" % (path, root))
    # 本番の mountpoint は FUSE の root に stat を送らないよう文字列で比べる（realpath の前に）
    if testvol._overlaps(path, prod.mountpoint) or testvol._overlaps(root, prod.mountpoint):
        raise testvol.SafetyError("温めたキャッシュの写しの場所が本番の mountpoint と重なるので止めます: %s" % path)
    real, rroot = os.path.realpath(path), os.path.realpath(root)
    if real == rroot or not _under(real, rroot):
        raise testvol.SafetyError("温めたキャッシュの写しの場所が %s の下ではない: %s" % (rroot, real))
    home = os.path.realpath(os.path.expanduser("~"))
    if _under(home, real):
        raise testvol.SafetyError("温めたキャッシュの写しの場所が $HOME 自体かその上: %s" % real)
    for label, other in (("本番のキャッシュ", prod.cache_dir), ("試験のキャッシュ", cache_dir)):
        if other:
            o = os.path.realpath(os.path.expanduser(other))
            if _under(real, o) or _under(o, real):
                raise testvol.SafetyError("温めたキャッシュの写しの場所が%sと重なるので止めます: %s" % (label, real))
    if mountpoint:
        o = os.path.abspath(mountpoint)
        if _under(real, o) or _under(o, real):
            raise testvol.SafetyError("温めたキャッシュの写しの場所が試験の mountpoint と重なるので止めます: %s" % real)
    return real


def _rawstaging_clean(vol):
    """<vol>/rawstaging にファイルか symlink が無いか（空のディレクトリは良い）。無ければ真。"""
    st = os.path.join(vol, "rawstaging")
    if not os.path.lexists(st):
        return True
    if not stat.S_ISDIR(os.lstat(st).st_mode):
        return False
    return not any(fs or any(os.path.islink(os.path.join(r, x)) for x in ds) for r, ds, fs in os.walk(st))


def _tree_bytes(src, skip=()):
    """src の下の普通のファイルの合計のバイト数（symlink はたどらない。1 段目の skip の名前は除く）。"""
    total = 0
    for e in os.scandir(src):
        if e.name in skip or e.is_symlink():
            continue
        if e.is_dir(follow_symlinks=False):
            total += _tree_bytes(e.path)
        elif e.is_file(follow_symlinks=False):
            total += e.stat(follow_symlinks=False).st_size
    return total


def _check_space(d, need):
    """d のある filesystem に need + SPACE_MARGIN の空きが無ければ RunError（容量不足を隠さない）。"""
    from replay import run as runmod
    free = shutil.disk_usage(d).free
    if free < need + SPACE_MARGIN:
        raise runmod.RunError("温めたキャッシュの写しに容量が足りない: %s の空き %d バイト、必要 %d バイト（+ 余裕 %d）"
                              % (d, free, need, SPACE_MARGIN))


def _copy_tree(src, dst, skip=()):
    """src の中身を dst（既にあるディレクトリ）へ、普通のファイルの複製（copy2、hardlink なし）で写す。
    symlink とそれ以外の特殊なファイルは写さずに数える。1 段目の skip の名前は除く。戻り値は (ファイル数, バイト, 写さなかった数)。"""
    files = nbytes = skipped = 0
    for e in os.scandir(src):
        if e.name in skip:
            continue
        d = os.path.join(dst, e.name)
        if e.is_symlink():
            skipped += 1
        elif e.is_dir(follow_symlinks=False):
            os.makedirs(d, exist_ok=True)
            f, b, s = _copy_tree(e.path, d)
            files, nbytes, skipped = files + f, nbytes + b, skipped + s
            shutil.copystat(e.path, d, follow_symlinks=False)
        elif e.is_file(follow_symlinks=False):
            shutil.copy2(e.path, d, follow_symlinks=False)
            files += 1
            nbytes += e.stat(follow_symlinks=False).st_size
        else:
            skipped += 1
    return files, nbytes, skipped


def _vol_dir(real_cache, uuid):
    """<real_cache>/<uuid> を返す。uuid が UUID の形でなければ SafetyError。"""
    from replay import run as runmod
    if not isinstance(uuid, str) or not runmod.UUID_RE.match(uuid):
        raise testvol.SafetyError("volume の UUID の形ではない: %r" % (uuid,))
    return os.path.join(real_cache, uuid)


def save(cache_dir, uuid, dest, info, *, mountpoint, prod, state_dir, root):
    """試験の mount が外れているときだけ、<cache_dir>/<uuid>/（rawstaging を除く）を dest/cache/ へ写し、
    dest/meta.json（info に uuid・数・時刻を足したもの）を書く。前の写しは、新しい写しができてから置き換える。
    rawstaging にファイルが残っていれば RunError（上がっていない block は写さない）。戻り値は meta の dict。"""
    from replay import run as runmod
    real = runmod.check_cache_dir(cache_dir, mountpoint, prod, state_dir)
    if os.path.ismount(mountpoint):
        raise runmod.RunError("試験の mount が上がっている間はキャッシュを写さない（状態のファイルが揃わない）: %s" % mountpoint)
    vol = _vol_dir(real, uuid)
    if not os.path.lexists(vol) or not stat.S_ISDIR(os.lstat(vol).st_mode):
        raise runmod.RunError("volume のキャッシュが実のディレクトリとして無い: %s" % vol)
    if not _rawstaging_clean(vol):
        raise runmod.RunError("staging に上がっていない block が残っているので、キャッシュを写さない: %s" % vol)
    dreal = check_warm_dir(dest, root, cache_dir=real, mountpoint=mountpoint, prod=prod)
    os.makedirs(os.path.dirname(dreal), mode=0o700, exist_ok=True)
    _check_space(os.path.dirname(dreal), _tree_bytes(vol, skip=("rawstaging",)))
    tmp = "%s.tmp-%d" % (dreal, os.getpid())
    if os.path.lexists(tmp):
        runmod._rm_tree(check_warm_dir(tmp, root, cache_dir=real, mountpoint=mountpoint, prod=prod))
    old = None
    try:
        os.makedirs(os.path.join(tmp, CONTENT), mode=0o700)
        t = time.monotonic()
        files, nbytes, skipped = _copy_tree(vol, os.path.join(tmp, CONTENT), skip=("rawstaging",))
        meta = dict(info, version=VERSION, uuid=uuid, files=files, bytes=nbytes, skipped=skipped,
                    saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), save_seconds=round(time.monotonic() - t, 1))
        with open(os.path.join(tmp, META), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        if os.path.lexists(dreal):
            old = "%s.old-%d" % (dreal, os.getpid())
            os.rename(dreal, old)
        os.rename(tmp, dreal)
    except BaseException:
        # 途中の失敗（容量不足・I/O エラー・中断）: 作りかけの写しを消し、退けた前の写しを戻してから元の例外を上げる
        if old and not os.path.lexists(dreal) and os.path.lexists(old):
            os.rename(old, dreal)
            old = None
        if os.path.lexists(tmp):
            runmod._rm_tree(check_warm_dir(tmp, root, cache_dir=real, mountpoint=mountpoint, prod=prod))
        raise
    if old:
        runmod._rm_tree(check_warm_dir(old, root, cache_dir=real, mountpoint=mountpoint, prod=prod))
    return meta


def restore(src, cache_dir, *, mountpoint, prod, state_dir, root):
    """試験の mount が外れているときだけ、写し src/cache/ を空の <cache_dir>/<UUID>/ へ写す（UUID は src/meta.json）。
    戻す先に rawstaging の空のディレクトリ以外があれば RunError（空にしてから戻す）。戻り値は {uuid, files, bytes, skipped, seconds}。"""
    from replay import run as runmod
    meta = load_meta(src)
    if meta is None:
        raise runmod.RunError("温めたキャッシュの写しの meta.json が無い: %s" % src)
    sreal = check_warm_dir(src, root, cache_dir=cache_dir, mountpoint=mountpoint, prod=prod)
    content = os.path.join(sreal, CONTENT)
    if not os.path.lexists(content) or not stat.S_ISDIR(os.lstat(content).st_mode):
        raise runmod.RunError("温めたキャッシュの写しの中身が実のディレクトリとして無い: %s" % content)
    real = runmod.check_cache_dir(cache_dir, mountpoint, prod, state_dir)
    if os.path.ismount(mountpoint):
        raise runmod.RunError("試験の mount が上がっている間はキャッシュを戻さない: %s" % mountpoint)
    vol = _vol_dir(real, meta.get("uuid"))
    if os.path.lexists(vol):
        if not stat.S_ISDIR(os.lstat(vol).st_mode):
            raise testvol.SafetyError("volume のキャッシュが実のディレクトリではない: %s" % vol)
        extra = [n for n in os.listdir(vol) if n != "rawstaging"]
        if extra or not _rawstaging_clean(vol):
            raise runmod.RunError("戻す先のキャッシュが空ではない（%s）: %s" % (", ".join(sorted(extra)[:5]) or "rawstaging", vol))
    else:
        os.makedirs(vol, mode=0o700)
    _check_space(vol, _tree_bytes(content))
    t = time.monotonic()
    files, nbytes, skipped = _copy_tree(content, vol)
    return {"uuid": meta["uuid"], "files": files, "bytes": nbytes, "skipped": skipped,
            "seconds": round(time.monotonic() - t, 1)}
