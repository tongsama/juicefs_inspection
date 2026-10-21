"""常駐の 3 つの sampler。記録の形は、今の metrics_sampler.sh（2026-10-21 版）、llen_sampler.py、
key_tracker.py と同じにして、前回までの解析の道具がそのまま読めるようにする（読み取りのみ）。
起動: python3 -I -m measure.samplers metrics|llen|keys <出力ディレクトリ>
プロファイルと間隔は <出力ディレクトリ>/run.json から読む。"""
import json
import os
import re
import sys
import time

from lib import config, metrics_io
from measure.conn import connect_profile

PAT = re.compile(r"^\d+_\d+_\d+$")
SLOW_CHUNK_RECORDS = 350  # llen の分布で「多い」と数える record の数


def load_run(outdir):
    """run.json から (プロファイル, 間隔の dict) を読む。"""
    with open(os.path.join(outdir, "run.json"), encoding="utf-8") as f:
        run = json.load(f)
    return config.load_profile(run["profile"], run.get("profiles_path")), run["intervals"]


def now_iso(ts):
    """UNIX 秒を ISO 時刻の文字列にする。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def metrics_once(profile, out):
    """/metrics を 1 回取り、対象の系列を追記する。失敗は _scrape_error の行にする（計測は止めない）。"""
    now = int(time.time())
    try:
        series = metrics_io.scrape(profile.metrics_url, metrics_io.DEFAULT_INCLUDE)
    except Exception:
        series = {"_scrape_error": 1}
    metrics_io.append_sample(out, now, series)


def llen_sample(r, inode, nchunks):
    """全 chunk の LLEN をパイプラインで取り、record のある chunk の値のリストを返す。"""
    r.send(*[("LLEN", "c%d_%d" % (inode, i)) for i in range(nchunks)])
    return [x for x in (r.read() for _ in range(nchunks)) if x]


def llen_row(now, lens):
    """record の数の分布の 1 行（UNIX 秒、ISO、chunk 数、合計、平均、中央、90%、最大、350 超の chunk 数）。"""
    lens = sorted(lens)
    n = len(lens)
    return [now, now_iso(now), n, sum(lens), "%.1f" % (sum(lens) / n) if n else 0,
            lens[n // 2] if n else 0, lens[min(n - 1, n * 9 // 10)] if n else 0,
            lens[-1] if n else 0, sum(1 for x in lens if x > SLOW_CHUNK_RECORDS)]


def run_llen(profile, interval, out, nchunks=4800, once=False):
    """一定間隔で LLEN の分布を追記し続ける。失敗は error の行にして、次回は接続し直す。"""
    r = None
    while True:
        try:
            if profile.inode <= 0:
                raise ValueError("inode が未設定")
            if r is None:
                r = connect_profile(profile)
            now = int(time.time())
            row = llen_row(now, llen_sample(r, profile.inode, nchunks))
        except Exception as e:  # 取得の失敗は記録し、計測を止めない
            if r is not None:
                r.close()
            r = None
            now = int(time.time())
            row = [now, now_iso(now), "error", type(e).__name__]
        with open(out, "a", encoding="utf-8") as f:
            f.write("\t".join(str(x) for x in row) + "\n")
        if once:
            return
        time.sleep(interval)


def scan_cache(root):
    """raw/chunks の下のブロックファイルを {名前: (atime, ctime, size)} で返す。"""
    out = {}
    for dp, _, fns in os.walk(os.path.join(root, "raw", "chunks")):
        for fn in fns:
            if not PAT.match(fn):
                continue
            try:
                st = os.stat(os.path.join(dp, fn))
            except FileNotFoundError:
                continue
            out[fn] = (int(st.st_atime), int(st.st_ctime), st.st_size)
    return out


def run_keys(root, outdir, interval, once=False):
    """起動時に全ファイルの snapshot を書き、その後は間隔ごとの add／del を keys-events.tsv に追記する。
    del には消える前に最後に見た atime を付ける（追い出しの判定に使う）。"""
    now = int(time.time())
    prev = scan_cache(root)
    with open(os.path.join(outdir, "keys-snapshot-%d.tsv" % now), "w", encoding="utf-8") as f:
        f.write("key\tatime\tctime\tsize\n")
        for k, (a, c, s) in sorted(prev.items()):
            f.write("%s\t%d\t%d\t%d\n" % (k, a, c, s))
    ev = open(os.path.join(outdir, "keys-events.tsv"), "a", buffering=1, encoding="utf-8")
    try:
        while True:
            if once:
                return
            time.sleep(interval)
            now = int(time.time())
            try:
                cur = scan_cache(root)
            except OSError as e:  # 走査の失敗は記録し、前回の状態を保ったまま次の回へ（偽の del を出さない）
                print("%s keys: キャッシュの走査に失敗（%s）、次の回で再試行" % (now_iso(now), type(e).__name__),
                      file=sys.stderr, flush=True)
                continue
            for k in cur.keys() - prev.keys():
                a, c, s = cur[k]
                ev.write("%d\tadd\t%s\t%d\t%d\t%d\n" % (now, k, a, c, s))
            for k in prev.keys() - cur.keys():
                a, c, s = prev[k]
                ev.write("%d\tdel\t%s\t%d\t%d\t%d\n" % (now, k, a, c, s))
            prev = cur
    finally:
        ev.close()


def main(argv=None):
    """sampler の入口。metrics|llen|keys <出力ディレクトリ>。"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] not in ("metrics", "llen", "keys"):
        sys.exit("使い方: python3 -I -m measure.samplers metrics|llen|keys <出力ディレクトリ>")
    kind, outdir = argv
    profile, iv = load_run(outdir)
    if kind == "metrics":
        while True:
            metrics_once(profile, os.path.join(outdir, "metrics.tsv"))
            time.sleep(iv["metrics"])
    elif kind == "llen":
        run_llen(profile, iv["llen"], os.path.join(outdir, "llen.tsv"))
    else:
        run_keys(profile.cache_dir, outdir, iv["keys"])


if __name__ == "__main__":
    main()
