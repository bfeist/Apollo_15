"""Channel maps with a missing-track pattern shared by tapes of one kind (stage 10).

Per-tape solving (``channelid_solve``) found every tape numbered from the opposite edge
(delivered CH01 at the true-CH30 side; channel numbering only, the audio plays forward), with an offset that
slips by one track part-way across the head. The slips fall on the same *true* tracks
for tapes of one head stack (e.g. HR1L loses true CH15 whatever the reel's offset), so
here a tape is modelled as:

    delivered d0, d0+1, d0+2, ... read true 30, 29, 28, ... in order, skipping the true
    tracks in M; delivered channels before d0 or past true 1 read no track. True CH01 is
    anchored by decoded IRIG-B time code where it exists (``channelid_irig_nara``).

``M`` (the missing true tracks, at most ``MAX_MISSING``) is shared by every tape of a
group; ``d0`` is per tape (how the reel sat on the playhead). Emissions are the solver's
(GLM answers tempered, plus the annotation anchor and weak-bleed terms). For each group
``M`` is chosen by the summed likelihood of its tapes, each missing track costing
``MISSING_COST`` (a gap must earn that much evidence across the group). Per-channel posteriors marginalize over ``d0`` and over ``M`` with the
group's posterior. Groupings (all tapes, recorder, head stack) are compared by total score.

A tape whose own best ``M`` beats the group's by more than ``DEVIATION_NATS`` is flagged
for review; its own best map is reported alongside, the group map stays primary.

Physical evidence (v2, docs/23): the measured head row of each channel (07a stagger) fixes the
parity of its true track (``channelid_rows.row_logp``), and a channel without a 1 kHz carrier
cannot be true CH01. True CH01 may then be a skipped track like any other.

Read-only on ``glm/`` and ``evidence/``; writes ``<root>/shared-v2/`` (v1 maps stay in ``shared/``).
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from scipy.special import logsumexp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channelid_evidence as ce  # noqa: E402
import channelid_report as cr  # noqa: E402
import channelid_solve as cv  # noqa: E402
import channelid_irig_nara as cin  # noqa: E402
import channelid_rows as crow  # noqa: E402
from channelid_sheets import IRIG_CHANNEL, sheet  # noqa: E402

DEFAULT_ROOT = Path('D:/Apollo_15_30-Track/channel_id')
MAX_MISSING = 2
MISSING_COST = 10.0   # evidence (tempered nats, summed over a group) a missing track must earn. A third gap
                      # gains only 1-5 nats on any head stack and lands at true CH02-03 (the vague top end).
TAPE_MISSING_COST = -math.log(cv.STEP_P)   # 4.6 nats: one tape's own pattern, as a step in channelid_solve
DEVIATION_NATS = 8.0
D0_RANGE = range(-2, 9)                     # delivered channel reading true 30 (<=0: off the edge)
GROUPINGS = {'all': lambda t: 'all', 'recorder': lambda t: t['head'][:3], 'head': lambda t: t['head'],
             'tape': lambda t: t['tape']}   # tape: each reel its own skips (they follow its lateral offset; docs/23)
SHARED_VERSION = 'channelid-shared-v2'   # v2: head-row parity and carrier evidence (docs/23)
CARRIER_ABSENT = math.log(1e-12) / 2.0   # true CH01 on a channel without its 1 kHz carrier
OUT_NAME = 'shared-v2'                   # v1 maps stay in shared/


def missing_sets():
    """Up to MAX_MISSING skipped tracks among true 2-29, each set also with true CH01 skipped (a head
    past the time-code track reads no track: T510073 CH30 carries no 1 kHz carrier, only bleed)."""
    out = [()]
    for k in range(1, MAX_MISSING + 1):
        out += list(itertools.combinations(range(2, 30), k))
    return out + [(1,) + m for m in out]


def mapping(d0, missing, delivered):
    """{d: true or 0} for one (d0, M)."""
    order = [t for t in range(30, 0, -1) if t not in missing]
    out = {}
    for d in delivered:
        i = d - d0
        out[d] = order[i] if 0 <= i < len(order) else 0
    return out


def load(root: Path, tape: str):
    ev = ce.read(root / 'evidence' / f'{tape}.json.gz')
    answers = {}
    for d in ev['channels']:
        rec = json.loads((root / 'glm' / tape / f'CH{int(d):02d}.json').read_text(encoding='utf-8'))
        answers[int(d)] = rec['parsed']
        model = rec['model']
    irig = cin.tape_irig(root, tape)
    lp, notes = cv.emissions(answers, ev, irig)
    # physical evidence (docs/23): measured head row -> parity of the true track; no 1 kHz carrier -> not CH01
    measured, edition = crow.rows(tape)
    for d in lp:
        info = measured.get(d)
        if info and info['intervals'] >= crow.MIN_INTERVALS:
            lp[d] = lp[d] + crow.row_logp(info)
            notes[d] = (notes.get(d, '') + f" row {info['row']} {info['sequence']}"
                        + (' (row changes along the tape)' if info['changes'] else '')).strip()
    carriers = carrier_table(root, tape, irig, lp)
    for d, db in carriers.items():
        if db is None or d not in lp:
            continue
        if db < crow.CARRIER_MIN_DB:
            lp[d][IRIG_CHANNEL] = CARRIER_ABSENT
            notes[d] = (notes.get(d, '') + f' no 1 kHz carrier ({db:+.1f} dB): not true CH01').strip()
        else:
            # a carrier without decodable frames is still the time-code track's: neutral, not absent
            lp[d][IRIG_CHANNEL] = max(lp[d][IRIG_CHANNEL], float(np.median(lp[d])))
            notes[d] = (notes.get(d, '') + f' 1 kHz carrier {db:+.1f} dB, no decodable frames').strip()
    return dict(tape=tape, head=ev['head'], ev=ev, answers=answers, lp=lp, notes=notes, model=model, irig=irig,
                rows=measured, rows_edition=edition, carriers=carriers)


def carrier_table(root, tape, irig, lp):
    """1 kHz carrier strength of the channels that could read true CH01 without decoded time code (cached)."""
    decoded = cv.irig_anchor(irig) if irig else None
    candidates = [d for d in (28, 29, 30) if d in lp and d != decoded]
    path = Path(root) / 'carrier' / f'{tape}.json'
    cached = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    out = {}
    for d in candidates:
        if str(d) not in cached:
            cached[str(d)] = crow.carrier(tape, d)
        out[d] = cached[str(d)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cached, indent=1), encoding='utf-8')
    return out


def tape_table(t, msets):
    """S[m, k] = log-lik of tape under missing set m and d0 = D0_RANGE[k]."""
    ds = sorted(t['lp'])
    S = np.zeros((len(msets), len(D0_RANGE)))
    for i, m in enumerate(msets):
        for k, d0 in enumerate(D0_RANGE):
            mp = mapping(d0, m, ds)
            S[i, k] = sum(t['lp'][d][mp[d]] for d in ds)
    return S


def group_fit(members, msets):
    per = TAPE_MISSING_COST if len(members) == 1 else MISSING_COST
    # true CH01 skipped costs nothing extra: its evidence is the carrier/time-code term itself
    cost = np.array([per * len([x for x in m if x != 1]) for m in msets])
    per_m = sum(logsumexp(t['S'], axis=1) - math.log(len(D0_RANGE)) for t in members)   # marginal over d0
    score = per_m - cost
    post = np.exp(score - logsumexp(score))
    best = int(np.argmax(score))
    order = np.argsort(-score)
    runner = int(order[1])
    return dict(best=best, score=float(score[best]), post=post, runner=runner,
                margin=float(score[best] - score[runner]))


def solve_tape(t, msets, group, sh):
    """Primary map under the group's M, posteriors marginalized over M (group posterior) and d0."""
    ds = sorted(t['lp'])
    S = t['S']
    # joint posterior over (M, d0) for this tape given the group's posterior on M
    logw = np.log(np.maximum(group['post'], 1e-300))[:, None] + S
    logw -= logsumexp(logw)
    w = np.exp(logw)
    mb = group['best']
    kbest = int(np.argmax(S[mb]))
    d0 = D0_RANGE[kbest]
    primary = mapping(d0, msets[mb], ds)
    marg = {d: {} for d in ds}
    for i, m in enumerate(msets):
        if group['post'][i] < 1e-6:
            continue
        for k, dd in enumerate(D0_RANGE):
            if w[i, k] < 1e-9:
                continue
            mp = mapping(dd, m, ds)
            for d in ds:
                marg[d][mp[d]] = marg[d].get(mp[d], 0.0) + float(w[i, k])
    cost = np.array([TAPE_MISSING_COST * len([x for x in m if x != 1]) for m in msets])
    own_scores = S.max(axis=1) - cost
    own = int(np.argmax(own_scores))
    deviation = float(own_scores[own] - (S[mb].max() - cost[mb]))
    d0_post = np.exp(S[mb] - logsumexp(S[mb]))
    channels = {}
    for d in ds:
        tt = primary[d]
        a = t['answers'][d]
        cands = a['candidates']
        top = cands[0]
        p_t = next((x['p'] for x in cands if x['channel'] == tt), 0.0)
        if top['channel'] == tt and top['p'] >= 0.5:
            agreement = 'confirmed'
        elif p_t > 0:
            agreement = 'supported'
        elif top['channel'] == 0:
            agreement = 'no own speech'
        elif top['p'] >= 0.6:
            agreement = 'conflict'
        else:
            agreement = 'unconfirmed'
        post = marg[d].get(tt, 0.0)
        level = 'high' if post >= 0.9 else 'medium' if post >= 0.6 else 'low'
        if agreement == 'conflict' and level == 'high':
            level = 'medium'
        channels[d] = dict(true=tt or None, offset=tt + d if tt else None, posterior=round(post, 4),
                           alternatives=sorted(((x, round(p, 4)) for x, p in marg[d].items() if x != tt and p >= 0.02),
                                               key=lambda z: -z[1]),
                           confidence=level, agreement=agreement, p_glm=round(p_t, 3), glm_top=top['channel'],
                           glm_top_p=round(top['p'], 3), free=None, note=t['notes'].get(d, ''))
    return dict(d0=d0, d0_posterior=round(float(d0_post[kbest]), 4), missing=list(msets[mb]), primary=primary,
                own_missing=list(msets[own]), own_d0=D0_RANGE[int(np.argmax(S[own]))],
                deviation=round(deviation, 2), channels=channels)


