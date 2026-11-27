"""Stage 07i: tape timing from IRIG-B time code recorded on the tape (hybrid with the 07a SG evidence).

A few 30-track reels carry their IRIG-B track (true CH01; delivered CH30 on the tapes found so
far, see stage 10). Its frames place canonical tape seconds on GMT to about a millisecond, and
the 1 kHz carrier survives stages 03-05b, so the time code is decoded on the 05b output, which
is already on the canonical tape clock every other stage uses. No deflutter is redone: the
blind 05 map already matches the carrier's flutter (stage-10 check), what the time code adds is
the absolute rate and placement that 07a otherwise estimates from SG matches.

Steps (artifacts under ``RETIMING_ROOT/irig/<tape>/``):

1. ``decode``: every candidate 05b channel (``deflutter.wav``) is decoded in full with the NARA
   decoder (``nara_irig.decode_stream``). One record per channel, bound to the WAV identity and
   its 05b review checksum: ``decode/CHnn.json``.
2. ``fit``: the channel with the most strict frames (at least ``MIN_STRICT``) is the time-code
   track. Its frames are cleaned and segmented with the NARA map logic (``nara_tape_map``:
   chains, recorder-stop segments, 10 s binned smoothing spline) after removing the reel's
   median rate, because these reels play 1.4-2.3 % slow and the NARA tolerances assume tapes
   within 0.5 % of speed. Inside a segment, time code with frames no more than ``BRIDGE_S`` apart
   is *covered* and defines the curve. Everywhere else:

   * if a complete 07a anchors edition exists for the tape (hybrid), the SG text, audio and
     Quindar evidence is fitted together with time-code positions (``sg_curve``; the anchors are
     on the earlier, real-time row, the time-code row, so no offset enters). Across an uncovered gap inside a segment that curve is corrected by
     a cubic Hermite term so value and rate meet the time code at both edges exactly; before
     the first and after the last frame the correction decays over ``TAIL_DECAY_S``;
   * without SG evidence, gaps are bridged by a cubic Hermite in GET (value and rate continuous)
     and the rate is held beyond the ends, as on the NARA tapes.

   The plan has the layout ``quindar_tape.load_shared_plan`` / ``target_map`` read, so 07b applies
   it like a 07a plan: ``fit-<digest>/plan.json``, ``curve.json``, ``timing_map.npz``.

GET placement: ``GET = output seconds + get_equals_output_plus_s`` per recorder segment, for the
earlier head row. The time code (true CH01) is on that row, which is on real time (docs/23), so
its GET applies with no offset; channels of the later row are moved earlier by 07b
(``head_rows``). The SG events' offset against the time code is the IA reel offset and is a
diagnostic (``timecode_minus_sg_events``).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

import sg_curve
from sg_inputs import RETIMING_ROOT, read_json, sha256_file, tape_channels, write_json

VERSION = 'irig-timing-v1'          # decode records (unchanged)
PLAN_VERSION = 'irig-timing-v2'     # v2: the time-code row is the real-time reference (docs/23); no voice offset
IRIG_ROOT = RETIMING_ROOT / 'irig'
DECODE_CHANNELS = (28, 29, 30)
MIN_STRICT = 1000
BRIDGE_S = 60.0              # gaps up to this are interpolated from the time code itself
SAMPLE_S = 30.0              # time-code positions given to the hybrid fit
IRIG_SIGMA_S = 0.005
IRIG_OFFSET_PRIOR_S = 3.0    # time-code track vs voice reference group (stagger groups are ~1.4 s apart)
TAIL_DECAY_S = 300.0
GRID_S = 0.1
SAMPLE_RATE = 8000


def _nara():
    """The NARA modules (sg-audio/01_sg-nara), imported on demand."""
    import nara_irig
    import nara_tape_map
    return nara_irig, nara_tape_map


def code_sha():
    here = Path(__file__).resolve()
    nara_irig, nara_tape_map = _nara()
    h = hashlib.sha256()
    for p in (here, Path(sg_curve.__file__), Path(nara_irig.__file__), Path(nara_tape_map.__file__)):
        h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------------ decode
def wav_identity(wav: Path):
    st = Path(wav).stat()
    return dict(path=str(wav), bytes=st.st_size, mtime_ns=st.st_mtime_ns)


def decode_path(tape, channel, root=IRIG_ROOT):
    return Path(root) / tape / 'decode' / f'CH{channel:02d}.json'


def decode_current(path: Path, channel_row: dict):
    if not path.is_file():
        return None
    rec = read_json(path)
    ok = (rec.get('version') == VERSION and rec.get('source') == wav_identity(channel_row['wav'])
          and rec.get('review_sha256') == channel_row['review_sha256'] and rec.get('origin_s') == channel_row['origin_s'])
    return rec if ok else None


def _decode_job(args):
    tape, row, out = args
    nara_irig, _ = _nara()
    t0 = time.monotonic()
    frames, total = nara_irig.decode_stream(row['wav'], None, SAMPLE_RATE)
    strict = [f for f in frames if f.strict]
    rec = dict(version=VERSION, tape=tape, channel=row['channel'], source=wav_identity(row['wav']),
               review_sha256=row['review_sha256'], origin_s=row['origin_s'], decoded_s=round(total, 3),
               seconds=round(time.monotonic() - t0, 1), candidates=len(frames), strict=len(strict),
               time_domain='canonical tape seconds = origin_s + 05b file seconds (Pr leading edge)',
               clock_basis='seconds since 1971-01-01 00:00 GMT (IRIG day 1 = Jan 1)',
               columns=['t_canonical_s', 'clock_s', 'quality'],
               frames=[[round(row['origin_s'] + f.t, 5), f.clock, round(f.quality, 3)] for f in strict])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.partial')
    tmp.write_text(json.dumps(rec, separators=(',', ':')), encoding='utf-8')
    tmp.replace(out)
    return rec


def decode(tapes, channels=DECODE_CHANNELS, workers=6, root=IRIG_ROOT, log=print):
    jobs, done = [], {}
    for tape in tapes:
        for row in tape_channels(tape):
            if row['channel'] not in channels or row['problem']:
                continue
            out = decode_path(tape, row['channel'], root)
            rec = decode_current(out, row)
            if rec:
                done[(tape, row['channel'])] = rec
            else:
                jobs.append((tape, row, out))
    log(f'{len(done)} decodes current, {len(jobs)} to decode with {workers} workers')
    t0 = time.monotonic()
    with ProcessPoolExecutor(workers) as ex:
        futs = {ex.submit(_decode_job, j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            tape, row, _ = futs[f]
            rec = f.result()
            done[(tape, row['channel'])] = rec
            el = time.monotonic() - t0
            log(f'[{i}/{len(jobs)} {el:6.0f}s] {tape} CH{row["channel"]:02d}: {rec["strict"]:,} strict frames')
    return done


def timecode_track(tape, root=IRIG_ROOT):
    """(decode record of the time-code channel, all decode records) or (None, records)."""
    recs = [read_json(p) for p in sorted((Path(root) / tape / 'decode').glob('CH[0-9][0-9].json'))]
    recs = [r for r in recs if r.get('version') == VERSION]
    best = max(recs, key=lambda r: r['strict'], default=None)
    return (best if best and best['strict'] >= MIN_STRICT else None), recs


# --------------------------------------------------------------- time-code curve
MIN_SEGMENT_FRAMES = 300  # an extra segment needs this many frames ...
MAX_SEGMENT_RMS_S = 0.05  # ... and a fit within this, else it is misreads, not a recorder stop
TREND_BIN_S = 60.0       # fine bins where frames are dense (late-reel slow-downs change the rate by 1 % in minutes)
TREND_COARSE_S = 300.0   # fallback bins where they are sparse
TREND_MIN_SLOPES = 15
TREND_MEDIAN_BINS = 5


def _binned(mid, s, width):
    edges = np.arange(mid.min(), mid.max() + width, width)
    b = np.digitize(mid, edges)
    out = []
    for k in np.unique(b):
        m = b == k
        if m.sum() >= TREND_MIN_SLOPES:
            out.append((float(np.median(mid[m])), float(np.median(s[m]))))
    return out


def trend(t, o):
    """(grid, rate) of do/dt: medians of consecutive-frame slopes per bin, interpolated over empty bins.

    Misread frames give outlier slopes; medians ignore them, and a running median over
    ``TREND_MEDIAN_BINS`` fine bins removes single bad bins. Slopes beyond 5 % of the reel median are
    discarded outright (no transfer ran that far off speed). Where a coarse bin has no fine bin, the
    coarse median fills in.
    """
    dt, do = np.diff(t), np.diff(o)
    ok = (dt > 0.5) & (dt < 5)
    s, mid = do[ok] / dt[ok], (t[1:][ok] + t[:-1][ok]) / 2
    if len(s) < TREND_MIN_SLOPES:
        r = float(np.median(s)) if len(s) else 0.0
        return np.array([t[0], t[-1]]), np.array([r, r])
    keep = np.abs(s - np.median(s)) < 0.05
    s, mid = s[keep], mid[keep]
    fine = _binned(mid, s, TREND_BIN_S)
    if len(fine) >= TREND_MEDIAN_BINS:
        ft, fr = map(np.asarray, zip(*fine))
        half = TREND_MEDIAN_BINS // 2
        fr = np.array([np.median(fr[max(0, i - half):i + half + 1]) for i in range(len(fr))])
        fine = list(zip(ft, fr))
    coarse = _binned(mid, s, TREND_COARSE_S)
    have = np.array([x for x, _ in fine]) if fine else np.zeros(0)
    rows = fine + [(x, r) for x, r in coarse if not len(have) or np.min(np.abs(have - x)) > TREND_COARSE_S / 2]
    rows.sort()
    if len(rows) < 2:
        r = float(np.median(s))
        return np.array([t[0], t[-1]]), np.array([r, r])
    gt, gr = map(np.asarray, zip(*rows))
    return gt, gr


class IrigCurve:
    """GMT seconds of the time-code track as a function of canonical tape seconds, per segment."""

    def __init__(self, t, clock):
        _, ntm = _nara()
        order = np.argsort(t)
        t, clock = np.asarray(t, float)[order], np.asarray(clock, float)[order]
        o = clock - t
        # slowly varying rate trend from consecutive frames (robust medians per TREND_BIN_S), so the
        # NARA link/join tolerances (0.5 % of speed) apply to what is left
        self.trend_t, self.trend_r = trend(t, o)
        self.r0 = float(np.median(self.trend_r))
        od = o - self._trend(t)
        runs = ntm.chains(t, od)
        segs = ntm.segments(t, od, runs)
        if not segs:
            raise ValueError('time-code frames form no consistent chains')
        self.t, self.clock = t, clock
        self.segments = []
        used = np.zeros(len(t), bool)
        for s in segs:
            used[s] = True
            bt, f, res = ntm.fit_segment(t[s], od[s])
            ts = t[s]
            covered = []
            gaps = np.flatnonzero(np.diff(ts) > BRIDGE_S)
            starts = np.r_[0, gaps + 1]
            ends = np.r_[gaps, len(ts) - 1]
            for a, b in zip(starts, ends):
                if b > a:
                    covered.append((float(ts[a]), float(ts[b])))
            r = res[np.abs(res) < 0.02]
            self.segments.append(dict(idx=s, f=f, first_t=float(ts[0]), last_t=float(ts[-1]), covered=covered,
                                      frames=int(len(s)), smoothing=f.smoothing,
                                      fit_rms_s=float(np.sqrt(np.mean(res ** 2))),
                                      residual_rms_ms=round(float(np.sqrt(np.mean(r ** 2))) * 1000, 3) if len(r) else None,
                                      residual_p99_ms=round(float(np.percentile(np.abs(res), 99)) * 1000, 3)))
        # A small or inconsistent extra segment is a run of misreads (typically where the time code
        # fades), not a recorder stop: a real stop starts a consistent new chain.
        if len(self.segments) > 1:
            best = max(self.segments, key=lambda x: x['frames'])
            kept = [x for x in self.segments if x is best or
                    (x['frames'] >= MIN_SEGMENT_FRAMES and x['fit_rms_s'] <= MAX_SEGMENT_RMS_S)]
            for x in self.segments:
                if not any(x is y for y in kept):
                    used[x['idx']] = False
            self.dropped_segments = [dict(frames=x['frames'], first_t=x['first_t'], last_t=x['last_t'],
                                          fit_rms_s=x['fit_rms_s']) for x in self.segments
                                     if not any(x is y for y in kept)]
            self.segments = kept
        else:
            self.dropped_segments = []
        self.rejected = int((~used).sum())

    def _trend(self, x):
        """Integral of the trend rate from tape second 0."""
        x = np.asarray(x, float)
        g, r = self.trend_t, self.trend_r
        cum = np.r_[0.0, np.cumsum((r[1:] + r[:-1]) / 2 * np.diff(g))]
        # exact integral of the piecewise-linear rate (quadratic inside each bin)
        i = np.clip(np.searchsorted(g, x, side='right') - 1, 0, len(g) - 2)
        h = g[i + 1] - g[i]
        u = np.clip(x, g[0], g[-1]) - g[i]
        base = cum[i] + r[i] * u + (r[i + 1] - r[i]) / (2 * h) * u ** 2
        # linear continuation beyond the trend grid (rate held)
        base = np.where(x < g[0], (x - g[0]) * r[0], base)
        return np.where(x > g[-1], cum[-1] + (x - g[-1]) * r[-1], base)

    def gmt(self, k, x):
        x = np.asarray(x, float)
        return x + self._trend(x) + self.segments[k]['f'](x)

    def rate(self, k, x):
        """dGMT / d(tape second) inside segment k."""
        x = np.asarray(x, float)
        return 1.0 + np.interp(x, self.trend_t, self.trend_r) + self.segments[k]['f'](x, 1)

    def covered_mask(self, grid):
        m = np.zeros(len(grid), bool)
        seg = np.full(len(grid), -1)
        for k, s in enumerate(self.segments):
            for a, b in s['covered']:
                sel = (grid >= a) & (grid <= b)
                m |= sel
                seg[sel] = k
        return m, seg


# ------------------------------------------------------------------ SG evidence
def latest_anchors(tape, root=RETIMING_ROOT):
    import sg_stage
    base = Path(root) / tape / 'sg_anchors'
    found = [p for p in base.iterdir() if p.is_dir() and p.name.startswith('anchors-') and sg_stage.complete(p)] \
        if base.is_dir() else []
    found = [p for p in found if read_json(p / 'manifest.json').get('status') == 'complete'
             and read_json(p / 'manifest.json').get('version') == sg_stage.VERSION]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def hybrid_fit(tape, curve: IrigCurve, anchors: Path, duration, root=RETIMING_ROOT, log=print):
    """SG curve fitted with time-code positions; returns (Curve, b_irig, details)."""
    import sg_stage
    ev, manifest, used = sg_stage.tape_evidence(tape, anchors)
    if ev is None or len(ev.t) < sg_curve.FIT['min_events']:
        return None, None, dict(status='no SG curve', anchors=anchors.name)
    ref = read_json(Path(root) / 'sg_reference' / manifest['reference'] / 'manifest.json')
    bprior = sg_stage.bprior_of(ref['segments'])
    launch = _nara()[1].RANGE_ZERO_S
    # The time code is on the earlier head row, which is the real-time row the 07a anchors are
    # placed on (docs/23): its GET applies to that row with no offset. What SG events show against
    # it is the IA reel offset (fitted as b_k) and is kept as a diagnostic only.
    offset = timecode_offset(curve, ev, launch)
    if offset['offset_s'] is None:
        return None, None, dict(status='no SG events inside the time code', anchors=anchors.name, offset=offset)
    b_irig = 0.0
    ts, gs = [], []
    for k, s in enumerate(curve.segments):
        for a, b in s['covered']:
            x = np.arange(a, b, SAMPLE_S)
            ts.append(x)
            gs.append(curve.gmt(k, x) - launch - b_irig)      # real-time (earlier) row GET
    ts, gs = np.concatenate(ts), np.concatenate(gs)
    cuts = sorted(set(cut_points(curve)) | {c for c in ev.cuts if not near_coverage(curve, c)})
    comb = sg_curve.TapeEvidence(
        tape=tape, duration=duration, t=np.r_[ev.t, ts], g=np.r_[ev.g, gs],
        sigma=np.r_[ev.sigma, np.full(len(ts), IRIG_SIGMA_S)], bkey=np.r_[ev.bkey, np.full(len(ts), None, object)],
        ref_kind=np.r_[ev.ref_kind, np.full(len(ts), 'nara', object)], kind=np.r_[ev.kind, np.full(len(ts), 'irig', object)],
        cuts=cuts, rate_t=ev.rate_t, rate_r=ev.rate_r, rate_sigma=ev.rate_sigma)
    lam, _, validation, _ = sg_curve.choose_lambda(comb, bprior)
    sol = sg_curve.solve([comb], {tape: lam}, bprior)
    fitted = sol.curves[tape]
    # SG accuracy where the time code is the truth: SG events against the time-code GET
    sg_sel = np.isin(comb.kind, ['text', 'audio'])
    t_sg, g_sg, kind_sg = comb.t[sg_sel], comb.g[sg_sel], comb.kind[sg_sel]
    truth = np.full(len(t_sg), np.nan)
    for k, s in enumerate(curve.segments):
        for a, b in s['covered']:
            m = (t_sg >= a) & (t_sg <= b)
            truth[m] = curve.gmt(k, t_sg[m]) - launch - b_irig
    check = {}
    for label in ('text', 'audio'):
        m = (kind_sg == label) & np.isfinite(truth)
        if m.any():
            r = g_sg[m] - truth[m]
            check[label] = dict(events=int(m.sum()), median_s=float(np.median(r)), median_abs_s=float(np.median(np.abs(r))),
                                p90_abs_s=float(np.percentile(np.abs(r), 90)))
    log(f'{tape}: hybrid with {int(sg_sel.sum()):,} SG events and {len(ts):,} time-code positions; lambda {lam:g}; '
        f'SG events against the time code {offset["offset_s"]:+.3f} s (+/- {offset["uncertainty_s"]:.3f}, '
        f'{offset["basis"]}; the IA reel offset, fitted as b_k)')
    return fitted, b_irig, dict(status='fitted', anchors=anchors.name, evidence=used, lam=lam, irig_offset_s=b_irig,
                                timecode_minus_sg_events=offset,
                                ia_offsets_s={k: float(v) for k, v in sol.b.items()}, sg_events=int(sg_sel.sum()),
                                irig_positions=int(len(ts)), heldout=validation['heldout'],
                                sg_against_timecode=check)


MIN_OFFSET_EVENTS = 50


def timecode_offset(curve: IrigCurve, ev, launch):
    """Time-code track GET minus voice reference-group GET, measured where the time code is the truth.

    Per SG reference segment (NARA tapes are IRIG-referenced; each IA drift-map segment carries
    its own offset error), the median of time-code GET - SG event GET. NARA segments decide when
    they have ``MIN_OFFSET_EVENTS`` events; otherwise the median of the IA segment medians, whose
    spread (MAD) is the uncertainty.
    """
    rows = {}
    for k, s in enumerate(curve.segments):
        for a, b in s['covered']:
            m = (ev.t >= a) & (ev.t <= b) & np.isin(ev.kind, ['text', 'audio'])
            for i in np.flatnonzero(m):
                key = ('nara', None) if ev.ref_kind[i] == 'nara' else ('ia', ev.bkey[i])
                rows.setdefault(key, []).append(float(curve.gmt(k, ev.t[i]) - launch - ev.g[i]))
    groups = [dict(ref_kind=k[0], segment=k[1], events=len(v), median_s=float(np.median(v)),
                   mad_s=float(np.median(np.abs(np.asarray(v) - np.median(v))))) for k, v in rows.items()]
    nara = [g for g in groups if g['ref_kind'] == 'nara' and g['events'] >= MIN_OFFSET_EVENTS]
    ia = [g for g in groups if g['ref_kind'] == 'ia' and g['events'] >= MIN_OFFSET_EVENTS]
    if nara:
        g = nara[0]
        return dict(offset_s=g['median_s'], uncertainty_s=1.4826 * g['mad_s'] / math.sqrt(g['events']),
                    basis='NARA (IRIG-referenced) SG events', groups=groups)
    if ia:
        med = np.array([g['median_s'] for g in ia])
        spread = 1.4826 * float(np.median(np.abs(med - np.median(med)))) if len(med) > 1 else 0.5
        return dict(offset_s=float(np.median(med)), uncertainty_s=max(spread / math.sqrt(len(med)), 0.05),
                    basis=f'median of {len(ia)} IA segment medians (IA reel offsets limit it)', groups=groups)
    return dict(offset_s=None, uncertainty_s=None, basis='no SG events inside the time code', groups=groups)


def cut_points(curve: IrigCurve):
    return [(a['last_t'] + b['first_t']) / 2 for a, b in zip(curve.segments[:-1], curve.segments[1:])]


def near_coverage(curve: IrigCurve, x, margin=BRIDGE_S):
    return any(a - margin <= x <= b + margin for s in curve.segments for a, b in s['covered'])


# -------------------------------------------------------------------------- map
def hermite(x, x0, x1, y0, y1, d0, d1):
    """Cubic Hermite value and derivative on [x0, x1]."""
    h = x1 - x0
    s = (x - x0) / h
    h00, h10, h01, h11 = 2 * s**3 - 3 * s**2 + 1, s**3 - 2 * s**2 + s, -2 * s**3 + 3 * s**2, s**3 - s**2
    v = h00 * y0 + h10 * h * d0 + h01 * y1 + h11 * h * d1
    dv = ((6 * s**2 - 6 * s) * y0 + (3 * s**2 - 4 * s + 1) * h * d0 + (-6 * s**2 + 6 * s) * y1 + (3 * s**2 - 2 * s) * h * d1) / h
    return v, dv


def build_rate(curve: IrigCurve, duration, sg=None, b_irig=0.0):
    """Rate dGET/dt on the grid and a per-grid label of where it came from."""
    launch = _nara()[1].RANGE_ZERO_S
    grid = np.unique(np.r_[np.arange(0.0, duration, GRID_S), duration])
    rate = np.full(len(grid), np.nan)
    source = np.full(len(grid), 'hold', dtype=object)
    covered, seg_of = curve.covered_mask(grid)
    for k in range(len(curve.segments)):
        m = covered & (seg_of == k)
        rate[m] = curve.rate(k, grid[m])
        source[m] = 'timecode'

    def base(x):     # SG curve in the time-code track's GET frame, and its rate
        if sg is None:
            return np.zeros(len(x)), np.zeros(len(x))
        return sg.get(x) + b_irig, sg.rate(x)

    # gaps inside a segment: both edges known, so value and rate meet the time code exactly
    for k, s in enumerate(curve.segments):
        for (a0, a1), (b0, b1) in zip(s['covered'][:-1], s['covered'][1:]):
            m = (grid > a1) & (grid < b0)
            if not m.any():
                continue
            x = grid[m]
            ya, yb = curve.gmt(k, a1) - launch, curve.gmt(k, b0) - launch
            da, db = curve.rate(k, a1), curve.rate(k, b0)
            B, dB = base(np.array([a1, b0]))
            v, dv = hermite(x, a1, b0, ya - B[0], yb - B[1], da - dB[0], db - dB[1])
            _, dBx = base(x)
            rate[m] = (dBx if sg is not None else 0.0) + dv
            source[m] = 'hybrid_gap' if sg is not None else 'bridge'
    # outside the segments: tails and recorder-stop gaps
    edges = [(s['first_t'], k, 'start') for k, s in enumerate(curve.segments)] + \
            [(s['last_t'], k, 'end') for k, s in enumerate(curve.segments)]
    rest = np.isnan(rate)
    if rest.any():
        x = grid[rest]
        if sg is not None:
            # nearest segment edge: decaying correction from the SG rate to the time-code rate
            E = np.array([e for e, _, _ in edges])
            D = np.array([float(curve.rate(k, e)) - float(sg.rate(np.array([e]))[0]) for e, k, _ in edges])
            near = np.argmin(np.abs(x[:, None] - E[None, :]), axis=1)
            rate[rest] = sg.rate(x) + D[near] * np.exp(-np.abs(x - E[near]) / TAIL_DECAY_S)
            source[rest] = 'hybrid_tail'
        else:
            kt = [e for e, _, _ in sorted(edges)]
            kr = [float(curve.rate(k, e)) for e, k, _ in sorted(edges)]
            rate[rest] = np.interp(x, kt, kr)
            source[rest] = 'hold'
    return grid, rate, source


def placement(curve: IrigCurve, grid, output, duration):
    """Segment table: GET = output + get_equals_output_plus_s (time-code track)."""
    launch = _nara()[1].RANGE_ZERO_S
    cuts = cut_points(curve)
    bounds = [0.0, *cuts, float(duration)]
    rows = []
    for k, s in enumerate(curve.segments):
        idx = s['idx']
        g = curve.clock[idx] - launch
        c = g - np.interp(curve.t[idx], grid, output)
        rows.append(dict(segment=k, tape_start_s=bounds[k], tape_end_s=bounds[k + 1],
                         get_equals_output_plus_s=float(np.median(c)),
                         frame_residual_rms_ms=float(np.sqrt(np.mean((c - np.median(c)) ** 2)) * 1000),
                         frame_residual_p99_ms=float(np.percentile(np.abs(c - np.median(c)), 99) * 1000),
                         frames=s['frames'], first_frame_s=s['first_t'], last_frame_s=s['last_t'],
                         covered_s=float(sum(b - a for a, b in s['covered']))))
    return rows


# ------------------------------------------------------------------------- plan
def fit_tape(tape, root=RETIMING_ROOT, use_sg=True, log=print):
    """Build (or reuse) the 07i plan of ``tape``. Returns the plan path or None when there is no time code."""
    import sg_stage
    irig_root = Path(root) / 'irig'
    track, recs = timecode_track(tape, irig_root)
    if track is None:
        log(f'{tape}: no time code on decoded channels {[r["channel"] for r in recs]}; use 07a')
        return None
    channels = tape_channels(tape)
    by_ch = {c['channel']: c for c in channels}
    row = by_ch.get(track['channel'])
    if row is None or decode_current(decode_path(tape, track['channel'], irig_root), row) is None:
        raise ValueError(f'{tape}: time-code decode of CH{track["channel"]:02d} is stale; run decode')
    anchors = latest_anchors(tape, root) if use_sg else None
    payload = dict(version=PLAN_VERSION, tape=tape, code=code_sha(), decode=sha256_file(decode_path(tape, track['channel'], irig_root)),
                   channels=[(c['channel'], c['review_sha256'], c['origin_s']) for c in channels],
                   anchors=anchors.name if anchors else None,
                   settings=dict(bridge_s=BRIDGE_S, sample_s=SAMPLE_S, sigma_s=IRIG_SIGMA_S,
                                 offset_prior_s=IRIG_OFFSET_PRIOR_S, tail_decay_s=TAIL_DECAY_S, min_strict=MIN_STRICT))
    name = sg_stage.edition_name('fit', payload, True)
    folder = irig_root / tape / name
    if sg_stage.complete(folder):
        return folder / 'plan.json'

    def writer(tmp):
        began = time.monotonic()
        f = np.asarray(track['frames'], float)
        curve = IrigCurve(f[:, 0], f[:, 1])
        duration = max(c['origin_s'] + c['duration_s'] for c in channels)
        sg, b_irig, hybrid = (None, None, dict(status='no complete 07a anchors edition'))
        if anchors is not None:
            sg, b_irig, hybrid = hybrid_fit(tape, curve, anchors, duration, root, log)
        grid, rate, source = build_rate(curve, duration, sg, b_irig or 0.0)
        if not np.isfinite(rate).all() or rate.min() <= 0:
            raise ValueError(f'{tape}: invalid rate')
        output = np.r_[0.0, np.cumsum((rate[1:] + rate[:-1]) / 2 * np.diff(grid))]
        segs = placement(curve, grid, output, duration)
        for s in segs:
            s['timecode_track_get_equals_output_plus_s'] = s['get_equals_output_plus_s']
        shares = {k: float(np.sum(source == k) / len(source)) for k in ('timecode', 'bridge', 'hybrid_gap', 'hybrid_tail', 'hold')}
        np.savez_compressed(tmp / 'timing_map.npz', input_seconds=grid, output_seconds=output, recorded_speed_ratio=rate)
        anchors_out = [dict(input_seconds=float(x), get_s=float(curve.gmt(k, x) - _nara()[1].RANGE_ZERO_S), kind='irig')
                       for k, s in enumerate(curve.segments) for a, b in s['covered'] for x in np.arange(a, b, SAMPLE_S)]
        support = dict(timecode_share=shares['timecode'], sources=shares,
                       covered_intervals_s=[iv for s in curve.segments for iv in s['covered']],
                       first_frame_s=curve.segments[0]['first_t'], last_frame_s=curve.segments[-1]['last_t'])
        model = dict(schema=1, method='irig_timecode' + ('+sg_hybrid' if sg is not None else ''),
                     anchors=anchors_out, kernel_sigma_seconds=10.0,
                     smoothing_note='time code: 10 s binned smoothing spline per segment (nara_tape_map); '
                                    'kernel_sigma_seconds is that bin width',
                     chosen_validation=dict(segments=[{k: v for k, v in s.items() if k not in ('idx', 'f')}
                                                      for s in curve.segments], hybrid=hybrid),
                     support=support, input_duration_seconds=duration, output_duration_seconds=float(output[-1]),
                     timecode_rate_removed_before_chaining=curve.r0,
                     edge_policy=('SG curve, corrected to meet the time code at the nearest edge (decay '
                                  f'{TAIL_DECAY_S:g} s)') if sg is not None else 'rate held beyond the first/last frame',
                     gap_policy=(f'gaps over {BRIDGE_S:g} s: SG curve plus a cubic Hermite correction meeting value and '
                                 'rate at both edges') if sg is not None else 'cubic Hermite in GET between the edges')
        write_json(tmp / 'curve.json', model)
        references, targets, origins = [], [], set()
        for c in channels:
            review = read_json(c['review_path'])
            st = Path(c['wav']).stat()
            references.append(dict(id=f"{tape}:CH{c['channel']:02d}", channel=c['channel'], path=c['wav'], bytes=st.st_size,
                                   mtime_ns=st.st_mtime_ns, review_sha256=c['review_sha256'], start_seconds=c['origin_s']))
            targets.append(dict(review['source'], flutter_map_sha256=review.get('map_sha256')))
            origins.add(review['source']['absolute_start'] - review['source'].get('acquisition_start_seconds',
                                                                                 review['source']['start_seconds']))
        if len(origins) != 1:
            raise ValueError(f'{tape}: channels disagree on the tape-clock origin')
        flags = []
        if anchors is None:
            flags.append('no current 07a anchors edition: gaps and ends are bridged from the time code alone')
        if shares['hold'] + shares['hybrid_tail'] > 0.05:
            flags.append(f"{shares['hold'] + shares['hybrid_tail']:.0%} of the tape is outside the time-code span")
        plan = dict(
            schema=2, status='complete', input_stage='defluttered', tape=tape,
            scope='one time-code tape curve (hybrid with SG evidence outside the time code) applied identically to '
                  'every channel of this tape',
            method=model['method'], version=PLAN_VERSION, fit_edition=name,
            evidence=dict(timecode_channel=track['channel'], decode=str(decode_path(tape, track['channel'], irig_root)),
                          strict_frames=track['strict'], frames_rejected=curve.rejected,
                          dropped_segments=curve.dropped_segments,
                          decoded_channels={r['channel']: r['strict'] for r in recs}, hybrid=hybrid),
            origin_filename_seconds=origins.pop(), input_duration_seconds=duration,
            complete_defluttered_coverage=all(not c['problem'] for c in channels) and bool(channels),
            reference_count=len(references), references=references, targets=targets, clock_anchor_source=None,
            synchronization_claim=False,
            get_placement=dict(rule='GET = output seconds + segment get_equals_output_plus_s, for the earlier head row '
                                    '(the time-code row, real time); a channel of the later row is its stagger late and '
                                    '07b moves it earlier by it (head_rows.py; docs/23)',
                               segments=segs, timecode_track_offset_from_reference_s=0.0,
                               reference='earlier head row (time-code row)'),
            support=support,
            timing_offsets_policy='Capture offsets (stage 02) are in the canonical origins and are preserved. One inverse '
                                  'map for every channel; no channel is fitted, anchored or bypassed alone.',
            time_basis='Canonical post-05b tape seconds (review start_seconds + local seconds)',
            review_flags=dict(timecode=flags, recorder_cuts_s=cut_points(curve)), listening_approved=False,
            seconds=round(time.monotonic() - began, 1))
        plan['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp.iterdir()
                             if p.name in ('curve.json', 'timing_map.npz')}
        write_json(tmp / 'plan.json', plan)
        write_json(tmp / 'manifest.json', dict(status='complete', edition=name, step='irig_fit', version=PLAN_VERSION,
                                               inputs=payload, tape=tape))
        log(f"{tape}: time code on CH{track['channel']:02d}, {track['strict']:,} strict frames, "
            f"{len(curve.segments)} segment(s), {shares['timecode']:.0%} of tape time from time code, "
            f"output {output[-1]:.1f} s for {duration:.1f} s; frame residual "
            f"{max(s['frame_residual_rms_ms'] for s in segs):.2f} ms rms")

    sg_stage.publish(folder, writer)
    return folder / 'plan.json'


def irig_plan(tape, fit=None, root=RETIMING_ROOT):
    """The newest complete 07i plan of the current plan version (or the named edition), else None.
    Plans of earlier versions place GET on another reference row (docs/23) and are not applied."""
    if fit is not None:
        path = Path(fit) / 'plan.json'
        return path if path.is_file() else None
    base = Path(root) / 'irig' / tape
    found = [p for p in base.glob('fit-*') if (p / 'plan.json').is_file() and (p / 'manifest.json').is_file()
             and read_json(p / 'manifest.json').get('status') == 'complete'
             and read_json(p / 'manifest.json').get('version') == PLAN_VERSION] if base.is_dir() else []
    return max(found, key=lambda p: (p / 'plan.json').stat().st_mtime_ns) / 'plan.json' if found else None


def timecode_positions(tape, root=IRIG_ROOT, step=SAMPLE_S):
    """Time-code positions for the 07a joint fit: (tape seconds, GET, recorder cuts) on the
    earlier (real-time) row, every ``step`` seconds inside covered spans; None without time code."""
    track, _ = timecode_track(tape, root)
    if track is None:
        return None
    rows = {c['channel']: c for c in tape_channels(tape)}
    row = rows.get(track['channel'])
    if row is None or decode_current(decode_path(tape, track['channel'], root), row) is None:
        raise ValueError(f'{tape}: time-code decode of CH{track["channel"]:02d} is stale; run 07i decode')
    f = np.asarray(track['frames'], float)
    curve = IrigCurve(f[:, 0], f[:, 1])
    launch = _nara()[1].RANGE_ZERO_S
    ts, gs = [], []
    for k, s in enumerate(curve.segments):
        for a, b in s['covered']:
            x = np.arange(a, b, step)
            ts.append(x)
            gs.append(curve.gmt(k, x) - launch)
    return dict(t=np.concatenate(ts), g=np.concatenate(gs), cuts=cut_points(curve), channel=track['channel'],
                decode_sha256=sha256_file(decode_path(tape, track['channel'], root)), strict=track['strict'])
