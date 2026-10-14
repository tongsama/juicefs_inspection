"""Count PUTs of compaction-output slices of inode 627902 within [a, b)."""
import re, sys
log, a, b = sys.argv[1:4]
cre = re.compile(r'compaction inode=627902 chunk=\d+ slice=(\d+) phase=object')
pre = re.compile(r'PUT chunks/\d+/\d+/(\d+)_\d+_\d+ .*err: <nil>')
comp, puts = set(), []
for l in open(log, errors='replace'):
    m = cre.search(l)
    if m:
        comp.add(int(m.group(1)))
        continue
    m = pre.search(l)
    if m and a <= l[:19] < b:
        puts.append(int(m.group(1)))
print(sum(1 for s in puts if s in comp))
