#!/usr/bin/env python3
"""VM ディスク（inode 634040）の全 chunk の record の数（LLEN）を一定間隔で読み、分布を TSV に追記する（読み取りのみ）。

差分方式では chunk の record が増える（見込みは平均 280〜390）。読み込みのたびの Redis の LRANGE の量の目安として、
record の数の分布（合計・平均・中央・90%・最大・350 超の chunk の数）を残す。書き込み・削除は一切しない。
接続情報は cache_breakdown.py の connect_from_script（mount のスクリプトの MOUNT_FROM を読み、表示しない）を使う。
使い方: llen_sampler.py OUT.tsv [--interval 600] [--inode 634040] [--nchunks 4800]
出力列: UNIX 秒、ISO 時刻、record のある chunk の数、record の合計、平均、中央、90%、最大、350 超の chunk の数
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cache_churn", "2026-10-19"))
from cache_breakdown import connect_from_script  # noqa: E402  読み取り専用 RESP クライアント


def sample(r, inode, nchunks):
    """全 chunk の LLEN をパイプラインで取り、record のある chunk の値のリストを返す。"""
    r.send(*[("LLEN", f"c{inode}_{i}") for i in range(nchunks)])
    return [x for x in (r.read() for _ in range(nchunks)) if x]


def main():
    """一定間隔で LLEN の分布を取り、TSV に追記し続ける。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--script", default=os.path.expanduser("~/utilities/cloudfs/juicefs_mount.sh"))
    ap.add_argument("--inode", type=int, default=634040)
    ap.add_argument("--nchunks", type=int, default=4800)
    ap.add_argument("--interval", type=int, default=600)
    a = ap.parse_args()
    r = None
    while True:
        try:
            if r is None:
                r = connect_from_script(a.script)
            lens = sorted(sample(r, a.inode, a.nchunks))
            now = int(time.time())
            n = len(lens)
            row = [now, time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), n, sum(lens),
                   f"{sum(lens) / n:.1f}" if n else 0, lens[n // 2] if n else 0,
                   lens[min(n - 1, n * 9 // 10)] if n else 0, lens[-1] if n else 0, sum(1 for x in lens if x > 350)]
        except Exception as e:  # 取得の失敗は記録し、次回は接続し直して続ける（計測を止めない）
            r = None
            now = int(time.time())
            row = [now, time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "error", type(e).__name__]
        with open(a.out, "a") as f:
            f.write("\t".join(str(x) for x in row) + "\n")
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
