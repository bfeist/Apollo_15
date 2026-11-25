"""W3 core: check-tone-free consensus with guaranteed beep-band exclusion.

Copies of e4_blind_seed / e3_consensus / e5_window_eval logic with one change:
candidates and census lines whose capture window touches the Quindar/check-beep
band 2150-2850 Hz are dropped, so the consensus cannot use beep energy.
Optionally injects historical hum lines (59.3-59.9, 118-119.5 Hz) as extra
families. Research only; reads originals read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / 'scripts' / '30-track' / '05_deflutter'))

from common import RATE, SR, corr, demod, dewarp, frac_dev, read, exact_mains, remove_stationary  # noqa: E402
from e2_sharpen import line_spectrum  # noqa: E402
from e3_consensus import near_mains_sideband, same_source, families, concentration  # noqa: E402
from e4_blind_seed import track as e4_track  # noqa: E402
from e5_window_eval import raw_fm  # noqa: E402

BEEP_BAND = (2150., 2850.)
HUM_WINDOWS = ((59.3, 59.9), (118.0, 119.5))
EDGE = slice(4 * RATE, -4 * RATE)


def cap_for(hz: float) -> float:
    """Tracking capture used by e4/e5 raw_fm."""
    return float(np.clip(.04 * hz + 14., 8., min(120., hz - 20.)))


def touches_beep(hz: float) -> bool:
    """True when the demod capture around hz reaches into the beep band."""
    cap = cap_for(hz)
    return not (hz + cap < BEEP_BAND[0] or hz - cap > BEEP_BAND[1])


# ---------------------------------------------------------------- blind seed
def candidates_full(x, per_ch=10, fmin=50., fmax=3900.):
    """Exact copy of e4_blind_seed.candidates (strict 2150-2850 exclusion)."""
    f, p = signal.welch(x, SR, nperseg=2 * SR, noverlap=SR)
    lf = np.exp(np.arange(np.log(fmin), np.log(fmax), .0025))
    lp = np.interp(lf, f, 10 * np.log10(p + 1e-30))
    sm = ndimage.gaussian_filter1d(lp, 3)
    bg = ndimage.median_filter(sm, size=161, mode='nearest')
    pk, prop = signal.find_peaks(sm - bg, height=3, prominence=3, distance=8)
    order = np.argsort(-prop['peak_heights'])[:per_ch * 2]
    rows = []
    for i in pk[order]:
        hz = float(lf[i])
        if 2150 < hz < 2850:  # keep check-tone band as independent witness
            continue
        if abs(hz - 60 * round(hz / 60)) < .5 and hz < 1000:
            continue
        rows.append(dict(hz=hz, prom_db=float((sm - bg)[i])))
    return rows[:per_ch]


def candidates_noband(x, per_ch=10, fmin=50., fmax=3900.):
    """candidates_full with the stricter capture-window exclusion."""
    rows = candidates_full(x, per_ch, fmin, fmax)
    return [r for r in rows if not touches_beep(r['hz'])]


def blind_seed(clean, beep_safe=True, per_ch=10, r_min=.6):
    """e4_blind_seed.run on pre-cleaned audio (same numerics, no re-read).
    beep_safe=False reproduces e5_window_eval.evaluate exactly."""
    cand = candidates_noband if beep_safe else candidates_full
    C = []
    for ch in sorted(clean):
        x = clean[ch]
        for r in cand(x, per_ch):
            e, ok, m = e4_track(x, r['hz'])
            if m['support'] < .5:
                continue
            m.update(ch=ch, prom_db=r['prom_db'])
            m['e'] = e
            C.append(m)
    edge = EDGE
    n = len(C)
    R = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            R[i, j] = R[j, i] = corr(C[i]['e'][edge], C[j]['e'][edge])

    def indep(i, j):
        a, b = C[i], C[j]
        return a['ch'] != b['ch'] and abs(a['centre'] - b['centre']) > .02 * max(a['centre'], b['centre'])

    A = np.array([[R[i, j] > r_min and indep(i, j) for j in range(n)] for i in range(n)]) if n else np.zeros((0, 0))
    deg = A.sum(1) if n else np.array([])
    out = dict(n_candidates=n, beep_safe=beep_safe)
    if n and deg.max() > 0:
        s = int(np.argmax(deg + 1e-3 * np.array([c['flutter_share'] for c in C])))
        mem = [s] + [j for j in range(n) if j != s and R[s, j] > r_min]
        es = C[s]['e'][edge]
        acc, wsum = 0., 0.
        for j in mem:
            rj = .95 if j == s else min(R[s, j], .95)
            g = float(np.dot(C[j]['e'][edge], es) / np.dot(es, es)) if j != s else 1.
            if g < .3:
                continue
            w = rj ** 2 / (1 - rj ** 2)
            acc = acc + w * C[j]['e'] / g
            wsum += w
        seed = acc / wsum
        top = sorted(mem, key=lambda j: -(R[s, j] if j != s else 1.))[:5]
        target = float(np.median([C[j]['e'][edge].std() for j in top]))
        seed *= target / max(seed[edge].std(), 1e-12)
        out.update(seed_index=s,
                   cluster=[dict(ch=C[j]['ch'], hz=round(C[j]['centre'], 2),
                                 r_to_seed=round(float(R[s, j]) if j != s else 1., 3),
                                 rms_pct=round(C[j]['rms_pct'], 3)) for j in mem],
                   n_independent_partners=int(deg[s]),
                   cluster_channels=sorted({C[j]['ch'] for j in mem}),
                   seed_rms_pct=100 * float(seed[edge].std()))
        out['_seed'] = seed
    return out


# ------------------------------------------------------------------- census
def census_spectra(clean, e):
    """Per-channel spectra for the census: raw, +e dewarped, -e dewarped.

    Shared across census variants so the expensive dewarps run once."""
    S = {}
    for ch, x in clean.items():
        f, _, s0 = line_spectrum(x)
        pk0, _ = signal.find_peaks(s0, height=15)
        raw = [f[i] for i in pk0 if abs(f[i] - 60 * round(f[i] / 60)) < .3]
        _, _, sp = line_spectrum(dewarp(x, e))
        _, _, sm = line_spectrum(dewarp(x, -e))
        S[ch] = dict(f=f, s0=s0, sp=sp, sm=sm, raw=raw)
    return S


def _rows_from_spectra(S, thr=10., max_per_ch=12, beep_safe=True):
    """e3_consensus.census_lines on precomputed spectra.

    beep_safe=True additionally drops any line whose demod capture window
    touches 2150-2850 Hz (guarantees beep independence)."""
    out = []
    for ch, d in S.items():
        f, sp, sm, raw = d['f'], d['sp'], d['sm'], d['raw']
        pk, _ = signal.find_peaks(sp, height=12, distance=int(2 / (f[1] - f[0])))
        rows = []
        for i in pk:
            hz = float(f[i])
            if hz < 40 or hz > 3900:
                continue
            if beep_safe and touches_beep(hz):
                continue
            con = sp[i] - sm[max(0, i - 2):i + 3].max()
            if con < thr or near_mains_sideband(hz, raw):
                continue
            rows.append(dict(ch=ch, hz=hz, snr=float(sp[i]), contrast=float(con)))
        rows.sort(key=lambda r: -r['snr'])
        out += rows[:max_per_ch]
    return out


def add_hum_rows(S, rows, min_snr=8., min_contrast=5.):
    """Historical mains hum lines (fluttered, on-tape) as extra census lines.

    The e3 sideband guard near exact 60 Hz multiples would reject these (they
    sit 0.1-0.7 Hz below the digitization comb), so it is bypassed for the
    declared hum windows only."""
    for ch, d in S.items():
        f, sp, sm = d['f'], d['sp'], d['sm']
        for lo, hi in HUM_WINDOWS:
            k = np.flatnonzero((f >= lo) & (f <= hi))
            if len(k) == 0:
                continue
            i = int(k[np.argmax(sp[k])])
            if sp[i] < min_snr:
                continue
            con = float(sp[i] - sm[max(0, i - 2):i + 3].max())
            if con < min_contrast:
                continue
            hz = float(f[i])
            if any(abs(hz - r['hz']) < .3 for r in rows if r['ch'] == ch):
                continue
            rows.append(dict(ch=ch, hz=hz, snr=float(sp[i]), contrast=con, hum=True))
    return rows


# ------------------------------------------------------- consensus building
def build_consensus(clean, seed, mode, want_validation=True):
    """Census -> families -> raw_fm measurement -> LOO weights -> consensus.

    mode: 'full' (exact e3/e5 semantics, beep band allowed),
          'noband' (beep-band-safe), 'hum' ('noband' + historical hum lines).
    Returns (cons or None, meta dict)."""
    S = census_spectra(clean, seed)
    if mode == 'full':
        lines = _rows_from_spectra(S, beep_safe=False)
    elif mode == 'noband':
        lines = _rows_from_spectra(S, beep_safe=True)
    elif mode == 'hum':
        lines = add_hum_rows(S, _rows_from_spectra(S, beep_safe=True))
    else:
        raise ValueError(mode)
    fam = families(lines)
    F_est = []
    for F in fam:
        acc, n, mem = 0., 0, []
        for r in F[:6]:
            others = [l['hz'] for l in lines if l['ch'] == r['ch']]
            fam_hz = [m['hz'] for m in F]
            others = [o for o in others if all(abs(o - h) > 2.5 for h in fam_hz)]
            e, ok, noise, cap = raw_fm(clean[r['ch']], r['hz'], others)
            need = .6 * float(np.clip(.04 * r['hz'] + 14., 8., 120.))
            if ok.mean() < .6 or cap < need:
                continue
            acc = acc + e
            n += 1
            mem.append((r['ch'], round(r['hz'], 2), round(cap, 1)))
        if n:
            F_est.append(dict(F=F, e=acc / n, members=mem))
    meta = dict(n_lines=len(lines), n_families=len(fam), n_measured_families=len(F_est),
                line_channels=sorted({l['ch'] for l in lines}),
                hum_lines=[[l['ch'], round(l['hz'], 2)] for l in lines if l.get('hum')],
                members=[f['members'] for f in F_est])
    if not F_est:
        return None, meta
    E = np.array([f['e'] for f in F_est])
    val = []
    if len(F_est) >= 2:
        W = np.ones(len(F_est))
        for _ in range(2):
            rs = []
            for i in range(len(F_est)):
                Wo = np.delete(W, i)
                pred = (Wo[:, None] * np.delete(E, i, 0)).sum(0) / max(Wo.sum(), 1e-12)
                rs.append(corr(E[i][EDGE], pred[EDGE]))
            rs = np.clip(np.array(rs), 0, .97)
            W = rs ** 2 / (1 - rs ** 2) + 1e-6
        if want_validation:
            for i, f in enumerate(F_est):
                Wo = np.delete(W, i)
                pred = (Wo[:, None] * np.delete(E, i, 0)).sum(0) / Wo.sum()
                y, p = f['e'][EDGE], pred[EDGE]
                g = float(np.dot(y, p) / max(np.dot(p, p), 1e-30))
                r0 = f['F'][0]
                x = clean[r0['ch']]
                q = {k: concentration(dewarp(x, c) if c is not None else x, r0['hz'])
                     for k, c in [('none', None), ('pred', pred), ('wrong_sign', -pred),
                                  ('shift', np.roll(pred, int(1.5 * RATE)))]}
                cg = q['pred'] / max(q['none'], q['wrong_sign'], q['shift'], 1e-9)
                rr = corr(y, p)
                amp_, fr_ = demod(dewarp(x, pred), SR, r0['hz'], 4.)
                res_, _, _ = frac_dev(fr_, amp_, band=(2., 30.), capture=4.)
                val.append(dict(members=f['members'], weight=round(float(W[i] / W.sum()), 4),
                                fm_rms_pct=round(100 * float(y.std()), 3), r=round(rr, 3),
                                gain=round(g, 3),
                                residual_ratio=round(float((y - p).std() / max(y.std(), 1e-9)), 3),
                                conc={k: round(v, 4) for k, v in q.items()},
                                conc_gain=round(float(cg), 2), conc_pass=bool(cg >= 2.5),
                                residual_after_pct=round(100 * float(res_[EDGE].std()), 3),
                                validated=bool(cg >= 2.5 and rr > .5)))
    else:
        W = np.ones(1)
        meta['single_family'] = True
    cons = (W[:, None] * E).sum(0) / W.sum()
    meta['weights'] = [round(float(w / W.sum()), 4) for w in W]
    f_, pe = signal.welch(cons[EDGE], RATE, nperseg=1024)
    kb = (f_ > 2) & (f_ < 30)
    meta['consensus_rms_pct'] = round(100 * float(cons[EDGE].std()), 3)
    meta['flutter_peak_hz'] = round(float(f_[kb][np.argmax(pe[kb])]), 2)
    meta['seed_vs_consensus_r'] = round(corr(seed[EDGE], cons[EDGE]), 3)
    meta['validation'] = val
    return cons, meta


def clean_channels(tape, t0, dur):
    """Read canonical interval on every channel and remove stationary lines."""
    from common import inventory
    xs = {ch: read(tape, ch, t0, dur) for ch in sorted(inventory(tape))}
    xs = {k: v for k, v in xs.items() if v.any()}
    return {ch: remove_stationary(x, exact_mains(x)) for ch, x in xs.items()}


def proc_curve(e):
    """2-30 Hz bandpass + bound exactly as flutter_tracking.signed_map."""
    import flutter_tracking as ft
    sos = signal.butter(3, [2., 30.], fs=RATE, btype='bandpass', output='sos')
    return ft.bound(signal.sosfiltfilt(sos, np.asarray(e, float)), np.ones(len(np.asarray(e))))
