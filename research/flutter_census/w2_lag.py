"""W2: per-channel flutter lag census -- is one shared zero-lag speed map right?

Per window: on every channel remove stationary digitization lines, then track
the historical on-tape mains hum (~59.3-59.9 / 118-119.5 Hz, fluttered) with
common.demod/frac_dev to get a 200 Hz fractional speed-error curve. Estimate
each channel's lag vs a robust per-window ensemble reference (and, for the
primary window, vs the CH30 buzz harmonic) by cross-correlation with parabolic
sub-sample interpolation, plus gain. Two-component (zero-lag + content-delayed)
model test for channels whose content is staggered. Results -> JSON/NPZ.

Research only; writes only to the given output directory.
"""
import json, os, sys, time
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, corr, demod, frac_dev, exact_mains, inventory, read, remove_stationary

EDGE = slice(4 * RATE, -4 * RATE)  # 4 s zeroed at each end of the 64 s window


def hum_band_peaks(x):
    """Fallback: strongest non-exact peak in the known hum ranges."""
    f, p = signal.welch(x, SR, nperseg=8 * SR, noverlap=4 * SR)
    lp = 10 * np.log10(p + 1e-30)
    out = []
    for lo, hi in ((58.8, 59.95), (117.8, 119.9)):
        k = (f >= lo) & (f <= hi)
        i = int(np.argmax(lp[k]))
        snr = float(lp[k][i] - np.median(lp[k]))
        if snr > 4.:
            out.append(float(f[k][i]))
    return out


def track_curve(x, hz, cap=None):
    """Demod/frac_dev track of one line; returns dict with e (200 Hz) or None."""
    cap = float(np.clip(.04 * hz + 14., 10., 30.)) if cap is None else float(cap)
    amp, fr = demod(x, SR, hz, cap)
    e, ok, c = frac_dev(fr, amp, band=(2., 30.), capture=cap)
    if ok.mean() < .5:
        return None
    rms = float(e[EDGE].std())
    if rms < .003:
        return None
    return dict(hz=hz, cap=cap, support=round(float(ok.mean()), 3), rms_pct=round(100 * rms, 3), e=e)


def channel_curves(tape, ch, t0, dur=64., per_ch=6):
    """Flutter curves for one channel: e4 candidates + forced hum-band peaks."""
    from e4_blind_seed import candidates
    x = read(tape, ch, t0, dur)
    if not x.any():
        return []
    x = remove_stationary(x, exact_mains(x))
    hzs = [r['hz'] for r in candidates(x, per_ch)]
    hzs += hum_band_peaks(x)
    out, used = [], []
    for hz in hzs:
        if any(abs(hz - s) < .02 * max(hz, s) for s in used):
            continue
        m = track_curve(x, hz)
        if m is not None:
            used.append(hz)
            out.append(m)
    return out


def xlag(ref, y, maxlag=2.0):
    """Lag of y relative to ref (s, positive = y delayed), parabolic sub-sample,
    plus correlation r. Returns (lag_s, r)."""
    a = ref - ref.mean(); b = y - y.mean()
    na, nb = (a * a).sum(), (b * b).sum()
    if na <= 0 or nb <= 0:
        return None, 0.
    cc = signal.fftconvolve(b, a[::-1], mode='full')
    lags = np.arange(-len(a) + 1, len(a))
    k = np.abs(lags) <= int(maxlag * RATE)
    ck, lk = cc[k], lags[k]
    i = int(np.argmax(ck))
    lag = float(lk[i])
    if 0 < i < len(ck) - 1:
        y0, y1, y2 = ck[i - 1], ck[i], ck[i + 1]
        d = .5 * (y0 - y2) / (y0 - 2 * y1 + y2 + 1e-30)
        if abs(d) <= 1:
            lag += float(np.clip(d, -.5, .5))
    r = float(ck[i] / np.sqrt(na * nb))
    return lag / RATE, r


def aligned(y, lag):
    """y advanced by lag so it lines up with the reference time base."""
    t = np.arange(len(y))
    return np.interp(t, t + lag, y, left=0., right=0.)


