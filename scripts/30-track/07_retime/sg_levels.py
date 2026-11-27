"""Level gaps for the 07a joint fit: an island without time code takes its GET level from the journals.

docs/23_2026-11-26_head_rows_and_real_time.md, "Level gaps".

Across a long stretch of tape without position evidence the P-spline rate is only weakly
constrained. Carried across six hours, a 0.03 % rate error is 6.5 s, and the joint fit put that
drift into the IA segment offsets instead of following the journal (TEC/PDF) placement of the IA
reels (fit-fee097094a89eb4c: offsets -7.9..+6.5 s on the IA-only tapes T510079-T510097).

Rule: where consecutive position anchors of a tape are more than ``min_gap_s`` apart, no recorder
cut lies there, and an island on either side has fewer than ``min_island_events`` NARA or
time-code positions, the GET level on each side is fitted separately (a free constant, like a
recorder cut). Such an island is then placed by its IA segments through their drift-map (journal)
placement and the IA offset prior. Gaps between two time-coded islands keep the continuous fit.
The rate stays continuous. In the published curve the level difference is spread over the whole
gap as a smoothstep, so the timing map stays continuous and monotone. It adds at most
1.5 * |step| / gap to the rate inside the gap, recorded per gap (``level_ramps`` in curve.json).

The position fit is exact: no position anchor lies inside a ramp, except members of islands
smaller than ``min_island_events``, which are fitted against the level of the side their time is on.
Quindar rate witnesses inside a gap are fitted to the spline rate alone; the ramp is added on
top of them.

Only the final joint solve changes. The smoothing (lambda) of each tape is chosen exactly as in
``sg_curve.choose_lambda``, without level gaps. The joint solve is done on GET minus a unit-rate
line per tape (``conditioned_solve``). The spline reproduces that line exactly and the penalty
does not see it, so the model is the same; without it the normal equations mix GET-sized values
(6e5 s) with the penalty and lose precision at large lambda (T510099: second-scale NARA residuals
from lambda 1e8; T510104 at 1e4: a negative roughness c'Pc and 0.5 s differences 3 h from the
nearest anchor).

The wrapper also adds the journal anchors (sg_journal.py): TEC/AFJ events in every tape's
evidence before its level gaps are found, a ``journal:TEC`` offset among the reference segments,
and journal anchors editions for tapes without an SG curve.

This module only wraps ``sg_stage.build_fit``. The upstream editions (reference, anchors, audio,
quindar) hash sg_stage.CODE and are unchanged. The fit edition records this module's hash and
settings in each tape's evidence (``level_gaps``), so it gets a new name.
"""
from __future__ import annotations

import dataclasses
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import sg_curve
import sg_stage

VERSION = 'level-gaps-v1'
LEVELS = dict(
    min_gap_s=3600.0,         # longer stretches without position anchors can separate GET levels
    min_island_events=20,     # fewer anchors between two gaps do not start their own level, and an
                              # island with fewer NARA/time-code positions takes its level from IA
)


def code_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def level_gaps(t, absolute, cuts, settings=LEVELS):
    """Gaps [(a, b)] between evidence islands where the level is fitted separately.

    ``absolute`` marks NARA/time-code positions. ``a`` and ``b`` are the last and first position
    anchors of the islands on either side."""
    order = np.argsort(np.asarray(t, float))
    t = np.asarray(t, float)[order]
    absolute = np.asarray(absolute, bool)[order]
    if len(t) < 2:
        return []
    bounds = np.r_[0, np.flatnonzero(np.diff(t) > settings['min_gap_s']) + 1, len(t)]
    runs = [(t[i], t[j - 1], j - i, int(absolute[i:j].sum())) for i, j in zip(bounds[:-1], bounds[1:])]
    runs = [r for r in runs if r[2] >= settings['min_island_events']]
    out = []
    for (_, a, _, abs_a), (b, _, _, abs_b) in zip(runs[:-1], runs[1:]):
        timed = min(abs_a, abs_b) >= settings['min_island_events']
        if b - a > settings['min_gap_s'] and not timed and not any(a <= c <= b for c in cuts):
            out.append((float(a), float(b)))
    return out


