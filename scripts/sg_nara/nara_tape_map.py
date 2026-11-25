"""NARA step 2: one shared timing map per flight tape, from the IRIG-B frames of all its tracks.

All four files of a tape were played back by one multitrack head in a single pass, cut at
different start points: ``nara_align.py`` places each file on one tape axis (offsets constant
to a fraction of a millisecond for the whole tape). The map is built once per tape, on that
axis, from the union of every file's strict frames (each at its ``clock_start_s``), and applied
identically to every file.

Model. A strict frame says: at original-file second t the recorded GMT was G (seconds since
1971-01-01). The offset o = G - t changes slowly (the transfer ran off speed, and the speed
drifted along the tape) and jumps where the 1971 recorder stopped while the IRIG generator ran
on. The map is built in four steps:

1. Pool: every strict frame of every track; duplicates of the same second on several tracks
   are merged (median time).
2. Chains: consecutive frames are linked when their offsets agree within the speed limits
   (``LINK_S`` + ``MAX_RATE`` x elapsed). Chains shorter than ``MIN_CHAIN`` frames, which is
   where bit errors in the day/time fields land, are dropped.
3. Segments: neighbouring chains whose offsets continue each other join one segment; an offset
   step larger than that starts a new segment (a recorder stop; the boundary is placed in the
   middle of the gap between the two chains and its uncertainty is that gap).
4. Curve: per segment, frames are binned (``BIN_S``) and a cubic smoothing spline o_k(t) is
   fitted to the bin medians. The bin rate never changes faster than the tape transport can,
   so the spline follows wow slower than ``BIN_S`` and ignores frame-time jitter.

The respeed curve is D(t) = integral of o_k'(t): output seconds u = t + D(t) run at mission
rate inside every segment. Between segments, and before the first or after the last frame,
the rate is held at the nearest measured value and ramps linearly across gaps, so nothing is
forced where there is no timecode. Each segment's placement is GET = u + c_k with constant c_k.

Outputs under ``F:/Apollo_15_data/processing/sg_nara/maps/``: ``<tape>.json`` (segments,
grid of t, D and rate, residual statistics, provenance) and ``maps.tsv`` (one row per tape).

    uv run python scripts/run.py nara_tape_map.py --tape all
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.interpolate import make_smoothing_spline

from nara_align import ALIGN_ROOT
from nara_irig_survey import IRIG_ROOT, load as load_survey
from nara_sources import DATA_ROOT, NARA_ROOT, by_tape, tracks

MAP_ROOT = DATA_ROOT / 'maps'
VERSION = 'nara-map-v1'
RANGE_ZERO_S = (207 - 1) * 86400 + 13 * 3600 + 34 * 60   # 1971-07-26 13:34:00 GMT (day 207)
LINK_S = 0.010        # offset agreement between neighbouring frames, plus ...
MAX_RATE = 0.005      # ... this much per second elapsed (0.5 %: above any speed error seen here)
MAX_LINK_GAP_S = 60   # frames further apart than this are not linked directly (a 1 s field
                      # error then always exceeds the tolerance)
MIN_CHAIN = 8         # frames in a chain before it counts
MIN_SEGMENT = 30      # frames a segment needs to be placed on its own
JOIN_S = 0.050        # chains whose offsets continue each other within this join a segment ...
JOIN_RATE = 0.004     # ... plus this much per second of gap (the speed range seen on these tapes)
BIN_S = 10.0          # binning for the curve
MIN_BIN = 3           # frames needed in a bin
SMOOTHING = 1e3       # spline penalty on the bin medians (weights normalised to mean 1). Chosen on
                      # 608 and 640: the spline's rate then matches independent 60 s line fits to
                      # 10-14 ppm rms with ~0.5 ms frame residuals; GCV's choice follows the noise
                      # (60 ppm, and 7,000 ppm at a segment end)
RATE_BOUNDS = (-0.005, 0.010)  # physical range for the fitted rate (seen: -380 to +4,500 ppm)
SUPPORT_GAP_S = 30.0  # coverage counts tape time between frames closer than this
EDGE_S = 60.0         # the rate held beyond a segment end is a line fit over this many seconds
GRID_S = 0.1          # map grid written for the renderer


def pooled_frames(tape_tracks, root, clock_start):
    """(tape seconds, G, track index, quality) of strict frames, duplicates merged.

    A file's frames go on the tape axis at file seconds + its ``clock_start_s``; files without
    a timecode placement contribute nothing.
    """
    rows = []
    for k, tr in enumerate(tape_tracks):
        if clock_start.get(tr.name) is None:
            continue
        j = load_survey(root, tr)
        if j is None:
            raise ValueError(f'{tr.key}: no current IRIG survey (run nara_irig_survey.py)')
        f = np.array(j['frames'], dtype=float).reshape(-1, 9)
        f = f[f[:, 7] == 1]
        g = (f[:, 1] - 1) * 86400 + f[:, 2] * 3600 + f[:, 3] * 60 + f[:, 4]
        rows += [(t + clock_start[tr.name], gg, k, q) for t, gg, q in zip(f[:, 0], g, f[:, 6])]
    if not rows:
        return np.empty((0, 4))
    a = np.array(sorted(rows))
    # merge the same second seen on several tracks (same G, times within 50 ms)
    out, i = [], 0
    while i < len(a):
        j = i + 1
        while j < len(a) and a[j, 0] - a[i, 0] < 0.05 and a[j, 1] == a[i, 1]:
            j += 1
        grp = a[i:j]
        out.append((np.median(grp[:, 0]), grp[0, 1], grp[np.argmax(grp[:, 3]), 2], grp[:, 3].max()))
        i = j
    return np.array(out)


def chains(t, o):
    """Index arrays of runs of mutually consistent frames, in time order.

    Each frame joins the open chain whose last frame it continues (offset within
    ``LINK_S + MAX_RATE`` x elapsed, at most ``MAX_LINK_GAP_S`` apart), else starts a chain.
    Frames with a misread field therefore form short chains of their own instead of
    breaking the true run; chains shorter than ``MIN_CHAIN`` are dropped.
    """
    open_, done = [], []
    for i in range(len(t)):
        still = []
        for c in open_:
            (still if t[i] - t[c[-1]] <= MAX_LINK_GAP_S else done).append(c)
        open_ = still
        for c in sorted(open_, key=len, reverse=True):
            if abs(o[i] - o[c[-1]]) <= LINK_S + MAX_RATE * (t[i] - t[c[-1]]):
                c.append(i)
                break
        else:
            open_.append([i])
    done += open_
    runs = [np.array(c) for c in done if len(c) >= MIN_CHAIN]
    return sorted(runs, key=lambda r: t[r[0]])


def chain_line(t, o):
    k = np.polyfit(t, o, 1) if len(t) > 1 else (0.0, o[0])
    return float(k[0]), float(k[1])


def continues(t, o, a, b) -> bool:
    """Does chain ``b`` (later) continue chain ``a`` without a clock step?"""
    gap = t[b[0]] - t[a[-1]]
    if gap < -1:  # overlapping in time: two different clocks cannot both be right
        return False
    mid = (t[b[0]] + t[a[-1]]) / 2
    tail, head = a[-min(len(a), 200):], b[:min(len(b), 200)]
    s1, b1 = chain_line(t[tail] - mid, o[tail])
    s2, b2 = chain_line(t[head] - mid, o[head])
    return abs(b2 - b1) <= JOIN_S + JOIN_RATE * max(gap, 0)


def segments(t, o, runs):
    """Join chains into segments; returns list of index arrays.

    A short chain (< ``MIN_SEGMENT`` frames) is dropped when it overlaps a longer chain, or
    when the chains on either side of it continue each other: it is a misread, not a clock
    step. Remaining short standalone segments are dropped too (not enough to place them).
    """
    runs = sorted(runs, key=lambda r: t[r[0]])
    # overlaps: keep the longer chain
    kept = []
    for r in sorted(runs, key=len, reverse=True):
        if all(t[r[-1]] < t[k[0]] + 0.5 or t[r[0]] > t[k[-1]] - 0.5 or len(r) >= MIN_SEGMENT and
               continues(t, o, *sorted((k, r), key=lambda x: t[x[0]])) for k in kept):
            kept.append(r)
    runs = sorted(kept, key=lambda r: t[r[0]])
    changed = True
    while changed and len(runs) >= 3:
        changed = False
        for k in range(1, len(runs) - 1):
            if len(runs[k]) < MIN_SEGMENT and not continues(t, o, runs[k - 1], runs[k]) \
                    and continues(t, o, runs[k - 1], runs[k + 1]):
                runs.pop(k)
                changed = True
                break
    segs = []
    for r in runs:
        if segs and continues(t, o, segs[-1], r):
            segs[-1] = np.r_[segs[-1], r]
        else:
            segs.append(r)
    return [s for s in segs if len(s) >= MIN_SEGMENT]


def fit_segment(t, o):
    """Smoothing spline through bin means. Returns (bin centres, fitted curve, frame residuals).

    Every frame counts: a bin holds its frames' local line (or their mean when it has fewer
    than ``MIN_BIN``), weighted by the frame count. Should the rate leave ``RATE_BOUNDS``
    (sparse frames with long gaps let a lightly penalised spline swing), the penalty is raised
    tenfold until it stays inside.
    """
    edges = np.arange(np.floor(t[0] / BIN_S) * BIN_S, t[-1] + BIN_S, BIN_S)
    b = np.digitize(t, edges)
    bt, bo, bw = [], [], []
    for k in np.unique(b):
        m = b == k
        tm = t[m].mean()
        bt.append(tm)
        bo.append(np.polyfit(t[m] - tm, o[m], 1)[1] if m.sum() >= MIN_BIN else o[m].mean())
        bw.append(m.sum())
    bt, bo, bw = map(np.asarray, (bt, bo, bw))
    if len(bt) < 5:
        k = np.polyfit(t, o, 1)
        f = lambda x, nu=0: np.polyval(np.polyder(k, nu) if nu else k, x)
        f.smoothing = None
        return bt, f, o - f(t)
    lam = SMOOTHING
    probe = np.linspace(t[0], t[-1], max(100, int((t[-1] - t[0]) / 5)))
    while True:
        spl = make_smoothing_spline(bt, bo, w=bw / bw.mean(), lam=lam)
        r = spl(probe, 1)
        if (RATE_BOUNDS[0] <= r.min() and r.max() <= RATE_BOUNDS[1]) or lam >= 1e9:
            break
        lam *= 10
    f = lambda x, nu=0: spl(x, nu)
    f.smoothing = lam
    return bt, f, o - f(t)


def supported(t, gap_s=SUPPORT_GAP_S):
    """Total tape time where consecutive frames are less than ``gap_s`` apart."""
    d = np.diff(t)
    return float(d[d < gap_s].sum())


def edge_rates(t, o) -> tuple[float, float]:
    """Rates from line fits over the first and last ``EDGE_S`` seconds of a segment's frames."""
    a = t <= t[0] + EDGE_S
    b = t >= t[-1] - EDGE_S
    ra = np.polyfit(t[a], o[a], 1)[0] if a.sum() >= 5 else np.polyfit(t, o, 1)[0]
    rb = np.polyfit(t[b], o[b], 1)[0] if b.sum() >= 5 else np.polyfit(t, o, 1)[0]
    return float(ra), float(rb)


