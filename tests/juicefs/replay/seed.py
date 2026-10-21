"""jfsreplay seed: 流し直しが clone する「種のファイル」を試験の volume に作る。

種は <mountpoint>/seed/<trace の名前>.img。VM のディスクと同じ長さ（trace の最大 off+size を 64 MiB に切り上げ、
または --length）の疎なファイルで、trace が触れた（write と read）64 MiB の chunk だけに、圧縮のよく効く中身を書き、fsync する。
その後 block mode の mount で `juicefs compact` をかけ、各 chunk の record が 1 つ 4 MiB 以下（ブロックの形）に
なったことを、試験の Redis の LRANGE で確かめる。
最後に、試験の mount の staging_blocks と object_request_uploading が 0 になる（書き出しが終わる）まで待つ。
待たずに run へ進むと、run の最初の `umount --flush` が書き出しの途中で時間切れになるため。"""
import json
import os
import re
import struct
import subprocess

from lib import config, metrics_io, resp
from replay import trace, testvol

CHUNK = 64 << 20  # JuiceFS の chunk の大きさ
BLOCK = 4 << 20  # block mode の record の上限（block の大きさ）
PAGE = 4096
SLICE_REC = struct.Struct(">IQIII")  # Redis の chunk の list の 1 要素: pos, id, size, off, len（24 バイト）


class SeedError(Exception):
    """種の作成を続けられないとき（上限超過、mount の mode 違い、record がブロックの形にならない等）。"""


