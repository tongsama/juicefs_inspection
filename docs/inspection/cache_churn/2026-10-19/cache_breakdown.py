#!/usr/bin/env python3
"""A-2: ローカルのブロックキャッシュの中身を、metadata（Redis）上の slice の生死と突き合わせて内訳を出す。

読み取りのみ。Redis には AUTH／SELECT／SCAN／LRANGE／DBSIZE だけを送り、キャッシュのディレクトリは
stat するだけで変更しない。接続情報は mount スクリプトの MOUNT_FROM から読み、出力には出さない。

分類（キャッシュのブロックファイル {id}_{indx}_{size} ごと）:
  dead    : slice id がどの chunk の list にも無い（削除済みなのにキャッシュに残る）
  hidden  : list にはあるが、そのブロックの範囲は後の slice に全部覆われて見えない
  partial : ブロックの一部だけが見える
  full    : ブロック全体が見える
  new     : 走査開始より後に作られたファイル（判定から外す）
"""
import argparse
import collections
import json
import os
import re
import socket
import struct
import sys
import time
from urllib.parse import unquote, urlparse

SLICE_BYTES = 24
CHUNK_SIZE = 64 << 20


class Resp:
    """パイプライン対応の最小限の Redis（RESP2）クライアント。"""

    def __init__(self, host, port, timeout=30):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = self.sock.makefile("rb", buffering=1 << 20)

    def send(self, *cmds):
        """複数のコマンドをまとめて送る。"""
        out = bytearray()
        for cmd in cmds:
            out += b"*%d\r\n" % len(cmd)
            for a in cmd:
                a = a if isinstance(a, bytes) else str(a).encode()
                out += b"$%d\r\n%s\r\n" % (len(a), a)
        self.sock.sendall(out)

    def read(self):
        """応答を 1 つ読む。"""
        line = self.buf.readline()
        t, rest = line[:1], line[1:-2]
        if t == b"+":
            return rest
        if t == b"-":
            raise RuntimeError("redis error: " + rest.decode(errors="replace"))
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = self.buf.read(n + 2)
            return data[:-2]
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self.read() for _ in range(n)]
        raise RuntimeError("unexpected reply type %r" % line[:20])

    def call(self, *cmd):
        """コマンドを 1 つ送って応答を返す。"""
        self.send(cmd)
        return self.read()


def connect_from_script(script):
    """mount スクリプトの MOUNT_FROM の URL で接続する（URL や認証情報は表示しない）。"""
    url = None
    for line in open(script, encoding="utf-8"):
        m = re.match(r'\s*MOUNT_FROM="([^"]+)"', line)
        if m:
            url = re.sub(r"\$\{[^}]*\}", "", m.group(1))
    if not url:
        sys.exit("MOUNT_FROM not found in mount script")
    u = urlparse(url)
    r = Resp(u.hostname, u.port or 6379)
    if u.password is not None:
        if u.username:
            r.call("AUTH", unquote(u.username), unquote(u.password))
        else:
            r.call("AUTH", unquote(u.password))
    elif u.username:
        r.call("AUTH", unquote(u.username))
    db = (u.path or "/0").strip("/") or "0"
    r.call("SELECT", db)
    return r


def scan_chunks(r, batch):
    """c{inode}_{indx} の chunk の list をすべて読み、(inode, indx, [slice...]) を返す。"""
    pat = re.compile(rb"^c(\d+)_(\d+)$")
    cursor = b"0"
    keys = []
    while True:
        cursor, ks = r.call("SCAN", cursor, "MATCH", "c*_*", "COUNT", 2000)
        keys.extend(k for k in ks if pat.match(k))
        if cursor == b"0":
            break
    out = []
    for i in range(0, len(keys), batch):
        part = keys[i:i + batch]
        r.send(*[("LRANGE", k, 0, -1) for k in part])
        for k in part:
            vals = r.read()
            m = pat.match(k)
            ss = []
            for v in vals or []:
                if len(v) != SLICE_BYTES:
                    continue
                pos, sid, size, off, ln = struct.unpack(">IQIII", v)
                ss.append((pos, sid, size, off, ln))
            out.append((int(m.group(1)), int(m.group(2)), ss))
    return out


def visible_segments(ss):
    """list の順（後ほど上）に塗り重ね、見えている区間 [start,end) と slice を返す。"""
    segs = []  # (start, end, idx) を start 順に保つ
    for idx, (pos, sid, size, off, ln) in enumerate(ss):
        s, e = pos, pos + ln
        nxt = []
        for a, b, j in segs:
            if b <= s or a >= e:
                nxt.append((a, b, j))
                continue
            if a < s:
                nxt.append((a, s, j))
            if b > e:
                nxt.append((e, b, j))
        nxt.append((s, e, idx))
        nxt.sort()
        segs = nxt
    return segs


def block_visibility(chunks, bsize):
    """slice id ごとに、ブロック番号 → 見えているバイト数 と、参照している inode を集計する。"""
    vis = collections.defaultdict(lambda: collections.defaultdict(int))
    owners = collections.defaultdict(set)
    for inode, indx, ss in chunks:
        for pos, sid, size, off, ln in ss:
            if sid:
                owners[sid].add(inode)
                vis[sid]  # 参照があることを記録（見えなくても）
        for a, b, j in visible_segments(ss):
            pos, sid, size, off, ln = ss[j]
            if not sid:
                continue
            d0 = off + (a - pos)
            d1 = off + (b - pos)
            blk = d0 // bsize
            while blk * bsize < d1:
                lo = max(d0, blk * bsize)
                hi = min(d1, (blk + 1) * bsize)
                vis[sid][blk] += hi - lo
                blk += 1
    return vis, owners


