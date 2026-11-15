"""E4: blind seed search -- no known reference, no check tones.

Per channel: find spectral humps/peaks on a log-frequency axis (resolution
0.25%, smoothing ~1%), track each with a capture wide enough for percent-level
FM, and compute its 2-30 Hz fractional deviation. Tape motion is common to all
tracks, so candidates from different channels and unrelated frequencies agree
(r high at ~zero lag); stationary/post-tape lines give independent noise.

Output: candidate list, agreement graph, the largest agreeing cluster and its
seed curve (weighted mean). Validation of the seed is done by E2/E3 controls.
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
from scipy import ndimage, signal
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, corr, demod, frac_dev, inventory, read, exact_mains, remove_stationary


def candidates(x, per_ch=10, fmin=50., fmax=3900.):
    f, p = signal.welch(x, SR, nperseg=2 * SR, noverlap=SR)
    lf = np.exp(np.arange(np.log(fmin), np.log(fmax), .0025))
    lp = np.interp(lf, f, 10 * np.log10(p + 1e-30))
    sm = ndimage.gaussian_filter1d(lp, 3)            # ~0.75% smoothing
    bg = ndimage.median_filter(sm, size=161, mode='nearest')  # ~40% window
    pk, prop = signal.find_peaks(sm - bg, height=3, prominence=3, distance=8)
    order = np.argsort(-prop['peak_heights'])[:per_ch * 2]
    rows = []
    for i in pk[order]:
        hz = float(lf[i])
        if 2150 < hz < 2850:  # keep check-tone band as independent witness
            continue
        if abs(hz - 60 * round(hz / 60)) < .5 and hz < 1000:
            # exact digitization comb at low f: little FM leverage, skip
            continue
        rows.append(dict(hz=hz, prom_db=float((sm - bg)[i])))
    return rows[:per_ch]


import os
CAP_A = float(os.environ.get('CENSUS_CAP_A', '.04'))
CAP_B = float(os.environ.get('CENSUS_CAP_B', '14'))


def track(x, hz):
    cap = float(np.clip(CAP_A * hz + CAP_B, 8., min(120., hz - 20.)))
    amp, fr = demod(x, SR, hz, cap)
    e, ok, c = frac_dev(fr, amp, band=(2., 30.), capture=cap)
    # flutter-likeness: share of e power in 4-10 Hz
    f, pe = signal.welch(e[ok] if ok.sum() > 2000 else e, RATE, nperseg=1024)
    share = float(pe[(f > 4) & (f < 10)].sum() / max(pe[(f > 2) & (f < 30)].sum(), 1e-30))
    hb = signal.sosfiltfilt(signal.butter(3, [35, 90], fs=RATE, btype='band', output='sos'), (fr - c) / c)
    return e, ok, dict(hz=hz, centre=c, cap=cap, support=float(ok.mean()),
                       rms_pct=100 * float(e.std()), flutter_share=share,
                       noise_pct=100 * float(hb[ok].std()) if ok.any() else 99.)


def run(tape, t0, dur, per_ch=10, r_min=.6):
    t = time.time()
    C = []
    for ch in sorted(inventory(tape)):
        x = read(tape, ch, t0, dur)
        if not x.any():
            continue
        x = remove_stationary(x, exact_mains(x))
        for r in candidates(x, per_ch):
            e, ok, m = track(x, r['hz'])
            if m['support'] < .5:
                continue
            m.update(ch=ch, prom_db=r['prom_db']); m['e'] = e
            C.append(m)
    edge = slice(4 * RATE, -4 * RATE)
    n = len(C)
    R = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            R[i, j] = R[j, i] = corr(C[i]['e'][edge], C[j]['e'][edge])
    # independent agreement: different channel AND unrelated frequency (not same Hz)
    def indep(i, j):
        a, b = C[i], C[j]
        return a['ch'] != b['ch'] and abs(a['centre'] - b['centre']) > .02 * max(a['centre'], b['centre'])
    A = np.array([[R[i, j] > r_min and indep(i, j) for j in range(n)] for i in range(n)]) if n else np.zeros((0, 0))
    deg = A.sum(1) if n else np.array([])
    # cluster: seed = best-degree node, members = nodes agreeing with seed (r > r_min, any relation)
    out = dict(tape=tape, t0=t0, dur=dur, n_candidates=n, seconds=None)
    if n and deg.max() > 0:
        s = int(np.argmax(deg + 1e-3 * np.array([c['flutter_share'] for c in C])))
        mem = [s] + [j for j in range(n) if j != s and R[s, j] > r_min]
        # gain-normalise members to the seed node (contaminated tracks read low),
        # weight by coherence r^2/(1-r^2); then rescale to the best member's rms.
        es = C[s]['e'][edge]
        acc, wsum = 0., 0.
        for j in mem:
            rj = .95 if j == s else min(R[s, j], .95)
            g = float(np.dot(C[j]['e'][edge], es) / np.dot(es, es)) if j != s else 1.
            if g < .3:
                continue
            w = rj ** 2 / (1 - rj ** 2)
            acc = acc + w * C[j]['e'] / g; wsum += w
        seed = acc / wsum
        # amplitude: median rms of the strongest-correlated, least-contaminated members
        top = sorted(mem, key=lambda j: -(R[s, j] if j != s else 1.))[:5]
        target = float(np.median([C[j]['e'][edge].std() for j in top]))
        seed *= target / max(seed[edge].std(), 1e-12)
        out.update(seed_index=s, cluster=[dict(ch=C[j]['ch'], hz=round(C[j]['centre'], 2),
                                                r_to_seed=round(float(R[s, j]) if j != s else 1., 3),
                                                rms_pct=round(C[j]['rms_pct'], 3)) for j in mem],
                   n_independent_partners=int(deg[s]),
                   cluster_channels=sorted({C[j]['ch'] for j in mem}),
                   seed_rms_pct=100 * float(seed[edge].std()))
        out['_seed'] = seed
    out['candidates'] = [dict({k: v for k, v in c.items() if k != 'e'}, degree=int(deg[i])) for i, c in enumerate(C)]
    out['seconds'] = time.time() - t
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tape'); ap.add_argument('t0', type=float); ap.add_argument('dur', type=float)
    ap.add_argument('--out')
    a = ap.parse_args()
    o = run(a.tape, a.t0, a.dur)
    seed = o.pop('_seed', None)
    print(f"{o['n_candidates']} candidates, {o['seconds']:.1f}s")
    if seed is not None:
        print('independent partners of seed:', o['n_independent_partners'], 'channels', o['cluster_channels'],
              f"seed rms {o['seed_rms_pct']:.2f}%")
        for c in o['cluster'][:30]:
            print('  ', c)
    else:
        print('no agreeing cluster')
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(o, indent=1))
        if seed is not None:
            np.save(Path(a.out).with_suffix('.npy'), seed)


if __name__ == '__main__':
    main()
