"""E11: autofocus refinement of a blind seed using every on-tape line that
concentrates under the seed (gate prevents the circular-residual pitfall).

For each iteration:
  for each channel: clean (exact mains removed), dewarp by e, find narrow lines
  (snr > 12 dB) outside 2150-2850 Hz, away from exact-mains FM sidebands;
  keep a line if its +-0.25 Hz concentration under +e is >= 2.5x the best of
  {none, -e, 1.5 s shift}; track its residual with a 5 Hz capture; weight by
  line power over local floor; e <- e + robust weighted mean residual.
Output: refined curve (.npy) and per-iteration line counts.
"""
import sys, json, os, glob
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
from scipy import signal, ndimage
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, demod, dewarp, frac_dev, inventory, read, exact_mains, remove_stationary

FM = 6.6


def spec(x, nper=8 * SR):
    f, p = signal.welch(x, SR, nperseg=nper, noverlap=nper // 2)
    lp = 10 * np.log10(p + 1e-30)
    bg = ndimage.median_filter(lp, size=int(30 / (f[1] - f[0])) | 1, mode='nearest')
    return f, p, lp - bg


def conc(f, p, hz):
    c = np.abs(f - hz) <= .25
    w = np.abs(f - hz) <= max(4., .04 * hz)
    return float(p[c].sum() / max(p[w].sum(), 1e-30))


def refine(xs, e, iters=2, cap=5., max_lines=15, exclude=(2150, 2850)):
    hist = []
    for it in range(iters):
        num, den, used = 0., 0., []
        for ch, x in xs.items():
            mains = [60 * n for n in range(1, 66)]
            yd = dewarp(x, e)
            f, pd, sd = spec(yd)
            _, pm, _ = spec(dewarp(x, -e))
            _, p0, _ = spec(x)
            _, ps, _ = spec(dewarp(x, np.roll(e, int(1.5 * RATE))))
            pk, _ = signal.find_peaks(sd, height=12, distance=int(3 / (f[1] - f[0])))
            pk = sorted(pk, key=lambda i: -sd[i])
            k = 0
            for i in pk:
                hz = float(f[i])
                if hz < 45 or hz > 3900 or exclude[0] < hz < exclude[1]:
                    continue
                if any(abs(hz - (m + j * FM)) < .7 for m in mains for j in range(-3, 4)):
                    continue
                cp = conc(f, pd, hz)
                cc = max(conc(f, p0, hz), conc(f, pm, hz), conc(f, ps, hz))
                if cp < 2.5 * cc or cp < .15:
                    continue
                amp, fr = demod(yd, SR, hz, cap)
                r, ok, _ = frac_dev(fr, amp, band=(2., 30.), capture=cap, centre=hz)
                if ok.mean() < .8:
                    continue
                w = 10 ** (sd[i] / 10) * cp * ok
                num = num + w * r; den = den + w
                used.append((ch, round(hz, 2), round(cp / cc, 1)))
                k += 1
                if k >= max_lines:
                    break
        if not used:
            hist.append(dict(iter=it, lines=0)); break
        upd = num / np.maximum(den, 1e-30)
        upd[den <= 0] = 0
        e = e + upd
        hist.append(dict(iter=it, lines=len(used), channels=len({u[0] for u in used}),
                         update_rms_pct=round(100 * float(upd[800:-800].std()), 3), sample=used[:12]))
    return e, hist


def job(a):
    npy, out = a
    tape, t0 = Path(npy).stem.rsplit('_', 1); t0 = float(t0)
    xs = {ch: read(tape, ch, t0, 64.) for ch in sorted(inventory(tape))}
    xs = {c: remove_stationary(x, exact_mains(x)) for c, x in xs.items() if x.any()}
    e0 = np.load(npy).astype(float)
    e, hist = refine(xs, e0)
    e[:4 * RATE] = 0; e[-4 * RATE:] = 0
    np.save(Path(out) / Path(npy).name, e.astype(np.float32))
    return tape, t0, hist


if __name__ == '__main__':
    src, out = sys.argv[1], sys.argv[2]
    Path(out).mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(src + '/*.npy'))
    os.environ['OMP_NUM_THREADS'] = '1'
    log = []
    with ProcessPoolExecutor(int(sys.argv[3]) if len(sys.argv) > 3 else 5) as ex:
        for tape, t0, hist in ex.map(job, [(f, out) for f in files]):
            print(tape, t0, [(h['iter'], h['lines'], h.get('channels'), h.get('update_rms_pct')) for h in hist], flush=True)
            log.append(dict(tape=tape, t0=t0, hist=hist))
    Path(out, 'refine_log.json').write_text(json.dumps(log, indent=1))
