"""Stage-10 channel maps and the missing-channel inventory for the README (section 9).

Read-only on the archive. Recomputes the shared (per head stack) channel maps in memory from
the saved stage-10 evidence, GLM answers and IRIG-B decodes, with the unchanged
``channelid_shared`` code (no GLM calls, nothing written under ``channel_id/``), and writes
``channel_maps.json`` beside this script. ``10_channelid.py shared`` writes the same maps to
the archive.

    uv run python research/readme_figures/channel_maps.py [--table A15_T510085]
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO/'scripts/30-track/10_channelid'))
import channelid_report as cr  # noqa: E402
import channelid_shared as cs  # noqa: E402
from channelid_sheets import sheet  # noqa: E402

ROOT = Path('D:/Apollo_15_30-Track/channel_id')
HERE = Path(__file__).resolve().parent
INV = json.loads((HERE/'inventory.json').read_text(encoding='utf-8'))


def compute():
    tapes = sorted(p.name for p in (ROOT/'glm').glob('A15_T*') if p.is_dir()
                   and (ROOT/'evidence'/f'{p.name}.json.gz').is_file())
    data = [cs.load(ROOT, t) for t in tapes]
    msets = cs.missing_sets()
    for t in data:
        t['S'] = cs.tape_table(t, msets)
    out = dict(groups={}, tapes={}, comparison={})
    for gname, key in cs.GROUPINGS.items():
        gs = {}
        for t in data:
            gs.setdefault(key(t['head']), []).append(t)
        out['comparison'][gname] = round(sum(cs.group_fit(m, msets)['score'] for m in gs.values()), 1)
    cost = cs.np.array([cs.MISSING_COST * len(m) for m in msets])
    out['comparison']['per tape'] = round(sum(float(cs.np.max(cs.logsumexp(t['S'], axis=1)
                                                          - cs.math.log(len(cs.D0_RANGE)) - cost)) for t in data), 1)
    groups = {}
    for t in data:
        groups.setdefault(t['head'], []).append(t)
    for g, members in sorted(groups.items()):
        fit = cs.group_fit(members, msets)
        out['groups'][g] = dict(tapes=len(members), missing=list(msets[fit['best']]),
                                posterior=round(float(fit['post'][fit['best']]), 3), margin=round(fit['margin'], 2),
                                runner_up=list(msets[fit['runner']]))
        for t in members:
            s = cs.solve_tape(t, msets, fit, sheet(t['ev']['recorder']))
            sol = cs.as_solution(t, s, 'head', g, fit, msets)
            m = cr.tape_map(t['tape'], t['ev'], t['answers'], sol, t['model'])
            irig = cs.cin.tape_irig(ROOT, t['tape']) or {}
            hits = {int(c): v for c, v in irig.get('channels', {}).items() if v['strict'] >= 1000}
            inv = next(r for r in INV['tapes'] if r['tape'] == t['tape'])
            out['tapes'][t['tape']] = dict(
                head=g, hours=inv['hours'], d0=s['d0'], group_missing=s['missing'], own_missing=s['own_missing'],
                deviation_nats=s['deviation'], review=sol['shared']['review'], confidence=m['confidence'],
                median_posterior=m['median_channel_posterior'], confirmed=m['confirmed'],
                delivered=len(m['channels']), anchor=m['anchor']['delivered_channel'],
                anchor_consistent=m['anchor']['consistent'],
                irig_channel=max(hits, key=lambda c: hits[c]['strict']) if hits else None,
                irig=hits, irig_searched=irig.get('searched', []),
                missing_true=[r['true_channel'] for r in m['true_channels'] if r['status'] == 'missing'],
                no_track=m['delivered_channels_without_true_track'],
                channels={d: dict(label=c['delivered_label'], true=c['true_channel'], true_label=c['true_label'],
                                  true_sheet=c['true_sheet'], position=c['true_position'],
                                  confidence=c['confidence'], posterior=c['posterior'], agreement=c['agreement'],
                                  glm_top=c['glm_top'], note=c['note'])
                          for d, c in m['channels'].items()},
                true_channels=m['true_channels'])
    return out


def table(out, tape):
    m = out['tapes'][tape]
    rows = ['| Delivered | File label | Reads true | Track-sheet position | Confidence | GLM |',
            '|---:|---|---:|---|---|---|']
    for d, c in sorted(m['channels'].items(), key=lambda kv: int(kv[0])):
        true = f'CH{c["true"]:02d}' if c['true'] else '—'
        pos = f'{c["true_sheet"]} ({c["position"]})' if c['true'] and c['position'] else (c['true_sheet'] or '(no track)')
        rows.append(f'| CH{int(d):02d} | {c["label"]} | {true} | {pos} | {c["confidence"]} ({c["posterior"]:.2f}) | '
                    f'{c["agreement"]} |')
    return '\n'.join(rows)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--table', default='A15_T510085')
    args = p.parse_args()
    out = compute()
    (HERE/'channel_maps.json').write_text(json.dumps(out, indent=1), encoding='utf-8')
    print(out['comparison'])
    for g, v in out['groups'].items():
        print(g, v)
    for t, m in out['tapes'].items():
        print(t, m['head'], 'd0', m['d0'], 'missing', m['missing_true'], 'no track', m['no_track'],
              'IRIG', m['irig_channel'], m['confidence'], 'review' if m['review'] else '', 'dev', m['deviation_nats'])
    print(table(out, args.table))