def build(tape, tape_tracks, root, align):
    duration = align['tape_duration_s']
    clock_start = {n: f['clock_start_s'] for n, f in align['files'].items()}
    P = pooled_frames(tape_tracks, root, clock_start)
    base = dict(tape=tape, version=VERSION, duration_s=duration,
                tracks=[dict(name=tr.name, path=tr.path, sha256=tr.sha256, duration_s=tr.duration_s,
                             clock_start_s=clock_start.get(tr.name)) for tr in tape_tracks])
    if len(P) == 0:
        return dict(base, status='no_timecode', segments=[], note='no strict IRIG-B frame on any track')
    t, G, src = P[:, 0], P[:, 1], P[:, 2].astype(int)
    o = G - t
    runs = chains(t, o)
    segs = segments(t, o, runs)
    if not segs:
        return dict(base, status='no_timecode', segments=[], strict_frames=int(len(t)),
                    note='strict frames exist but none form consistent chains')
    used = np.zeros(len(t), bool)
    seg_out, fits = [], []
    for s in segs:
        used[s] = True
        bt, f, res = fit_segment(t[s], o[s])
        f.edge = edge_rates(t[s], o[s])
        r = res[np.abs(res) < 0.02]
        seg_out.append(dict(
            frames=int(len(s)), first_t=float(t[s[0]]), last_t=float(t[s[-1]]),
            residual_rms_ms=round(float(np.sqrt(np.mean(r ** 2))) * 1000, 3) if len(r) else None,
            residual_p99_ms=round(float(np.percentile(np.abs(res), 99)) * 1000, 3),
            frames_by_track={tape_tracks[k].name: int(np.sum(src[s] == k)) for k in range(len(tape_tracks))
                             if np.any(src[s] == k)},
            coverage_bins=int(len(bt)), coverage_s=round(supported(t[s]), 1),
            smoothing=f.smoothing))
        fits.append(f)
    # segment boundaries: middle of the gap between consecutive segments
    bounds = [0.0] + [(seg_out[k]['last_t'] + seg_out[k + 1]['first_t']) / 2 for k in range(len(segs) - 1)] + [duration]
    grid = np.unique(np.r_[np.arange(0, duration, GRID_S), duration])
    rate = np.empty_like(grid)  # o'(t): extra GMT seconds per original second
    # measured rate inside each segment's coverage; held at the edges, ramped across gaps
    knots_t, knots_r = [], []
    for k, (s, f) in enumerate(zip(seg_out, fits)):
        inside = (grid >= s['first_t']) & (grid <= s['last_t'])
        rate[inside] = f(grid[inside], 1)
        knots_t += [s['first_t'], s['last_t']]
        knots_r += list(f.edge)
    covered = np.zeros(len(grid), bool)
    for s in seg_out:
        covered |= (grid >= s['first_t']) & (grid <= s['last_t'])
    rate[~covered] = np.interp(grid[~covered], knots_t, knots_r)  # holds the end values outside
    D = np.r_[0.0, np.cumsum((rate[1:] + rate[:-1]) / 2 * np.diff(grid))]
    u = grid + D
    if np.any(np.diff(u) <= 0):
        raise ValueError(f'{tape}: non-monotone map')
    for k, (s, f) in enumerate(zip(seg_out, fits)):
        mid = np.linspace(s['first_t'], s['last_t'], 50)
        c = f(mid) + mid - (mid + np.interp(mid, grid, D)) - RANGE_ZERO_S
        s.update(source_start_s=round(bounds[k], 3), source_end_s=round(bounds[k + 1], 3),
                 # untimed audio before the first and after the last frame of this segment
                 untimed_before_s=round(s['first_t'] - (seg_out[k - 1]['last_t'] if k else 0.0), 3),
                 untimed_after_s=round((seg_out[k + 1]['first_t'] if k + 1 < len(seg_out) else duration)
                                       - s['last_t'], 3),
                 output_start_s=round(float(bounds[k] + np.interp(bounds[k], grid, D)), 3),
                 output_end_s=round(float(bounds[k + 1] + np.interp(bounds[k + 1], grid, D)), 3),
                 get_equals_output_plus_s=round(float(np.median(c)), 4),
                 constant_spread_ms=round(float(np.ptp(c)) * 1000, 4),
                 rate_ppm_first=round(f.edge[0] * 1e6, 1),
                 rate_ppm_last=round(f.edge[1] * 1e6, 1),
                 get_first=fmt_get(float(f(s['first_t']) + s['first_t'] - RANGE_ZERO_S)),
                 get_last=fmt_get(float(f(s['last_t']) + s['last_t'] - RANGE_ZERO_S)))
    covered_s = sum(s['coverage_s'] for s in seg_out)
    return dict(base, status='mapped', strict_frames=int(len(t)), frames_used=int(used.sum()),
                frames_rejected=int((~used).sum()), segments=seg_out,
                timecode_coverage_s=covered_s, timecode_coverage_share=round(covered_s / duration, 4),
                output_duration_s=float(u[-1]), added_s=float(D[-1]),
                rate_ppm_min=round(float(rate.min()) * 1e6, 1), rate_ppm_max=round(float(rate.max()) * 1e6, 1),
                grid=dict(step_s=GRID_S, t=None),  # replaced by the .npz below
                time_domain='output seconds = original seconds + D(original seconds); '
                            'GET = output seconds + segment constant',
                range_zero_gmt='1971-07-26T13:34:00Z'), grid, D, rate


