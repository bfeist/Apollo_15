"""E14: contiguous overlapping windows (64 s, step 50 s) -> eligible fraction
and curve agreement in overlaps (independent re-estimates of the same time)."""
import sys, os, json, glob
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from e7_channel_loo import _job
from common import RATE, corr

if __name__ == '__main__':
    out, tape, a, b, step = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
    Path(out).mkdir(parents=True, exist_ok=True)
    os.environ['OMP_NUM_THREADS'] = '1'
    work = [(tape, float(t), out) for t in range(a, b, step)]
    res = []
    with ProcessPoolExecutor(12) as ex:
        for o in ex.map(_job, work):
            res.append(o)
    el = [o for o in res if o.get('eligible')]
    # overlap agreement between consecutive eligible windows: valid overlap
    # = [t0+step+4, t0+60] of the earlier window
    rs = []
    for o in res:
        t0 = int(o['t0']); p1 = Path(out) / f'{tape}_{t0}.npy'; p2 = Path(out) / f'{tape}_{t0+step}.npy'
        if p1.exists() and p2.exists():
            e1, e2 = np.load(p1), np.load(p2)
            s1 = slice((step + 4) * RATE, 60 * RATE); s2 = slice(4 * RATE, (60 - step) * RATE)
            if s1.stop > s1.start:
                rs.append(corr(e1[s1].astype(float), e2[s2].astype(float)))
    summ = dict(tape=tape, start=a, end=b, step=step, windows=len(res), eligible=len(el),
                overlap_r_median=float(np.median(rs)) if rs else None, overlap_r_p10=float(np.percentile(rs, 10)) if rs else None,
                ineligible=[o['t0'] for o in res if not o.get('eligible')])
    print(json.dumps(summ))
    Path(out, 'summary.json').write_text(json.dumps(summ, indent=1))
