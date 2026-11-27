"""Retiming evidence for the README (section 10.6, Figures 14 and 15). Read-only on the archive.

Reads three published 07a fit editions and writes ``retime_evidence.json`` beside this script:

* ``FIRST``: the first full v3 fit (no level gaps, no journal anchors);
* ``LEVELS``: the fit with level gaps (sg_levels.py);
* ``JOURNAL``: the fit with level gaps and journal anchors (sg_journal.py), the one 07b applies.

Per tape (JOURNAL): which tape time each kind of evidence covers (time code, NARA, IA, journal),
in 5-minute bins, and the share of tape time within 600 s of any anchor in LEVELS and JOURNAL.
Journal residuals on tapes with and without NARA/time code. The GET change JOURNAL - LEVELS along
three tapes whose old curves extrapolated, with the journal events measured against the old plan.
The IA segment offsets of all three fits, at each segment's GET.

    uv run python research/readme_figures/retime_evidence.py
"""
import csv
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FITS = Path('D:/Apollo_15_30-Track/stage_evidence/retiming/sg_fit')
REFERENCE = Path('D:/Apollo_15_30-Track/stage_evidence/retiming/sg_reference')
FIRST, LEVELS, JOURNAL = 'fit-fee097094a89eb4c', 'fit-0539f7b78e46931e', 'fit-4c59ce211a77c83c'
BIN_S = 300.0
CHANGE_TAPES = ('A15_T510105', 'A15_T510110', 'A15_T510090')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def kind_of(anchor):
    if anchor['kind'] == 'irig':
        return 'timecode'
    return {'nara': 'nara', 'ia': 'ia', 'journal': 'journal'}.get(anchor['ref_kind'])


def get_map(fit, tape):
    d = FITS / fit / 'tapes' / tape
    plan = read_json(d / 'plan.json')
    with np.load(d / 'timing_map.npz') as z:
        tt, oo = z['input_seconds'].copy(), z['output_seconds'].copy()
    segs = plan['get_placement']['segments']
    starts = [s['tape_start_s'] for s in segs]

    def get(t):
        t = np.atleast_1d(np.asarray(t, float))
        k = np.clip(np.searchsorted(starts, t, side='right') - 1, 0, len(segs) - 1)
        return np.interp(t, tt, oo) + np.array([segs[i]['get_equals_output_plus_s'] for i in k])
    return get, plan['input_duration_seconds']


def coverage(fit):
    with (FITS / fit / 'coverage.tsv').open(encoding='utf-8') as f:
        return {r['tape']: float(r['fraction_600']) for r in csv.DictReader(f, delimiter='\t')}


def offsets(fit):
    with (FITS / fit / 'ia_offsets.tsv').open(encoding='utf-8') as f:
        return {r['key']: (float(r['offset_s']), float(r['se_s']), int(float(r['events']))) for r in csv.DictReader(f, delimiter='\t')}


def main():
    manifest = read_json(FITS / JOURNAL / 'manifest.json')
    ref = read_json(REFERENCE / manifest['reference'] / 'manifest.json')
    seg_get = {s['key']: s['get_start_s'] for s in ref['segments']}
    cov_old, cov_new = coverage(LEVELS), coverage(JOURNAL)
    tapes, residuals = {}, dict(timed=[], ia_only=[], journal_only=[])
    for d in sorted((FITS / JOURNAL / 'tapes').iterdir()):
        model = read_json(d / 'curve.json')
        duration = model['input_duration_seconds']
        nbin = int(np.ceil(duration / BIN_S))
        lanes = {k: np.zeros(nbin, bool) for k in ('timecode', 'nara', 'ia', 'journal')}
        for a in model['anchors']:
            k = kind_of(a)
            if k:
                lanes[k][min(nbin - 1, int(a['input_seconds'] // BIN_S))] = True
        ramps = [[r['gap_start_s'], r['gap_end_s'], r['level_step_s']] for r in model['curve'].get('level_ramps', [])]
        tapes[d.name] = dict(duration_s=duration, bins={k: np.flatnonzero(v).tolist() for k, v in lanes.items()},
                             fraction_600_levels=cov_old.get(d.name), fraction_600_journal=cov_new.get(d.name),
                             level_ramps=ramps, anchors=model['curve'].get('first_anchor_s'))
        timed = any(lanes['timecode']) or any(lanes['nara'])
        ia = any(lanes['ia'])
        group = 'timed' if timed else ('ia_only' if ia else 'journal_only')
        residuals[group] += [round(a['residual_s'], 3) for a in model['anchors'] if a['ref_kind'] == 'journal']
    b = offsets(JOURNAL)['journal:TEC'][0]
    change = {}
    for tape in CHANGE_TAPES:
        new, duration = get_map(JOURNAL, tape)
        old, _ = get_map(LEVELS, tape)
        t = np.linspace(0, duration, 800)
        events = [a for a in read_json(FITS / JOURNAL / 'tapes' / tape / 'curve.json')['anchors'] if a['ref_kind'] == 'journal']
        te = np.array([a['input_seconds'] for a in events])
        ge = np.array([a['get_s'] for a in events]) - b      # printed GET corrected by the journal offset
        old_first = read_json(FITS / LEVELS / 'tapes' / tape / 'curve.json')['curve']
        change[tape] = dict(t_s=t.round(1).tolist(), new_minus_old_s=(new(t) - old(t)).round(3).tolist(),
                            events_t_s=te.round(1).tolist(), events_minus_old_s=(ge - old(te)).round(3).tolist(),
                            old_anchor_span_s=[old_first['first_anchor_s'], old_first['last_anchor_s']])
    fits = {name: offsets(fit) for name, fit in (('first', FIRST), ('levels', LEVELS), ('journal', JOURNAL))}
    ia = []
    for key in sorted(k for k in fits['journal'] if k.startswith('ia:')):
        row = dict(key=key, get_s=seg_get.get(key), events=fits['journal'][key][2])
        for name in fits:
            row[name] = fits[name].get(key, (None,))[0]
        ia.append(row)
    out = dict(fits=dict(first=FIRST, levels=LEVELS, journal=JOURNAL), bin_s=BIN_S, journal_offset_s=b,
               journal_offset_se_s=offsets(JOURNAL)['journal:TEC'][1], tapes=tapes, journal_residuals=residuals,
               change=change, ia_offsets=ia)
    (HERE / 'retime_evidence.json').write_text(json.dumps(out) + '\n', encoding='utf-8')
    n = {k: len(v) for k, v in residuals.items()}
    print(f'wrote {HERE / "retime_evidence.json"}: {len(tapes)} tapes, journal residuals {n}, offset {b:+.3f} s')


if __name__ == '__main__':
    main()