def smoothstep(t, a, b):
    x = np.clip((np.asarray(t, float) - a) / (b - a), 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def smoothstep_rate(t, a, b):
    x = np.clip((np.asarray(t, float) - a) / (b - a), 0.0, 1.0)
    return 6.0 * x * (1.0 - x) / (b - a)


@dataclass
class RampedCurve(sg_curve.Curve):
    """A ``sg_curve.Curve`` plus level ramps [(a, b, step_s)] across evidence gaps (smoothstep)."""
    ramps: list = field(default_factory=list)

    def rate(self, t):
        r = super().rate(t)
        tc = np.clip(np.asarray(t, float), self.first_s, self.last_s)
        for a, b, step in self.ramps:
            r = r + step * smoothstep_rate(tc, a, b)
        return r

    def get(self, t):
        g = super().get(t)
        for a, b, step in self.ramps:
            g = g + step * smoothstep(t, a, b)
        return g

    def to_json(self):
        return dict(super().to_json(), level_ramps=[
            dict(gap_start_s=a, gap_end_s=b, level_step_s=step,
                 max_rate_added=1.5 * abs(step) / (b - a)) for a, b, step in self.ramps],
            level_gap_rule=f'{VERSION}: GET = spline + recorder-segment constant + sum of level_step_s * smoothstep '
                           'over each gap (sg_levels.py)')

    @classmethod
    def from_json(cls, d):
        base = sg_curve.Curve.from_json(d)
        return cls(**vars(base), ramps=[(r['gap_start_s'], r['gap_end_s'], r['level_step_s'])
                                        for r in d.get('level_ramps', [])])


def ramped(curve, gaps):
    """The cut-model ``curve`` (real cuts plus one level cut per gap) as real cuts plus ramps.

    Identical to ``curve`` outside the gaps."""
    level = {(a + b) / 2: (a, b) for a, b in gaps}
    cuts = list(curve.cuts)
    consts = np.asarray(curve.seg_const, float)
    real, ramps, carried = [], [], 0.0
    for i in range(len(cuts) + 1):
        boundary = cuts[i - 1] if i else None
        if boundary is not None and boundary in level:
            step = float(consts[i] - consts[i - 1])
            ramps.append((*level[boundary], step))
            carried += step
        else:
            real.append(float(consts[i] - carried))
    return RampedCurve(curve.tape, curve.knots, curve.coef, np.asarray(real), [c for c in cuts if c not in level],
                       curve.first_s, curve.last_s, curve.lam, ramps=ramps)


def greville(knots, n):
    """Coefficients of the line t on a cubic B-spline basis (Greville abscissae)."""
    k = np.asarray(knots, float)
    return (k[1:n + 1] + k[2:n + 2] + k[3:n + 3]) / 3.0


def conditioned_solve(tapes, lambdas, bprior, settings=sg_curve.FIT, masks=None, robust=True):
    """``sg_curve.solve`` on GET minus a unit-rate line per tape; the line is added back to each curve."""
    lines = {e.tape: float(np.median(e.g - e.t)) if len(e.t) else 0.0 for e in tapes}
    shifted = [dataclasses.replace(e, g=e.g - (lines[e.tape] + e.t), rate_r=e.rate_r - 1.0) for e in tapes]
    sol = sg_curve.solve(shifted, lambdas, bprior, settings, masks, robust)
    for e in tapes:
        c = sol.curves[e.tape]
        c.coef = c.coef + lines[e.tape] + greville(c.knots, len(c.coef))
    return sol


class _CurveModule:
    """``sg_curve`` as seen by ``sg_stage.build_fit``: its joint solve frees the level gaps."""

    def __init__(self, gaps):
        self._gaps = gaps

    def __getattr__(self, name):
        return getattr(sg_curve, name)

    def solve(self, tapes, lambdas, bprior, settings=sg_curve.FIT, masks=None, robust=True):
        gaps = {e.tape: self._gaps.get(e.tape, []) for e in tapes}
        cut = [dataclasses.replace(e, cuts=[*e.cuts, *((a + b) / 2 for a, b in gaps[e.tape])]) for e in tapes]
        sol = conditioned_solve(cut, lambdas, bprior, settings, masks, robust)
        for e in tapes:
            curve = ramped(sol.curves[e.tape], gaps[e.tape])
            sol.curves[e.tape] = curve
            sol.residuals[e.tape] = e.g - sg_curve.predict(curve, e, sol.b)
        return sol


@contextmanager
def _hooked(root, log, journal=True):
    import sg_journal
    gaps = {}
    tape_evidence = sg_stage.tape_evidence
    read_json = sg_stage.read_json

    def with_levels(tape, anchors):
        ev, manifest, extra = tape_evidence(tape, anchors)
        if ev is None:
            return ev, manifest, extra
        if journal:  # journal events first: they fill gaps before the level gaps are found
            ev, extra = sg_journal.extend(ev, manifest, extra, tape, anchors, root, log)
        gaps[tape] = level_gaps(ev.t, ev.ref_kind == 'nara', ev.cuts)
        extra = dict(extra, level_gaps=dict(version=VERSION, code=code_sha(), settings=LEVELS,
                                            gaps_s=[[a, b] for a, b in gaps[tape]]))
        return ev, manifest, extra

    def with_journal_segment(path):
        """The reference manifest as the fit reads it, with the journal offset among its segments
        (its prior and its row in ia_offsets.tsv)."""
        value = read_json(path)
        p = Path(path)
        if journal and p.name == 'manifest.json' and p.parent.parent.name == 'sg_reference':
            value = dict(value, segments=[*value['segments'], sg_journal.SEGMENT])
        return value

    original = sg_stage.sg_curve
    sg_stage.tape_evidence = with_levels
    sg_stage.sg_curve = _CurveModule(gaps)
    sg_stage.read_json = with_journal_segment
    try:
        yield gaps
    finally:
        sg_stage.tape_evidence = tape_evidence
        sg_stage.sg_curve = original
        sg_stage.read_json = read_json


def build_fit(tapes, root=sg_stage.RETIMING_ROOT, log=print, allow_provisional=False, anchors_by_tape=None):
    """``sg_stage.build_fit`` with a separate GET level per island without time code (module doc)
    and the journal anchors (sg_journal.py): journal events on every tape, and a journal anchors
    edition for a tape whose SG anchors have no curve."""
    import sg_journal
    anchors_by_tape = sg_journal.substitute(anchors_by_tape, root, log)
    with _hooked(root, log) as gaps:
        folder = sg_stage.build_fit(list(anchors_by_tape), root, log=log, allow_provisional=allow_provisional,
                                    anchors_by_tape=anchors_by_tape)
    n = sum(len(v) for v in gaps.values())
    log(f'Level gaps ({VERSION}, > {LEVELS["min_gap_s"]:.0f} s without anchors, beside an island without time code): '
        f'{n} on {sum(1 for v in gaps.values() if v)} tapes')
    return folder