def gain_of(y, ref, lag, r_min=.4):
    """LS gain of y on ref after removing y's lag; None if too weak."""
    if abs(r := corr(aligned(y, lag)[EDGE], ref[EDGE])) < r_min:
        return None
    a, b = aligned(y, lag)[EDGE], ref[EDGE]
    return float((a * b).sum() / (b * b).sum())


def build_ensemble(curves):
    """Robust reference: pivot = curve with most agreeing partners (r>.5),
    then one re-estimate against the gain-normalised weighted mean."""
    n = len(curves)
    if n < 4:
        return None
    E = np.array([c['e'] for c in curves])
    E = (E - E.mean(1, keepdims=True)) / (E.std(1, keepdims=True) + 1e-12)
    R = E @ E.T / E.shape[1]
    np.fill_diagonal(R, 0)
    deg = (R > .5).sum(1)
    s = int(np.argmax(deg))
    if deg[s] < 3:
        return None
    ref = curves[s]['e']
    for _ in range(2):
        acc, wsum = 0., 0.
        for i in range(n):
            lag, r = xlag(ref, curves[i]['e'])
            g = gain_of(curves[i]['e'], ref, lag)
            if r > .5 and g is not None and .3 < g < 3:
                w = r * r / (1 - min(r, .95) ** 2)
                acc = acc + w * aligned(curves[i]['e'], lag) / g
                wsum += w
        if wsum == 0:
            return None
        ens = acc / wsum
        ens = ens / max(ens[EDGE].std(), 1e-12) * ref[EDGE].std()
        ref = ens
    return ref, s


def two_component(y, ref, d_grid=np.arange(-2., 2.001, .05), fixed=None):
    """Compare single-component y~a*ref(t) vs y~a*ref(t)+b*ref(t+d).
    Returns dict(best_d, a1, r1, a2, b2, r2, d_1369, r_1369)."""
    yb, rb = y[EDGE], ref[EDGE]
    a1 = float((rb * yb).sum() / (rb * rb).sum())
    res1 = yb - a1 * rb
    r1 = corr(yb, rb)
    def fit2(d):
        rd = np.interp(np.arange(len(ref)) + d * RATE, np.arange(len(ref)), ref, left=0., right=0.)[EDGE]
        A = np.stack([rb, rd], 1)
        coef, *_ = np.linalg.lstsq(A, yb, rcond=None)
        res = yb - A @ coef
        return coef, corr(yb, A @ coef), float(np.sqrt((res * res).sum()))
    best = None
    for d in d_grid:
        if abs(d) < .1:
            continue
        coef, r2, s2 = fit2(d)
        if best is None or s2 < best[2]:
            best = (d, coef, r2, s2)
    (d, coef, r2, s2) = best
    s1 = float(np.sqrt((res1 * res1).sum()))
    coef9, r9, _ = fit2(-1.369)
    return dict(best_d=round(d, 3), a1=round(a1, 3), r1=round(r1, 3), s1=round(s1, 1),
                a2=round(float(coef[0]), 3), b2=round(float(coef[1]), 3),
                r2=round(r2, 3), s2=round(s2, 1), s2_vs_s1=round(s2 / max(s1, 1e-9), 3),
                d_1369=-1.369, r_1369=round(r9, 3),
                a_1369=round(float(coef9[0]), 3), b_1369=round(float(coef9[1]), 3))


