"""E10: development-only diagnostic of gain / band / lag of the blind consensus,
scored on check beeps of the beeps1 windows (DEV set). Confirm on a separate
set before adopting anything."""
import sys, json, glob, os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).parent)); sys.path.insert(0, str(Path(__file__).parents[2] / 'scripts/30-track/05_deflutter'))
from common import read, inventory, RATE, SR
from flutter_tracking import beeps, evaluate_events, bound

VARIANTS = [(g, b, l) for g in (1.0, 1.15, 1.3, 1.5) for b in ((2, 30), (3, 16)) for l in (0, -0.01, 0.01, -.025, .025)]


def job(npy):
    tape, t0 = Path(npy).stem.rsplit('_', 1); t0 = float(t0)
    e0 = np.load(npy).astype(float)
    gate = np.ones_like(e0); gate[:4 * RATE] = 0; gate[-4 * RATE:] = 0
    out = {}
    evs = {}
    for ch in sorted(inventory(tape)):
        x = read(tape, ch, t0, 64.)
        ev = [b for b in beeps(x, SR) if 5 < b['start'] < 59] if x.any() else []
        if ev: evs[ch] = (x, ev)
    for g, b, l in VARIANTS:
        e = e0.copy()
        if b != (2, 30):
            e = signal.sosfiltfilt(signal.butter(3, b, fs=RATE, btype='band', output='sos'), e)
        if l:
            tt = np.arange(len(e)) / RATE; e = np.interp(tt - l, tt, e)
        e = bound(g * e, gate)
        vals = []
        for ch, (x, ev) in evs.items():
            for r in evaluate_events(x, SR, e, ev):
                if r['margins']:
                    vals.append(float(np.median([q['after_rms_hz'] for q in r['margins']])))
        out[str((g, b, l))] = vals
    return tape, t0, out


if __name__ == '__main__':
    files = sorted(glob.glob(sys.argv[1] + '/*.npy'))
    agg = {}
    with ProcessPoolExecutor(6) as ex:
        for tape, t0, out in ex.map(job, files):
            for k, v in out.items(): agg.setdefault(k, []).extend(v)
    rows = sorted(((float(np.median(v)), float(np.percentile(v, 90)), k, len(v)) for k, v in agg.items()))
    for r in rows[:12]: print(r)
    print('baseline', [r for r in rows if r[2] == str((1.0, (2, 30), 0))])
    Path(sys.argv[2]).write_text(json.dumps(rows, indent=1))