def chunks(ops):
    """trace の write と read が触れた 64 MiB の chunk の番号（昇順、重複なし）。境目をまたぐ操作は両方の chunk を含む。"""
    s = set()
    for o in ops:
        if o.kind in ("write", "read") and o.size > 0:
            s.update(range(o.off // CHUNK, (o.off + o.size - 1) // CHUNK + 1))
    return sorted(s)


def content(off, n):
    """位置 off から n バイト。4 KiB ごとにその 4 KiB の先頭位置（8 バイト big endian）を 1 回だけ書き、残りは 0。
    位置だけで決まる（再現できる）うえ、zlib で 1/50 以下に縮む。"""
    start = off - off % PAGE
    end = off + n
    pages = [(a).to_bytes(8, "big") + bytes(PAGE - 8) for a in range(start, end, PAGE)]
    return b"".join(pages)[off - start:off - start + n]


def disk_length(ops, length=None):
    """種の長さ。length があればそれ、無ければ最大の off+size を 64 MiB に切り上げる。"""
    if length is not None:
        return length
    top = max((o.off + o.size for o in ops if o.kind in ("write", "read")), default=0)
    return max(1, -(-top // CHUNK)) * CHUNK


def parse_size(s):
    """"32G"、"512M"、"1024" のような大きさをバイト数にする。"""
    m = re.fullmatch(r"(\d+)\s*([KMGT]?)i?B?", str(s).strip(), re.I)
    if not m:
        raise ValueError("大きさの形が不正: %s" % s)
    return int(m.group(1)) << {"": 0, "K": 10, "M": 20, "G": 30, "T": 40}[m.group(2).upper()]


def check_limit(chunk_list, limit):
    """触れる chunk の数 × 64 MiB が limit を超えるなら、書く前に SeedError。"""
    need = len(chunk_list) * CHUNK
    if need > limit:
        raise SeedError("種の大きさ %d chunk × 64 MiB = %.1f GiB が上限 %.1f GiB を超えるので、書く前に止めた"
                        % (len(chunk_list), need / 2**30, limit / 2**30))


def write_seed(path, chunk_list, length):
    """path を length に伸ばし（ftruncate）、chunk_list の範囲だけに content を書いて fsync する。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.ftruncate(fd, length)
        for c in chunk_list:
            lo, hi = c * CHUNK, min((c + 1) * CHUNK, length)
            for off in range(lo, hi, BLOCK):
                n = min(BLOCK, hi - off)
                data = content(off, n)
                at = off
                while data:
                    k = os.pwrite(fd, data, at)
                    data, at = data[k:], at + k
                # block ごとに fsync して slice を確定させる。64 MiB を続けて書くと 1 つの 64 MiB の slice になり、
                # block mode の compact は「1 block 以上の record」を block の形でなくても残すため、ブロックの形にならない
                os.fsync(fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def slice_records(raw_records):
    """chunk の list の要素（24 バイト）から (pos, id, size, off, len) のリストを返す。形が不正なら ValueError。"""
    out = []
    for r in raw_records:
        if len(r) != SLICE_REC.size:
            raise ValueError("record の長さが %d（24 のはず）" % len(r))
        out.append(SLICE_REC.unpack(r))
    return out


def shape_problem(records, c, length):
    """chunk c の record が「4 MiB の block 1 つにつき 1 record」の形か確かめ、違えば理由、よければ None。
    pos は 4 MiB の倍数、off == 0、size == len == 4 MiB（seed の長さで切れる最後の block だけ短い）、record 数は block 数。"""
    span = min(CHUNK, length - c * CHUNK)
    want = -(-span // BLOCK)
    if len(records) != want:
        return "record が %d 個（%d 個のはず）" % (len(records), want)
    for i, (pos, _id, size, off, ln) in enumerate(sorted(records)):
        exp = min(BLOCK, span - i * BLOCK)
        if pos != i * BLOCK or off != 0 or size != exp or ln != exp:
            return "block %d の record が pos=%d size=%d off=%d len=%d（pos=%d size=len=%d off=0 のはず）" % (
                i, pos, size, off, ln, i * BLOCK, exp)
    return None


def bad_chunks(redis, inode, chunk_list, length):
    """試験の Redis で chunk ごとに LRANGE し、ブロックの形でない chunk の (番号, 理由) を返す。"""
    bad = []
    for c in chunk_list:
        recs = slice_records(redis.call("LRANGE", "c%d_%d" % (inode, c), 0, -1) or [])
        why = shape_problem(recs, c, length)
        if why:
            bad.append((c, why))
    return bad


def verify_block_shape(spec, inode, chunk_list, length):
    """全 chunk がブロックの形か 1 回で確かめる（compact は同期なので待たない）。空なら良い。"""
    with resp.Resp(spec.redis_addr[0], spec.redis_addr[1], db=spec.db, readonly=True) as r:
        return bad_chunks(r, inode, chunk_list, length)


def seed(trace_path, profile, spec=None, limit=None, length=None, create_bucket=False, up_kw=None, log=print,
         scrape=None, flush_max_s=None, poll_s=5.0):
    """種を作る。順序: 上限の確かめ → 試験の mount を block mode で up → 書く → fsync → compact → LRANGE で確認 →
    seed.json（trace の隣）→ 書き出し（staging_blocks と object_request_uploading が 0）を待つ。戻り値は seed.json の dict。
    scrape は試験の metrics を取る関数（既定は試験の mount の /metrics）。flush_max_s（既定 run.FLUSH_MAX_S）秒で終わらなければ SeedError
    （種と seed.json はできているので、書き出しが終わってから run する）。"""
    spec = spec or testvol.spec_from_profile(profile)
    limit = limit if limit is not None else config.load_common()["seed_limit_gib"] << 30
    up_kw = up_kw or {}
    # mount 済みでも必ず確かめる（本番の mountpoint を指す設定ミスで 32 GiB を書かないため）
    testvol.safety_check(profile, spec=spec, prod=up_kw.get("prod"), prod_redis=up_kw.get("prod_redis"))
    ops = trace.load(trace_path)
    cl = chunks(ops)
    if not cl:
        raise SeedError("trace に write／read が無いので種を作れない")
    check_limit(cl, limit)  # 何も起動・書き込みする前
    length = disk_length(ops, length)
    if cl[-1] * CHUNK >= length:
        raise SeedError("trace が触れる chunk %d が種の長さ %d バイトを超えている" % (cl[-1], length))
    name = os.path.splitext(os.path.basename(trace_path))[0]
    if os.path.ismount(spec.mountpoint):
        mode = testvol.read_mount_config(spec.mountpoint).get("Meta", {}).get("KazCompactionMode")
        if mode != "block":
            raise SeedError("試験の mount が mode=%r で動いている。block mode で立て直すため、先に testvol down すること" % mode)
    else:
        testvol.up(profile, {"name": "seed", "mode": "block"}, spec=spec, create_bucket=create_bucket, **up_kw)
    path = os.path.join(spec.mountpoint, "seed", name + ".img")
    log("種を書く: %s（%d chunk、長さ %d バイト）" % (path, len(cl), length))
    write_seed(path, cl, length)
    log("compact する（block mode）")
    p = subprocess.run([spec.binary, "compact", path], capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise SeedError("juicefs compact が失敗（終了 %d）:\n%s" % (p.returncode, (p.stderr or p.stdout)[-1500:]))
    inode = os.stat(path).st_ino
    bad = verify_block_shape(spec, inode, cl, length)
    if bad:
        raise SeedError("compact の後も record がブロックの形でない chunk がある: %s" % "; ".join("chunk %d: %s" % b for b in bad[:10]))
    info = {"name": name, "inode": inode, "path": path, "mountpoint": spec.mountpoint, "length": length,
            "chunks": cl, "block_shaped": True}
    with open(os.path.join(os.path.dirname(os.path.abspath(trace_path)), "seed.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    log("種を作った: inode %d、path %s、全 %d chunk の record は 1 つ 4 MiB 以下" % (inode, path, len(cl)))
    from replay import run as runmod  # run は player 経由で seed を読むので、循環を避けてここで読む
    flush_max_s = runmod.FLUSH_MAX_S if flush_max_s is None else flush_max_s
    if scrape is None:
        url = "http://%s:%d/metrics" % spec.metrics_addr

        def scrape():
            """試験の mount の staging と upload の系列を取る。"""
            return metrics_io.scrape(url, runmod.WATCH_INCLUDE, timeout=10)
    log("書き出し（staging の upload）が終わるのを待つ（上限 %d 秒）" % flush_max_s)
    w = runmod.wait_flushed(scrape, max_s=flush_max_s, poll_s=poll_s, log=log)
    if w["state"] != runmod.SETTLED:
        raise SeedError("種はできたが、書き出しが %d 秒で終わらない（最後の値 %s）。試験の mount は上げたままなので、"
                        "staging_blocks と object_request_uploading が 0 になってから run すること"
                        % (flush_max_s, w["last"]))
    log("書き出しが終わった（%.0f 秒）" % w["seconds"])
    return info
