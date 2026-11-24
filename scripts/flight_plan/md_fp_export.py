"""Interface export ``MISSION_DATA/indexes/flightPlanData.json`` (flight-plan guide §4).

Phases -> activities -> steps, each node with its planned time and page
references (page, witness, normalised box, PDF page and hosted image), the
as-flown result where one exists, and parsed burn fields for events. A proposal
for AiRT2: it has no flight-plan contract yet.
"""
from __future__ import annotations

from md_common import INDEXES, read_tsv, write_json
from md_flightplan import AFJ_PDF_URL, FLIGHT_PLAN, page_sort_key


def page_ref(page: dict, bbox_norm: str = '') -> dict:
    ref = {'page_id': page['page_id'], 'witness': 'afj_gif' if page['status'] in ('present', 'blank') else 'alsj_jpg',
           'image': page.get('hosted_full') or ''}
    if page.get('afj_pdf_page'):
        ref['pdf'] = f'{AFJ_PDF_URL}#page={page["afj_pdf_page"]}'
    if bbox_norm:
        ref['bbox_norm'] = [float(v) for v in bbox_norm.split(',')]
    return ref


def export() -> dict:
    pages = {p['page_id']: p for p in read_tsv(FLIGHT_PLAN / 'fp_pages.tsv')}
    blocks = {b['block_id']: b for b in read_tsv(FLIGHT_PLAN / 'fp_blocks.tsv')}
    actual = {a['activity_id']: a for a in read_tsv(FLIGHT_PLAN / 'fp_actual.tsv')}
    events = read_tsv(FLIGHT_PLAN / 'fp_events.tsv')
    phases: dict[str, dict] = {}
    for p in sorted(pages.values(), key=lambda p: page_sort_key(p['page_id'])):
        if p['chapter'] == '3' and p['phase'] and p['phase'] not in phases:
            phases[p['phase']] = {'phase': p['phase'], 'first_page': p['page_id'], 'activities': []}
    for a in read_tsv(FLIGHT_PLAN / 'fp_activities.tsv'):
        node = {'activity_id': a['activity_id'], 'title': a['title'], 'category': a['category'], 'crew': a['crew'],
                'vehicle': a['vehicle'],
                'planned': {'start': a['planned_start'], 'end': a['planned_end'], 'basis': a['start_basis'],
                            'source': a['block_ids'].split(';')[0]},
                'pages': [page_ref(pages[b['page_id']], b['bbox_norm'])
                          for b in (blocks.get(i) for i in a['block_ids'].split(';')[:1]) if b and b['page_id'] in pages]}
        act = actual.get(a['activity_id'])
        if act:
            node['actual'] = {'start': act['actual_start'], 'end': act['actual_end'], 'basis': act['start_basis'],
                              'status': act['status'], 'evidence': [e for e in act['evidence'].split(';') if e]}
        phases.setdefault(a['phase'], {'phase': a['phase'], 'first_page': '', 'activities': []})['activities'].append(node)
    out = {'mission': 'A15', 'edition': 'FINAL (7/26) 6/21/71', 'phases': list(phases.values()),
           'events': [{'event_id': e['event_id'], 'name': e['name'], 'planned_get': e['planned_get'],
                       'basis': e['get_basis'], 'vehicle': e['vehicle'],
                       'burn': {k: e[k] for k in ('tig', 'bt', 'dv', 'ullage', 'ha', 'hp') if e.get(k)}}
                      for e in events],
           'pages': [{**page_ref(p), 'status': p['status'], 'page_type': p.get('page_type', ''),
                      'title': p.get('printed_title') or p.get('index_title', ''),
                      'get_start': p.get('get_start', ''), 'get_end': p.get('get_end', '')}
                     for p in sorted(pages.values(), key=lambda p: page_sort_key(p['page_id']))]}
    write_json(INDEXES / 'flightPlanData.json', out)
    return out