def window_job(tape, t0, outdir, dur=64., ref_spec=None):
    t = time.time()
    curves, missing = [], []
    for ch in sorted(inventory(tape)):
        got = channel_curves(tape, ch, t0, dur)
        if not got:
            missing.append(ch)
        for m in got:
            m['ch'] = ch
            curves.append(m)
    out = dict(tape=tape, t0=t0, dur=dur, n_curves=len(curves), missing=missing)
    ens = build_ensemble(curves)
    if ens is None:
        out.update(eligible=False, seconds=time.time() - t)
        return out
    ens, s_pivot = ens
    out['pivot'] = dict(ch=curves[s_pivot]['ch'], hz=curves[s_pivot]['hz'])
    rows = []
    for i, c in enumerate(curves):
        lag, r = xlag(ens, c['e'])
        g = gain_of(c['e'], ens, lag)
        row = dict(ch=c['ch'], hz=c['hz'], rms_pct=c['rms_pct'],
                   lag_s=round(lag, 4), r=round(r, 3), gain=round(g, 3) if g else None)
        if abs(lag) > .5 and r > .6:
            row['two_comp'] = two_component(c['e'], ens)
        rows.append(row)
    # per channel: best curve by r
    per = {}
    for row in rows:
        c = row['ch']
        if c not in per or row['r'] > per[c]['r']:
            per[c] = row
    out['per_channel'] = [per[c] for c in sorted(per)]
    if ref_spec:
        ch, hz, cap = ref_spec['ch'], ref_spec['hz'], ref_spec.get('cap')
        ref_rows = channel_curves(tape, ch, t0, dur)
        m = next((m for m in ref_rows if abs(m['hz'] - hz) < .05), None)
        if m is None:
            refc = track_curve(remove_stationary(read(tape, ch, t0, dur), exact_mains(read(tape, ch, t0, dur))), hz, cap)
        else:
            refc = m
        if refc is not None:
            ref = refc['e'] / max(refc['e'][EDGE].std(), 1e-12) * ens[EDGE].std()
            lagr, rr = xlag(ens, ref)
            out['ref_vs_ensemble'] = dict(ch=ch, hz=hz, lag_s=round(lagr, 4), r=round(rr, 3))
            rrows = []
            for i, c in enumerate(curves):
                lag, r = xlag(ref, c['e'])
                g = gain_of(c['e'], ref, lag)
                rrows.append(dict(ch=c['ch'], hz=c['hz'], lag_s=round(lag, 4), r=round(r, 3),
                                  gain=round(g, 3) if g else None))
                if abs(lag) > .5 and r > .6:
                    rrows[-1]['two_comp'] = two_component(c['e'], ref)
            per2 = {}
            for row in rrows:
                c = row['ch']
                if c not in per2 or row['r'] > per2[c]['r']:
                    per2[c] = row
            out['per_channel_ref'] = [per2[c] for c in sorted(per2)]
    Path(outdir).mkdir(parents=True, exist_ok=True)
    stem = Path(outdir) / f"{tape}_{int(t0)}"
    out['seconds'] = round(time.time() - t, 1)
    stem.with_suffix('.json').write_text(json.dumps(out, indent=1))
    np.savez_compressed(stem.with_suffix('.npz'),
                        ens=ens, **{f"c{i}": c['e'] for i, c in enumerate(curves)},
                        **{f"ch{i}": curves[i]['ch'] for i in range(len(curves))},
                        **{f"hz{i}": curves[i]['hz'] for i in range(len(curves))})
    return out


WINDOWS = [
    ('A15_T510077', 19715.0, dict(ch=30, hz=1005.5, cap=90.5)),
    ('A15_T510077', 44595.0, None),
    ('A15_T510077', 3822.0, None),
    ('A15_T510077', 57328.0, None),
    ('A15_T510083', 13920.0, None),
    ('A15_T510083', 27105.0, None),
    ('A15_T510102', 3682.0, None),
]


def _j(w):
    tape, t0, outdir, ref = w
    try:
        return window_job(tape, t0, outdir, ref_spec=ref)
    except Exception as ex:
        return dict(tape=tape, t0=t0, error=repr(ex))


def _run_all(outdir):
    from concurrent.futures import ProcessPoolExecutor
    jobs = [(t, t0, str(outdir), r) for t, t0, r in WINDOWS]
    with ProcessPoolExecutor(min(6, len(jobs))) as ex:
        for o in ex.map(_j, jobs):
            pc = o.get('per_channel', [])
            lags = [r['lag_s'] for r in pc]
            print(o['tape'], int(o['t0']), 'curves', o.get('n_curves'), 'per_ch', len(pc),
                  'missing', o.get('missing'), 'lags[min,med,max]',
                  [round(min(lags), 3), round(float(np.median(lags)), 3), round(max(lags), 3)] if lags else '-',
                  o.get('seconds'), o.get('error', ''), flush=True)


if __name__ == '__main__':
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    OUT = Path(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == 'one':
        import json as _j2
        print(_j2.dumps(_j((WINDOWS[int(sys.argv[3])][0], WINDOWS[int(sys.argv[3])][1], sys.argv[1],
                             WINDOWS[int(sys.argv[3])][2])), indent=1)[:2000])
    else:
        _run_all(sys.argv[1])
