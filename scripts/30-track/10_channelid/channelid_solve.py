"""Tape-level channel map from per-channel GLM assessments (stage 10).

Model. Every delivered channel ``d`` reads true track ``t = a_d - d`` (delivered numbers run
opposite to the track sheet: counted from the other tape edge) or ``t = d + k_d`` (same
direction). This is channel *numbering* only; no audio is time-reversed. The offset (``a_d`` or ``k_d``) is a hidden state that is
normally the same for neighbouring delivered channels but may step by one track,
which allows a playback head whose registration drifts across the tape width. A step that
skips a true track (playback pitch wider than recorded) has probability ``STEP_P`` per
neighbour; the opposite step, which would read one true track twice, ``BACK_STEP_P``. A true track outside 1..30 is "no track" (channel 0).

Time code: true CH01 carries IRIG-B. Where it was decoded on a delivered channel, that
channel gets ``IRIG_BONUS`` for true CH01; on every other channel of a searched tape,
true CH01 has log-probability ``IRIG_ABSENT``, so it can only fall past the delivered range.

Emissions are GLM's per-channel probabilities ``p(t | d)`` tempered by ``TEMPER`` (they
are over-confident and correlated within a tape), with two deterministic terms:

* anchor: the delivered channel carrying most voice-annotation readouts gets a bonus for
  true 30;
* weak bleed: a channel with under ``WEAK_RATIO`` of the words of a delivered neighbour
  is mixed half-and-half with "no track", because its content is mostly bleed.

Forward-backward gives, per delivered channel, the posterior of each true channel; the
MAP path (Viterbi) is the map. Each direction's total evidence gives the direction
posterior. Rigid maps (``identity``, ``shift``, ``reverse``) are scored for reference,
and an unconstrained one-to-one assignment (Hungarian) is reported alongside.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.special import logsumexp

from channelid_sheets import IRIG_CHANNEL

FLOOR = 0.004
TEMPER = 2.0
STEP_P = 0.01          # offset step that skips a true track (playback pitch wider than recorded)
BACK_STEP_P = 0.0005   # offset step that reads a true track twice (pitch narrower); physically unexpected
ANCHOR_BONUS = 3.0
ANCHOR_MIN_SHARE = 0.5
ANCHOR_MIN_HITS = 10
ANCHOR_RIVAL = 0.5      # an adjacent channel with this fraction of the hits makes the anchor ambiguous
WEAK_RATIO = 0.1
IRIG_BONUS = 8.0                       # strict IRIG-B frames decoded on this delivered channel
IRIG_ABSENT = math.log(1e-6) / 2.0     # true CH01 on a channel where a full decode found no time code
IRIG_MIN_STRICT = 1000
REVERSE_OFFSETS = range(26, 41)
FORWARD_OFFSETS = range(-10, 11)


def channel_probs(candidates):
    """p(t | d) for t = 0..30 from GLM candidates; unnamed mass spread evenly."""
    named = {c['channel']: c['p'] for c in candidates}
    rest = max(0.03, 1.0 - sum(named.values()))
    share = rest / max(1, 31 - len(named))
    p = np.array([max(FLOOR, named.get(t, 0.0) + share) for t in range(31)])
    return p / p.sum()


def channel_logp_array(candidates):
    """Tempered log p(t | d), t = 0..30, without the anchor and bleed terms."""
    return np.log(channel_probs(candidates)) / TEMPER


def irig_anchor(irig):
    """Delivered channel with decoded time code, or None."""
    if not irig:
        return None
    best = max(irig.get('channels', {}).items(), key=lambda kv: kv[1]['strict'], default=None)
    return int(best[0]) if best and best[1]['strict'] >= IRIG_MIN_STRICT else None


def emissions(assessments, evidence=None, irig=None):
    """{d: log p'(t | d)} including anchor, weak-bleed and time-code terms."""
    ds = sorted(assessments)
    probs = {d: channel_probs(assessments[d]['candidates']) for d in ds}
    notes = {}
    if evidence:
        words = {int(k): v['words'] for k, v in evidence['channels'].items()}
        for d in ds:
            nb = max(words.get(d - 1, 0), words.get(d + 1, 0))
            if nb and words.get(d, 0) < WEAK_RATIO * nb:
                mix = np.zeros(31)
                mix[0] = 1.0
                probs[d] = 0.5 * probs[d] + 0.5 * mix
                notes[d] = 'weak (bleed from neighbour)'
    lp = {d: np.log(probs[d]) / TEMPER for d in ds}
    if evidence:
        d, _ = anchor_of(evidence['anchors'])
        if d is not None:
            if d in lp:
                lp[d][30] += ANCHOR_BONUS
                notes[d] = (notes.get(d, '') + ' annotation anchor').strip()
    if irig and irig.get('channels'):
        d_irig = irig_anchor(irig)
        for d in lp:
            if d == d_irig:
                lp[d][IRIG_CHANNEL] += IRIG_BONUS
                notes[d] = (notes.get(d, '') + ' IRIG time code').strip()
            else:
                lp[d][IRIG_CHANNEL] = min(lp[d][IRIG_CHANNEL], IRIG_ABSENT)
    return lp, notes


def anchor_of(anchors):
    """(channel or None, status) of the voice-annotation readout anchor."""
    ranking = [(int(c), k) for c, k in (anchors or {}).get('ranking') or []]
    if not ranking or ranking[0][1] < ANCHOR_MIN_HITS:
        return None, 'none'
    d, hits = ranking[0]
    rivals = [c for c, k in ranking[1:] if abs(c - d) == 1 and k >= ANCHOR_RIVAL * hits]
    if rivals:
        return None, f'ambiguous between CH{min(d, rivals[0]):02d} and CH{max(d, rivals[0]):02d}'
    if anchors.get('share', 0) < ANCHOR_MIN_SHARE:
        return None, 'diffuse'
    return d, 'clear'


def true_of(direction, offset, d):
    t = offset - d if direction < 0 else d + offset
    return t if 1 <= t <= 30 else 0


def hmm(lp, direction):
    ds = sorted(lp)
    offsets = list(REVERSE_OFFSETS if direction < 0 else FORWARD_OFFSETS)
    n, s = len(ds), len(offsets)
    e = np.array([[lp[d][true_of(direction, o, d)] for o in offsets] for d in ds])
    trans = np.full((s, s), -np.inf)
    skip = -1 if direction < 0 else 1          # offset change that skips a true track
    for i in range(s):
        trans[i, i] = math.log(1 - STEP_P - BACK_STEP_P)
        for j, p in ((i + skip, STEP_P), (i - skip, BACK_STEP_P)):
            if 0 <= j < s:
                trans[i, j] = math.log(p)
    init = np.full(s, -math.log(s))
    fwd = np.zeros((n, s))
    fwd[0] = init + e[0]
    for i in range(1, n):
        fwd[i] = logsumexp(fwd[i - 1][:, None] + trans, axis=0) + e[i]
    bwd = np.zeros((n, s))
    for i in range(n - 2, -1, -1):
        bwd[i] = logsumexp(trans + (e[i + 1] + bwd[i + 1])[None, :], axis=1)
    total = float(logsumexp(fwd[-1]))
    post = np.exp(fwd + bwd - total)
    # Viterbi
    v = init + e[0]
    back = np.zeros((n, s), dtype=int)
    for i in range(1, n):
        cand = v[:, None] + trans
        back[i] = cand.argmax(axis=0)
        v = cand.max(axis=0) + e[i]
    path = [int(v.argmax())]
    for i in range(n - 1, 0, -1):
        path.append(int(back[i][path[-1]]))
    path.reverse()
    out = {}
    for i, d in enumerate(ds):
        dist = {}
        for j, o in enumerate(offsets):
            t = true_of(direction, o, d)
            dist[t] = dist.get(t, 0.0) + float(post[i, j])
        o = offsets[path[i]]
        t = true_of(direction, o, d)
        out[d] = dict(offset=o, true=t, posterior=round(dist.get(t, 0.0), 4),
                      alternatives=sorted(((tt, round(p, 4)) for tt, p in dist.items() if tt != t and p >= 0.02),
                                          key=lambda x: -x[1]))
    return dict(direction=direction, log_evidence=round(total, 3), channels=out)


def rule_text(direction, segments):
    if direction < 0:
        parts = [f'true = {o} - delivered' for o, _, _ in segments]
    else:
        parts = ['true = delivered' if o == 0 else f'true = delivered {"+" if o > 0 else "-"} {abs(o)}'
                 for o, _, _ in segments]
    if len(segments) == 1:
        return parts[0]
    return '; '.join(f'{p} (CH{a:02d}-CH{b:02d})' for p, (_, a, b) in zip(parts, segments))


def segments_of(channels):
    segs = []
    for d in sorted(channels):
        o = channels[d]['offset']
        if segs and segs[-1][0] == o:
            segs[-1][2] = d
        else:
            segs.append([o, d, d])
    return [tuple(s) for s in segs]


def rigid_scores(lp):
    ds = sorted(lp)
    out = [('identity', 0, -0)]
    out += [('shift', k, 1) for k in FORWARD_OFFSETS if k]
    out += [('reverse', a, -1) for a in REVERSE_OFFSETS]
    scored = []
    for fam, p, direction in out:
        direction = direction or 1
        sc = sum(lp[d][true_of(direction, p, d)] for d in ds)
        scored.append(dict(family=fam, param=p, score=round(float(sc), 3),
                           rule=rule_text(direction, [(p, ds[0], ds[-1])])))
    return sorted(scored, key=lambda h: -h['score'])


def free(lp):
    ds = sorted(lp)
    cost = np.zeros((len(ds), 30 + len(ds)))
    for i, d in enumerate(ds):
        cost[i, :30] = -lp[d][1:31]
        cost[i, 30:] = -lp[d][0]
    rows, cols = linear_sum_assignment(cost)
    return {ds[r]: (int(c) + 1 if c < 30 else 0) for r, c in zip(rows, cols)}


def solve(assessments, evidence=None, irig=None):
    lp, notes = emissions(assessments, evidence, irig)
    if not lp:
        return None
    rev, fwd = hmm(lp, -1), hmm(lp, 1)
    best, other = (rev, fwd) if rev['log_evidence'] >= fwd['log_evidence'] else (fwd, rev)
    dir_post = 1.0 / (1.0 + math.exp(min(700, other['log_evidence'] - best['log_evidence'])))
    segs = segments_of(best['channels'])
    rigid = rigid_scores(lp)
    ref = free(lp)
    anchor, anchor_status = anchor_of((evidence or {}).get('anchors'))
    anchor_ok = None if anchor is None else best['channels'].get(anchor, {}).get('true') == 30

    channels = {}
    for d, c in best['channels'].items():
        a = assessments[d]
        cands = a['candidates']
        top = cands[0]
        t = c['true']
        p_t = next((x['p'] for x in cands if x['channel'] == t), 0.0)
        if top['channel'] == t and top['p'] >= 0.5:
            agreement = 'confirmed'
        elif p_t > 0:
            agreement = 'supported'
        elif top['channel'] == 0 or t == 0:
            agreement = 'no own speech' if top['channel'] == 0 else 'unconfirmed'
        elif top['p'] >= 0.6:
            agreement = 'conflict'
        else:
            agreement = 'unconfirmed'
        post = c['posterior']
        level = 'high' if post >= 0.9 else 'medium' if post >= 0.6 else 'low'
        if agreement == 'conflict' and level == 'high':
            level = 'medium'
        channels[d] = dict(true=t or None, offset=c['offset'], posterior=post, alternatives=c['alternatives'],
                           confidence=level, agreement=agreement, p_glm=round(p_t, 3), glm_top=top['channel'],
                           glm_top_p=round(top['p'], 3), free=ref[d], note=notes.get(d, ''))
    posts = sorted(c['posterior'] for c in channels.values())
    median = posts[len(posts) // 2]
    confirmed = sum(c['agreement'] == 'confirmed' for c in channels.values())
    if dir_post >= 0.999 and median >= 0.9 and anchor_ok is not False:
        level = 'high'
    elif dir_post >= 0.99 and median >= 0.7:
        level = 'medium'
    else:
        level = 'low'
    return dict(direction='opposite' if best['direction'] < 0 else 'same', direction_posterior=round(dir_post, 6),
                log_evidence=dict(opposite=rev['log_evidence'], same=fwd['log_evidence']),
                rule=rule_text(best['direction'], segs), segments=[dict(offset=o, first=a, last=b) for o, a, b in segs],
                confidence=level, median_channel_posterior=median, confirmed=confirmed,
                anchor_channel=anchor, anchor_status=anchor_status, anchor_consistent=anchor_ok, irig=irig, rigid=rigid[:8],
                reference=dict(identity=next(h for h in rigid if h['family'] == 'identity'),
                               pure_reverse=next(h for h in rigid if h['family'] == 'reverse' and h['param'] == 31),
                               best_rigid=rigid[0]),
                channels=channels)
