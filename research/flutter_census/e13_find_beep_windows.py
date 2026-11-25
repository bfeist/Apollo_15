"""E13: find fresh 64 s windows with check beeps (CH14/15/20 scan) on given tapes,
avoiding times where the saved v5 plan already has use_reference."""
import sys, json, os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent)); sys.path.insert(0, str(Path(__file__).parents[2] / 'scripts/30-track/05_deflutter'))
from common import read, inventory, SR
from flutter_tracking import beeps
from sweep import v5_union, _merge


def scan(a):
    tape, t0 = a
    n = 0
    for ch in (14, 15, 20):
        x = read(tape, ch, t0, 64.)
        if x.any(): n += len([b for b in beeps(x, SR) if 5 < b['start'] < 59])
    return tape, t0, n


if __name__ == '__main__':
    tapes = sys.argv[1].split(','); per = int(sys.argv[2])
    rng = np.random.default_rng(20261115)
    work = []
    for tape in tapes:
        sp, D = v5_union(tape); sp = _merge(sp)
        ts = sorted(rng.uniform(100, D - 100, 40))
        ts = [t for t in ts if not any(a - 64 < t < b for a, b in sp)]
        work += [(tape, float(round(t))) for t in ts]
    os.environ['OMP_NUM_THREADS'] = '1'
    best = {}
    with ProcessPoolExecutor(8) as ex:
        for tape, t0, n in ex.map(scan, work):
            if n >= 3: best.setdefault(tape, []).append((n, t0))
    sel = []
    for tape, v in best.items():
        for n, t0 in sorted(v, reverse=True)[:per]: sel.append(f'{tape}:{int(t0)}')
    print(','.join(sel))
