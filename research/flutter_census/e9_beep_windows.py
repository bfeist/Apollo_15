"""E9: run the blind E7 detector at given windows, then audit check beeps (E8)."""
import sys, json, os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from e7_channel_loo import window
from e8_beep_check import run as beepcheck


def job(a):
    tape, t0, out = a
    p = Path(out) / f'{tape}_{int(t0)}'
    o, seed = window(tape, t0)
    if seed is None:
        return dict(tape=tape, t0=t0, eligible=False, beeps=[])
    np.save(str(p) + '.npy', seed.astype(np.float32))
    rows = beepcheck(tape, t0, str(p) + '.npy')
    o['beeps'] = rows
    Path(str(p) + '.json').write_text(json.dumps(o, indent=1))
    return o


if __name__ == '__main__':
    out = sys.argv[1]; Path(out).mkdir(parents=True, exist_ok=True)
    work = [(w.split(':')[0], float(w.split(':')[1]), out) for w in sys.argv[2].split(',')]
    os.environ['OMP_NUM_THREADS'] = '1'
    allrows = []
    with ProcessPoolExecutor(int(sys.argv[3]) if len(sys.argv) > 3 else 6) as ex:
        for o in ex.map(job, work):
            rows = o.get('beeps', [])
            allrows += [dict(r, tape=o['tape']) for r in rows]
            if rows:
                a = np.array([[r['identity'], r['curve'], r['wrong_sign'], r['shift1.5s']] for r in rows])
                print(o['tape'], o['t0'], 'eligible', o.get('eligible'), 'n_agree', o.get('n_agreeing'), 'beeps', len(rows),
                      'median Hz id/curve/wrong/shift', np.round(np.median(a, 0), 1).tolist(),
                      'worst ratio', round(float(np.max(a[:, 1] / a[:, 0])), 2), flush=True)
            else:
                print(o['tape'], o['t0'], 'eligible', o.get('eligible'), 'no beeps', flush=True)
    Path(out, 'beeps_all.json').write_text(json.dumps(allrows, indent=1))
    if allrows:
        a = np.array([[r['identity'], r['curve'], r['wrong_sign'], r['shift1.5s']] for r in allrows])
        print('ALL n', len(a), 'median', np.round(np.median(a, 0), 2).tolist(), 'median ratio', round(float(np.median(a[:, 1] / a[:, 0])), 3),
              'p90 ratio', round(float(np.percentile(a[:, 1] / a[:, 0], 90)), 3), 'worse', int((a[:, 1] > a[:, 0]).sum()),
              'share<=6Hz', round(float(np.mean(a[:, 1] <= 6)), 3), 'identity<=6Hz', round(float(np.mean(a[:, 0] <= 6)), 3))
