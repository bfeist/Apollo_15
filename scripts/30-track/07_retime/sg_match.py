"""SG reference stream, text anchors on every channel, pooled stagger and tape events.

docs/20_2026-11-25_sg_retime_30track.md, method steps 1-3.

* ``build_reference``: every SG ASR word on GET, IA through its drift-map segment and NARA through
  its respeed segment constant, with source, source seconds, probability and segment index.
* ``text_pairs``: distinctive 4-grams of each channel's ASR matched to the reference. Each match
  is a pair (canonical tape second, SG GET).
* ``vote``: per 20-minute block the best (offset, slope) line of ``d = GET - prior(t)``; outlier
  blocks against the running median are dropped, persistent steps start a new recorder segment,
  and pairs within 6 s of the mode curve are kept. The slope search follows the in-block rate
  changes (2.4 % within an hour on T510104), which a flat mode would smear.
* ``relative_stagger``: ``stagger = channel time - reference time`` (``t_ref = t - stagger``) from
  the same SG word heard on two channels, chained from the reference channel (the one with most
  pairs), per tape and per interval. It is measured without the curve, so tape motion cannot leak
  into it, and it is a placement that never creates a channel map. All channels are then pooled
  on one curve; pairs on channels without a measured stagger join the group their curve residual
  clearly falls in.
* ``tape_events``: one vote per SG word, whichever channels heard it.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np

from sg_inputs import prior_get

MATCH = dict(
    ngram=4,
    gram_span_s=6.0,
    max_channel_repeats=6,
    max_reference_count=3,
    span_margin_s=1.5 * 3600,
    vote_window_s=1800.0,
    block_s=1200.0,
    bin_s=2.0,
    smooth_bins=3,
    min_block_votes=6,
    slopes=np.round(np.arange(-0.12, 0.1201, 0.0005), 4).tolist(),
    cluster_s=30.0,
    min_block_clusters=2,
    max_mode_departure_s=20.0,
    continuity_rate=0.005,
    alone_votes=30,
    keep_s=6.0,
    run_gap_s=3600.0,
    stagger_interval_s=1800.0,
    min_stagger_pairs=12,
    group_gap_s=0.15,
    group_mad_s=0.15,
    group_min_share=0.05,
    group_tolerance_s=0.3,
    stagger_iterations=2,
    stagger_window_s=3.0,
    mixed_share=0.25,
    banned_grams=('thank you thank you',),
)


# ----------------------------------------------------------------- reference
def reference_from_sources(sources, words_of):
    """Concatenate SG words on GET. ``words_of(source)`` returns ``Words`` or None."""
    from sg_inputs import source_get
    tok, get, src, seg, s, p, aligned = [], [], [], [], [], [], []
    segments, out_sources = [], []
    for i, source in enumerate(sources):
        words = words_of(source)
        record = dict(kind=source['kind'], name=source['name'], identity_dir=source.get('identity_dir'),
                      words=0, dropped_outside_segments=0, segments=[])
        if words is None or not len(words):
            record['excluded'] = 'no transcript of the mapped audio' if words is None else 'no words'
            out_sources.append(record)
            continue
        g, k = source_get(source, words.s)
        inside = k >= 0
        base = len(segments)
        for j, sg in enumerate(source['segments']):
            segments.append(dict(sg, index=base + j, source_index=i))
            record['segments'].append(base + j)
        tok.append(words.tok[inside])
        get.append(g[inside])
        src.append(np.full(inside.sum(), i, int))
        seg.append(k[inside] + base)
        s.append(words.s[inside])
        p.append(words.p[inside])
        aligned.append(words.aligned[inside])
        record.update(words=int(inside.sum()), dropped_outside_segments=int((~inside).sum()),
                      transcript=words.meta.get('digest'))
        out_sources.append(record)
    cat = lambda xs, dt: np.concatenate(xs).astype(dt) if xs else np.zeros(0, dt)
    ref = dict(tok=np.concatenate(tok) if tok else np.zeros(0, '<U1'), get=cat(get, float), src=cat(src, int),
               seg=cat(seg, int), s=cat(s, float), p=cat(p, float), aligned=cat(aligned, bool))
    return ref, out_sources, segments


def grams(tok, times, group, n, span):
    """(gram, index of first token) for consecutive tokens within ``span`` seconds and one group."""
    m = len(tok)
    if m < n:
        return []
    ok = (times[n - 1:] - times[:m - n + 1] <= span) & (group[n - 1:] == group[:m - n + 1])
    out = []
    for i in np.flatnonzero(ok):
        out.append((' '.join(tok[i:i + n]), int(i)))
    return out


# ---------------------------------------------------------------- text pairs
def text_pairs(channels, ref, prior, duration, settings=MATCH):
    """Pairs (channel, canonical t, GET) of distinctive 4-grams; ``channels`` = [(ch, origin, Words)]."""
    lo = prior_get(prior, 0.0) - settings['span_margin_s'] - settings['vote_window_s']
    hi = prior_get(prior, duration) + settings['span_margin_s'] + settings['vote_window_s']
    sel = np.flatnonzero((ref['get'] >= lo) & (ref['get'] <= hi))
    index = defaultdict(list)
    if len(sel):
        rt = ref['tok'][sel]
        for gram, i in grams(rt, ref['s'][sel], ref['src'][sel], settings['ngram'], settings['gram_span_s']):
            index[gram].append(int(sel[i]))
    banned = set(settings['banned_grams'])
    rows = defaultdict(list)
    stats = {}
    for channel, origin, words in channels:
        found = grams(words.tok, words.s, np.zeros(len(words), int), settings['ngram'], settings['gram_span_s'])
        counts = Counter(g for g, _ in found)
        kept = 0
        for gram, i in found:
            if counts[gram] > settings['max_channel_repeats'] or gram in banned or len(set(gram.split())) == 1:
                continue
            hits = index.get(gram)
            if not hits or len(hits) > settings['max_reference_count']:
                continue
            t = origin + float(words.s[i])
            d0 = prior_get(prior, t)
            for r in hits:
                d = float(ref['get'][r] - d0)
                if abs(d) > settings['vote_window_s']:
                    continue
                rows['channel'].append(channel)
                rows['t'].append(t)
                rows['local_s'].append(float(words.s[i]))
                rows['g'].append(float(ref['get'][r]))
                rows['d'].append(d)
                rows['ref'].append(r)
                rows['src'].append(int(ref['src'][r]))
                rows['seg'].append(int(ref['seg'][r]))
                rows['p'].append(float(min(words.p[i:i + settings['ngram']].min(), ref['p'][r])))
                rows['aligned'].append(bool(words.aligned[i] and ref['aligned'][r]))
                kept += 1
        stats[channel] = dict(words=len(words), grams=len(found), pairs=kept)
    pairs = {k: np.asarray(v) for k, v in rows.items()}
    if not pairs:
        pairs = {k: np.zeros(0, bool if k == 'aligned' else int if k in ('channel', 'ref', 'src', 'seg') else float)
                 for k in ('channel', 't', 'local_s', 'g', 'd', 'ref', 'src', 'seg', 'p', 'aligned')}
    order = np.argsort(pairs['t'], kind='stable')
    return {k: v[order] for k, v in pairs.items()}, stats


# ---------------------------------------------------------------------- vote
def _best_line(t, d, tc, slopes, settings):
    edges = np.arange(-settings['vote_window_s'] - 50, settings['vote_window_s'] + 50 + settings['bin_s'], settings['bin_s'])
    k = settings['smooth_bins']
    best = None
    for slope in slopes:
        h, _ = np.histogram(d - slope * (t - tc), edges)
        sm = np.convolve(h, np.ones(k), mode='same')
        j = int(np.argmax(sm))
        key = (int(sm[j]), -abs(slope))
        if best is None or key > best[0]:
            best = (key, slope, (edges[j] + edges[j + 1]) / 2)
    _, slope, centre = best
    y = d - slope * (t - tc)
    near = np.abs(y - centre) <= settings['bin_s'] * (k / 2 + 0.5)
    return slope, y, near


def _line_record(t, d, tc, slopes, settings, exclude=None):
    """One block line, or None. ``exclude`` masks pairs already taken by a stronger line."""
    use = np.ones(len(t), bool) if exclude is None else ~exclude
    if use.sum() < settings['min_block_votes']:
        return None
    tu, du = t[use], d[use]
    slope, y, near = _best_line(tu, du, tc, slopes, settings)
    clusters = len(np.unique(np.floor(tu[near] / settings['cluster_s'])))
    if clusters < 3 and slope != 0:
        slope, y, near = _best_line(tu, du, tc, [0.0], settings)
        clusters = len(np.unique(np.floor(tu[near] / settings['cluster_s'])))
    if near.sum() < settings['min_block_votes'] or clusters < settings['min_block_clusters']:
        return None
    taken = np.zeros(len(t), bool)
    taken[np.flatnonzero(use)[near]] = True
    return dict(tc=float(tc), offset=float(np.median(y[near])), slope=float(slope), votes=int(near.sum()),
                clusters=int(clusters), pairs=int(len(t))), taken


def block_line(t, d, tc, settings=MATCH):
    """Best (offset at tc, slope) by 2-s histogram votes over the slope grid, with the runner-up
    line of the remaining pairs as ``alternative`` (a block can hold a replay or a second loop
    that outvotes the true line).

    The votes must come from at least ``min_block_clusters`` separate 30-s stretches of tape: one
    repeated phrase yields many grams at one offset and is not a mode on its own. A slope needs
    three stretches; with two, any slope joins them, so the line is refitted flat.
    """
    if len(t) < settings['min_block_votes']:
        return None
    first = _line_record(t, d, tc, settings['slopes'], settings)
    if first is None:
        return None
    line, taken = first
    second = _line_record(t, d, tc, settings['slopes'], settings, exclude=taken)
    line['alternative'] = second[0] if second else None
    return line


def consistent(a, b, settings=MATCH):
    """Two block lines continue each other: the mean rate between them lies between their own
    rates (a monotone rate change), within ``continuity_rate`` and ``max_mode_departure_s``."""
    dt = b['tc'] - a['tc']
    lo = min(a['slope'], b['slope']) - settings['continuity_rate']
    hi = max(a['slope'], b['slope']) + settings['continuity_rate']
    change = b['offset'] - a['offset']
    slack = settings['max_mode_departure_s']
    return lo * dt - slack <= change <= hi * dt + slack


def strong(m, settings=MATCH):
    return m['votes'] >= settings['alone_votes'] and m['clusters'] >= 3


def vote(pairs, duration, settings=MATCH):
    """Block modes, recorder steps and the kept pairs (within ``keep_s`` of the mode curve).

    1. A mode whose surviving neighbours continue each other around it is a replay or a second
       loop, whatever its votes (repeated until none is removed).
    2. The remaining modes form runs of mutually continuing blocks less than ``run_gap_s`` apart.
       Across a longer gap, two runs whose rates explain the offset change simply continue. At each break between runs a
       forward step (GET later than the earlier run predicts) confirmed on both sides (two blocks
       or one strong block) is a recorder stop and starts a new segment. A backward step cannot be
       a stop, and an unconfirmed one is not evidence: the run with fewer votes is dropped and the
       breaks are examined again.

    The doc's 5-block running median is replaced by this continuity test because the rate can
    change by several per cent within an hour (T510104 hours 15-16, T510105 and T510107 late in
    the reel), which a level-only median rejects.
    """
    t, d = pairs['t'], pairs['d']
    width = settings['block_s']
    modes = []
    for b in range(int(np.ceil(max(duration, 1.0) / width))):
        sel = (t >= b * width) & (t < (b + 1) * width)
        line = block_line(t[sel], d[sel], (b + 0.5) * width, settings)
        if line:
            modes.append(dict(line, block=b, sandwiched=False, accepted=True, segment=None))
    # A block whose best line continues neither neighbour, while its runner-up continues one of
    # them, takes the runner-up (the best line was a second loop).
    for i, m in enumerate(modes):
        alt = m.pop('alternative', None)
        m['switched'] = False
        if alt is None:
            continue
        near = [modes[j] for j in (i - 1, i + 1) if 0 <= j < len(modes)]
        if near and not any(consistent(n, m, settings) if n['tc'] < m['tc'] else consistent(m, n, settings) for n in near)                 and any(consistent(n, alt, settings) if n['tc'] < alt['tc'] else consistent(alt, n, settings) for n in near):
            m.update({k: alt[k] for k in ('offset', 'slope', 'votes', 'clusters')}, switched=True)
    changed = True
    while changed:
        changed = False
        alive = [m for m in modes if m['accepted']]
        for i in range(1, len(alive) - 1):
            before, m, after = alive[i - 1], alive[i], alive[i + 1]
            if consistent(before, after, settings) and not consistent(before, m, settings) and not consistent(m, after, settings):
                m.update(accepted=False, sandwiched=True)
                changed = True
                break
    while True:
        cuts_after = set()
        alive = [m for m in modes if m['accepted']]
        runs = []
        for m in alive:
            if runs and m['tc'] - runs[-1][-1]['tc'] <= settings['run_gap_s'] and consistent(runs[-1][-1], m, settings):
                runs[-1].append(m)
            else:
                runs.append([m])
        dropped = False
        for r1, r2 in zip(runs[:-1], runs[1:]):
            a, b = r1[-1], r2[0]
            if consistent(a, b, settings):
                continue  # a long gap the rate range explains
            dt = b['tc'] - a['tc']
            step = b['offset'] - a['offset'] - max(a['slope'], b['slope']) * dt   # beyond the faster rate
            confirmed = (len(r1) >= 2 or strong(a, settings)) and (len(r2) >= 2 or strong(b, settings))
            if step > 0 and confirmed:
                cuts_after.add(id(a))
                continue
            loser = r1 if sum(m['votes'] for m in r1) < sum(m['votes'] for m in r2) else r2
            for m in loser:
                m['accepted'] = False
            dropped = True
            break
        if not dropped:
            break
    segment = 0
    for m in modes:
        if m['accepted']:
            m['segment'] = segment
            if id(m) in cuts_after:
                segment += 1
    accepted = [m for m in modes if m['accepted']]
    segment = max([m['segment'] for m in accepted], default=0)
    keep = np.zeros(len(t), bool)
    seg_of_pair = np.full(len(t), -1, int)
    cuts = []
    for s in range(segment + 1):
        group = [m for m in accepted if m['segment'] == s]
        if not group:
            continue
        tc = np.array([m['tc'] for m in group])
        off = np.array([m['offset'] for m in group])
        sel = (t >= group[0]['tc'] - width / 2) & (t < group[-1]['tc'] + width / 2) & (seg_of_pair < 0)
        ts = t[sel]
        curve = np.interp(ts, tc, off)
        before, after = ts < tc[0], ts > tc[-1]
        curve[before] = off[0] + group[0]['slope'] * (ts[before] - tc[0])
        curve[after] = off[-1] + group[-1]['slope'] * (ts[after] - tc[-1])
        idx = np.flatnonzero(sel)[np.abs(d[sel] - curve) <= settings['keep_s']]
        keep[idx] = True
        seg_of_pair[idx] = s
    for s in range(1, segment + 1):
        a, b = t[seg_of_pair == s - 1], t[seg_of_pair == s]
        if len(a) and len(b):
            cuts.append(float((a.max() + b.min()) / 2))
    return modes, keep, cuts


# ------------------------------------------------------------ stagger, events
def cluster_groups(rows, settings=MATCH):
    """Stagger groups from well-measured channel intervals (``rows`` of ``relative_stagger``).

    Only intervals with at least ``min_stagger_pairs`` differences and a MAD of at most
    ``group_mad_s`` count. They are split where neighbouring values are more than ``group_gap_s``
    apart, and a cluster is a group when it holds at least ``group_min_share`` of those intervals
    (and at least three). With CTC-aligned times one channel interval scatters by about 0.01 s, so
    single noisy intervals (a mixed channel, a few stray matches) cannot form a group of their
    own. The reference group (0) is always present.
    """
    v = np.sort([r['stagger_s'] for r in rows if r['stagger_s'] is not None
                 and r['pairs'] >= settings['min_stagger_pairs'] and (r['mad_s'] or 0.0) <= settings['group_mad_s']])
    groups = []
    if len(v):
        clusters, current = [], [v[0]]
        for x in v[1:]:
            if x - current[-1] > settings['group_gap_s']:
                clusters.append(current)
                current = [x]
            else:
                current.append(x)
        clusters.append(current)
        need = max(3, settings['group_min_share'] * len(v))
        groups = [float(np.median(c)) for c in clusters if len(c) >= need]
    if not any(abs(g) <= settings['group_tolerance_s'] for g in groups):
        groups.append(0.0)
    return sorted(groups)


def _shared(pairs, keep, interval=None):
    """Per SG word: {channel: [canonical t, ...]} over kept pairs (optionally one interval only)."""
    out = defaultdict(lambda: defaultdict(list))
    for i in np.flatnonzero(keep):
        out[int(pairs['ref'][i])][int(pairs['channel'][i])].append(float(pairs['t'][i]))
    return out


def _diffs(heard, c, known, window):
    """t_c - t_j + stagger_j for every SG word heard on c and on a channel j of known stagger."""
    out = []
    for by_channel in heard:
        if c not in by_channel:
            continue
        for j, st_j in known.items():
            if j == c or j not in by_channel:
                continue
            for tc in by_channel[c]:
                for tj in by_channel[j]:
                    x = tc - tj + st_j
                    if abs(x) <= window:
                        out.append(x)
    return out


def relative_stagger(pairs, keep, reference_channel, settings=MATCH):
    """Stagger of each channel from the same SG words heard on two channels, chained from the
    reference channel: no curve enters, so tape motion cannot leak into stagger.

    Returns the tape-level stagger per channel and the per channel-interval rows. A channel whose
    differences fall more than ``group_tolerance_s`` from their median in more than ``mixed_share``
    of cases carries both copies or changes copy along the tape: it is ``mixed`` at tape level and
    gets stagger per interval only.
    """
    window = settings['stagger_window_s']
    heard_all = list(_shared(pairs, keep).values())
    tape_level = {reference_channel: dict(stagger_s=0.0, pairs=None, mad_s=0.0, mixed=False)}
    channels = sorted({int(c) for c in pairs['channel'][keep]})
    for _ in range(4):
        known = {c: v['stagger_s'] for c, v in tape_level.items() if not v['mixed']}
        added = False
        for c in channels:
            if c in tape_level:
                continue
            x = np.asarray(_diffs(heard_all, c, known, window))
            if len(x) >= settings['min_stagger_pairs']:
                med = float(np.median(x))
                mad = float(np.median(np.abs(x - med)))
                away = float(np.mean(np.abs(x - med) > settings['group_tolerance_s']))
                tape_level[c] = dict(stagger_s=med, pairs=int(len(x)), mad_s=mad, away_share=away,
                                     mixed=away > settings['mixed_share'])
                added = True
        if not added:
            break
    interval = np.floor(pairs['t'] / settings['stagger_interval_s']).astype(int)
    rows = []
    known = {c: v['stagger_s'] for c, v in tape_level.items() if not v['mixed']}
    for k in np.unique(interval[keep]):
        sel = keep & (interval == k)
        heard = list(_shared(pairs, sel).values())
        for c in sorted({int(x) for x in pairs['channel'][sel]}):
            x = np.asarray(_diffs(heard, c, known, window))
            row = dict(channel=c, interval=int(k), start_s=k * settings['stagger_interval_s'],
                       end_s=(k + 1) * settings['stagger_interval_s'], pairs=int(len(x)),
                       stagger_s=float(np.median(x)) if len(x) else None,
                       mad_s=float(np.median(np.abs(x - np.median(x)))) if len(x) else None,
                       tape_level_s=tape_level.get(c, {}).get('stagger_s'))
            if c == reference_channel:
                row.update(stagger_s=0.0, mad_s=0.0, pairs=int((sel & (pairs['channel'] == c)).sum()))
            rows.append(row)
    return tape_level, rows


def curve_stagger(pairs, curve):
    """Per pair, the stagger the curve implies: Phi(t - stagger) = GET."""
    return (curve.get(pairs['t']) - pairs['g']) / curve.rate(pairs['t'])


def assign_stagger(pairs, keep, rows, tape_level, groups, curve=None, settings=MATCH):
    """Stagger used for each pair, in order of preference: its channel-interval measurement, its
    channel's tape-level stagger (not mixed), or the one group its curve residual clearly falls in.
    Pairs that fit no group are kept as evidence but excluded from positions (NaN)."""
    interval = np.floor(pairs['t'] / settings['stagger_interval_s']).astype(int)
    for r in rows:
        near = [g for g in groups if r['stagger_s'] is not None and abs(r['stagger_s'] - g) <= settings['group_tolerance_s']]
        r['group'] = groups.index(near[0]) if len(near) == 1 else None
        r['measured'] = r['pairs'] >= settings['min_stagger_pairs'] and r['group'] is not None
        r['mixed'] = r['pairs'] >= settings['min_stagger_pairs'] and r['group'] is None
    measured = {(r['channel'], r['interval']): r['stagger_s'] for r in rows if r['measured']}
    mixed = {(r['channel'], r['interval']) for r in rows if r['mixed']}
    level = {c: v['stagger_s'] for c, v in tape_level.items() if not v['mixed']}
    implied = curve_stagger(pairs, curve) if curve is not None else np.full(len(pairs['t']), np.nan)
    out = np.full(len(pairs['t']), np.nan)
    basis = np.full(len(pairs['t']), '', dtype=object)
    for i in np.flatnonzero(keep):
        key = (int(pairs['channel'][i]), int(interval[i]))
        if key in measured:
            out[i], basis[i] = measured[key], 'channel_interval'
        elif key not in mixed and key[0] in level:
            out[i], basis[i] = level[key[0]], 'channel'
        elif np.isfinite(implied[i]):
            near = [g for g in groups if abs(implied[i] - g) <= settings['group_tolerance_s']]
            if len(near) == 1:
                out[i], basis[i] = near[0], 'group'
    return out, basis


def tape_events(pairs, keep, stagger, ref_segments, sigma=(0.15, 0.5)):
    """One event per SG word: median reference time over the channels that heard it.

    ``sigma``: position sigma when both word times are CTC-aligned, and otherwise (Whisper times).
    The aligner's pilot onset error is 0.076 s median, 0.23 s p90.
    """
    use = keep & np.isfinite(stagger)
    by_ref = defaultdict(list)
    for i in np.flatnonzero(use):
        by_ref[int(pairs['ref'][i])].append(i)
    events = []
    for r, idx in by_ref.items():
        idx = np.asarray(idx)
        t_ref = pairs['t'][idx] - stagger[idx]
        seg = ref_segments[int(pairs['seg'][idx[0]])]
        events.append(dict(ref=r, t=float(np.median(t_ref)), g=float(pairs['g'][idx[0]]), copies=len(idx),
                           channels=sorted({int(c) for c in pairs['channel'][idx]}),
                           spread_s=float(np.ptp(t_ref)), ref_kind=seg['kind'],
                           bkey=seg['key'] if seg['kind'] == 'ia' else None, source=seg['source'],
                           p=float(np.max(pairs['p'][idx])), aligned=bool(np.all(pairs['aligned'][idx])),
                           sigma_s=sigma[0] if np.all(pairs['aligned'][idx]) else sigma[1]))
    events.sort(key=lambda e: e['t'])
    return events
