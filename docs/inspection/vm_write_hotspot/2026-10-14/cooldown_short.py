"""Compare short cooldowns with read-min only using staged_sim's model."""
import sys
sys.path.insert(0, '.')
import staged_sim as s
log, acc, end = sys.argv[1:4]
fz, init, ob, rd, tend = s.load(log, acc, end)
for p in [s.Policy('read-min 100', read_min=100)] + [s.Policy(f'read-min 100 + cooldown {c}s force 700', read_min=100, cooldown=c, force=700) for c in (2, 5, 10, 30, 60)]:
    r = s.simulate(p, fz, init, ob, rd, tend, 18, 3.77, 8.8)
    print(f'{p.name:40s} comp={r["compactions"]:4d} PUT={r["put_total"]:5d} (writer {r["put_writer"]}, compaction {r["put_compaction"]}) retired={r["retired_writer"] / r["frozen_writer_blocks"]:.0%}')
