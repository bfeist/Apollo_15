"""SG reels, step 5: one slow drift curve per reel, fitted to the transcript timing anchors.

docs/16 "Space-to-ground reels" step 5. The transcripts' GETs (journals and 1971 PDFs) are the
timing authority. The ASR word starts on each reel (CTC-aligned since 2026-11-25; ``sg_anchors.tsv``, written by
``sg_transcript.py sg anchors``) only measure where those utterances fall on the reel clock.
Their difference, offset = GET - reel time, grows slowly along a reel: the transfers run about
0.5 % off speed.

The model for a reel, at reel time t in continuous segment k:

    GET(t) = t + D(t) + c_k

* ``D`` is one low-degree polynomial across the whole file, with D(0) = 0: a straight line
  (constant speed error) or a parabola (a speed error that changes slowly), chosen by blocked
  cross-validation. It is never a connect-the-dots line through the anchors, so the playback
  rate 1 + D'(t) changes only gradually. Respeeding applies D alone.
* ``c_k`` is a constant per continuous segment. Segments split where the recorder stopped (the
  offset jumps by the stopped time) or the reel was edited (a jump of a few seconds, like the
  4.279 s repeat on 572-AAA). The constants place the respeeded audio on GET; they never bend D.
* A reel whose anchors cannot measure its rate well (slope standard error above
  ``PRIOR_SE_PPM``) takes the median rate of the well-measured reels, and only its segment
  constants are fitted. A reel without anchors gets that rate and no GET placement.

Anchors used: status ``ok`` (weight 1) and ``flagged`` (weight 0.5, a good start with a minor
flag); ``outlier``, ``rejected`` and ``isolated`` ones are not. The fit is robust (Huber).

Outputs:
* ``catalog/sg_drift/sg_drift_maps.json``   per reel: model, segments, errors, provenance
* ``catalog/sg_drift/sg_drift_summary.tsv`` one row per reel
* ``.local/mission_data/sg/sg_drift_residuals.tsv`` every anchor with its fitted and held-out error
* ``reports/sg_drift/<reel>.png`` and ``mission.png`` when matplotlib is available
  (``uv run --with matplotlib python scripts/run.py sg_drift_fit.py``)

Known edits that the anchors cannot resolve are listed in ``catalog/sg_drift/known_edits.tsv``
(reel, source_s, note) and always split a segment there.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]  # scripts/sg-audio/<stage>/
ANCHORS = REPO / '.local/mission_data/sg/sg_anchors.tsv'
REELS = REPO / 'MISSION_DATA/sg_reels.tsv'
CATALOG = REPO / 'catalog/sg_drift'
KNOWN_EDITS = CATALOG / 'known_edits.tsv'
RESIDUALS = REPO / '.local/mission_data/sg/sg_drift_residuals.tsv'
REPORTS = REPO / 'reports/sg_drift'
VERSION = 'sg-drift-v2'  # v2: replay islands dropped; anchors from CTC-aligned word times

WEIGHT = {'ok': 1.0, 'flagged': 0.5}
GAP_JUMP_S = 20.0      # an offset step this large between neighbouring anchors: the recorder stopped
REPLAY_MAX_ANCHORS = 4  # a segment entered by a backward step this large, with at most this many anchors, is a
                        # replay or a mismatch: a stop only moves GET forward. Its anchors are dropped.
EDIT_MIN_S = 2.0       # a smaller step counts as an edit only if at least this large ...
EDIT_Z = 4.0           # ... and this many standard errors of the two sides' medians
EDIT_MIN_SIDE = 5      # ... with at least this many anchors on each side
EDIT_WINDOW = 10       # anchors compared on each side of a step
FOLDS = 5              # contiguous blocks for held-out scoring
MAX_DEGREE = 2
DEGREE_GAIN = 0.9      # a higher degree must cut the held-out mean error to this share ...
DEGREE_GAIN_S = 0.05   # ... and by at least this many seconds
RATE_SPAN_PPM = 6000   # largest rate change across one file a curve may have (the data support up to ~5,800)
PRIOR_SE_PPM = 300     # slope standard error above which the reel takes the mission rate
SUMMARY_FIELDS = ('reel', 'duration_s', 'status', 'rate_from', 'degree', 'anchors', 'segments', 'edits', 'steps',
                  'rate_start_ppm', 'rate_end_ppm', 'slope_se_ppm', 'fit_mad_s', 'fit_p95_s', 'cv_mae_s', 'cv_p95_s',
                  'cv_mae_linear_s', 'cv_mae_quadratic_s', 'get_first', 'get_last', 'note')
RESIDUAL_FIELDS = ('anchor_id', 'reel', 'source_s', 'get_seconds', 'status', 'weight', 'segment', 'offset_s',
                   'fitted_s', 'residual_s', 'heldout_residual_s', 'event_id', 'get_choice')


def read_tsv(path: Path) -> list[dict]:
    csv.field_size_limit(10 ** 9)
    with path.open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def write_tsv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fields, delimiter='\t', extrasaction='ignore', lineterminator='\n')
        w.writeheader()
        w.writerows(rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def format_get(seconds: float) -> str:
    s = int(round(seconds))
    sign, s = ('-', -s) if s < 0 else ('', s)
    return f'{sign}{s // 3600:03d}:{s % 3600 // 60:02d}:{s % 60:02d}'


# ------------------------------------------------------------------ fitting
def design(t: np.ndarray, seg: np.ndarray, nseg: int, degree: int, scale: float) -> np.ndarray:
    """Columns: one constant per segment, then (t/scale)^d for d = 1..degree (D(0) = 0)."""
    cols = [(seg == k).astype(float) for k in range(nseg)]
    cols += [(t / scale) ** d for d in range(1, degree + 1)]
    return np.column_stack(cols) if cols else np.zeros((len(t), 0))


def robust_fit(X: np.ndarray, y: np.ndarray, w: np.ndarray, iterations: int = 30) -> np.ndarray:
    """Weighted Huber regression by iteratively reweighted least squares."""
    beta = np.linalg.lstsq(X * np.sqrt(w)[:, None], y * np.sqrt(w), rcond=None)[0]
    for _ in range(iterations):
        r = y - X @ beta
        sigma = max(1.4826 * np.median(np.abs(r - np.median(r))), 0.25)
        k = 1.5 * sigma
        h = np.where(np.abs(r) <= k, 1.0, k / np.maximum(np.abs(r), 1e-9))
        ww = w * h
        new = np.linalg.lstsq(X * np.sqrt(ww)[:, None], y * np.sqrt(ww), rcond=None)[0]
        if np.allclose(new, beta, atol=1e-6):
            beta = new
            break
        beta = new
    return beta


class Fit:
    """D(t) coefficients and segment constants for one reel."""

    def __init__(self, t, y, w, seg, degree, scale, slope=None):
        self.degree, self.scale, self.nseg = degree, scale, int(seg.max()) + 1 if len(seg) else 0
        if slope is None:
            beta = robust_fit(design(t, seg, self.nseg, degree, scale), y, w)
            self.constants, self.poly = beta[:self.nseg], beta[self.nseg:]
        else:  # rate from the mission prior: only the segment constants are fitted
            self.poly = np.array([slope * scale])
            self.degree = 1
            beta = robust_fit(design(t, seg, self.nseg, 0, scale), y - slope * t, w)
            self.constants = beta[:self.nseg]

    def drift(self, t):
        t = np.asarray(t, float)
        return sum(c * (t / self.scale) ** (d + 1) for d, c in enumerate(self.poly))

    def rate_ppm(self, t):
        t = np.asarray(t, float)
        return 1e6 * sum((d + 1) * c * (t / self.scale) ** d / self.scale for d, c in enumerate(self.poly))

    def predict(self, t, seg):
        return np.asarray(self.constants)[seg] + self.drift(t)


def segments_by_gaps(t, y, known: list[float]) -> np.ndarray:
    """Segment index per anchor: split at large offset steps and at known edits."""
    seg = np.zeros(len(t), int)
    k = 0
    for i in range(1, len(t)):
        if abs(y[i] - y[i - 1]) > GAP_JUMP_S or any(t[i - 1] < e <= t[i] for e in known):
            k += 1
        seg[i] = k
    return seg


def replay_islands(t, y, known: list[float]) -> np.ndarray:
    """Mask of anchors in short segments that begin with a backward offset step (GET earlier than
    the reel position allows). A recorder stop makes the offset jump forward; material whose GET
    goes back by more than GAP_JUMP_S is a replay on the reel or a wrong match, and a few anchors
    cannot place it as a segment of its own. Known edits (small repeats) are below GAP_JUMP_S."""
    drop = np.zeros(len(t), bool)
    while True:
        keep = np.flatnonzero(~drop)
        seg = segments_by_gaps(t[keep], y[keep], known)
        found = False
        for k in range(1, seg.max() + 1 if len(seg) else 0):
            idx = keep[seg == k]
            before = keep[seg == k - 1]
            if len(idx) <= REPLAY_MAX_ANCHORS and np.median(y[idx]) < np.median(y[before]) - GAP_JUMP_S:
                drop[idx] = True
                found = True
                break
        if not found:
            return drop


def split_edits(r: np.ndarray, seg: np.ndarray) -> list[int]:
    """Anchor indices where the residuals step: the median of the next EDIT_WINDOW anchors differs
    from that of the previous EDIT_WINDOW by at least EDIT_MIN_S and EDIT_Z standard errors.
    Both edges of a run of shifted anchors are found; the strongest step within a window wins."""
    found = []
    for k in np.unique(seg):
        idx = np.where(seg == k)[0]
        part = r[idx]
        n = len(part)
        if n < 2 * EDIT_MIN_SIDE:
            continue
        sigma = max(1.4826 * np.median(np.abs(part - np.median(part))), 0.25)
        win = min(EDIT_WINDOW, n // 2)
        threshold = max(EDIT_MIN_S, EDIT_Z * sigma * np.sqrt(2 / win))
        scores = np.zeros(n)
        for i in range(EDIT_MIN_SIDE, n - EDIT_MIN_SIDE + 1):
            left, right = part[max(0, i - win):i], part[i:i + win]
            scores[i] = abs(np.median(right) - np.median(left)) / threshold
        taken = []
        for i in np.argsort(-scores):
            if scores[i] <= 1:
                break
            if all(abs(i - j) >= win for j in taken):
                taken.append(int(i))
        found += [int(idx[i]) for i in taken]
    return sorted(found)


def renumber(seg: np.ndarray, cuts: list[int]) -> np.ndarray:
    out = seg.copy()
    for c in sorted(cuts):
        out[c:] += 1
    _, out = np.unique(out, return_inverse=True)
    return out


def heldout(t, y, w, seg, degree, scale, slope=None) -> np.ndarray:
    """Held-out residual per anchor from contiguous folds (NaN where its segment has no training anchor)."""
    out = np.full(len(t), np.nan)
    bounds = np.linspace(0, len(t), FOLDS + 1).astype(int)
    for a, b in zip(bounds, bounds[1:]):
        test = np.zeros(len(t), bool)
        test[a:b] = True
        train = ~test
        if train.sum() < degree + 3:
            continue
        kept = np.unique(seg[train])
        remap = {k: i for i, k in enumerate(kept)}
        tseg = np.array([remap[k] for k in seg[train]])
        fit = Fit(t[train], y[train], w[train], tseg, degree, scale, slope)
        for i in np.where(test)[0]:
            if seg[i] in remap:
                out[i] = y[i] - fit.predict(t[i:i + 1], np.array([remap[seg[i]]]))[0]
    return out


def mae(values: np.ndarray) -> float:
    v = np.abs(values[np.isfinite(values)])
    return float(np.mean(v)) if len(v) else float('nan')


def p95(values: np.ndarray) -> float:
    v = np.abs(values[np.isfinite(values)])
    return float(np.percentile(v, 95)) if len(v) else float('nan')


def slope_se_ppm(t, y, w, seg, scale) -> float:
    """Standard error of a linear D's slope, from the robust residual scale."""
    X = design(t, seg, int(seg.max()) + 1, 1, scale)
    beta = robust_fit(X, y, w)
    r = y - X @ beta
    sigma = max(1.4826 * np.median(np.abs(r - np.median(r))), 0.25)
    try:
        cov = sigma ** 2 * np.linalg.inv((X * w[:, None]).T @ X)
    except np.linalg.LinAlgError:
        return float('inf')
    return float(np.sqrt(max(cov[-1, -1], 0)) / scale * 1e6)


