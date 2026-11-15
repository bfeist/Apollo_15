"""E8: quick check-beep audit of a saved blind consensus curve (beeps held out:
the blind detector never uses 2150-2850 Hz)."""
import sys, json
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent)); sys.path.insert(0, str(Path(__file__).parents[2] / 'scripts/05_deflutter'))
from common import read, inventory, RATE, SR
from flutter_tracking import beeps, evaluate_events, bound


def run(tape, t0, npy, dur=64.):
    e = np.load(npy).astype(float)
    gate = np.ones_like(e); gate[:4 * RATE] = 0; gate[-4 * RATE:] = 0
    variants = {'identity': np.zeros_like(e), 'curve': bound(e, gate), 'wrong_sign': bound(-e, gate),
                'shift1.5s': bound(np.roll(e, int(1.5 * RATE)), gate)}
    rows = []
    for ch in sorted(inventory(tape)):
        x = read(tape, ch, t0, dur)
        if not x.any(): continue
        ev = [b for b in beeps(x, SR) if 5 < b['start'] < dur - 5]
        if not ev: continue
        res = {k: evaluate_events(x, SR, v, ev) for k, v in variants.items()}
        for i, b in enumerate(ev):
            m = {k: res[k][i]['margins'] for k in res}
            if not all(m.values()): continue
            rows.append(dict(ch=ch, start=round(t0 + b['start'], 3),
                             **{k: round(float(np.median([q['after_rms_hz'] for q in v])), 2) for k, v in m.items()},
                             before_cents=round(float(np.median([q['before_rms_cents'] for q in m['identity']])), 1),
                             after_cents=round(float(np.median([q['after_rms_cents'] for q in m['curve']])), 1)))
    return rows


if __name__ == '__main__':
    rows = run(sys.argv[1], float(sys.argv[2]), sys.argv[3])
    for r in rows: print(r)
    if rows:
        a = np.array([[r['identity'], r['curve'], r['wrong_sign'], r['shift1.5s']] for r in rows])
        print('n', len(rows), 'median Hz identity/curve/wrong/shift', np.round(np.median(a, 0), 2),
              'median ratio', round(float(np.median(a[:, 1] / a[:, 0])), 3), 'worst ratio', round(float(np.max(a[:, 1] / a[:, 0])), 3),
              'share <=6 Hz', round(float(np.mean(a[:, 1] <= 6)), 2))
    if len(sys.argv) > 4:
        Path(sys.argv[4]).write_text(json.dumps(rows, indent=1))
