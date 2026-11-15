"""W1: fast full-archive flutter screening prototype (research only).

Same science as e4/e7 (blind candidates -> cross-channel agreement -> LOO
consensus), restructured for long-block processing of all 30 channels:

- each channel is READ ONCE per block and decimated once (8 kHz -> 8/3 kHz,
  keeps carriers < 1.2 kHz, where the useful carriers live);
- stationary digitization lines are removed by a vectorised per-harmonic
  per-block least-squares (same math as common.remove_stationary, batched
  over harmonics with GEMV instead of a Python loop);
- ONE Welch spectrum per channel per block feeds candidate discovery;
- demodulation runs on the decimated signal (3x less mixing work, complex
  mix only on 2666.7 Hz-sampled audio);
- 64 s windows are screened directly from the block-level candidate curves
  (e7 per-channel LOO logic, no re-read / re-demod per window);
- e5-style full validation is only worth running on windows that pass
  screening; concentration controls can use small zoom-FFTs around each line.
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, corr, frac_dev, inventory, read, exact_mains, remove_stationary

DEC = 3                      # decimation factor for the fast path
SRE = SR / DEC               # 2666.667 Hz effective rate
FMAX_FAST = 1150.            # carriers kept on the fast path


def decimate(x):
    """8 kHz -> SR/DEC, float32, one resample_poly per channel per block."""
    return signal.resample_poly(np.asarray(x, np.float32), 1, DEC)


def exact_mains_fast(xd, sr=SRE, nper_s=16.):
    """exact_mains on decimated audio (same 1/16 Hz binning, fmax 1250)."""
    return exact_mains(xd, sr=sr, nper=int(nper_s * sr), fmax=1250.)


def fast_stationary(x, hz_list, sr=SRE, block_s=16.):
    """Vectorised per-harmonic, per-block least-squares removal.

    Identical fit to common.remove_stationary (independent 2x2 fit per
    harmonic per 16 s block, cross-harmonic coupling dropped there too).
    The complex basis E = exp(2j*pi*hz*t) is precomputed once for the 16 s
    grid and reused for every block: a constant per-block phase rotation is
    absorbed by the 2x2 solve, so all blocks share one basis. Per block the
    work is two complex GEMVs; the per-harmonic trig/loop cost is gone.
    """
    y = np.asarray(x, np.float64)
    if not hz_list:
        return y.copy()
    hz = np.asarray(hz_list, float)
    n = int(block_s * sr)
    E = None
    G = None
    out = np.array(y, dtype=np.float64)
    nb = len(out)
    starts = list(range(0, nb, n))
    for a in starts:
        m = min(n, nb - a)
        if m < sr:
            continue
        if E is None or m != len(E):
            t = np.arange(m) / sr
            E = np.exp(2j * np.pi * np.outer(t, hz))
            G = None
        Eb = E[:m]
        if G is None:
            E2 = Eb * Eb                 # sum E^2 -> cc-ss, 2cs
            G1 = (Eb * Eb.conj()).real.sum(0)
            G2r, G2i = E2.real.sum(0), E2.imag.sum(0)
            cc = (G1 + G2r) / 2
            ss = (G1 - G2r) / 2
            cs = G2i / 2
            det = cc * ss - cs * cs
            G = (cc, ss, cs, det)
        cc, ss, cs, det = G
        yb = out[a:a + m]
        X = yb @ Eb                      # xc + i*xs per harmonic
        xc, xs = X.real, X.imag
        u = (ss * xc - cs * xs) / det
        v = (cc * xs - cs * xc) / det
        out[a:a + m] = yb - (Eb @ (u - 1j * v)).real
    return out


def fast_demod(x, hz, capture, sr=SRE, out_rate=RATE, dec=DEC):
    """Complex demodulation on decimated audio; same math as common.demod.

    mix at sr -> resample_poly(z, dec, 8) to 1 kHz -> butter LP +-capture ->
    unwrap/gradient -> 200 Hz boxcar. Returns (amp, fr) at out_rate."""
    t = np.arange(len(x)) / sr
    z = np.asarray(x, np.float64) * np.exp(-2j * np.pi * hz * t)
    z = signal.resample_poly(z, dec, 8)
    sos = signal.butter(6, capture, fs=1000., output='sos')
    z = signal.sosfiltfilt(sos, z)
    step = 1000 // out_rate
    ph = np.unwrap(np.angle(z))
    fr = hz + np.gradient(ph) * 1000. / (2 * np.pi)
    k = len(z) // step * step
    amp = np.abs(z[:k]).reshape(-1, step).mean(1)
    fr = fr[:k].reshape(-1, step).mean(1)
    return amp, fr


def fast_candidates(x, per_ch=10, fmin=50., fmax=FMAX_FAST, sr=SRE):
    """e4.candidates on decimated audio (same 2 s Welch, same log axis,
    same smoothing/prominence logic)."""
    from scipy import ndimage
    f, p = signal.welch(x, sr, nperseg=int(2 * sr), noverlap=int(sr))
    lf = np.exp(np.arange(np.log(fmin), np.log(fmax), .0025))
    lp = np.interp(lf, f, 10 * np.log10(p + 1e-30))
    sm = ndimage.gaussian_filter1d(lp, 3)             # ~0.75% smoothing
    bg = ndimage.median_filter(sm, 161, mode='nearest')
    pk, prop = signal.find_peaks(sm - bg, height=3, prominence=3, distance=8)
    order = np.argsort(-prop['peak_heights'])[:per_ch * 2]
    rows = []
    for i in pk[order]:
        hz = float(lf[i])
        if 2150 < hz < 2850:      # keep check-tone band out
            continue
        if abs(hz - 60 * round(hz / 60)) < .5 and hz < 1000:
            continue              # exact digitization comb: little FM leverage
        rows.append(dict(hz=hz, prom_db=float((sm - bg)[i])))
    return rows[:per_ch]


def fast_track(x, hz, sr=SRE, spec=None):
    """e4.track on decimated audio; uses FFT band-isolation demod when a
    BlockSpec is given, otherwise the mix-at-sr path."""
    cap = float(np.clip(.04 * hz + 14., 8., min(120., hz - 20.)))
    if spec is not None:
        d = fft_demod(spec, hz, cap)
        if d is None:
            return None, None, None
        amp, fr = d
    else:
        amp, fr = fast_demod(x, hz, cap, sr=sr)
    e, ok, c = frac_dev(fr, amp, band=(2., 30.), capture=cap)
    f, pe = signal.welch(e[ok] if ok.sum() > 2000 else e, RATE, nperseg=1024)
    share = float(pe[(f > 4) & (f < 10)].sum() / max(pe[(f > 2) & (f < 30)].sum(), 1e-30))
    hb = signal.sosfiltfilt(signal.butter(3, [35, 90], fs=RATE, btype='band', output='sos'),
                            (fr - c) / c)
    return e, ok, dict(hz=hz, centre=c, cap=cap, support=float(ok.mean()),
                       rms_pct=100 * float(e.std()), flutter_share=share,
                       noise_pct=100 * float(hb[ok].std()) if ok.any() else 99.)


def scan_block(tape, t0, dur, per_ch=10, dec=DEC, keep_curves=True):
    """Read each channel ONCE for [t0, t0+dur), decimate, clean, discover
    candidates and track them over the whole block. Returns (meta, tracks):
    tracks[ch] = list of dicts(hz, centre, cap, support, e (block-length
    float32 curve at RATE), rms_pct, flutter_share)."""
    t = time.time()
    tracks, meta = {}, {}
    for ch in sorted(inventory(tape)):
        x = read(tape, ch, t0, dur)
        if not x.any():
            continue
        xd = decimate(x)
        hm = exact_mains_fast(xd)
        xc = fast_stationary(xd, hm)
        meta[ch] = dict(mains=len(hm))
        rows = []
        spec = BlockSpec(xc)
        for r in fast_candidates(xc, per_ch):
            e, ok, m = fast_track(xc, r['hz'], spec=spec)
            if e is None or m['support'] < .5:
                continue
            m.update(ch=ch, hz0=r['hz'], prom_db=r['prom_db'])
            if keep_curves:
                m['e'] = e.astype(np.float32); m['ok'] = ok
            rows.append(m)
        tracks[ch] = rows
    meta['_seconds'] = round(time.time() - t, 1)
    return meta, tracks


def screen_window(tracks, i0, n_win, r_min=.6):
    """e7-style per-channel LOO agreement on a sub-window of block curves.
    i0: sample index of the window start within the block curves (RATE Hz).
    Returns the e7 result dict and the full consensus curve (zero-padded)."""
    C = []
    edge = slice(4 * RATE, -4 * RATE)
    for ch in sorted(tracks):
        for m in tracks[ch]:
            e = m['e'][i0:i0 + n_win].astype(np.float64)
            ok = m['ok'][i0:i0 + n_win]
            if ok.mean() < .5:
                continue
            C.append((m, e[edge]))
    edge = slice(4 * RATE, -4 * RATE)
    n = len(C)
    out = dict(n_candidates=n)
    if n < 2:
        out.update(eligible=False, agreeing_channels=[])
        return out, None
    M = np.array([c[1] for c in C])
    Mn = (M - M.mean(1, keepdims=True)) / (M.std(1, keepdims=True) + 1e-12)
    R = Mn @ Mn.T / M.shape[1]
    np.fill_diagonal(R, 0)
    chs = np.array([c[0]['ch'] for c in C])
    other = chs[:, None] != chs[None, :]
    alive = np.ones(n, bool)
    clusters = []
    for _ in range(4):
        deg = ((R > r_min) & other & alive[None, :] & alive[:, None]).sum(1)
        if deg.max() < 2:
            break
        s0 = int(np.argmax(deg))
        mem0 = [s0] + [j for j in range(n) if alive[j] and R[s0, j] > r_min]
        clusters.append((s0, mem0, float(np.median([C[j][0]['rms_pct'] for j in mem0]))))
        alive[mem0] = False
    out['clusters'] = [dict(seed_ch=int(chs[a]), seed_hz=round(C[a][0]['centre'], 2),
                            size=len(m), median_rms_pct=round(r_, 3)) for a, m, r_ in clusters]
    if not clusters:
        out.update(eligible=False, agreeing_channels=[])
        return out, None
    s, mem, _ = max(clusters, key=lambda c: c[2] if len(c[1]) >= 3 else -1)
    es = M[s]
    norm = {}
    for j in mem:
        g = 1. if j == s else float(M[j] @ es / (es @ es))
        rj = .95 if j == s else min(R[s, j], .95)
        if g > .3:
            norm[j] = (M[j] / g, rj ** 2 / (1 - rj ** 2))
    per = {}
    for c in sorted(set(chs[list(norm)])):
        own = [j for j in norm if chs[j] == c]
        oth = [j for j in norm if chs[j] != c]
        if not oth:
            continue
        pred = sum(norm[j][1] * norm[j][0] for j in oth) / sum(norm[j][1] for j in oth)
        best = None
        for j in own:
            rr = corr(M[j], pred); g = float(M[j] @ pred / (pred @ pred))
            if best is None or rr > best[0]:
                best = (rr, g, C[j][0]['centre'], C[j][0]['rms_pct'])
        per[int(c)] = dict(r=round(best[0], 3), gain=round(best[1], 3),
                           hz=round(best[2], 2), rms_pct=round(best[3], 3))
    agree = [c for c, v in per.items() if v['r'] >= .7 and .6 <= v['gain'] <= 1.6]
    from e3_consensus import same_source
    fams = []
    for c in agree:
        hz = per[c]['hz']
        if not any(same_source(hz, f) or abs(hz - f) < .025 * max(hz, f) for f in fams):
            fams.append(hz)
    full = sum(w * e for e, w in norm.values()) / sum(w for _, w in norm.values())
    full *= np.median([per[c]['rms_pct'] for c in agree]) / 100 / max(full.std(), 1e-12) if agree else 1.
    seed = np.zeros(n_win); seed[edge] = full
    out.update(seed_channel=int(chs[s]), seed_hz=round(C[s][0]['centre'], 2),
               cluster_size=len(mem), per_channel=per, agreeing_channels=agree,
               n_agreeing=len(agree), families=[round(f, 2) for f in fams],
               eligible=len(agree) >= 3 and len(fams) >= 2,
               consensus_rms_pct=round(100 * float(full.std()), 3))
    return out, seed


def fast_window(tape, t0, dur=64., per_ch=10):
    """Screen one 64 s window with the fast primitives (block = the window).
    Returns (screen dict, consensus curve at RATE)."""
    meta, tracks = scan_block(tape, t0, dur, per_ch=per_ch)
    res, seed = screen_window(tracks, 0, int(dur * RATE))
    res['scan_seconds'] = meta['_seconds']
    res['tape'], res['t0'], res['dur'] = tape, t0, dur
    return res, seed


def zoom_concentration(x, hz, sr=SR, span=None, nper_s=8.):
    """concentration() via a small FFT around hz only (zoom-FFT).
    Same ratio: power in |f-hz|<=0.25 Hz over |f-hz|<=max(4, .04*hz)."""
    if span is None:
        span = max(6., .06 * hz)
    dec = max(1, int(sr / (8 * span)))
    t = np.arange(len(x)) / sr
    z = np.asarray(x, np.float64) * np.exp(-2j * np.pi * hz * t)
    z = signal.resample_poly(z, 1, dec)
    f = np.fft.rfftfreq(len(z), 1 / (sr / dec))
    p = np.abs(np.fft.rfft(z)) ** 2
    c = np.abs(f - 0) <= .25
    w = np.abs(f - 0) <= max(4., .04 * hz)
    return float(p[c].sum() / max(p[w].sum(), 1e-30))


def compare_with_e5(tape, t0, dur=64., e5_dir='reports/flutter_census_20261115/e5'):
    """Run fast_window and compare with the saved e5 consensus curve/json."""
    root = Path(__file__).resolve().parents[2]
    res, seed = fast_window(tape, t0, dur)
    e5p = root / e5_dir / f'{tape}_{int(t0)}.npy'
    out = dict(tape=tape, t0=t0, fast=res)
    if e5p.exists():
        e5c = np.load(e5p)
        n = min(len(e5c), len(seed))
        edge = slice(4 * RATE, -4 * RATE)
        out['cons_vs_e5_r'] = round(corr(seed[:n][edge], e5c[:n][edge]), 3)
        out['fast_rms_pct'] = res['consensus_rms_pct']
        e5j = json.loads((root / e5_dir / f'{tape}_{int(t0)}.json').read_text())
        out['e5_rms_pct'] = e5j.get('consensus_rms_pct')
        out['e5_validated'] = [v['members'] for v in e5j.get('validation', []) if v['validated']]
        # family overlap: e5 validated member Hz vs fast agreeing Hz
        e5_hz = sorted({h for v in e5j.get('validation', []) if v['validated']
                        for _, h, _ in v['members']})
        fast_hz = sorted({v['hz'] for c, v in res.get('per_channel', {}).items()
                          if c in res.get('agreeing_channels', [])})
        out['e5_validated_hz'] = e5_hz
        out['fast_agree_hz'] = fast_hz
    return out


def bench_block(tape='A15_T510077', t0=18000., dur=600., per_ch=10):
    """Wall time for a full 30-channel block scan -> source-hours per
    wall-hour per core for the screening pass."""
    t = time.time()
    meta, tracks = scan_block(tape, t0, dur, per_ch=per_ch)
    wall = time.time() - t
    nch = len(tracks)
    ntr = sum(len(v) for v in tracks.values())
    src_h = dur * nch / 3600.
    return dict(tape=tape, t0=t0, dur=dur, channels=nch, tracks=ntr,
                wall_s=round(wall, 1), source_hours=round(src_h, 2),
                source_hours_per_wall_hour_per_core=round(src_h / (wall / 3600.), 1),
                meta_seconds=meta['_seconds'])


def verify_primitives(tape, t0, dur=64., channels=(3, 11, 28)):
    """Check fast primitives against the common/e4 equivalents on one window."""
    from e4_blind_seed import candidates as slow_cand, track as slow_track
    out = {}
    for ch in channels:
        x = read(tape, ch, t0, dur)
        if not x.any():
            continue
        # stationary removal: fast (vectorised) vs common (loop), same decimated input
        t0w = time.time(); xd = decimate(x)
        hm = exact_mains_fast(xd)
        t1 = time.time(); a = fast_stationary(xd, hm)
        tf = time.time() - t1
        t1 = time.time(); b = remove_stationary(xd, hm, sr=SRE)
        tc = time.time() - t1
        out[f'ch{ch}_stationary'] = dict(hz=len(hm), max_abs_diff=float(np.abs(a - b).max()),
                                         corr=round(corr(a, b), 6), fast_s=round(tf, 3),
                                         common_s=round(tc, 3))
        # candidates: e4 on full-rate cleaned vs fast on decimated cleaned
        t1 = time.time(); xcf = remove_stationary(x, exact_mains(x))
        tcs = time.time() - t1
        slow_hz = [round(r['hz'], 2) for r in slow_cand(xcf, 10)]
        fast_hz = [round(r['hz'], 2) for r in fast_candidates(a, 10)]
        match = [h for h in fast_hz if any(abs(h - g) < .25 for g in slow_hz)]
        out[f'ch{ch}_candidates'] = dict(slow_n=len(slow_hz), fast_n=len(fast_hz),
                                         matched=len(match), slow=slow_hz[:6], fast=fast_hz[:6],
                                         common_clean_s=round(tcs, 2))
        # tracking: fast/decimated vs slow/full-rate on the same best carrier
        if match:
            hz = match[0]
            ef, okf, mf = fast_track(a, hz)
            es, oks, ms = slow_track(xcf, hz)
            n = min(len(ef), len(es))
            out[f'ch{ch}_track_hz{hz}'] = dict(r_fast_vs_slow=round(corr(ef[:n], es[:n]), 3),
                rms_fast=round(mf['rms_pct'], 3), rms_slow=round(ms['rms_pct'], 3),
                cap=round(mf['cap'], 1))
    print(json.dumps(out, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['verify', 'window', 'bench', 'compare'])
    ap.add_argument('tape')
    ap.add_argument('t0', type=float, nargs='?', default=19715.)
    ap.add_argument('--dur', type=float, default=64.)
    ap.add_argument('--out')
    a = ap.parse_args()
    if a.cmd == 'verify':
        verify_primitives(a.tape, a.t0, a.dur)
    elif a.cmd == 'window':
        res, seed = fast_window(a.tape, a.t0, a.dur)
        print(json.dumps({k: res.get(k) for k in ('n_candidates', 'clusters', 'n_agreeing',
              'agreeing_channels', 'families', 'eligible', 'consensus_rms_pct',
              'scan_seconds')}))
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(json.dumps(res, indent=1, default=float))
            if seed is not None:
                np.save(Path(a.out).with_suffix('.npy'), seed.astype(np.float32))
    elif a.cmd == 'bench':
        print(json.dumps(bench_block(a.tape, a.t0 or 18000., a.dur if a.dur != 64. else 600.)))
    elif a.cmd == 'compare':
        out = compare_with_e5(a.tape, a.t0, a.dur)
        print(json.dumps({k: v for k, v in out.items() if k != 'fast'}))
        print('fast screen:', json.dumps({k: out['fast'].get(k) for k in
              ('n_agreeing', 'agreeing_channels', 'families', 'eligible',
               'consensus_rms_pct', 'scan_seconds')}))
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(json.dumps(out, indent=1, default=float))


class BlockSpec:
    """Cached rFFT of one cleaned channel block, for FFT band-isolation
    demodulation: ONE forward FFT per channel per block, then each carrier
    costs only a small band iFFT + low-rate mix/filter."""
    def __init__(self, x, sr=SRE):
        self.x = np.asarray(x, np.float64)
        self.sr = sr
        self.n = len(self.x)
        self.df = sr / self.n
        self.X = np.fft.rfft(self.x)


def fft_demod(spec, hz, capture, out_rate=RATE):
    """Band-isolation demodulation from a BlockSpec.

    Crop [hz-cap, hz+cap] out of the block spectrum, keep every stride-th
    bin (frequency-domain decimation to rate sr/stride), small iFFT, mix to
    baseband at the reduced rate, butter +-capture, unwrap/gradient, then
    interpolate amp/fr onto the 200 Hz output grid. Same estimator as
    common.demod up to filter-shape details at the band edge."""
    sr, n, df = spec.sr, spec.n, spec.df
    rate = min(1000., max(300., 6 * capture, .25 * hz))
    d = max(1, int(sr / rate))
    L = n // d
    r = sr / d                      # reduced sample rate (>= ~rate)
    i0 = max(1, int(round((hz - capture) / df)))
    i1 = min(len(spec.X) - 1, int(round((hz + capture) / df)))
    if i1 - i0 < 8 or 2 * (i1 - i0 + 1) > L:
        return None
    Xd = np.zeros(L, complex)
    idx = np.arange(i0, i1 + 1) % L          # time-decimation aliasing fold
    Xd[idx] += spec.X[i0:i1 + 1]
    z = np.fft.ifft(Xd) * d
    fc = (hz / r) % 1.0                       # folded band centre (signed)
    if fc > .5:
        fc -= 1.
    fc *= r
    t = np.arange(L) / r
    z = z * np.exp(-2j * np.pi * fc * t)
    sos = signal.butter(6, capture, fs=r, output='sos')
    z = signal.sosfiltfilt(sos, z)
    ph = np.unwrap(np.angle(z))
    fr = hz + np.gradient(ph) * r / (2 * np.pi)   # re-centre on the carrier
    t200 = np.arange(int(round(n / sr * RATE))) / RATE
    amp = np.interp(t200, t, np.abs(z))
    fr = np.interp(t200, t, fr)
    return amp, fr


if __name__ == '__main__':
    main()
