"""One tape-motion curve per tape: GET = Phi_tape(t_ref) + b_k, fitted as a penalized spline in rate.

docs/20_2026-11-25_sg_retime_30track.md, method step 5.

``Phi_tape`` is a cubic B-spline on uniform knots plus one constant per recorder segment (a
recorder stop or splice starts a new segment: GET jumps, the playback rate stays continuous).
The penalty is on third differences of the spline coefficients, i.e. on the curvature of the
rate, so that across gaps without evidence the rate ramps linearly between its measured values.
Before the first and after the last position anchor of a tape the rate is held.

``b_k`` is a nuisance offset for each IA drift-map segment, with prior standard deviation equal to
the segment's held-out error (``cv_mae_s``). NARA segments are IRIG and held at zero. Tapes that
share an IA segment are fitted jointly, so overlapping recorders and reels measure the IA
constants against each other and against NARA.

Observations:

* positions ``Phi(t_ref) + b_k = GET`` (text events, audio windows), each with a sigma;
* rates ``Phi'(t) = r`` (Quindar pitch ratio against the calibrated generator or the matched NARA
  burst). Quindar positions never enter the fit; they are an independent check.

Smoothing is chosen per tape by withheld 30-minute blocks scored against NARA (IRIG) positions;
the smoothest penalty within ``choose_within`` of the best held-out error wins and is raised
tenfold while the rate leaves the physical bounds. IA-only tapes take the NARA tapes' choice
(``choose_lambda``). Everything is robust: Huber weights on standardized residuals, hard rejection
beyond ``reject_s``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import sparse
from scipy.interpolate import BSpline
from scipy.sparse.linalg import splu, spsolve

FIT = dict(
    knot_spacing_s=120.0,
    penalty_order=3,
    lambdas=[10.0 ** k for k in range(0, 9)],   # below 1 the curve follows word-timing noise between knots
    folds=3,
    block_s=1800.0,
    choose_within=1.05,
    rate_bounds=(0.85, 1.10),        # hard sanity bounds; the penalty is raised beyond them
    max_rate_change_per_hour=0.10,
    review_rate_bounds=(0.94, 1.06),  # open decision 3: outside these the plan is flagged for review
    review_rate_change_per_hour=0.04,  # (late-reel slow-downs reach 6-8 %, T510105 and T510107)
    huber=1.5,
    reject_s=5.0,
    irls=6,
    min_events=20,
    support_radius_s=600.0,
    score_clip_s=10.0,
    max_lambda=1e12,
    min_nara_events=200,          # held-out scoring uses NARA (IRIG) positions only, when this many exist
    default_lambda=1e4,           # IA-only tapes: the NARA tapes' median choice, else this
    min_nara_tapes=3,             # ... of at least this many NARA tapes
    wiggle_sigma_s=1800.0,
)


@dataclass
class TapeEvidence:
    tape: str
    duration: float
    t: np.ndarray                  # reference-group tape seconds of each position
    g: np.ndarray                  # observed GET
    sigma: np.ndarray
    bkey: np.ndarray               # object: IA segment key or None
    ref_kind: np.ndarray           # 'ia' / 'nara'
    kind: np.ndarray               # 'text' / 'audio'
    cuts: list = field(default_factory=list)          # recorder-stop boundaries (tape s)
    rate_t: np.ndarray = field(default_factory=lambda: np.zeros(0))
    rate_r: np.ndarray = field(default_factory=lambda: np.zeros(0))
    rate_sigma: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def __post_init__(self):
        for name in ('t', 'g', 'sigma', 'rate_t', 'rate_r', 'rate_sigma'):
            setattr(self, name, np.asarray(getattr(self, name), float))
        self.bkey = np.asarray(self.bkey, dtype=object)
        self.ref_kind = np.asarray(self.ref_kind, dtype=object)
        self.kind = np.asarray(self.kind, dtype=object)
        self.cuts = sorted(float(c) for c in self.cuts)

    def segment(self, t):
        return np.searchsorted(np.asarray(self.cuts, float), np.asarray(t, float), side='right')


class Basis:
    """Cubic B-splines on uniform knots covering [0, duration]."""

    def __init__(self, duration, spacing):
        self.h = float(spacing)
        n_int = int(np.ceil(duration / self.h))
        self.knots = np.arange(-3, n_int + 4, dtype=float) * self.h
        self.n = len(self.knots) - 4
        self.duration = float(duration)

    def _x(self, x):
        return np.clip(np.asarray(x, float), 0.0, self.knots[-4] - 1e-9)

    def value(self, x):
        return sparse.csr_matrix(BSpline.design_matrix(self._x(x), self.knots, 3))

    def deriv(self, x):
        d2 = sparse.csr_matrix(BSpline.design_matrix(self._x(x), self.knots, 2))
        return ((d2[:, :-1] - d2[:, 1:]) / self.h).tocsr()

    def penalty(self, order):
        d = sparse.eye(self.n, format='csr')
        for _ in range(order):
            d = (d[1:] - d[:-1])
        return (d.T @ d).tocsr()


@dataclass
class Curve:
    """A fitted tape curve. ``get(t)`` is the GET of reference-group tape second ``t``."""
    tape: str
    knots: np.ndarray
    coef: np.ndarray
    seg_const: np.ndarray      # per segment (first is 0)
    cuts: list
    first_s: float             # first and last position anchor: the rate is held outside
    last_s: float
    lam: float

    def _spline(self):
        return BSpline(self.knots, self.coef, 3, extrapolate=True)

    def segment(self, t):
        return np.searchsorted(np.asarray(self.cuts, float), np.asarray(t, float), side='right')

    def rate(self, t):
        t = np.clip(np.asarray(t, float), self.first_s, self.last_s)
        return self._spline().derivative()(t)

    def get(self, t):
        """Inside [first, last]: the spline plus its segment constant; outside: held rate."""
        t = np.asarray(t, float)
        spl = self._spline()
        a, b = self.first_s, self.last_s
        inner = spl(np.clip(t, a, b))
        inner = inner + spl.derivative()(np.clip(t, a, b)) * (t - np.clip(t, a, b))
        return inner + self.seg_const[self.segment(t)]

    def output_map(self, duration, step=0.1):
        """(input s, output s, speed) on a grid: output = integral of the rate from 0.

        GET = output + ``get_at_output_zero`` + segment constant (``segments`` below).
        """
        grid = np.unique(np.r_[np.arange(0.0, duration, step), duration])
        r = self.rate(grid)
        out = np.r_[0.0, np.cumsum((r[1:] + r[:-1]) / 2 * np.diff(grid))]
        return grid, out, r

    def segments(self, duration):
        grid, out, _ = self.output_map(duration, step=1.0)
        base = float(self.get(np.array([self.first_s]))[0] - np.interp(self.first_s, grid, out)
                     - self.seg_const[self.segment(np.array([self.first_s]))[0]])
        bounds = [0.0, *self.cuts, float(duration)]
        return [dict(segment=k, tape_start_s=bounds[k], tape_end_s=bounds[k + 1],
                     get_equals_output_plus_s=base + float(self.seg_const[k])) for k in range(len(bounds) - 1)]

    def to_json(self):
        return dict(tape=self.tape, knots=self.knots.tolist(), coef=self.coef.tolist(),
                    segment_constants_s=self.seg_const.tolist(), cuts_s=list(self.cuts), first_anchor_s=self.first_s,
                    last_anchor_s=self.last_s, lam=self.lam)

    @classmethod
    def from_json(cls, d):
        return cls(d['tape'], np.asarray(d['knots']), np.asarray(d['coef']), np.asarray(d['segment_constants_s']),
                   list(d['cuts_s']), d['first_anchor_s'], d['last_anchor_s'], d['lam'])


@dataclass
class Solution:
    curves: dict
    b: dict                      # IA segment key -> offset (s)
    b_se: dict
    weights: dict                # tape -> robust position weights (0 = rejected)
    residuals: dict              # tape -> g - (Phi(t) + b)


def _layout(tapes, settings):
    bases, offsets, n = {}, {}, 0
    for e in tapes:
        basis = Basis(e.duration, settings['knot_spacing_s'])
        bases[e.tape] = basis
        offsets[e.tape] = n
        n += basis.n + len(e.cuts)
    keys = sorted({k for e in tapes for k in e.bkey if k is not None})
    bindex = {k: n + i for i, k in enumerate(keys)}
    return bases, offsets, bindex, n + len(keys)


def solve(tapes, lambdas, bprior, settings=FIT, masks=None, robust=True):
    """Fit all ``tapes`` jointly. ``masks[tape]`` selects the position rows used (cross-validation)."""
    bases, offsets, bindex, n = _layout(tapes, settings)
    rows_A, rows_y, rows_w, meta = [], [], [], []
    for e in tapes:
        basis, off = bases[e.tape], offsets[e.tape]
        nseg = len(e.cuts)
        mask = np.ones(len(e.t), bool) if masks is None or e.tape not in masks else masks[e.tape]
        idx = np.flatnonzero(mask)
        B = basis.value(e.t[idx])
        cols = [sparse.csr_matrix((B.data, B.indices + off, B.indptr), shape=(len(idx), n))]
        seg = e.segment(e.t[idx])
        if nseg:
            sel = seg > 0
            cols.append(sparse.csr_matrix((np.ones(sel.sum()), (np.flatnonzero(sel), off + basis.n + seg[sel] - 1)),
                                          shape=(len(idx), n)))
        bk = e.bkey[idx]
        has_b = np.array([k is not None for k in bk], bool)
        if has_b.any():
            cols.append(sparse.csr_matrix((np.ones(has_b.sum()), (np.flatnonzero(has_b), [bindex[k] for k in bk[has_b]])),
                                          shape=(len(idx), n)))
        rows_A.append(sum(cols[1:], cols[0]))
        rows_y.append(e.g[idx])
        rows_w.append(1.0 / e.sigma[idx] ** 2)
        meta.append((e.tape, 'pos', idx))
        if len(e.rate_t):
            D = basis.deriv(e.rate_t)
            rows_A.append(sparse.csr_matrix((D.data, D.indices + off, D.indptr), shape=(len(e.rate_t), n)))
            rows_y.append(e.rate_r)
            rows_w.append(1.0 / e.rate_sigma ** 2)
            meta.append((e.tape, 'rate', None))
    A = sparse.vstack(rows_A).tocsr()
    y = np.concatenate(rows_y)
    w0 = np.concatenate(rows_w)
    P = sparse.lil_matrix((n, n))
    for e in tapes:
        basis, off = bases[e.tape], offsets[e.tape]
        npos = int((masks[e.tape] if masks and e.tape in masks else np.ones(len(e.t), bool)).sum())
        scale = max(1.0, float(np.mean(1.0 / e.sigma ** 2)) * max(npos, 1) / basis.n)
        pen = basis.penalty(settings['penalty_order']) * (lambdas[e.tape] * scale)
        P[off:off + basis.n, off:off + basis.n] = pen
        for j in range(len(e.cuts)):
            P[off + basis.n + j, off + basis.n + j] = 1e-9
    for k, i in bindex.items():
        sigma = bprior.get(k)
        P[i, i] = 1.0 / sigma ** 2 if sigma else 1e6
    P = P.tocsr()
    w = w0.copy()
    sigmas = 1.0 / np.sqrt(w0)
    pos_rows = np.concatenate([np.full(len(yy), m[1] == 'pos') for yy, m in zip(rows_y, meta)])
    for _ in range(settings['irls'] if robust else 1):
        N = (A.T @ sparse.diags(w) @ A + P).tocsc()
        x = spsolve(N, A.T @ (w * y))
        r = y - A @ x
        if not robust:
            break
        z = r / sigmas
        zp = z[pos_rows]
        scale = max(1.0, 1.4826 * float(np.median(np.abs(zp - np.median(zp))))) if len(zp) else 1.0
        c = settings['huber'] * scale
        robust_w = np.minimum(1.0, c / np.maximum(np.abs(z), 1e-12))
        robust_w[pos_rows & (np.abs(r) > settings['reject_s'])] = 0.0
        w = w0 * robust_w
    curves, weights, residuals, start = {}, {}, {}, 0
    wpos = {}
    for yy, m in zip(rows_y, meta):
        if m[1] == 'pos':
            wpos[m[0]] = (m[2], w[start:start + len(yy)] / w0[start:start + len(yy)], r[start:start + len(yy)])
        start += len(yy)
    for e in tapes:
        basis, off = bases[e.tape], offsets[e.tape]
        idx, rw, rr = wpos[e.tape]
        used = idx[rw > 0]
        first, last = (float(e.t[used].min()), float(e.t[used].max())) if len(used) else (0.0, e.duration)
        curves[e.tape] = Curve(e.tape, basis.knots, x[off:off + basis.n],
                               np.r_[0.0, x[off + basis.n:off + basis.n + len(e.cuts)]], list(e.cuts), first, last,
                               float(lambdas[e.tape]))
        full_w = np.zeros(len(e.t))
        full_w[idx] = rw
        weights[e.tape] = full_w
        residuals[e.tape] = e.g - predict(curves[e.tape], e, {k: x[i] for k, i in bindex.items()})
    b = {k: float(x[i]) for k, i in bindex.items()}
    b_se = {}
    if bindex:
        lu = splu(N)
        for k, i in bindex.items():
            unit = np.zeros(n)
            unit[i] = 1.0
            b_se[k] = float(np.sqrt(max(lu.solve(unit)[i], 0.0)))
    return Solution(curves, b, b_se, weights, residuals)


def predict(curve, evidence, b, t=None, bkey=None):
    t = evidence.t if t is None else t
    bkey = evidence.bkey if bkey is None else bkey
    return curve.get(t) + np.array([b.get(k, 0.0) if k is not None else 0.0 for k in bkey])


def rate_violations(curve, settings=FIT, review=False):
    """Rate outside the hard bounds (``review=True``: outside the review bounds)."""
    grid = np.arange(curve.first_s, curve.last_s + 60.0, 60.0)
    if len(grid) < 3:
        return []
    r = curve.rate(grid)
    out = []
    lo, hi = settings['review_rate_bounds' if review else 'rate_bounds']
    limit = settings['review_rate_change_per_hour' if review else 'max_rate_change_per_hour']
    if r.min() < lo or r.max() > hi:
        out.append(f'rate {r.min():.4f}-{r.max():.4f} outside {lo}-{hi}')
    step = int(3600 / 60)
    if len(r) > step:
        change = np.abs(r[step:] - r[:-step]).max()
        if change > limit:
            out.append(f'rate changes by {change:.4f} within an hour')
    return out


def heldout(evidence, lam, bprior, settings=FIT):
    """Withheld 30-minute blocks: residuals of each held-out position under the training fit."""
    blocks = np.floor(evidence.t / settings['block_s']).astype(int)
    fold = blocks % settings['folds']
    seg = evidence.segment(evidence.t)
    res = np.full(len(evidence.t), np.nan)
    for f in range(settings['folds']):
        train = fold != f
        if train.sum() < settings['min_events'] or (~train).sum() == 0:
            continue
        sol = solve([evidence], {evidence.tape: lam}, bprior, settings, masks={evidence.tape: train})
        # A recorder segment without training events has no constant: its events are not predicted.
        trained = {k for k in np.unique(seg[train]) if (seg[train] == k).sum() >= 5}
        test = ~train & np.isin(seg, list(trained))
        res[test] = evidence.g[test] - predict(sol.curves[evidence.tape], evidence, sol.b, evidence.t[test],
                                               evidence.bkey[test])
    return res, blocks


def score(res, settings=FIT):
    ok = np.isfinite(res)
    return float(np.mean(np.minimum(np.abs(res[ok]), settings['score_clip_s']))) if ok.any() else np.inf


def summarize_heldout(res, blocks, ref_kind):
    out = {}
    for label, sel in (('all', np.ones(len(res), bool)), ('nara', ref_kind == 'nara'), ('ia', ref_kind == 'ia')):
        r = res[sel & np.isfinite(res)]
        if not len(r):
            continue
        bias = [float(np.median(r_b)) for b in np.unique(blocks[sel & np.isfinite(res)])
                for r_b in [res[sel & np.isfinite(res) & (blocks == b)]] if len(r_b) >= 3]
        out[label] = dict(events=int(len(r)), median_abs_s=float(np.median(np.abs(r))),
                          p95_abs_s=float(np.percentile(np.abs(r), 95)),
                          blocks=len(bias),
                          block_bias_median_abs_s=float(np.median(np.abs(bias))) if bias else None,
                          block_bias_p90_abs_s=float(np.percentile(np.abs(bias), 90)) if bias else None,
                          block_bias_max_abs_s=float(np.max(np.abs(bias))) if bias else None)
    return out


def rate_wiggle(curve, settings=FIT):
    """Short-term rate departures (below ~30 min) of a fitted curve, in ppm: a smoothness report."""
    from scipy.ndimage import gaussian_filter1d
    grid = np.arange(curve.first_s, curve.last_s, 10.0)
    if len(grid) < 10:
        return None
    r = curve.rate(grid)
    hp = r - gaussian_filter1d(r, settings['wiggle_sigma_s'] / 10.0, mode='nearest')
    return dict(std_ppm=float(hp.std() * 1e6), max_ppm=float(np.abs(hp).max() * 1e6))


def choose_lambda(evidence, bprior, settings=FIT, fixed=None):
    """Smoothest penalty within ``choose_within`` of the best held-out score, within rate bounds.

    Held-out positions are scored against IRIG time only (NARA) when the tape has at least
    ``min_nara_events`` of them. IA positions are on each reel's own clock: a reel recorder's wow
    is in them, and a curve chosen to predict them follows that wow (T510093: 0.3 % rms short-term
    rate at the smallest penalty). A tape without enough NARA positions takes ``fixed`` (the NARA
    tapes' choice) or ``default_lambda``; its held-out errors are still reported.
    """
    nara = evidence.ref_kind == 'nara'
    scored = nara if nara.sum() >= settings['min_nara_events'] else None
    trials = []
    if scored is not None:
        for lam in settings['lambdas']:
            res, blocks = heldout(evidence, lam, bprior, settings)
            trials.append(dict(lam=lam, score_s=score(res[scored], settings), res=res, blocks=blocks))
        best = min(t['score_s'] for t in trials)
        chosen = max((t for t in trials if t['score_s'] <= best * settings['choose_within']), key=lambda t: t['lam'])
        basis = 'held-out NARA positions'
    else:
        lam = fixed if fixed is not None else settings['default_lambda']
        res, blocks = heldout(evidence, lam, bprior, settings)
        chosen = dict(lam=lam, res=res, blocks=blocks, score_s=score(res, settings))
        trials = [chosen]
        basis = 'NARA tapes choice' if fixed is not None else 'default (no NARA tape fitted)'
    lam = chosen['lam']
    raised = []
    while True:
        sol = solve([evidence], {evidence.tape: lam}, bprior, settings)
        problems = rate_violations(sol.curves[evidence.tape], settings)
        if not problems or lam >= settings['max_lambda']:
            break
        raised.append(dict(lam=lam, problems=problems))
        lam *= 10
    res, blocks = (chosen['res'], chosen['blocks']) if lam == chosen['lam'] else heldout(evidence, lam, bprior, settings)
    validation = dict(trials=[dict(lam=t['lam'], score_s=t['score_s']) for t in trials], chosen_lam=lam,
                      cv_chosen_lam=chosen['lam'], lambda_basis=basis, raised_for_rate_bounds=raised,
                      remaining_rate_problems=rate_violations(sol.curves[evidence.tape], settings),
                      rate_review=rate_violations(sol.curves[evidence.tape], settings, review=True),
                      rate_wiggle=rate_wiggle(sol.curves[evidence.tape], settings),
                      heldout=summarize_heldout(res, blocks, evidence.ref_kind))
    return lam, sol, validation, res


def support_intervals(t, radius):
    """Union of [t - radius, t + radius] around every used position anchor."""
    out = []
    for a in np.sort(np.asarray(t, float)):
        lo, hi = a - radius, a + radius
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def clip_intervals(intervals, lo, hi):
    return [[max(a, lo), min(b, hi)] for a, b in intervals if min(b, hi) > max(a, lo)]


def covered(intervals):
    return float(sum(b - a for a, b in intervals))
