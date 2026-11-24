"""Step 9: the table of contents, mission stages and crew status (flight-plan guide §6).

A TOC time is an actual time: its basis must be the Mission Report, the AFJ
index to events, an utterance event, ``measured`` or a recorded human choice,
never ``planned`` or ``slip_interpolated``. Items without an actual time are
held back and listed. Levels are 1 and 2 only (AiRT2).

Outputs:

* ``MISSION_DATA/A15_TOC.csv`` (legacy ``GET|level|title``) and ``A15_TOC.tsv`` (provenance)
* ``MISSION_DATA/indexes/TOCData.csv`` (AiRT2 ``timeId|level|label``)
* ``MISSION_DATA/indexes/missionStagesData.csv`` and ``crewStatusData.csv`` drafts
* ``MISSION_DATA/review/toc.tsv``: one row per TOC row with evidence and alternatives; the
  decision columns (``decision``, ``new_label``, ``new_level``, ``reviewer``, ``date``, ``note``)
  are kept across rebuilds and reapplied
"""
from __future__ import annotations

import re

from md_common import INDEXES, MISSION_DATA, REVIEW, WORK, format_get, read_tsv, write_pipe, write_tsv
from md_flightplan import FLIGHT_PLAN

ALLOWED_BASIS = ('mission_report', 'afj_index', 'utterance', 'measured', 'human')
TOC_FIELDS = ('toc_id', 'get', 'level', 'label', 'description', 'source', 'get_basis', 'evidence')
REVIEW_FIELDS = ('toc_id', 'get', 'level', 'label', 'get_basis', 'evidence', 'alternatives', 'decision',
                 'new_label', 'new_level', 'reviewer', 'date', 'note')
DECISIONS = ('decision', 'new_label', 'new_level', 'reviewer', 'date', 'note')
# Level-1 backbone (§6) that should exist; anything missing is reported as held back.
BACKBONE = ('countdown', 'launch', 'staging_sic', 'eoi', 'tli', 'csm_sivb_sep', 'td_dock', 'lm_ejection',
            'sps_test', 'mcc4', 'sim_door', 'loi', 'doi', 'doi_trim', 'lm_activation', 'undock', 'circ', 'pdi',
            'landing', 'seva', 'eva1_prep', 'eva1', 'station1', 'station2', 'eva1_end', 'eva2_prep', 'eva2',
            'station6', 'station6a', 'station7', 'station4', 'station8', 'eva2_end', 'eva3_prep', 'eva3', 'station9',
            'station9a', 'station10', 'eva3_end', 'lunar_liftoff', 'tpi', 'docking', 'lm_jett', 'plane_change',
            'subsat', 'tei', 'csm_eva', 'press', 'eclipse', 'mcc7', 'cmsm_sep', 'ei', 'splashdown', 'recovery',
            'sleep', 'wake')
AUTHORITY = {'human': 0, 'mission_report': 1, 'afj_index': 2, 'utterance': 3, 'measured': 4}
STAGES = (('countdown', 'Countdown', 'Counting down to launch'),
          ('launch', 'Launch', 'Launching into orbit around the Earth'),
          ('eoi', 'Earth Orbit', 'Orbiting the Earth, preparing to go to the Moon'),
          ('tli', 'Translunar Coast', 'Travelling to the Moon'),
          ('loi', 'Lunar Orbit', 'In lunar orbit'),
          ('landing', 'Lunar Surface', 'On the surface at Hadley-Apennine'),
          ('lunar_liftoff', 'Lunar Orbit', 'Rendezvous, docking and orbital science'),
          ('tei', 'Transearth Coast', 'Returning to Earth'),
          ('cmsm_sep', 'Entry', 'Entry and splashdown'))


def time_id(seconds: int) -> str:
    """AiRT2 timeId: ``HHHMMSS`` for positive GET, ``-HHMMSS`` before launch."""
    sign = '-' if seconds < 0 else ''
    h, rem = divmod(abs(int(seconds)), 3600)
    return f'{sign}{h:02d}{rem // 60:02d}{rem % 60:02d}' if sign else f'{h:03d}{rem // 60:02d}{rem % 60:02d}'


def plain(label: str) -> str:
    """Plain text: no markup, no pipes, single spaces."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', label or '')).replace('|', '/').strip()


def candidates() -> list[dict]:
    rows = []
    for a in read_tsv(FLIGHT_PLAN / 'fp_anchors.tsv'):
        rows.append({'toc_id': f'toc:{a["anchor_id"]}', 'key': a['key'], 'seconds': int(a['get_seconds']),
                     'level': int(a['level']), 'label': plain(a['toc_label']),
                     'description': plain(a['label']) if plain(a['label']) != plain(a['toc_label']) else '',
                     'source': a['anchor_id'], 'get_basis': a['basis'], 'evidence': a['evidence']})
    # Planned activities that were confirmed as flown, with an actual basis, become level-2 rows.
    for r in read_tsv(FLIGHT_PLAN / 'fp_actual.tsv'):
        if r['status'] in ('done', 'done_shifted', 'partial') and r['start_basis'] in ALLOWED_BASIS and \
                r.get('review_state') in ('human', 'auto_rule'):
            from md_common import parse_get
            seconds = parse_get(r['actual_start'])
            if seconds is not None:
                rows.append({'toc_id': f'toc:{r["activity_id"]}', 'key': 'activity', 'seconds': seconds, 'level': 2,
                             'label': plain(r['title']).capitalize(), 'description': '', 'source': r['activity_id'],
                             'get_basis': r['start_basis'], 'evidence': r['evidence']})
    return rows


def build(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Apply release rules. Returns (kept rows, dropped rows with reasons, alternatives map entries)."""
    kept, dropped = [], []
    rows = sorted(rows, key=lambda r: (r['seconds'], r['level'], AUTHORITY.get(r['get_basis'], 9), r['toc_id']))
    wakes = [r['seconds'] for r in rows if r['key'] == 'wake']
    for r in rows:
        if r['get_basis'] not in ALLOWED_BASIS:
            dropped.append({**r, 'reason': f'basis {r["get_basis"]} is not an actual time'})
            continue
        if r['key'] == 'afj_index' and re.match(r'first communication', r['label'], re.I) and any(
                abs(r['seconds'] - w) <= 120 for w in wakes):
            dropped.append({**r, 'reason': 'same moment as a wake-up row'})
            continue
        clash = next((k for k in kept if k['level'] == r['level'] and abs(k['seconds'] - r['seconds']) <= 30), None)
        if clash:
            clash.setdefault('alternatives', []).append(f'{r["toc_id"]}@{format_get(r["seconds"])}')
            dropped.append({**r, 'reason': f'within 30 s of {clash["toc_id"]}'})
            continue
        kept.append(dict(r))
    return kept, dropped


