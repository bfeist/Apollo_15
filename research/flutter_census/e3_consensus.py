"""E3: multi-line consensus and leave-one-family-out validation (non-circular).

Seed e0 (reference CH:HZ:CAP). Census on-tape lines with +e/-e contrast and a
digitization-sideband guard. Group lines into families: same Hz (+-0.6) on any
channel (crosstalk/shared feed) or integer harmonics (+-0.3 Hz) of a member.

Each family's FM is measured as e_dw + residual, where the residual is tracked
in a domain dewarped by a curve that EXCLUDES that family (leave-one-out), with
a narrow capture. A stationary line then shows FM ~ 0 (residual = -e_dw), so
agreement with the others is not built in. Controls per held-out family:
spectral concentration gain under e_others vs wrong sign / 1.5 s shift.
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, demod, dewarp, frac_dev, inventory, read, corr
from e2_sharpen import line_spectrum

FM = 6.6


def near_mains_sideband(hz, raw_lines, fm=FM, k=4, tol=.6):
    return any(abs(hz - (h + j * fm)) < tol for h in raw_lines for j in range(-k, k + 1))


def census_lines(xs, e, thr=10., max_per_ch=12):
    out = []
    for ch, x in xs.items():
        f, _, s0 = line_spectrum(x)
        pk0, _ = signal.find_peaks(s0, height=15)
        raw = [f[i] for i in pk0 if abs(f[i] - 60 * round(f[i] / 60)) < .3]
        _, _, sp = line_spectrum(dewarp(x, e))
        _, _, sm = line_spectrum(dewarp(x, -e))
        pk, _ = signal.find_peaks(sp, height=12, distance=int(2 / (f[1] - f[0])))
        rows = []
        for i in pk:
            hz = float(f[i])
            if hz < 40 or hz > 3900:
                continue
            con = sp[i] - sm[max(0, i - 2):i + 3].max()
            if con < thr or near_mains_sideband(hz, raw):
                continue
            rows.append(dict(ch=ch, hz=hz, snr=float(sp[i]), contrast=float(con)))
        rows.sort(key=lambda r: -r['snr'])
        out += rows[:max_per_ch]
    return out


def same_source(a, b, tol=.3):
    """Same frequency (crosstalk/shared feed) or both integer harmonics of a
    common fundamental f0 >= 80 Hz with low harmonic index on the lower line."""
    if abs(a - b) < 2.5:
        return True
    lo, hi = min(a, b), max(a, b)
    for k in range(1, 5):
        f0 = lo / k
        if f0 < 80:
            break
        n = round(hi / f0)
        if n > k and abs(hi - n * f0) < tol * (n / k):
            return True
    return False


def families(lines):
    fam = []
    for r in sorted(lines, key=lambda r: -r['snr']):
        for F in fam:
            if any(same_source(r['hz'], m['hz']) for m in F[:4]):
                F.append(r); break
        else:
            fam.append([r])
    return fam


def concentration(x, hz, nper=8 * SR):
    f, p = signal.welch(x, SR, nperseg=nper, noverlap=nper // 2)
    c = np.abs(f - hz) <= .25
    w = np.abs(f - hz) <= max(4., .04 * hz)
    return float(p[c].sum() / max(p[w].sum(), 1e-30))


def track_residual(x_dw, hz, cap=4.):
    amp, fr = demod(x_dw, SR, hz, cap)
    r, ok, c = frac_dev(fr, amp, band=(2., 30.), capture=cap)
    hb = signal.sosfiltfilt(signal.butter(3, [35, 90], fs=RATE, btype='band', output='sos'), (fr - c) / c)
    return r, ok, float(np.std(hb[ok])) if ok.sum() > 100 else 1.


def measure_family(F, xs, e_dw, cache):
    """FM estimate of family F given a dewarp curve that excludes F."""
    est, wsum, okall = 0., 0., None
    for r in F:
        key = (r['ch'], id(e_dw))
        if key not in cache:
            cache[key] = dewarp(xs[r['ch']], e_dw)
        res, ok, noise = track_residual(cache[key], r['hz'])
        w = ok.mean() / max(noise, 1e-5) ** 2
        est = est + w * (res + e_dw); wsum += w
    return est / max(wsum, 1e-30), wsum


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tape'); ap.add_argument('t0', type=float); ap.add_argument('dur', type=float)
    ap.add_argument('--ref', help='CH:HZ:CAP seed'); ap.add_argument('--seed-npy')
    ap.add_argument('--out')
    a = ap.parse_args()
    t = time.time()
    xs = {ch: read(a.tape, ch, a.t0, a.dur) for ch in sorted(inventory(a.tape))}
    xs = {k: v for k, v in xs.items() if v.any()}
    tread = time.time() - t
    if a.ref:
        ch, hz, cap = a.ref.split(':')
        amp, fr = demod(xs[int(ch)], SR, float(hz), float(cap))
        seed, _, _ = frac_dev(fr, amp, band=(2., 30.), capture=float(cap))
    else:
        seed = np.load(a.seed_npy)
    edge = slice(4 * RATE, -4 * RATE)
    tc = time.time()
    lines = census_lines(xs, seed)
    fam = families(lines)
    print(f'{len(lines)} lines in {len(fam)} families on channels {sorted({l["ch"] for l in lines})} ({time.time()-tc:.1f}s)')
    # pass 1: each family measured in the seed-dewarped domain
    cache = {}
    est = [measure_family(F, xs, seed, cache) for F in fam]
    W = np.array([w for _, w in est]); E = np.array([e for e, _ in est])
    cons = (W[:, None] * E).sum(0) / W.sum()
    # pass 2 (leave-one-family-out, non-circular)
    val = []
    shift = int(1.5 * RATE)
    for i, F in enumerate(fam):
        Wo = np.delete(W, i)
        if Wo.sum() <= 0:
            continue
        pred = (Wo[:, None] * np.delete(E, i, 0)).sum(0) / Wo.sum()
        y, _ = measure_family(F, xs, pred, {})
        r0 = F[0]
        x = xs[r0['ch']]
        q = {k: concentration(dewarp(x, c) if c is not None else x, r0['hz'])
             for k, c in [('none', None), ('pred', pred), ('wrong_sign', -pred), ('shift', np.roll(pred, shift))]}
        yy, pp = y[edge], pred[edge]
        val.append(dict(members=[(m['ch'], round(m['hz'], 2)) for m in F][:10], n=len(F),
                        weight=round(float(W[i] / W.sum()), 4), fm_rms_pct=round(100 * float(yy.std()), 3),
                        r=round(corr(yy, pp), 3),
                        residual_ratio=round(float((yy - pp).std() / max(yy.std(), 1e-9)), 3),
                        residual_rms_pct=round(100 * float((yy - pp).std()), 3),
                        conc={k: round(v, 3) for k, v in q.items()}))
    val.sort(key=lambda v: -v['weight'])
    for v in val[:40]:
        print(v)
    log = dict(tape=a.tape, t0=a.t0, dur=a.dur, ref=a.ref, read_seconds=tread, seconds=time.time() - t,
               n_lines=len(lines), n_families=len(fam), lines=lines,
               seed_vs_consensus_r=corr(seed[edge], cons[edge]), validation=val)
    print('r(seed, consensus) =', round(log['seed_vs_consensus_r'], 3), f'total {log["seconds"]:.0f}s')
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(log, indent=1))
        np.save(Path(a.out).with_suffix('.npy'), cons)


if __name__ == '__main__':
    main()
