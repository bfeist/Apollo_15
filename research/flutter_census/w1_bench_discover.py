"""W1: micro-benchmark v5 production discover()/trajectory() on one 68 s context."""
import sys, time, json
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts' / '05_deflutter'))
sys.path.insert(0, 'scripts/05_deflutter')
from common import inventory, read, exact_mains, remove_stationary
from flutter_tracking import discover, trajectory

TAPE, T0, DUR = 'A15_T510077', 19715., 68.
out = {}
inv = inventory(TAPE)
chs = sorted(inv)
t = time.time()
xs = {ch: read(TAPE, ch, T0, DUR) for ch in chs}
t_read = time.time() - t
out['read_30x68s_s'] = round(t_read, 3)

t = time.time()
disc = {}
for ch in chs:
    x = xs[ch]
    if not x.any():
        continue
    disc[ch] = discover(x, 8000, 8)
t_disc = time.time() - t
out['discover_30ch_s'] = round(t_disc, 3)
out['discover_per_ch_ms'] = round(1000 * t_disc / max(len(disc), 1), 1)
out['discover_cands_per_ch'] = round(float(np.mean([len(v) for v in disc.values()])), 1)

# trajectory: take up to 8 candidates from 3 channels, all capture_candidates
from carrier_capture import capture_candidates
t = time.time()
ntr = 0
for ch in [c for c in chs if c in disc][:3]:
    x = xs[ch]
    for c in disc[ch][:8]:
        for cap in capture_candidates(c['carrier_hz'], 8000, (2., 30.)):
            trajectory(x, 8000, c['carrier_hz'], cap)
            ntr += 1
t_traj = time.time() - t
out['trajectory_s'] = round(t_traj, 3)
out['trajectory_n'] = ntr
out['trajectory_ms_each'] = round(1000 * t_traj / max(ntr, 1), 1)
# projected full-tape survey compute (30 ch x 1019 windows x (discover + 8 cands x 2 caps x traj))
per_win_ch = out['discover_per_ch_ms'] / 1000 + 8 * 2 * out['trajectory_ms_each'] / 1000
out['projected_survey_s_per_tape_1core'] = round(1019 * 30 * per_win_ch, 0)
Path('reports/flutter_census_20261115/w1_profiling/discover_bench.json').write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
