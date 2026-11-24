"""Step 10: data checks and release checks.

Flight-plan checks (flight-plan guide §7 step 10). A check whose inputs do not
exist yet (for example blocks before OCR) is reported as ``pending``, not as a
pass. ``fail`` rows make the command exit nonzero; ``warn`` rows are listed for
review.
"""
from __future__ import annotations

import json

from md_common import MISSION_DATA, WORK, parse_get, read_tsv, write_tsv
from md_flightplan import FLIGHT_PLAN, page_sort_key

ALLOWED_TOC_BASIS = ('mission_report', 'afj_index', 'utterance', 'measured', 'human')


def check_flight_plan() -> list[dict]:
    out = []

    def add(check, status, detail):
        out.append({'check': check, 'status': status, 'detail': detail})

    pages = {p['page_id']: p for p in read_tsv(FLIGHT_PLAN / 'fp_pages.tsv')}
    lep = read_tsv(FLIGHT_PLAN / 'fp_lep.tsv')
    if not pages:
        add('pages', 'pending', 'fp_pages.tsv missing')
        return out
    unknown = [r['page_id'] for r in lep if r['page_id'] not in pages]
    add('pages: every LEP page is present or recorded missing', 'fail' if unknown else 'pass',
        ', '.join(unknown) or f'{len(lep)} LEP pages')
    recon = read_tsv(FLIGHT_PLAN / 'fp_reconciliation.tsv')
    lacks = {r['detail'] for r in recon if r['item'] == 'afj_pdf_lacks'}
    no_pdf = [p for p, r in pages.items() if r['status'] in ('present', 'blank') and not r.get('afj_pdf_page')
              and p not in lacks]
    add('pages: each AFJ page has one PDF witness match', 'fail' if no_pdf else 'pass',
        ', '.join(no_pdf) or f'recorded absences: {", ".join(sorted(lacks)) or "none"}')

    blocks = read_tsv(FLIGHT_PLAN / 'fp_blocks.tsv')
    if blocks:
        bad = []
        for b in blocks:
            p = pages.get(b['page_id'], {})
            if b['get_planned'] and p.get('get_start') and p.get('get_end'):
                start, end = parse_get(p['get_start'] + ':00'), parse_get(p['get_end'] + ':00')
                if start is not None and end is not None and not start - 60 <= int(b['get_planned']) <= end + 60:
                    bad.append(b['block_id'])
        add('pages: footer range contains every block GET', 'fail' if bad else 'pass',
            f'{len(bad)} outside: {", ".join(bad[:20])}' if bad else f'{len(blocks)} blocks')
        printed_ann = [b['block_id'] for b in blocks if b['kind'] == 'annotation' and b['origin'] == 'printed']
        add('origin: no annotation block has origin printed', 'fail' if printed_ann else 'pass',
            ', '.join(printed_ann[:20]) or 'ok')
        ids = {b['block_id'] for b in blocks}
        dangling = [b['block_id'] for b in blocks if b['activity_group'] and b['activity_group'] not in ids]
        add('as flown: GLM outputs reference existing IDs only', 'fail' if dangling else 'pass',
            ', '.join(dangling[:20]) or 'ok')
    else:
        add('pages: footer range contains every block GET', 'pending', 'no blocks until OCR and structure run')
        add('origin: no annotation block has origin printed', 'pending', 'no blocks')
        add('as flown: GLM outputs reference existing IDs only', 'pending', 'no blocks')

    ruler = read_tsv(FLIGHT_PLAN / 'fp_ruler.tsv')
    over = sorted({f'{r["page_id"]}#{r["strip"]}' for r in ruler if r['tick_s'] and r['fit_max_s'] and
                   float(r['fit_max_s']) > float(r['tick_s'])}, key=lambda k: page_sort_key(k.split('#')[0]))
    add('times: ruler residual under one tick', 'warn' if over else ('pass' if ruler else 'pending'),
        f'{len(over)} strips: {", ".join(over[:20])}' if over else f'{len(ruler)} ruler points')
    geometry = read_tsv(FLIGHT_PLAN / 'fp_geometry.tsv')
    mism = [g['page_id'] for g in geometry if g.get('cdt_check') == 'mismatch']
    checked = sum(1 for g in geometry if g.get('cdt_check'))
    add('times: CDT label agrees with GET + 13:34 GMT - 5 h', 'warn' if mism else ('pass' if checked else 'pending'),
        f'{checked} pages checked; mismatches (text-layer readings): {", ".join(mism)}' if mism else
        f'{checked} pages checked')

    footers = [p for p in pages.values() if p.get('get_start') and p.get('get_end') and p.get('vehicle') in ('CSM', 'LM')]
    if footers:
        gaps = []
        for vehicle in ('CSM', 'LM'):
            lane = sorted((p for p in footers if p['vehicle'] == vehicle), key=lambda p: page_sort_key(p['page_id']))
            for a, b in zip(lane, lane[1:]):
                if a['get_end'] != b['get_start']:
                    gaps.append(f'{a["page_id"]}->{b["page_id"]} ({a["get_end"]}/{b["get_start"]})')
        add('continuity: each lane ends one page where the next begins', 'warn' if gaps else 'pass',
            f'{len(gaps)}: {"; ".join(gaps[:15])}' if gaps else 'ok')
    else:
        add('continuity: each lane ends one page where the next begins', 'pending', 'footers not read yet (OXR)')
    events = read_tsv(FLIGHT_PLAN / 'fp_events.tsv')
    add('continuity: Chapter 3 events agree with Table 2-9 and the 5-series summary', 'pending' if not events else 'warn',
        'no structured events yet' if not events else 'comparison needs the Table 2-9 rows (fp_tables.tsv)')

    toc = read_tsv(MISSION_DATA / 'A15_TOC.tsv')
    bad_toc = [r['toc_id'] for r in toc if r['get_basis'] not in ALLOWED_TOC_BASIS]
    add('as flown: no TOC row has a planned or interpolated basis', 'fail' if bad_toc else ('pass' if toc else 'pending'),
        ', '.join(bad_toc) or f'{len(toc)} rows')
    levels = [r['toc_id'] for r in toc if r['level'] not in ('1', '2')]
    add('toc: levels are 1 or 2', 'fail' if levels else ('pass' if toc else 'pending'), ', '.join(levels) or 'ok')
    order = [parse_get(r['get']) for r in toc]
    add('toc: rows sorted by GET', 'fail' if order != sorted(order) else ('pass' if toc else 'pending'), 'ok')
    dup = []
    for level in ('1', '2'):
        times = sorted(parse_get(r['get']) for r in toc if r['level'] == level)
        dup += [f'L{level} {t}' for a, t in zip(times, times[1:]) if t - a <= 30]
    add('toc: no duplicates within 30 s at the same level', 'fail' if dup else ('pass' if toc else 'pending'),
        ', '.join(dup[:10]) or 'ok')

    actual = read_tsv(FLIGHT_PLAN / 'fp_actual.tsv')
    if actual:
        overlaps = []
        lanes = {}
        for r in actual:
            if r['actual_start'] and r['actual_end'] and r['status'] not in ('cancelled', 'unconfirmed'):
                lanes.setdefault(r['vehicle'], []).append((parse_get(r['actual_start']), parse_get(r['actual_end']),
                                                           r['activity_id']))
        for lane, items in lanes.items():
            items.sort()
            overlaps += [f'{a[2]}/{b[2]}' for a, b in zip(items, items[1:]) if b[0] < a[1]]
        add('as flown: actual intervals do not overlap within a lane', 'warn' if overlaps else 'pass',
            ', '.join(overlaps[:10]) or 'ok')
    else:
        add('as flown: actual intervals do not overlap within a lane', 'pending', 'fp_actual.tsv empty until structure')
    return out


def main(args) -> int:
    rows = [{'area': 'flight_plan', **r} for r in check_flight_plan()]
    write_tsv(WORK / 'validate.tsv', ('area', 'check', 'status', 'detail'), rows)
    for r in rows:
        print(f'{r["status"]:7} {r["check"]}: {r["detail"][:160]}')
    return 1 if any(r['status'] == 'fail' for r in rows) else 0