def fmt_get(s: float) -> str:
    sign = '-' if s < 0 else ''
    s = abs(s)
    return f'{sign}{int(s // 3600):03d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}'


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--tape', nargs='+', required=True, help="tapes (255-PAOa-640-AAA or 640-AAA), or 'all'")
    p.add_argument('--nara-root', type=Path, default=NARA_ROOT)
    p.add_argument('--irig-root', type=Path, default=IRIG_ROOT)
    p.add_argument('--align-root', type=Path, default=ALIGN_ROOT)
    p.add_argument('--output-root', type=Path, default=MAP_ROOT)
    args = p.parse_args(argv)
    args.output_root.mkdir(parents=True, exist_ok=True)
    impl = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    rows = ['tape\tstatus\tsegments\tstrict_frames\trejected\tcoverage_share\trate_ppm_min\trate_ppm_max\t'
            'added_s\tresidual_rms_ms\tget_first\tget_last']
    for tape, items in by_tape(tracks(args.nara_root, args.tape)).items():
        align_path = args.align_root / f'{tape}.json'
        if not align_path.is_file():
            print(f'{tape}: no alignment (no timecode on any file); not mapped', flush=True)
            continue
        align = json.loads(align_path.read_text(encoding='utf-8'))
        if [t.sha256 for t in items] != [align['files'][t.name]['sha256'] for t in items]:
            raise ValueError(f'{tape}: alignment was made from other source files')
        result = build(tape, items, args.irig_root, align)
        if isinstance(result, tuple):
            m, grid, D, rate = result
            npz = args.output_root / f'{tape}.npz'
            np.savez(npz, t=grid, D=D, rate=rate)
            m['grid'] = dict(step_s=GRID_S, file=npz.name, sha256=hashlib.sha256(npz.read_bytes()).hexdigest())
        else:
            m = result
        m.update(alignment=dict(path=str(align_path), sha256=hashlib.sha256(align_path.read_bytes()).hexdigest()),
                 implementation_sha256=impl, built=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 irig_surveys={tr.name: str(args.irig_root / tape / f'{tr.name}.json') for tr in items})
        (args.output_root / f'{tape}.json').write_text(json.dumps(m, indent=1) + '\n', encoding='utf-8')
        segs = m['segments']
        rms = ','.join(str(s['residual_rms_ms']) for s in segs)
        rows.append(f"{tape}\t{m['status']}\t{len(segs)}\t{m.get('strict_frames', 0)}\t{m.get('frames_rejected', '')}\t"
                    f"{m.get('timecode_coverage_share', '')}\t{m.get('rate_ppm_min', '')}\t{m.get('rate_ppm_max', '')}\t"
                    f"{round(m['added_s'], 3) if 'added_s' in m else ''}\t{rms}\t"
                    f"{segs[0]['get_first'] if segs else ''}\t{segs[-1]['get_last'] if segs else ''}")
        print(rows[-1], flush=True)
    if 'all' in args.tape:
        (args.output_root / 'maps.tsv').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
