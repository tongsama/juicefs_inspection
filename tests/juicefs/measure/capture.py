"""開始の時点と再起動の直前に取る材料（/metrics の全体、キャッシュの内訳とファイルの一覧、kaz-2q の管理ファイルの写し）、
と mount の設定（.config）の写し。キャッシュの内訳は cache_breakdown.py の分類をそのまま移したもの。
Redis には読み取りの命令（SCAN／LRANGE）だけを送る。"""
import collections
import json
import os
import re
import shutil
import struct
import time
import urllib.request

from measure.conn import connect_profile

SLICE_BYTES = 24
STATE_FILE = "kaz-cache-state.v1"
# 設定の項目名にこれが入る、または値に :// が入るものは伏せる
REDACT_KEY_WORDS = ("key", "secret", "pass", "token", "addr", "url", "bucket")
REDACTED = "<redacted>"
CATS = ("full", "partial", "hidden", "dead", "new")


def redact_config(obj, key=""):
    """mount の設定（JSON を読んだもの）を再帰的に走査し、接続先や鍵らしい項目を <redacted> にして返す。
    項目名（大小文字を無視）に key／secret／pass／token／addr／url／bucket を含む、または文字列の値に :// を含むものが対象。"""
    if isinstance(obj, dict):
        return {k: redact_config(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_config(v, key) for v in obj]
    if any(w in key.lower() for w in REDACT_KEY_WORDS):
        return REDACTED
    if isinstance(obj, str) and "://" in obj:
        return REDACTED
    return obj


def copy_config(mountpoint, outdir):
    """<mountpoint>/.config を読み、伏せたものを config.json に書く。読めないときは理由の文字列を返す（成功は None）。"""
    try:
        with open(os.path.join(mountpoint, ".config"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return ".config を読めない（%s）" % type(e).__name__
    with open(os.path.join(outdir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(redact_config(cfg), f, ensure_ascii=False, indent=1, sort_keys=True)
    return None


def scan_chunks(r, batch):
    """c{inode}_{indx} の chunk の list をすべて読み、(inode, indx, [(pos, sid, size, off, len)...]) を返す。"""
    pat = re.compile(rb"^c(\d+)_(\d+)$")
    cursor, keys = b"0", []
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
            ss = [struct.unpack(">IQIII", v) for v in (vals or []) if len(v) == SLICE_BYTES]
            out.append((int(m.group(1)), int(m.group(2)), ss))
    return out


def visible_segments(ss):
    """list の順（後ほど上）に塗り重ね、見えている区間 [start,end) と slice の添字を返す。"""
    segs = []
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
            d0, d1 = off + (a - pos), off + (b - pos)
            blk = d0 // bsize
            while blk * bsize < d1:
                lo, hi = max(d0, blk * bsize), min(d1, (blk + 1) * bsize)
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


def cache_breakdown(r, cache, block_size=4 << 20, batch=500, margin=300, top=15):
    """キャッシュの内訳を (表示のテキスト, ファイルごとの行のリスト) で返す。
    分類は dead（slice が無い）、hidden、partial、full、new（走査開始の margin 秒前以降に作られた）。"""
    t0 = time.time()
    chunks = scan_chunks(r, batch)
    t1 = time.time()
    files = walk_cache(cache)
    t2 = time.time()
    vis, owners = block_visibility(chunks, block_size)
    cat_bytes, cat_files = collections.Counter(), collections.Counter()
    age = collections.defaultdict(collections.Counter)
    by_inode = collections.defaultdict(collections.Counter)
    visible_in_partial = 0
    rows = []
    for sid, blk, size, real, at, ct in files:
        if ct >= t0 - margin:
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
    L = ["chunks scanned %d in %.1fs, cache files %d in %.1fs" % (len(chunks), t1 - t0, len(files), t2 - t1),
         "referenced slice ids %d" % len(vis), "", "== category (bytes, files) =="]
    for c in CATS:
        L.append("%-8s %8.2f GiB %5.1f%% %7d files" % (c, gib(cat_bytes[c]), 100 * cat_bytes[c] / max(total, 1), cat_files[c]))
    L.append("total    %8.2f GiB" % gib(total))
    L.append("partial blocks: visible %.2f GiB of %.2f GiB" % (gib(visible_in_partial), gib(cat_bytes["partial"])))
    buckets = ["<1h", "1-6h", "6-24h", "1-3d", "3-7d", ">=7d"]
    L += ["", "== last access age (GiB) ==", "%-8s " % "" + " ".join("%7s" % b for b in buckets)]
    for c in CATS:
        L.append("%-8s " % c + " ".join("%7.2f" % gib(age[c][b]) for b in buckets))
    L += ["", "== top inodes by cached bytes (GiB) =="]
    for k, cnt in sorted(by_inode.items(), key=lambda kv: -sum(kv[1].values()))[:top]:
        L.append("%-24s total %6.2f  " % (k, gib(sum(cnt.values()))) + " ".join(
            "%s=%.2f" % (c, gib(cnt[c])) for c in CATS if cnt[c]))
    return "\n".join(L) + "\n", sorted(rows)


def write_files_csv(path, rows):
    """ファイルごとの分類（id, indx, size, real, atime, ctime, cat, inodes）を CSV に書く。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("id,indx,size,real,atime,ctime,cat,inodes\n")
        for row in rows:
            f.write("%d,%d,%d,%d,%d,%d,%s,\"%s\"\n" % row)


def capture(profile, outdir, label):
    """label の材料を outdir に取る。取れなかったものの説明のリストを返す（空なら全部取れた）。
    metrics-<label>-<時刻>.txt、breakdown-…txt、files-…csv、state-…v1。1 つの失敗で残りを諦めない。"""
    ts = time.strftime("%m%d%H%M%S")
    problems = []
    try:
        with urllib.request.urlopen(profile.metrics_url, timeout=20) as resp:
            body = resp.read()
        with open(os.path.join(outdir, "metrics-%s-%s.txt" % (label, ts)), "wb") as f:
            f.write(body)
    except Exception as e:
        problems.append("/metrics を取れない（%s）" % type(e).__name__)
    src = os.path.join(profile.cache_dir, STATE_FILE)
    try:
        shutil.copyfile(src, os.path.join(outdir, "state-%s-%s.v1" % (label, ts)))
    except OSError as e:
        problems.append("%s を写せない（%s）" % (STATE_FILE, type(e).__name__))
    try:
        with connect_profile(profile) as r:
            text, rows = cache_breakdown(r, profile.cache_dir)
        with open(os.path.join(outdir, "breakdown-%s-%s.txt" % (label, ts)), "w", encoding="utf-8") as f:
            f.write(text)
        write_files_csv(os.path.join(outdir, "files-%s-%s.csv" % (label, ts)), rows)
    except Exception as e:  # 接続の例外の文言には接続先が入りうるので、型の名前だけを出す
        problems.append("キャッシュの内訳を取れない（%s）" % type(e).__name__)
    return problems