def rule_text(d0, missing):
    gap = f', skipping true {", ".join(f"CH{m:02d}" for m in missing)}' if missing else ''
    if d0 >= 1:
        return f'delivered CH{d0:02d} = true CH30; each higher delivered channel is the next lower true channel{gap}'
    return (f'true CH30 not delivered (would be CH{d0:02d}); each higher delivered channel is the next lower '
            f'true channel{gap}')


def as_solution(t, s, grouping, gname, group, msets):
    """Shape a shared solution like ``channelid_solve.solve`` so the report code can render it."""
    posts = sorted(c['posterior'] for c in s['channels'].values())
    median = posts[len(posts) // 2]
    confirmed = sum(c['agreement'] == 'confirmed' for c in s['channels'].values())
    anchor, status = cv.anchor_of(t['ev']['anchors'])
    anchor_ok = None if anchor is None else s['primary'].get(anchor) == 30
    if median >= 0.9 and s['d0_posterior'] >= 0.9 and s['deviation'] <= DEVIATION_NATS and anchor_ok is not False:
        level = 'high'
    elif median >= 0.7 and s['deviation'] <= 2 * DEVIATION_NATS:
        level = 'medium'
    else:
        level = 'low'
    segs, start, cur = [], None, None
    for d in sorted(s['channels']):
        o = s['channels'][d]['offset']
        if o is None:
            continue
        if segs and segs[-1][0] == o:
            segs[-1][2] = d
        else:
            segs.append([o, d, d])
    rigid = cv.rigid_scores(t['lp'])
    return dict(direction='opposite', direction_posterior=1.0, log_evidence={},
                rule=rule_text(s['d0'], s['missing']),
                segments=[dict(offset=o, first=a, last=b) for o, a, b in segs],
                confidence=level, median_channel_posterior=median, confirmed=confirmed,
                anchor_channel=anchor, anchor_status=status, anchor_consistent=anchor_ok, irig=t['irig'],
                rigid=rigid[:8],
                reference=dict(identity=next(h for h in rigid if h['family'] == 'identity'),
                               pure_reverse=next(h for h in rigid if h['family'] == 'reverse' and h['param'] == 31),
                               best_rigid=rigid[0]),
                channels=s['channels'],
                shared=dict(grouping=grouping, group=gname, group_missing=s['missing'],
                            group_margin_nats=round(group['margin'], 2),
                            group_runner_up=list(msets[group['runner']]),
                            group_missing_posterior=round(float(group['post'][group['best']]), 4),
                            d0=s['d0'], d0_posterior=s['d0_posterior'],
                            own_missing=s['own_missing'], own_d0=s['own_d0'], deviation_nats=s['deviation'],
                            review=s['deviation'] > DEVIATION_NATS))


def label_extra(m):
    sh = m['shared']
    gm = ', '.join(f'CH{x:02d}' for x in sh['group_missing']) or 'none'
    ru = ', '.join(f'CH{x:02d}' for x in sh['group_runner_up']) or 'none'
    om = ', '.join(f'CH{x:02d}' for x in sh['own_missing']) or 'none'
    lines = [f'**Shared pattern ({sh["grouping"]} {sh["group"]}):** missing true {gm} '
             f'(group posterior {sh["group_missing_posterior"]:.3f}, margin {sh["group_margin_nats"]:.1f} nats over {ru}); '
             f'this reel: true CH30 on delivered CH{sh["d0"]:02d} (posterior {sh["d0_posterior"]:.3f}).  ']
    if sh['review']:
        lines.append(f'**REVIEW:** this tape on its own prefers missing {om} with CH30 on delivered '
                     f'CH{sh["own_d0"]:02d}, {sh["deviation_nats"]:.1f} nats better than the shared pattern.  ')
    else:
        lines.append(f'Tape on its own: missing {om}, CH30 on CH{sh["own_d0"]:02d} '
                     f'({sh["deviation_nats"]:.1f} nats better than shared; within tolerance).  ')
    return lines


def run(root: Path, grouping: str):
    tapes = sorted(p.name for p in (root / 'glm').glob('A15_T*') if p.is_dir()
                   and (root / 'evidence' / f'{p.name}.json.gz').is_file())
    data = [load(root, t) for t in tapes]
    msets = missing_sets()
    for t in data:
        t['S'] = tape_table(t, msets)

    comparison = {}
    for gname, key in GROUPINGS.items():
        groups = {}
        for t in data:
            groups.setdefault(key(t), []).append(t)
        fits = {g: group_fit(ms, msets) for g, ms in groups.items()}
        comparison[gname] = dict(score=round(sum(f['score'] for f in fits.values()), 2),
                                 groups={g: dict(tapes=len(groups[g]), missing=list(msets[f['best']]),
                                                 margin=round(f['margin'], 2),
                                                 runner_up=list(msets[f['runner']]),
                                                 posterior=round(float(f['post'][f['best']]), 4))
                                         for g, f in sorted(fits.items())})
    # per-tape free (no sharing) for reference
    cost = np.array([MISSING_COST * len([x for x in m if x != 1]) for m in msets])
    own = [float(np.max(logsumexp(t['S'], axis=1) - math.log(len(D0_RANGE)) - cost)) for t in data]
    comparison['per tape (no sharing)'] = dict(score=round(sum(own), 2), groups={})

    key = GROUPINGS[grouping]
    groups = {}
    for t in data:
        groups.setdefault(key(t), []).append(t)
    out = root / OUT_NAME
    (out / 'maps').mkdir(parents=True, exist_ok=True)
    (out / 'labels').mkdir(parents=True, exist_ok=True)
    summary = []
    for gname, members in sorted(groups.items()):
        fit = group_fit(members, msets)
        for t in members:
            s = solve_tape(t, msets, fit, sheet(t['ev']['recorder']))
            sol = as_solution(t, s, grouping, gname, fit, msets)
            m = cr.tape_map(t['tape'], t['ev'], t['answers'], sol, t['model'])
            m['version'] = SHARED_VERSION
            m['shared'] = sol['shared']
            (out / 'maps' / f'{t["tape"]}.json').write_text(json.dumps(m, indent=1, ensure_ascii=False),
                                                            encoding='utf-8')
            md = cr.label_markdown(m).split('\n')
            md[3:3] = label_extra(m)
            (out / 'labels' / f'{t["tape"]}.md').write_text('\n'.join(md), encoding='utf-8')
            summary.append(m)
    write_combined(out, summary, comparison, grouping)
    return comparison, summary


def write_combined(out: Path, maps, comparison, grouping):
    maps = sorted(maps, key=lambda m: m['tape'])
    combined = {m['tape']: dict(head=m['head'], rule=m['rule'], confidence=m['confidence'],
                                gap_tracks=m['shared']['group_missing'], d0=m['shared']['d0'],
                                review=m['shared']['review'],
                                delivered_to_true={d: c['true_channel'] for d, c in m['channels'].items()},
                                true_to_delivered={str(r['true_channel']): r['delivered_channel']
                                                   for r in m['true_channels']},
                                missing_true={str(r['true_channel']): r['reason'] for r in m['true_channels']
                                              if r['status'] == 'missing'},
                                channel_confidence={d: c['confidence'] for d, c in m['channels'].items()},
                                channel_posterior={d: c['posterior'] for d, c in m['channels'].items()})
                for m in maps}
    (out / 'channel_map.json').write_text(json.dumps(dict(version=SHARED_VERSION, grouping=grouping,
                                                          created=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                                          comparison=comparison, tapes=combined), indent=1),
                                          encoding='utf-8')
    tsv = ['tape\thead\tdelivered_channel\tdelivered_label\ttrue_channel\ttrue_label\tconfidence\tposterior\t'
           'agreement\tglm_top\tglm_top_p\ttape_rule\ttape_confidence\treview']
    for m in maps:
        for d, c in sorted(m['channels'].items(), key=lambda kv: int(kv[0])):
            tsv.append('\t'.join(str(x) for x in (
                m['tape'], m['head'], d, c['delivered_label'], c['true_channel'] or '', c['true_label'] or '',
                c['confidence'], c['posterior'], c['agreement'], c['glm_top'], c['glm_top_p'], m['rule'],
                m['confidence'], m['shared']['review'])))
    (out / 'channel_map.tsv').write_text('\n'.join(tsv) + '\n', encoding='utf-8')
    cr.write_true_tsv(out, maps)
    md = ['# Stage 10 channel maps (shared missing-track pattern)', '',
          f'Grouping used: **{grouping}**. Delivered numbers run opposite to the track sheet (numbering only; the audio plays forward): '
          'delivered CH(d0) reads true CH30 and later delivered '
          'channels count down through the track sheet, skipping the group\'s missing true tracks. Per-tape labels '
          'are in `labels/`.', '', '## Grouping comparison (higher is better)', '',
          '| Grouping | Score | Groups: missing true tracks (margin nats) |', '|---|---:|---|']
    for g, v in comparison.items():
        desc = '; '.join(f'{k} ({x["tapes"]}): {", ".join(map(str, x["missing"])) or "none"} ({x["margin"]:.1f})'
                         for k, x in v['groups'].items())
        md.append(f'| {g} | {v["score"]:.1f} | {desc} |')
    md += ['', '## Tapes', '', '| Tape | Head | True CH30 on | Missing true (all reasons) | Confidence | Median p | GLM confirmed | '
           'Anchor | Review |', '|---|---|---|---|---|---:|---:|---|---|']
    for m in maps:
        sh, a = m['shared'], m['anchor']
        anchor = f'CH{a["delivered_channel"]:02d} ' + ('ok' if a['consistent'] else 'NO') if a['delivered_channel'] \
            else a['status']
        md.append(f'| {m["tape"]} | {m["head"]} | CH{sh["d0"]:02d} ({sh["d0_posterior"]:.2f}) | '
                  f'{", ".join(str(r["true_channel"]) for r in m["true_channels"] if r["status"] == "missing")} | '
                  f'{m["confidence"].upper()} | '
                  f'{m["median_channel_posterior"]:.2f} | {m["confirmed"]}/{len(m["channels"])} | {anchor} | '
                  f'{"REVIEW: own " + (", ".join(map(str, sh["own_missing"])) or "none") + " CH%02d" % sh["own_d0"] + " +%.1f" % sh["deviation_nats"] if sh["review"] else ""} |')
    (out / 'CHANNEL_MAP.md').write_text('\n'.join(md) + '\n', encoding='utf-8')


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', default=str(DEFAULT_ROOT))
    p.add_argument('--grouping', choices=sorted(GROUPINGS), default='tape')
    args = p.parse_args(argv)
    comparison, maps = run(Path(args.root), args.grouping)
    from rich.console import Console
    from rich.table import Table
    con = Console()
    tab = Table(title='Grouping comparison (shared missing true tracks; higher score is better)')
    for c in ('grouping', 'score', 'groups'):
        tab.add_column(c)
    for g, v in comparison.items():
        tab.add_row(g, f'{v["score"]:.1f}', '\n'.join(
            f'{k} ({x["tapes"]} tapes): missing {", ".join(map(str, x["missing"])) or "none"}  '
            f'p={x["posterior"]:.2f} margin {x["margin"]:.1f} (next {", ".join(map(str, x["runner_up"])) or "none"})'
            for k, x in v['groups'].items()))
    con.print(tab)
    t2 = Table(title=f'Maps (grouping: {args.grouping})')
    for c in ('tape', 'head', 'CH30 on', 'missing', 'conf', 'median p', 'GLM ok', 'anchor', 'review'):
        t2.add_column(c)
    for m in sorted(maps, key=lambda m: (m['head'], m['tape'])):
        sh, a = m['shared'], m['anchor']
        t2.add_row(m['tape'][-6:], m['head'], f'CH{sh["d0"]:02d} {sh["d0_posterior"]:.2f}',
                   ','.join(map(str, sh['group_missing'])) or '-', m['confidence'],
                   f'{m["median_channel_posterior"]:.2f}', f'{m["confirmed"]}/{len(m["channels"])}',
                   (f'CH{a["delivered_channel"]:02d} ' + ('ok' if a['consistent'] else 'NO'))
                   if a['delivered_channel'] else a['status'][:9],
                   (f'own {",".join(map(str, sh["own_missing"])) or "-"} CH{sh["own_d0"]:02d} +{sh["deviation_nats"]:.0f}'
                    if sh['review'] else ''))
    con.print(t2)
    print(f'Wrote {Path(args.root) / OUT_NAME}')


if __name__ == '__main__':
    main()