def fit_reel(reel: dict, anchors: list[dict], known: list[float], prior_ppm: float | None,
             max_degree: int = MAX_DEGREE, apply_steps: bool = False) -> dict:
    duration = float(reel['duration_s'])
    scale = duration
    rows = sorted(anchors, key=lambda a: float(a['source_s']))
    out = {'reel': reel['reel'], 'file': reel['file'], 'sha256': reel['sha256'], 'duration_s': duration,
           'known_edits_s': known, 'anchors': len(rows)}
    if len(rows) < 3:
        out.update(status='rate_only' if prior_ppm is not None else 'no_fit', rate_from='mission_median',
                   degree=1, drift_coefficients_s=[prior_ppm * 1e-6 * scale] if prior_ppm is not None else [],
                   segments=[], note=f'{len(rows)} usable anchors: rate only, no GET placement')
        return out
    t = np.array([float(a['source_s']) for a in rows])
    y = np.array([float(a['offset_s']) for a in rows])
    replay = replay_islands(t, y, known)
    if replay.any():
        out['replay_anchors_dropped_s'] = [round(float(x), 3) for x in t[replay]]
        rows = [a for a, r in zip(rows, replay) if not r]
        t, y = t[~replay], y[~replay]
        out['anchors'] = len(rows)
    w = np.array([WEIGHT[a['status']] for a in rows])
    seg = segments_by_gaps(t, y, known)

    se = slope_se_ppm(t, y, w, seg, scale)
    use_prior = se > PRIOR_SE_PPM and prior_ppm is not None
    slope = prior_ppm * 1e-6 if use_prior else None

    def settle(degree):
        """Fit with the gap and known-edit segments. Residual steps are only reported: on the reels
        checked (564-AAA, 577-AAA) they begin and end at 1971 transcript page boundaries and
        return, so they are steps in the printed GETs, not reel edits. Applied only on request."""
        s = seg.copy()
        fit = Fit(t, y, w, s, degree, scale, slope)
        cuts = split_edits(y - fit.predict(t, s), s)
        detected = [float((t[c - 1] + t[c]) / 2) for c in cuts]
        if cuts and apply_steps:
            s = renumber(s, cuts)
            fit = Fit(t, y, w, s, degree, scale, slope)
        return fit, s, detected

    scores = {}
    candidates = {}
    for degree in ([1] if use_prior else range(1, max_degree + 1)):
        fit, s, detected = settle(degree)
        span = abs(fit.rate_ppm(duration) - fit.rate_ppm(0))
        if degree > 1 and span > RATE_SPAN_PPM:
            continue  # a curve bending faster than a slow drift: not physical
        ho = heldout(t, y, w, s, degree, scale, slope)
        scores[degree] = mae(ho)
        candidates[degree] = (fit, s, detected, ho)
    degree = 1
    for d in sorted(scores):
        if d > degree and np.isfinite(scores[d]) and scores[d] <= DEGREE_GAIN * scores[degree] and \
                scores[degree] - scores[d] >= DEGREE_GAIN_S:
            degree = d
    fit, seg, detected, ho = candidates[degree]
    r = y - fit.predict(t, seg)

    segments = []
    for k in range(fit.nseg):
        idx = np.where(seg == k)[0]
        start = 0.0 if k == 0 else float((t[idx[0] - 1] + t[idx[0]]) / 2)
        end = duration if k == fit.nseg - 1 else float((t[idx[-1]] + t[idx[-1] + 1]) / 2)
        segments.append({'segment': k, 'source_start_s': round(start, 3), 'source_end_s': round(end, 3),
                         'first_anchor_s': round(float(t[idx[0]]), 3), 'last_anchor_s': round(float(t[idx[-1]]), 3),
                         'anchors': int(len(idx)), 'constant_s': round(float(fit.constants[k]), 3),
                         'get_at_start': format_get(start + fit.drift(start) + fit.constants[k]),
                         'boundary': 'file_start' if k == 0 else (
                             'known_edit' if any(t[idx[0] - 1] < e <= t[idx[0]] for e in known) else (
                                 'recorder_gap' if abs(y[idx[0]] - y[idx[0] - 1]) > GAP_JUMP_S else 'detected_edit'))})
    out.update(status='fitted', rate_from='mission_median' if use_prior else 'reel', degree=degree,
               drift_coefficients_s=[round(float(c), 6) for c in fit.poly], drift_scale_s=scale,
               slope_se_ppm=round(se, 1), rate_start_ppm=round(float(fit.rate_ppm(0)), 1),
               rate_end_ppm=round(float(fit.rate_ppm(duration)), 1),
               fit_mad_s=round(float(np.median(np.abs(r))), 3), fit_p95_s=round(p95(r), 3),
               cv_mae_s=round(mae(ho), 3), cv_p95_s=round(p95(ho), 3),
               cv_mae_by_degree_s={str(d): round(v, 3) for d, v in scores.items()},
               residual_steps_s=[round(e, 1) for e in detected], residual_steps_applied=apply_steps,
               segments=segments,
               note=f'slope s.e. {se:.0f} ppm: mission rate used' if use_prior else '')
    out['_arrays'] = (t, y, w, seg, fit, r, ho, rows)
    return out


