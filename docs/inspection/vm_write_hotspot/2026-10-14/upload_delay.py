"""Compare --upload-delay values on top of read-min 100 with staged_sim's model."""
import sys
sys.path.insert(0, '.')
import staged_sim as s
log, acc, end = sys.argv[1:4]
fz, init, ob, rd, tend = s.load(log, acc, end)
for d in (0, 10, 30, 60, 120):
    s.UPLOAD_DELAY = d
    for p in (s.Policy('read-min 100', read_min=100), s.Policy('kaz.5', read_min=5)):
        r = s.simulate(p, fz, init, ob, rd, tend, 18, 3.77, 8.8)
        print(f'delay {d:4d}s {p.name:13s} comp={r["compactions"]:4d} PUT={r["put_total"]:5d} (writer {r["put_writer"]}, compaction {r["put_compaction"]}) retired={r["retired_writer"] / r["frozen_writer_blocks"]:.0%}')