def apply_decisions(rows: list[dict], decisions: dict) -> list[dict]:
    out = []
    for r in rows:
        d = decisions.get(r['toc_id'], {})
        choice = (d.get('decision') or '').lower()
        if choice == 'drop':
            continue
        if d.get('new_label'):
            r['label'] = plain(d['new_label'])
        if d.get('new_level') in ('1', '2'):
            r['level'] = int(d['new_level'])
        out.append(r)
    return out


def crew_status(times: dict) -> list[tuple[str, str]]:
    def at(key):
        return times.get(key)
    rows = [(at('countdown'), 'Preparing for launch'), (at('launch'), 'In Command Module'),
            (at('td_dock'), 'In Command Module, docked with the Lunar Module'),
            (at('undock'), 'Scott and Irwin in the Lunar Module; Worden in the Command Module'),
            (at('landing'), 'Scott and Irwin on the lunar surface; Worden in lunar orbit'),
            (at('eva1'), 'EVA-1: Scott and Irwin outside'), (at('eva1_end'), 'Scott and Irwin in the Lunar Module'),
            (at('eva2'), 'EVA-2: Scott and Irwin outside'), (at('eva2_end'), 'Scott and Irwin in the Lunar Module'),
            (at('eva3'), 'EVA-3: Scott and Irwin outside'), (at('eva3_end'), 'Scott and Irwin in the Lunar Module'),
            (at('lunar_liftoff'), 'Lunar Module ascending; Worden in the Command Module'),
            (at('docking'), 'Docked; crew transferring to the Command Module'),
            (at('lm_jett'), 'All three in the Command Module'),
            (at('csm_eva'), 'Worden outside on the trans-Earth EVA'),
            (at('csm_eva_end'), 'All three in the Command Module'),
            (at('splashdown'), 'Splashdown; awaiting recovery')]
    sleeps = []
    return sorted([(format_get(t), f'<span class="value">{label}</span>') for t, label in rows if t is not None] + sleeps)


def main(args) -> int:
    rows = candidates()
    if not rows:
        raise SystemExit('fp_anchors.tsv is empty; run `flight_plan.py actual` first')
    kept, dropped = build(rows)
    previous = {r['toc_id']: r for r in read_tsv(REVIEW / 'toc.tsv')}
    final = apply_decisions([dict(r) for r in kept], previous)
    write_pipe(MISSION_DATA / 'A15_TOC.csv', [(format_get(r['seconds']), r['level'], r['label']) for r in final])
    write_tsv(MISSION_DATA / 'A15_TOC.tsv', TOC_FIELDS,
              [{**r, 'get': format_get(r['seconds'])} for r in final])
    write_pipe(INDEXES / 'TOCData.csv', [(time_id(r['seconds']), r['level'], r['label']) for r in final])
    review = []
    for r in kept:
        old = previous.get(r['toc_id'], {})
        review.append({'toc_id': r['toc_id'], 'get': format_get(r['seconds']), 'level': r['level'],
                       'label': r['label'], 'get_basis': r['get_basis'], 'evidence': r['evidence'],
                       'alternatives': ';'.join(r.get('alternatives', [])),
                       **{k: old.get(k, '') for k in DECISIONS}})
    for toc_id, old in previous.items():  # keep decisions for rows that disappeared, so nothing is lost
        if toc_id not in {r['toc_id'] for r in kept} and any(old.get(k) for k in DECISIONS):
            review.append({**old, 'note': (old.get('note') or '') + ' [row no longer generated]'})
    write_tsv(REVIEW / 'toc.tsv', REVIEW_FIELDS, review)
    present = {r['key'] for r in kept}
    held = [{'key': k, 'reason': 'no actual time from the Mission Report, AFJ index or an utterance yet'}
            for k in BACKBONE if k not in present]
    write_tsv(WORK / 'toc_held.tsv', ('key', 'reason'), held)
    write_tsv(WORK / 'toc_dropped.tsv', ('toc_id', 'get', 'level', 'label', 'get_basis', 'reason'),
              [{**d, 'get': format_get(d['seconds'])} for d in dropped])
    times = {}
    for r in kept:
        times.setdefault(r['key'], r['seconds'])
    stages = [(format_get(times[k]), name, text) for k, name, text in STAGES if k in times]
    write_pipe(INDEXES / 'missionStagesData.csv', stages)
    write_pipe(INDEXES / 'crewStatusData.csv', crew_status(times))
    print(f'toc: {len(final)} rows ({sum(r["level"] == 1 for r in final)} level 1); dropped {len(dropped)}; '
          f'held back {len(held)}: {", ".join(h["key"] for h in held)}')
    return 0
