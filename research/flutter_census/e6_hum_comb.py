"""E6: historical-mains (ENF-style) comb on tape.

The 1971 mains hum recorded on the tape appears at n*f_h with f_h = 60*rho
(rho = playback/record speed ratio, ~0.985 on T510077) and carries the tape
flutter. Digitization hum at exact 60n does not. Steps per channel:
 1. remove exact stationary 60n lines (block LS);
 2. comb score over f_h in 57.5..60.5 Hz: sum over n of power within
    n*f_h*(1 +- 2.5 %) relative to a +-(6..10 %) side background;
 3. for the best f_h, track harmonics n=2..K (capture 0.035*n*f_h + 8 Hz,
    limited away from exact 60n residue), combine FM with coherence weights.
"""
import sys, json, time
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).parent))
from common import *


def comb_score(x, f0s, nmax=16, sr=SR):
    f, p = signal.welch(x, sr, nperseg=4 * sr)
    cs = np.concatenate([[0], np.cumsum(p)])
    def band(lo, hi):
        i, j = np.searchsorted(f, lo), np.searchsorted(f, hi)
        return (cs[j] - cs[i]) / max(j - i, 1)
    out = []
    for f0 in f0s:
        s = 0.
        for n in range(2, nmax + 1):
            c = n * f0
            if c > 3900: break
            w = max(.025 * c, 1.5)
            on = band(c - w, c + w)
            off = .5 * (band(c - 4 * w, c - 2 * w) + band(c + 2 * w, c + 4 * w))
            s += np.log10(on / max(off, 1e-30))
        out.append(s)
    return np.array(out)


def comb_fm(x, f0, ns=range(2, 15)):
    tr = []
    for n in ns:
        c = n * f0
        if c > 3800: break
        cap = float(np.clip(.035 * c + 8, 6, 120))
        amp, fr = demod(x, SR, c, cap)
        e, ok, cc = frac_dev(fr, amp, band=(2., 30.), capture=cap, centre=c)
        tr.append((n, e, ok))
    return tr


def main(tape, t0, dur=64.):
    t = time.time()
    f0s = np.arange(57.5, 60.51, .01)
    rows = {}
    E = []
    for ch in sorted(inventory(tape)):
        x = read(tape, ch, t0, dur)
        if not x.any(): continue
        x = remove_stationary(x, exact_mains(x))
        sc = comb_score(x, f0s)
        i = int(np.argmax(sc))
        rows[ch] = dict(f0=round(float(f0s[i]), 3), score=round(float(sc[i]), 2),
                        score_median=round(float(np.median(sc)), 2))
        for n, e, ok in comb_fm(x, f0s[i]):
            E.append((ch, n, e, ok))
    edge = slice(800, -800)
    # pairwise correlation of harmonic FM tracks across different channels
    M = np.array([e[edge] for _, _, e, _ in E])
    Mn = (M - M.mean(1, keepdims=True)) / (M.std(1, keepdims=True) + 1e-12)
    R = Mn @ Mn.T / M.shape[1]
    lab = [(c, n) for c, n, _, _ in E]
    good = []
    for i, (c, n) in enumerate(lab):
        other = [j for j, (c2, n2) in enumerate(lab) if c2 != c]
        rr = np.sort(R[i, other])[::-1]
        good.append((float(np.median(rr[:5])), c, n, float(M[i].std() * 100)))
    good.sort(reverse=True)
    print(tape, t0, f'{time.time()-t:.0f}s')
    print('per-channel comb f0/score:', {c: (r['f0'], r['score']) for c, r in rows.items()})
    print('top harmonic tracks (median r to 5 best other-channel tracks, ch, n, rms%):')
    for g in good[:25]: print('  ', [round(g[0], 3), g[1], g[2], round(g[3], 2)])
    return rows, good


if __name__ == '__main__':
    main(sys.argv[1], float(sys.argv[2]))
