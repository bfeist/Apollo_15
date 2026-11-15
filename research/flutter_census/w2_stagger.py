"""W2 stagger test: find channels whose CONTENT is ~1.3 s delayed relative to
the window ensemble (speech-envelope cross-correlation), then test whether
their flutter curve needs a second, content-delayed component in addition to
the zero-lag one. Uses saved w2_lag NPZ curves + window JSON."""
import json, sys
from pathlib import Path
import numpy as np
from scipy import ndimage, signal
sys.path.insert(0, str(Path(__file__).parent))
from common import SR, corr, inventory, read
from w2_lag import EDGE, two_component

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('reports/flutter_census_20261115/w2_lag')
WINDOWS = ['A15_T510077_19715', 'A15_T510077_44595', 'A15_T510077_3822', 'A15_T510077_57328',
           'A15_T510083_13920', 'A15_T510083_27105', 'A15_T510102_3682']
RATE_ENV = 100  # envelope rate


def envelope(tape, ch, t0, dur=64.):
    x = read(tape, ch, t0, dur)
    if not x.any():
        return None
    b, a = signal.butter(4, 20., fs=SR)
    env = np.abs(signal.filtfilt(b, a, x))
    step = SR // RATE_ENV
    return env[:len(env) // step * step].reshape(-1, step).mean(1)


def env_best_lag(ref, y, lo=0.5, hi=2.5):
    """Best |lag| in [lo,hi] s and the lag-0 correlation; normalized envelopes."""
    y = y - y.mean(); ref = ref - ref.mean()
    cc = signal.fftconvolve(y, ref[::-1], mode='full')
    lags = np.arange(-len(y) + 1, len(y))
    k = np.abs(lags) <= int(hi * RATE_ENV)
    ck, lk = cc[k], lags[k]
    norm = np.sqrt((y * y).sum() * (ref * ref).sum()) + 1e-30
    m = (np.abs(lk) >= lo * RATE_ENV)
    i = int(np.argmax(np.abs(ck[m]))) if m.any() else None
    best_far = (float(lk[m][i] / RATE_ENV), float(abs(ck[m][i]) / norm)) if i is not None else (None, 0.)
    zero = float(abs(cc[lags == 0][0]) / norm)
    return best_far[0], best_far[1], zero


def run(w, outdir=OUT):
    o = json.loads((outdir / f'{w}.json').read_text())
    tape, t0 = o['tape'], o['t0']
    z = np.load(outdir / f'{w}.npz')
    ch_of = {int(str(k)[2:]): int(z[f'ch{str(k)[2:]}']) for k in z.files if k.startswith('ch')}
    envs = {}
    for ch in sorted(map(int, inventory(tape))):
        e = envelope(tape, ch, t0)
        if e is not None and e.std() > 1e-6:
            envs[ch] = e
    ref_env = np.median([e / e.std() for e in envs.values()], axis=0)
    rows = {}
    for ch, e in envs.items():
        lag, r_far, r0 = env_best_lag(ref_env, e)
        rows[ch] = dict(best_far_lag_s=round(lag, 2) if lag is not None else None,
                        r_far=round(r_far, 3), r_zero=round(r0, 3))
    stag = {c: v for c, v in rows.items() if v['r_far'] >= .5 and v['best_far_lag_s'] is not None
            and 1.0 <= abs(v['best_far_lag_s']) <= 1.7}
    print(w, 'staggered-content channels:', {c: (v['best_far_lag_s'], v['r_far']) for c, v in stag.items()})
    # two-component flutter test for staggered channels; if none, run it on the
    # single channel with the strongest far-lag envelope match (negative control)
    ens = z['ens']
    res = {}
    if not stag:
        top = max(rows, key=lambda c: rows[c]['r_far'])
        stag = {top: rows[top]}
        print('  negative control: top far-lag ch', top, rows[top])
    for ch in sorted(stag):
        best = None
        for idx, c2 in ch_of.items():
            if c2 != ch:
                continue
            rr = corr(z[f'c{idx}'][EDGE], ens[EDGE])
            if best is None or rr > best[0]:
                best = (rr, idx)
        if best and best[0] > .4:
            tc = two_component(z[f'c{best[1]}'], ens)
            res[ch] = dict(r=round(best[0], 3), **tc)
            print('  ch', ch, 'r', res[ch]['r'], 'r1', tc['r1'], 'best_d', tc['best_d'],
                  'r2', tc['r2'], 's2/s1', tc['s2_vs_s1'], 'b_1369', tc['b_1369'], 'r_1369', tc['r_1369'])
    return dict(envelope=rows, staggered=stag, two_comp=res)


if __name__ == '__main__':
    allres = {}
    for w in WINDOWS:
        allres[w] = run(w)
    (OUT / 'w2_stagger.json').write_text(json.dumps(allres, indent=1))