def walk_cache(root):
    """raw/chunks の下のブロックファイルを列挙し、(id, indx, size, 実サイズ, atime, ctime) を返す。"""
    pat = re.compile(r"^(\d+)_(\d+)_(\d+)$")
    out = []
    for dp, _, fns in os.walk(os.path.join(root, "raw", "chunks")):
        for fn in fns:
            m = pat.match(fn)
            if not m:
                continue
            try:
                st = os.stat(os.path.join(dp, fn))
            except FileNotFoundError:
                continue
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), st.st_size, st.st_atime, st.st_ctime))
    return out


def age_bucket(sec):
    """経過時間をおおまかな区分に分ける。"""
    for lim, name in ((3600, "<1h"), (6 * 3600, "1-6h"), (24 * 3600, "6-24h"), (3 * 86400, "1-3d"), (7 * 86400, "3-7d")):
        if sec < lim:
            return name
    return ">=7d"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", default=os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh"))
    ap.add_argument("--cache", required=True, help="キャッシュの UUID ディレクトリ")
    ap.add_argument("--block-size", type=int, default=4 << 20)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--margin", type=int, default=300, help="走査開始のこの秒数前以降に作られたファイルを new にする")
    ap.add_argument("--probe", action="store_true", help="DBSIZE と chunk の key の数だけ見る")
    ap.add_argument("--json", help="集計を JSON で保存する先")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--files-csv", help="ファイルごとの分類（id, indx, size, real, atime, ctime, cat, inodes）を CSV で保存する先")
    a = ap.parse_args()

    r = connect_from_script(a.script)
    if a.probe:
        print("dbsize", r.call("DBSIZE"))
        t = time.time()
        n, cursor = 0, b"0"
        while True:
            cursor, ks = r.call("SCAN", cursor, "MATCH", "c*_*", "COUNT", 2000)
            n += len(ks)
            if cursor == b"0":
                break
        print("chunk keys", n, "scan %.1fs" % (time.time() - t))
        return

    t0 = time.time()
    chunks = scan_chunks(r, a.batch)
    t1 = time.time()
    files = walk_cache(a.cache)
    t2 = time.time()
    vis, owners = block_visibility(chunks, a.block_size)

    cat_bytes = collections.Counter()
    cat_files = collections.Counter()
    age = collections.defaultdict(collections.Counter)
    by_inode = collections.defaultdict(collections.Counter)
    visible_in_partial = 0
    rows = []
    for sid, blk, size, real, at, ct in files:
        if ct >= t0 - a.margin:
            cat = "new"
        elif sid not in vis:
            cat = "dead"
        else:
            v = vis[sid].get(blk, 0)
            if v <= 0:
                cat = "hidden"
            elif v < size:
                cat = "partial"
                visible_in_partial += v
            else:
                cat = "full"
        cat_bytes[cat] += real
        cat_files[cat] += 1
        age[cat][age_bucket(t2 - at)] += real
        own = owners.get(sid)
        key = ",".join(str(i) for i in sorted(own)[:3]) if own else "-"
        by_inode[key][cat] += real
        rows.append((sid, blk, size, real, int(at), int(ct), cat, key))

    total = sum(cat_bytes.values())
    gib = lambda b: b / (1 << 30)
    print("chunks scanned %d in %.1fs, cache files %d in %.1fs" % (len(chunks), t1 - t0, len(files), t2 - t1))
    print("referenced slice ids %d" % len(vis))
    print("\n== category (bytes, files) ==")
    for c in ("full", "partial", "hidden", "dead", "new"):
        print("%-8s %8.2f GiB %5.1f%% %7d files" % (c, gib(cat_bytes[c]), 100 * cat_bytes[c] / max(total, 1), cat_files[c]))
    print("total    %8.2f GiB" % gib(total))
    print("partial blocks: visible %.2f GiB of %.2f GiB" % (gib(visible_in_partial), gib(cat_bytes["partial"])))
    buckets = ["<1h", "1-6h", "6-24h", "1-3d", "3-7d", ">=7d"]
    print("\n== last access age (GiB) ==")
    print("%-8s " % "" + " ".join("%7s" % b for b in buckets))
    for c in ("full", "partial", "hidden", "dead", "new"):
        print("%-8s " % c + " ".join("%7.2f" % gib(age[c][b]) for b in buckets))
    print("\n== top inodes by cached bytes (GiB) ==")
    tops = sorted(by_inode.items(), key=lambda kv: -sum(kv[1].values()))[:a.top]
    for k, cnt in tops:
        print("%-24s total %6.2f  " % (k, gib(sum(cnt.values()))) + " ".join("%s=%.2f" % (c, gib(cnt[c])) for c in ("full", "partial", "hidden", "dead", "new") if cnt[c]))
    if a.files_csv:
        with open(a.files_csv, "w", encoding="utf-8") as f:
            f.write("id,indx,size,real,atime,ctime,cat,inodes\n")
            for row in sorted(rows):
                f.write("%d,%d,%d,%d,%d,%d,%s,\"%s\"\n" % row)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({
                "scan_start": t0, "chunks": len(chunks), "cache_files": len(files),
                "category_bytes": cat_bytes, "category_files": cat_files,
                "partial_visible_bytes": visible_in_partial,
                "age_bytes": {c: dict(v) for c, v in age.items()},
                "by_inode": {k: dict(v) for k, v in tops},
            }, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
