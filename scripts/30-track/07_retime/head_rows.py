"""Head-row placement of every 30-track channel along its tape (stage 07b; docs/23).

The 1971 recorders wrote odd and even true tracks with two staggered head rows. In the delivered
capture every sound reaches the channels of the later row about 1.38 s after the channels of the
earlier row. The earlier row carries the IRIG-B time code (true CH01) and is on real time; the
tape plans (07i, 07a) define GET for it. 07b moves each later-row channel earlier by its measured
stagger, so that all channels of a tape play on real time.

Which row a delivered channel reads is measured, not assumed:

* Delivered numbers do not give it: the playback head sat off the recorded track pitch, so true
  tracks are skipped part-way across the head (stage 10) and the odd/even alternation breaks
  there.
* It can change along the tape: where the head slides by one track a channel moves from one row
  to the other part-way through the reel (e.g. T510075 CH19).

Evidence, in order:

1. ``measured``: the channel's stagger in each 30-minute interval of the current 07a anchors
   edition (``stagger.tsv``; staggers are relative to the earlier row since sg-retime-v3).
2. ``held``: an interval without a measurement takes the row of the nearest measured interval of
   the same channel. A change of row is placed half-way between the last interval of the old
   row and the first of the new one and ramped over ``RAMP_S`` (at most 1.38/RAMP_S speed
   change, under the tapes' flutter).
3. ``timecode``: the decoded time-code channel is on the earlier row by definition.
4. ``neighbours``: a channel with no measurement at all, between two channels on the same row in
   an interval, is on the other row (the rows alternate wherever no track is skipped).
5. ``channel_map``: otherwise the parity of its stage-10 true track (odd: earlier row).
6. ``unknown``: otherwise no shift (bypass), flagged.

Intervals measured as ``mixed`` (the channel carries both copies: it straddles two tracks) keep
the held row and are flagged; a single shift cannot separate two copies.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from sg_inputs import RETIMING_ROOT, read_json, read_tsv, sha256_file

VERSION = 'head-rows-v1'
ROW_TOLERANCE_S = 0.3
RAMP_S = 600.0
INTERVAL_S = 1800.0
MIN_RUN = 2   # runs of at least this many measured intervals on both sides make a lone interval an outlier
CHANNEL_MAPS = Path(str(RETIMING_ROOT)).parents[1] / 'channel_id' / 'shared-v2' / 'maps'


def current_anchors(tape, root=RETIMING_ROOT):
    """Newest complete anchors edition of the current sg_stage version with a curve, else None."""
    import sg_stage
    base = Path(root) / tape / 'sg_anchors'
    found = []
    for p in base.iterdir() if base.is_dir() else []:
        m = p / 'manifest.json'
        if p.name.startswith('anchors-') and m.is_file():
            rec = read_json(m)
            if rec.get('status') == 'complete' and rec.get('version') == sg_stage.VERSION and rec.get('result') == 'curve':
                found.append(p)
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _map_parity(tape, root):
    path = Path(root) / f'{tape}.json'
    if not path.is_file():
        return {}, None
    m = read_json(path)
    out = {}
    for d, c in m.get('channels', {}).items():
        t = c.get('true_channel')
        if t:
            out[int(d)] = 'E' if int(t) % 2 else 'L'
    return out, sha256_file(path)


def row_plan(tape, channels, duration, timecode_channel=None, root=RETIMING_ROOT, maps_root=CHANNEL_MAPS):
    """Per delivered channel: the row in every interval, its basis, and the shift knots.

    ``channels``: delivered channel numbers present on the tape. Returns a JSON-ready dict;
    ``channels[d]['knots']`` is [[canonical tape s, seconds to move earlier], ...] (piecewise
    linear, constant beyond the ends)."""
    anchors = current_anchors(tape, root)
    n_int = int(math.ceil(duration / INTERVAL_S))
    rows = {d: [None] * n_int for d in channels}
    basis = {d: [None] * n_int for d in channels}
    flags = {d: [] for d in channels}
    staggers = {d: [] for d in channels}
    groups, anchors_name, late_gap = [], None, None
    if anchors is not None:
        manifest = read_json(anchors / 'manifest.json')
        anchors_name = anchors.name
        groups = list(manifest.get('stagger_groups_s') or [])
        if len(groups) >= 2 and not manifest.get('single_row_group'):
            late_gap = float(groups[1])
        for r in read_tsv(anchors / 'stagger.tsv'):
            d, k = int(r['channel']), int(r['interval'])
            if d not in rows or not 0 <= k < n_int or late_gap is None:
                continue
            if r['measured'] in ('True', True) and r['stagger_s'] not in ('', None):
                s = float(r['stagger_s'])
                if abs(s) <= ROW_TOLERANCE_S:
                    rows[d][k], basis[d][k] = 'E', 'measured'
                elif abs(s - late_gap) <= ROW_TOLERANCE_S:
                    rows[d][k], basis[d][k] = 'L', 'measured'
                    staggers[d].append(s)
            elif r['mixed'] in ('True', True):
                basis[d][k] = 'mixed'
                flags[d].append(f'interval {k}: both rows (straddles two tracks); held row used')
    # an isolated measured interval against runs of the other row on both sides is a measurement
    # outlier, not a head slide (those persist: T510075 CH19, CH20)
    for d in channels:
        m = [k for k in range(n_int) if basis[d][k] == 'measured']
        for i in range(1, len(m) - 1):
            a, k, b = m[i - 1], m[i], m[i + 1]
            if rows[d][a] == rows[d][b] != rows[d][k] and _run(rows[d], basis[d], m, i - 1, -1) >= MIN_RUN \
                    and _run(rows[d], basis[d], m, i + 1, 1) >= MIN_RUN:
                flags[d].append(f'interval {k}: isolated {rows[d][k]} measurement against {rows[d][a]} runs; ignored')
                rows[d][k], basis[d][k] = None, 'outlier'
    # 2. hold the nearest measured row of the same channel
    measured = {d: [k for k in range(n_int) if basis[d][k] == 'measured'] for d in channels}
    for d in channels:
        m = measured[d]
        if not m:
            continue
        for k in range(n_int):
            if rows[d][k] is None:
                near = min(m, key=lambda j: (abs(j - k), j))
                rows[d][k] = rows[d][near]
                basis[d][k] = 'held' if basis[d][k] is None else basis[d][k] + '+held'
    # 3. the time-code channel
    if timecode_channel in rows and not measured.get(timecode_channel):
        rows[timecode_channel] = ['E'] * n_int
        basis[timecode_channel] = ['timecode'] * n_int
    # 4. neighbours, 5. channel-map parity, 6. unknown
    parity, maps_sha = _map_parity(tape, maps_root)
    for d in channels:
        if measured[d] or d == timecode_channel:
            continue
        for k in range(n_int):
            a, b = rows.get(d - 1, [None] * n_int)[k], rows.get(d + 1, [None] * n_int)[k]
            if a is not None and a == b and basis[d - 1][k] in ('measured', 'held') and basis[d + 1][k] in ('measured', 'held'):
                rows[d][k], basis[d][k] = ('L' if a == 'E' else 'E'), 'neighbours'
            elif d in parity:
                rows[d][k], basis[d][k] = parity[d], 'channel_map'
            else:
                basis[d][k] = 'unknown'
        if any(x == 'unknown' for x in basis[d]):
            flags[d].append('row unknown in some intervals: not shifted there')
    # knots
    out = {}
    for d in channels:
        gap = float(np.median(staggers[d])) if staggers[d] else late_gap
        value = [None if r is None else (gap if r == 'L' else 0.0) for r in rows[d]]
        knots = []
        last_k, last_v = None, None
        for k in range(n_int):
            v = value[k]
            if v is None:
                continue
            if last_v is None:
                knots.append([0.0, v])
            elif v != last_v:
                # change between the end of interval last_k and the start of interval k
                lo, hi = (last_k + 1) * INTERVAL_S, k * INTERVAL_S
                mid = (lo + hi) / 2 if hi > lo else lo
                half = RAMP_S / 2
                knots.append([max(mid - half, 0.0), last_v])
                knots.append([min(mid + half, duration), v])
            last_k, last_v = k, v
        if not knots:
            knots = [[0.0, 0.0]]
            flags[d].append('no row: not shifted')
        knots.append([float(duration), knots[-1][1]])
        out[d] = dict(rows=''.join(r or '?' for r in rows[d]), basis=basis[d], knots=knots,
                      late_stagger_s=gap, flags=flags[d])
    return dict(version=VERSION, tape=tape, anchors=anchors_name, stagger_groups_s=groups, late_row_gap_s=late_gap,
                interval_s=INTERVAL_S, ramp_s=RAMP_S, timecode_channel=timecode_channel,
                channel_maps_sha256=maps_sha, real_time_row='earlier head row (time-code row; docs/23)',
                channels={str(d): v for d, v in sorted(out.items())})


def _run(row, basis, m, i, step):
    """Length of the run of equal measured rows starting at m[i] and going in ``step``."""
    n, j = 0, i
    while 0 <= j < len(m) and basis[m[j]] == 'measured' and row[m[j]] == row[m[i]]:
        n += 1
        j += step
    return n


def shift_at(knots, t):
    """Seconds to move a channel earlier at canonical tape seconds ``t``."""
    k = np.asarray(knots, float)
    return np.interp(np.asarray(t, float), k[:, 0], k[:, 1])


def shifted_local_map(local_t, start, knots, tt, oo, ss):
    """Output seconds and speed of local input seconds of a channel moved earlier by its row shift.

    A later-row sample at canonical tape time ``t`` was recorded with the earlier row at ``t - s(t)``;
    it is placed at the tape map's output of that time. Outside the map the end rate is held."""
    t = start + np.asarray(local_t, float)
    s = shift_at(knots, t)
    q = t - s
    o = np.interp(q, tt, oo)
    lo, hi = q < tt[0], q > tt[-1]
    if lo.any():
        o[lo] = oo[0] + (q[lo] - tt[0]) * ss[0]
    if hi.any():
        o[hi] = oo[-1] + (q[hi] - tt[-1]) * ss[-1]
    ds = np.gradient(s, t) if len(t) > 1 else np.zeros(len(t))
    speed = np.interp(q, tt, ss) * (1.0 - ds)
    return o, speed, s


def row_plan_digest(plan):
    return json.dumps(plan, sort_keys=True)
