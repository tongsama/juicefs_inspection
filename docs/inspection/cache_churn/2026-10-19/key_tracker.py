#!/usr/bin/env python3
"""キャッシュのブロックファイルの出入りを一定間隔で記録する（読み取りのみ）。

起動時に全ファイルの一覧を snapshot として書き、その後は間隔ごとに前回との差分
（add: 新しく現れたファイル、del: 消えたファイル）を events に追記する。
del には、消える前に最後に見た atime を付ける。次の VM 起動で GET して補充したブロックが、
以前キャッシュにあって消えたもの（追い出し）かどうかを、この記録から判定する。
retire（slice の削除）で消えたものも del に含まれるが、削除済みの slice は再び読まれないので判定には影響しない。
使い方: key_tracker.py <キャッシュの UUID ディレクトリ> <出力ディレクトリ> [間隔秒(既定300)]
"""
import os
import re
import sys
import time

PAT = re.compile(r"^\d+_\d+_\d+$")


def scan(root):
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


def main():
    root, outdir = sys.argv[1], sys.argv[2]
    interval = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    now = int(time.time())
    prev = scan(root)
    with open(os.path.join(outdir, "keys-snapshot-%d.tsv" % now), "w") as f:
        f.write("key\tatime\tctime\tsize\n")
        for k, (a, c, s) in sorted(prev.items()):
            f.write("%s\t%d\t%d\t%d\n" % (k, a, c, s))
    ev = open(os.path.join(outdir, "keys-events.tsv"), "a", buffering=1)
    while True:
        time.sleep(interval)
        now = int(time.time())
        cur = scan(root)
        for k in cur.keys() - prev.keys():
            a, c, s = cur[k]
            ev.write("%d\tadd\t%s\t%d\t%d\t%d\n" % (now, k, a, c, s))
        for k in prev.keys() - cur.keys():
            a, c, s = prev[k]
            ev.write("%d\tdel\t%s\t%d\t%d\t%d\n" % (now, k, a, c, s))
        prev = cur


if __name__ == "__main__":
    main()
