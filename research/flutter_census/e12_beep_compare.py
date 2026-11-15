"""E12: compare check-beep residuals for several curve sets on the same windows."""
import sys, glob, os, json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from e8_beep_check import run


def job(a):
    d, f = a
    tape, t0 = Path(f).stem.rsplit('_', 1)
    return d, Path(f).stem, run(tape, float(t0), f)


if __name__ == '__main__':
    dirs = sys.argv[1].split(',')
    stems = sorted(Path(f).stem for f in glob.glob(dirs[0] + '/*.npy'))
    work = [(d, f'{d}/{s}.npy') for d in dirs for s in stems if Path(f'{d}/{s}.npy').exists()]
    res = {}
    with ProcessPoolExecutor(6) as ex:
        for d, s, rows in ex.map(job, work):
            res.setdefault(d, {})[s] = rows
    for d in dirs:
        a = np.array([[r['identity'], r['curve'], r['wrong_sign']] for s in res[d] for r in res[d][s]])
        print(d, 'n', len(a), 'median id/curve/wrong', np.round(np.median(a, 0), 2).tolist(),
              'median ratio', round(float(np.median(a[:, 1] / a[:, 0])), 3), 'p90', round(float(np.percentile(a[:, 1], 90)), 1),
              'share<=6Hz', round(float(np.mean(a[:, 1] <= 6)), 3), 'worse', int((a[:, 1] > a[:, 0]).sum()))
        for s in res[d]:
            b = np.array([[r['identity'], r['curve']] for r in res[d][s]])
            if len(b): print('   ', s, len(b), np.round(np.median(b, 0), 1).tolist())
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(res, indent=1))
