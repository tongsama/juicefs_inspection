"""Count compactions per chunk of inode 627902 and the bytes they rewrote, from
the fixed DEBUG log prefix (log.prefix), to show write amplification."""
import collections
import re

LINE = re.compile(r'compact 627902:(\d+): skipped (\d+) slices \((\d+) bytes\) (\d+) slices \((\d+) bytes\)')


def main():
    """Print per-chunk compaction counts, merged slice counts and rewritten bytes."""
    per = collections.Counter()
    merged = collections.Counter()
    total_bytes = 0
    for line in open('log.prefix', errors='replace'):
        m = LINE.search(line)
        if not m:
            continue
        c = int(m.group(1))
        per[c] += 1
        merged[c] += int(m.group(4))
        total_bytes += int(m.group(5))
    print('compactions', sum(per.values()), 'rewritten GiB', round(total_bytes / 2**30, 2))
    for c, n in per.most_common(8):
        print(f' chunk {c}: compactions={n} merged_slices={merged[c]}')


if __name__ == '__main__':
    main()