# ------------------------------------------------------------------ outputs
def plot(results: list[dict]) -> bool:
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    REPORTS.mkdir(parents=True, exist_ok=True)
    for res in results:
        if '_arrays' not in res:
            continue
        t, y, w, seg, fit, r, ho, _ = res['_arrays']
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True)
        grid = np.linspace(0, res['duration_s'], 400)
        for k in range(fit.nseg):
            m = seg == k
            a1.scatter(t[m] / 60, y[m] - fit.constants[k], s=8 * w[m], label=None)
        a1.plot(grid / 60, fit.drift(grid), 'k-', lw=1.5, label=f'D(t), degree {fit.degree}')
        a1.set_ylabel('offset − segment constant (s)')
        a1.legend(loc='upper left')
        a1.set_title(f'{res["reel"]}: rate {res["rate_start_ppm"]:+.0f} → {res["rate_end_ppm"]:+.0f} ppm, '
                     f'{len(res["segments"])} segment(s), held-out MAE {res["cv_mae_s"]:.2f} s')
        a2.scatter(t / 60, r, s=6, c='tab:blue', label='fitted residual')
        a2.scatter(t / 60, ho, s=6, c='tab:orange', alpha=0.6, label='held-out residual')
        a2.axhline(0, color='k', lw=0.8)
        for s in res['segments'][1:]:
            for ax in (a1, a2):
                ax.axvline(s['source_start_s'] / 60, color='r' if s['boundary'] != 'recorder_gap' else 'grey',
                           ls='--', lw=0.8)
        a2.set_ylim(-8, 8)
        a2.set_ylabel('residual (s)')
        a2.set_xlabel('reel time (min)')
        a2.legend(loc='upper left')
        fig.tight_layout()
        fig.savefig(REPORTS / f'{res["reel"]}.png', dpi=110)
        plt.close(fig)
    fitted = [r for r in results if r['status'] == 'fitted']
    fig, ax = plt.subplots(figsize=(11, 4))
    for r in fitted:
        g0 = r['segments'][0]['get_at_start']
        h = int(g0.split(':')[0].lstrip('-') or 0) * (-1 if g0.startswith('-') else 1)
        ax.plot([h, h + r['duration_s'] / 3600], [r['rate_start_ppm'], r['rate_end_ppm']],
                color='tab:blue' if r['rate_from'] == 'reel' else 'tab:grey', lw=2)
        ax.annotate(r['reel'][:3], (h, r['rate_start_ppm']), fontsize=7)
    ax.set_xlabel('GET at reel start (h)')
    ax.set_ylabel('rate error (ppm)')
    ax.set_title('SG reels: fitted playback rate error (grey: mission median used)')
    fig.tight_layout()
    fig.savefig(REPORTS / 'mission.png', dpi=110)
    plt.close(fig)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--reel', nargs='+', help='only these reels (outputs are not written)')
    parser.add_argument('--max-degree', type=int, default=MAX_DEGREE)
    parser.add_argument('--apply-steps', action='store_true',
                        help='also split segments at residual steps (normally reported only: transcript GET steps)')
    args = parser.parse_args(argv)
    if not ANCHORS.is_file():
        print(f'{ANCHORS} is missing; run `sg_transcript.py sg anchors` first', file=sys.stderr)
        return 1
    reels = read_tsv(REELS)
    anchors = defaultdict(list)
    for a in read_tsv(ANCHORS):
        if a['status'] in WEIGHT:
            anchors[a['reel']].append(a)
    known = defaultdict(list)
    if KNOWN_EDITS.is_file():
        for e in read_tsv(KNOWN_EDITS):
            known[e['reel']].append(float(e['source_s']))
    wanted = [r for r in reels if not args.reel or r['reel'] in args.reel]

    # First pass: well-measured reels give the mission rate for the others.
    first = [fit_reel(r, anchors[r['reel']], known[r['reel']], None, max_degree=1) for r in reels]
    good = [f['rate_start_ppm'] for f in first if f.get('status') == 'fitted' and f['slope_se_ppm'] <= PRIOR_SE_PPM]
    prior = float(np.median(good)) if good else None
    print(f'mission rate: median {prior:.0f} ppm over {len(good)} well-measured reels'
          if prior is not None else 'no well-measured reel')
    results = [fit_reel(r, anchors[r['reel']], known[r['reel']], prior, args.max_degree, args.apply_steps)
               for r in wanted]

    summary, residuals = [], []
    for res in results:
        segs = res.get('segments', [])
        print(f'  {res["reel"]}: {res["status"]}, {res["rate_from"]} rate, degree {res.get("degree")}, '
              f'{res["anchors"]} anchors, {len(segs)} segment(s), rate {res.get("rate_start_ppm", "")}->'
              f'{res.get("rate_end_ppm", "")} ppm, held-out MAE {res.get("cv_mae_s", "")} s {res.get("note", "")}')
        by = res.get('cv_mae_by_degree_s', {})
        summary.append({**res, 'segments': len(segs), 'steps': len(res.get('residual_steps_s', [])), 'edits': sum(s['boundary'] in ('detected_edit', 'known_edit')
                                                                 for s in segs),
                        'cv_mae_linear_s': by.get('1', ''), 'cv_mae_quadratic_s': by.get('2', ''),
                        'get_first': segs[0]['get_at_start'] if segs else '',
                        'get_last': format_get(res['duration_s'] + (res['drift_coefficients_s'] and
                                                                    sum(res['drift_coefficients_s'])) +
                                               segs[-1]['constant_s']) if segs else ''})
        if '_arrays' in res:
            t, y, w, seg, fit, r, ho, rows = res['_arrays']
            for i, a in enumerate(rows):
                residuals.append({'anchor_id': a['anchor_id'], 'reel': a['reel'], 'source_s': a['source_s'],
                                  'get_seconds': a['get_seconds'], 'status': a['status'], 'weight': w[i],
                                  'segment': int(seg[i]), 'offset_s': a['offset_s'],
                                  'fitted_s': round(float(y[i] - r[i]), 3), 'residual_s': round(float(r[i]), 3),
                                  'heldout_residual_s': '' if not np.isfinite(ho[i]) else round(float(ho[i]), 3),
                                  'event_id': a['event_id'], 'get_choice': a['get_choice']})
    plotted = plot(results)
    if args.reel:
        print('partial run: outputs not written')
        return 0
    CATALOG.mkdir(parents=True, exist_ok=True)
    maps = {'version': VERSION, 'anchors_file': str(ANCHORS.relative_to(REPO)).replace('\\', '/'),
            'anchors_sha256': sha256(ANCHORS), 'reels_sha256': sha256(REELS),
            'known_edits_sha256': sha256(KNOWN_EDITS) if KNOWN_EDITS.is_file() else '',
            'mission_rate_ppm': round(prior, 1) if prior is not None else None,
            'model': 'GET(t) = t + D(t) + constant[segment]; D(t) = sum_d coef[d-1] * (t / drift_scale_s)^d; '
                     'respeed applies D only (rate 1 + dD/dt)',
            'reels': [{k: v for k, v in res.items() if not k.startswith('_')} for res in results]}
    (CATALOG / 'sg_drift_maps.json').write_text(json.dumps(maps, indent=1) + '\n', encoding='utf-8')
    write_tsv(CATALOG / 'sg_drift_summary.tsv', SUMMARY_FIELDS, summary)
    write_tsv(RESIDUALS, RESIDUAL_FIELDS, residuals)
    print(f'wrote {CATALOG / "sg_drift_maps.json"}, sg_drift_summary.tsv and {RESIDUALS}'
          + ('' if plotted else '; no plots (run with `uv run --with matplotlib`)'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
