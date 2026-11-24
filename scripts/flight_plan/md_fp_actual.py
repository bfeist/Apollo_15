"""Flight plan steps 6–7: read-up updates, as-flown anchors, the slip map and ``fp_actual.tsv`` (§5).

Evidence, in order of authority: the Mission Report sequence of events, the
AFJ "Index to events" tables, utterance events (with ALSJ chapter starts), the
flight-plan read-ups, and Woods's annotations (supporting only). Planned and
actual times are never pooled. A planned activity with no evidence keeps its
planned or slip-interpolated time and is marked ``unconfirmed``; it never
supplies a TOC time.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from md_common import (JOURNALS, RAW_ROOT, REVIEW, SCRAPED, WORK, format_get, parse_get, read_tsv, squash, today,
                       write_tsv)
from md_flightplan import FLIGHT_PLAN, normalise_page_id, page_sort_key

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

EVENT_INDEX_FIELDS = ('row_id', 'chapter', 'row', 'label', 'get', 'get_seconds', 'anchor', 'block_key')
MR_FIELDS = ('row_id', 'pdf_page', 'label', 'get', 'get_seconds', 'raw')
ANCHOR_FIELDS = ('anchor_id', 'key', 'label', 'get', 'get_seconds', 'basis', 'vehicle', 'evidence', 'level',
                 'toc_label')
UPDATE_FIELDS = ('update_id', 'cited_page_id', 'cited_get', 'change_text', 'utterance_event_ids', 'source_get',
                 'source', 'annotation_block_ids', 'kind', 'kind_basis')
ACTUAL_FIELDS = ('activity_id', 'title', 'vehicle', 'status', 'planned_start', 'actual_start', 'actual_end',
                 'start_basis', 'end_basis', 'uncertainty_s', 'evidence', 'slip_s', 'review_state')
REVIEW_FIELDS = ('activity_id', 'choice', 'actual_start', 'reviewer', 'date', 'note', 'methods')

# Canonical as-flown events: key, actual-label pattern, planned-name pattern, vehicle lane, TOC level and label.
CANON = (
    ('launch', r'^lift-?off\b', r'LIFT ?OFF|LAUNCH', 'CSM', 1, 'Lift-off'),
    ('tower_clear', r'tower clear', r'TOWER', 'CSM', 2, 'Tower clear'),
    ('staging_sic', r's-ic/s-ii staging', r'S-?IC.*STAG', 'CSM', 1, 'S-IC/S-II staging'),
    ('staging_sii', r's-ii/s-ivb staging', r'S-?II.*STAG', 'CSM', 2, 'S-II/S-IVB staging'),
    ('eoi', r'earth orbit insertion|parking orbit insertion', r'INSERTION|EOI', 'CSM', 1, 'Earth orbit insertion'),
    ('tli', r'translunar injection|^tli burn begins', r'^TLI\b', 'CSM', 1, 'Translunar injection'),
    ('csm_sivb_sep', r's-ivb/command and service module separation|csm/s-ivb separation', r'S-?IVB SEP|CSM/S-?IVB',
     'CSM', 1, 'CSM/S-IVB separation'),
    ('td_dock', r'translunar docking|csm/lm hard dock', r'DOCK', 'CSM', 1, 'Transposition and docking'),
    ('lm_ejection', r'spacecraft ejection|lm ejection', r'EJECT', 'CSM', 1, 'LM ejection from the S-IVB'),
    ('sps_test', r'first midcourse correction|sps test burn', r'SPS.*TEST|MCC-?1', 'CSM', 1,
     'First midcourse correction, SPS test'),
    ('mcc4', r'second midcourse correction|^midcourse 4 manoeuvre$', r'MCC-?4', 'CSM', 1,
     'Second midcourse correction (MCC-4)'),
    ('sim_door', r'scientific instrument module door jettison|sim bay door jettison', r'SIM.*DOOR', 'CSM', 1,
     'SIM bay door jettison'),
    ('loi', r'lunar orbit insertion|ignition, loi burn', r'^LOI\b', 'CSM', 1, 'Lunar orbit insertion'),
    ('sivb_impact', r's-ivb lunar impact', r'S-?IVB IMPACT', 'CSM', 2, 'S-IVB lunar impact'),
    ('doi', r'^descent orbit insertion$|descent orbit insertion \(doi\) manoeuver', r'^DOI\b(?!.*TRIM)', 'CSM', 1,
     'Descent orbit insertion'),
    ('doi_trim', r'descent orbit trim|doi trim manoeuvre', r'DOI.*TRIM|TRIM', 'LM', 1, 'DOI trim'),
    ('undock', r'undocking and separation|separation \(undocking\)', r'UNDOCK|^SEP\b', 'LM', 1,
     'Undocking and separation'),
    ('circ', r'circularization maneuver|circularisation burn', r'CIRC', 'CSM', 1, 'CSM circularisation'),
    ('pdi', r'powered descent initiation', r'^PDI\b', 'LM', 1, 'Powered descent initiation'),
    ('landing', r'^lunar landing$|lunar touchdown', r'TOUCHDOWN|LANDING', 'LM', 1, 'Landing at Hadley'),
    ('seva', r'start standup extravehicular', r'SEVA|STAND-?UP', 'LM', 1, 'Stand-up EVA begins'),
    ('seva_end', r'end standup extravehicular', r'SEVA|STAND-?UP', 'LM', 2, 'Stand-up EVA ends'),
    ('eva1', r'start first extravehicular', r'EVA-?1|EGRESS', 'LM', 1, 'EVA-1 begins'),
    ('alsep_data', r'data from apollo lunar surface experiment', r'ALSEP', 'LM', 2, 'First ALSEP data'),
    ('eva1_end', r'end first extravehicular', r'INGRESS|REPRESS', 'LM', 1, 'EVA-1 ends'),
    ('eva2', r'start second extravehicular', r'EVA-?2|EGRESS', 'LM', 1, 'EVA-2 begins'),
    ('eva2_end', r'end second extravehicular', r'INGRESS|REPRESS', 'LM', 1, 'EVA-2 ends'),
    ('eva3', r'start third extravehicular', r'EVA-?3|EGRESS', 'LM', 1, 'EVA-3 begins'),
    ('plane_change', r'plane change', r'PLANE CHANGE|LOPC', 'CSM', 1, 'CSM plane change'),
    ('eva3_end', r'end third extravehicular', r'INGRESS|REPRESS', 'LM', 1, 'EVA-3 ends'),
    ('lunar_liftoff', r'^lunar ascent$|lm ascent lift-?off', r'LIFT ?OFF|ASCENT', 'LM', 1, 'Lunar lift-off'),
    ('tpi', r'terminal phase initiate|^tpi', r'^TPI\b', 'LM', 1, 'Terminal phase initiation'),
    ('tpf', r'terminal phase finalize', r'TPF|BRAKING', 'LM', 2, 'Terminal phase finalisation'),
    ('docking', r'^docking$|^hard dock$', r'DOCK', 'CSM', 1, 'Docking'),
    ('lm_jett', r'^lunar module jettison$|^lm jettison$', r'LM JETT|JETT', 'CSM', 1, 'LM jettison'),
    ('lm_deorbit', r'lunar module deorbit', r'DEORBIT', 'LM', 2, 'LM deorbit burn'),
    ('lm_impact', r'^lunar module impact$', r'IMPACT', 'LM', 2, 'LM impact'),
    ('orbit_shaping', r'orbit shaping maneuver', r'SHAPING', 'CSM', 2, 'Orbit shaping burn'),
    ('subsat', r'subsatellite launch', r'SUBSAT|S/S', 'CSM', 1, 'Subsatellite launch'),
    ('tei', r'^transearth injection$|ignition, trans-earth injection', r'^TEI\b', 'CSM', 1, 'Trans-Earth injection'),
    ('csm_eva', r'start transearth extravehicular|hatch reported open', r'EVA|HATCH', 'CSM', 1,
     'Trans-Earth EVA (Worden) begins'),
    ('csm_eva_end', r'end transearth extravehicular|hatch reported closed', r'EVA|HATCH', 'CSM', 2,
     'Trans-Earth EVA ends'),
    ('eclipse', r'^lunar eclipse$', r'ECLIPSE', 'CSM', 1, 'Lunar eclipse'),
    ('press', r'press conference tv starts', r'PRESS', 'CSM', 1, 'Press conference'),
    ('mcc7', r'third midcourse correction|midcourse 7 manoeuvre burn', r'MCC-?7', 'CSM', 1,
     'Third midcourse correction (MCC-7)'),
    ('cmsm_sep', r'command module/service module separation|cm/sm separation', r'CM/SM SEP', 'CSM', 1,
     'CM/SM separation'),
    ('ei', r'^entry interface$', r'ENTRY INTERFACE|^EI\b', 'CSM', 1, 'Entry interface'),
    ('blackout', r'^begin blackout$', r'BLACKOUT', 'CSM', 2, 'Communications blackout begins'),
    ('drogue', r'drogue (chute )?deployment', r'DROGUE', 'CSM', 2, 'Drogue parachutes'),
    ('main_chutes', r'main (parachute|chute) deployment', r'MAIN', 'CSM', 2, 'Main parachutes'),
    ('splashdown', r'^landing$|^splashdown$', r'SPLASH', 'CSM', 1, 'Splashdown'),
)
# AFJ index rows that are themselves notable level-2 items.
LEVEL2 = (r'tv transmission|tv coverage', r'light flash', r'uv photograph', r'landmark tracking', r'^first communication',
          r'chlorination port leak', r'broken glass', r'failure to sep', r'lgc plugs', r'recheck hatch seals',
          r'manual separation burn', r'midcourse correction 5 cancelled', r'leaving the lunar sphere',
          r'mapping camera film cassette', r'panoramic camera film cassette', r'passive thermal control',
          r'electrical glitch', r'lm repressurisation', r'worden spots lm', r'littrow', r'question 1$')
# ALSJ chapters (first timed utterance) that become TOC rows: chapter -> (key, level, label).
ALSJ_TOC = {
    'a15.sevaprep.html': ('seva_prep', 2, 'Preparations for the stand-up EVA'),
    'a15.seva.html': ('seva', 1, 'Stand-up EVA'),
    'a15.eva1wake.html': ('wake', 1, 'Wake-up for EVA-1'),
    'a15.eva1prep.html': ('eva1_prep', 1, 'EVA-1 preparations'),
    'a15.lrvdep.html': ('lrv_deploy', 2, 'Deploying the Lunar Roving Vehicle'),
    'a15.elbowtrv.html': ('eva1_drive', 2, 'Driving to Elbow crater'),
    'a15.elbow.html': ('station1', 1, 'Station 1 at Elbow crater'),
    'a15.sta2.html': ('station2', 1, 'Station 2 on Mt. Hadley Delta'),
    'a15.alsepoff.html': ('alsep', 2, 'ALSEP off-load and deployment'),
    'a15.clsout1.html': ('eva1_closeout', 1, 'EVA-1 closeout'),
    'a15.eva2wake.html': ('wake', 1, 'Wake-up for EVA-2'),
    'a15.eva2prep.html': ('eva2_prep', 1, 'EVA-2 preparations'),
    'a15.sta6abv.html': ('station6', 1, 'Station 6 on Mt. Hadley Delta'),
    'a15.sta6a.html': ('station6a', 1, 'Station 6a, the Green Boulder'),
    'a15.spur.html': ('station7', 1, 'Station 7 at Spur crater, the Genesis Rock'),
    'a15.trvsta4.html': ('station4', 1, 'Station 4 at Dune crater'),
    'a15.sta8.html': ('station8', 1, 'Station 8'),
    'a15.clsout2.html': ('eva2_closeout', 1, 'EVA-2 closeout'),
    'a15.eva3prep.html': ('eva3_prep', 1, 'EVA-3 preparations'),
    'a15.sta9.html': ('station9', 1, 'Station 9'),
    'a15.rille.html': ('station9a', 1, 'Station 9a at Hadley Rille'),
    'a15.sta10.html': ('station10', 1, 'Station 10'),
    'a15.clsout3.html': ('eva3_closeout', 1, 'EVA-3 closeout, the hammer and the feather'),
}
SLEEP_RE = re.compile(r'\bgood ?night\b|\bsleep well\b|\bsweet dreams\b|\bpleasant dreams\b', re.I)
WAKE_RE = re.compile(r'\bgood morning\b|\brise and shine\b|\btime to (get up|wake up)\b|\bwake[- ]?up\b', re.I)


# ---------------------------------------------------------------- loaders
def afj_event_index() -> list[dict]:
    """Every 'Index to events' row of the AFJ chapters, with its journal anchor."""
    manifest = sorted(read_tsv(JOURNALS / 'ap15fj' / 'manifest.tsv'), key=lambda r: int(r['order']))
    rows = []
    for m in manifest:
        if m['role'] != 'chapter':
            continue
        soup = BeautifulSoup((JOURNALS / 'ap15fj' / m['file']).read_bytes(), 'html.parser')
        head = soup.find(lambda t: t.name == 'b' and 'index to events' in t.get_text().lower())
        if not head:
            continue
        table = head.find_next('table')
        for n, tr in enumerate(table.find_all('tr') if table else []):
            cells = tr.find_all('td')
            if len(cells) < 2:
                continue
            label = squash(cells[0].get_text(' '))
            link = cells[1].find('a', href=True)
            stamp = squash(cells[1].get_text(' ')).replace('GET', '').strip()
            seconds = parse_get(stamp)
            anchor = link['href'].lstrip('#') if link else ''
            chapter = Path(m['file']).stem
            rows.append({'row_id': f'afj:{chapter}:event:{n:02d}', 'chapter': m['file'], 'row': n, 'label': label,
                         'get': format_get(seconds) if seconds is not None else '', 'get_seconds': seconds,
                         'anchor': anchor, 'block_key': f'afj:{chapter}:{anchor}' if anchor else ''})
    return rows


def mission_report_events() -> list[dict]:
    """Mission Report Table 3-I (sequence of events).

    The table is an image without a text layer, so the recorded keyed reading
    ``review/mission_report_soe.tsv`` is used; OXR becomes a second witness once
    it runs. A text-layer table, if a later scan has one, is parsed directly.
    """
    keyed = read_tsv(REVIEW / 'mission_report_soe.tsv')
    if keyed:
        return [{'row_id': f'mr:t3-1:{int(r["row"]):02d}', 'pdf_page': r['pdf_page'], 'label': r['label'],
                 'get': r['get'], 'get_seconds': parse_get(r['get']), 'raw': f'{r["reading"]}; {r["check"]}'}
                for r in keyed if not r['label'].startswith('Range zero')]
    path = RAW_ROOT / 'original_docs/ap15mr.pdf'
    if not path.is_file():
        return []
    import pymupdf
    rows = []
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, 1):
            text = page.get_text()
            if not re.search(r'SEQUENCE OF EVENTS', text, re.I):
                continue
            for line in text.splitlines():
                m = re.match(r'^\s*([A-Za-z][^\d]{3,80}?)[\s.]{2,}(\d{1,3}:\d{2}:\d{2}(?:\.\d)?)\s*$', line)
                if m:
                    seconds = parse_get(m.group(2).split('.')[0])
                    if seconds is not None:
                        rows.append({'row_id': f'mr:p{number:03d}:{len(rows):03d}', 'pdf_page': number,
                                     'label': squash(m.group(1).rstrip('. ')), 'get': m.group(2),
                                     'get_seconds': seconds, 'raw': line.strip()})
    return rows


def journal_rows(source: str) -> list[dict]:
    return read_tsv(SCRAPED / f'journal_{source}.tsv')


def alsj_chapter_starts() -> list[dict]:
    """First timed utterance of each ALSJ chapter (an utterance-event basis)."""
    out, seen = [], set()
    for r in journal_rows('alsj'):
        if r['chapter'] in seen or r['kind'] != 'utterance' or r['get_basis'] not in ('anchor', 'stamp'):
            continue
        seen.add(r['chapter'])
        out.append({'chapter': r['chapter'], 'get_seconds': int(r['get_seconds']), 'block_key': r['block_key'],
                    'text': r['text'][:120]})
    return out


def rest_candidates(utterances: list[dict]) -> list[dict]:
    """Sleep and wake-up calls: the last 'good night' before, and first 'good morning' after, each long quiet gap."""
    timed = sorted((u for u in utterances if u.get('get_seconds') not in ('', None)),
                   key=lambda u: int(u['get_seconds']))
    out = []
    for prev, nxt in zip(timed, timed[1:]):
        a, b = int(prev['get_seconds']), int(nxt['get_seconds'])
        if b - a < 4 * 3600 or b - a > 14 * 3600:
            continue  # rest periods only: not comm gaps, not the other vehicle's absence
        before = [u for u in timed if a - 3 * 3600 <= int(u['get_seconds']) <= a and SLEEP_RE.search(u['text'])]
        after = [u for u in timed if b <= int(u['get_seconds']) <= b + 2 * 3600 and WAKE_RE.search(u['text'])]
        sleep = before[-1] if before else prev
        wake = after[0] if after else nxt
        out.append({'sleep_s': int(sleep['get_seconds']), 'sleep_key': sleep['block_key'],
                    'sleep_basis': 'utterance' if before else 'last utterance before gap',
                    'wake_s': int(wake['get_seconds']), 'wake_key': wake['block_key'],
                    'wake_basis': 'utterance' if after else 'first utterance after gap', 'gap_s': b - a})
    return out


# -------------------------------------------------------------- anchors
def build_anchors() -> list[dict]:
    anchors = []
    mr = mission_report_events()
    write_tsv(SCRAPED / 'mission_report_events.tsv', MR_FIELDS, mr)
    index = afj_event_index()
    write_tsv(SCRAPED / 'journal_event_index.tsv', EVENT_INDEX_FIELDS, index)
    used = set()
    for key, actual_re, _planned, vehicle, level, label in CANON:
        pattern = re.compile(actual_re, re.I)
        found = [r for r in mr if pattern.search(r['label'])]
        basis = 'mission_report'
        if not found:
            found = [r for r in index if r['get_seconds'] is not None and pattern.search(r['label'])]
            basis = 'afj_index'
        if found:
            r = found[0]
            used.add(r['row_id'])
            anchors.append({'anchor_id': f'{key}', 'key': key, 'label': r['label'], 'get_seconds': r['get_seconds'],
                            'basis': basis, 'vehicle': vehicle, 'evidence': r['row_id'] + (
                                f';{r["block_key"]}' if r.get('block_key') else ''), 'level': level,
                            'toc_label': label})
    for r in index:
        if r['row_id'] in used or r['get_seconds'] is None:
            continue
        if any(re.search(p, r['label'], re.I) for p in LEVEL2):
            anchors.append({'anchor_id': r['row_id'], 'key': 'afj_index', 'label': r['label'],
                            'get_seconds': r['get_seconds'], 'basis': 'afj_index', 'vehicle': '',
                            'evidence': r['row_id'] + (f';{r["block_key"]}' if r['block_key'] else ''), 'level': 2,
                            'toc_label': r['label']})
    have = {a['key'] for a in anchors}
    for start in alsj_chapter_starts():
        spec = ALSJ_TOC.get(start['chapter'])
        if not spec:
            continue
        key, level, label = spec
        if key in have and key != 'wake':
            continue
        anchors.append({'anchor_id': f'alsj:{start["chapter"]}', 'key': key, 'label': label,
                        'get_seconds': start['get_seconds'], 'basis': 'utterance', 'vehicle': 'LM',
                        'evidence': start['block_key'], 'level': level, 'toc_label': label})
    # Landing: the ALSJ anchor 1044229 ("Contact light") when the Mission Report is not parsed.
    if 'landing' not in {a['key'] for a in anchors}:
        for r in journal_rows('alsj'):
            if r['chapter'] == 'a15.landing.html' and r['kind'] == 'utterance' and re.search(
                    r'contact( light)?', r['text'], re.I):
                anchors.append({'anchor_id': 'landing', 'key': 'landing', 'label': 'Contact light',
                                'get_seconds': int(r['get_seconds']), 'basis': 'utterance', 'vehicle': 'LM',
                                'evidence': r['block_key'], 'level': 1, 'toc_label': 'Landing at Hadley'})
                break
    if 'pdi' not in {a['key'] for a in anchors}:
        for r in journal_rows('alsj'):
            if r['chapter'] == 'a15.landing.html' and r['kind'] == 'utterance' and re.search(
                    r'\bignition\b|we have ignition|go for (powered descent|pdi)', r['text'], re.I) and r['get_seconds']:
                anchors.append({'anchor_id': 'pdi', 'key': 'pdi', 'label': squash(r['text'])[:60],
                                'get_seconds': int(r['get_seconds']), 'basis': 'utterance', 'vehicle': 'LM',
                                'evidence': r['block_key'], 'level': 1, 'toc_label': 'Powered descent initiation'})
                break
    # Sleep and wake-up per crew lane: the whole crew while docked, Worden (AFJ) and Scott and Irwin
    # (ALSJ) between undocking and docking.
    times = {a['key']: int(a['get_seconds']) for a in anchors}
    solo = (times.get('undock', 362356), times.get('docking', 624986))
    afj = [r for r in journal_rows('afj') if r['kind'] == 'utterance']
    alsj = [r for r in journal_rows('alsj') if r['kind'] == 'utterance']

    def inside(r):
        return r['get_seconds'] not in ('', None) and solo[0] <= int(r['get_seconds']) <= solo[1]
    lanes = (('crew', 'Crew', [r for r in afj if not inside(r)]),
             ('cmp', 'Worden', [r for r in afj if inside(r)]),
             ('lm', 'Scott and Irwin', [r for r in alsj if inside(r)]))
    chapter_wakes = [int(a['get_seconds']) for a in anchors if a['key'] == 'wake']
    for lane, who, pool in lanes:
        for n, c in enumerate(rest_candidates(pool)):
            if lane != 'crew' and not (solo[0] < c['sleep_s'] < solo[1]):
                continue
            vehicle = {'crew': '', 'cmp': 'CSM', 'lm': 'LM'}[lane]
            anchors.append({'anchor_id': f'rest:{lane}:{n:02d}:sleep', 'key': 'sleep', 'label': f'{who}: sleep period begins',
                            'get_seconds': c['sleep_s'], 'basis': 'utterance', 'vehicle': vehicle,
                            'evidence': f'{c["sleep_key"]};{c["sleep_basis"]};gap {c["gap_s"]}s', 'level': 1,
                            'toc_label': 'Crew sleep' if lane == 'crew' else f'{who}: sleep'})
            if any(abs(c['wake_s'] - w) <= 2 * 3600 for w in chapter_wakes):
                continue  # the ALSJ wake-up chapter already marks this one
            anchors.append({'anchor_id': f'rest:{lane}:{n:02d}:wake', 'key': 'wake', 'label': f'{who}: wake-up',
                            'get_seconds': c['wake_s'], 'basis': 'utterance', 'vehicle': vehicle,
                            'evidence': f'{c["wake_key"]};{c["wake_basis"]}', 'level': 1,
                            'toc_label': 'Crew wake-up' if lane == 'crew' else f'{who}: wake-up'})
    # Countdown: the first printed PAO announcement (header time) opens the record.
    for e in read_tsv(SCRAPED / 'utterance_events.tsv'):
        if e['get_seconds'] not in ('', None) and int(e['get_seconds']) < 0:
            anchors.append({'anchor_id': 'countdown', 'key': 'countdown', 'label': squash(e['printed_text'])[:60],
                            'get_seconds': int(e['get_seconds']), 'basis': 'utterance', 'vehicle': 'CSM',
                            'evidence': f'{e["event_id"]};{e["get_basis"]} time', 'level': 1,
                            'toc_label': 'Countdown'})
            break
    for a in anchors:
        a['get'] = format_get(a['get_seconds'])
    anchors.sort(key=lambda a: (int(a['get_seconds']), a['anchor_id']))
    write_tsv(FLIGHT_PLAN / 'fp_anchors.tsv', ANCHOR_FIELDS, anchors)
    return anchors


# --------------------------------------------------------------- slip map
def slip_map(points: list[tuple[int, int]]):
    """Piecewise-linear actual-minus-planned offset through (planned_s, slip_s) anchors, constant outside."""
    points = sorted(points)

    def at(planned: int) -> tuple[float, int]:
        if not points:
            return 0.0, 10 ** 9
        if planned <= points[0][0]:
            return float(points[0][1]), points[0][0] - planned
        if planned >= points[-1][0]:
            return float(points[-1][1]), planned - points[-1][0]
        for (a, sa), (b, sb) in zip(points, points[1:]):
            if a <= planned <= b:
                f = (planned - a) / (b - a) if b > a else 0
                return sa + f * (sb - sa), min(planned - a, b - planned)
        return 0.0, 10 ** 9
    return at


def uncertainty(distance_s: int) -> int:
    """Grows with distance to the nearest anchor: 5 min plus 10 % of the distance."""
    return int(300 + 0.1 * min(distance_s, 48 * 3600))


def check_monotonic(points: list[tuple[int, int]]) -> list[str]:
    """Actual times must keep the planned order: actual(b) >= actual(a) for planned b > a."""
    issues = []
    points = sorted(points)
    for (a, sa), (b, sb) in zip(points, points[1:]):
        if b + sb < a + sa:
            issues.append(f'anchors at planned {format_get(a)} and {format_get(b)} reverse order')
    return issues


# ------------------------------------------------------------------ steps
def page_hours() -> dict[str, int]:
    """Approximate start GET of each Chapter 3 page: footer when read, else the index title hour."""
    out = {}
    for p in read_tsv(FLIGHT_PLAN / 'fp_pages.tsv'):
        if p.get('get_start'):
            v = parse_get(p['get_start'] + ':00')
            if v is not None:
                out[p['page_id']] = v
                continue
        m = re.match(r'(\d{3}) hours', p.get('index_title') or '')
        if m:
            out[p['page_id']] = int(m.group(1)) * 3600
    return out


PAGE_REF = re.compile(r'(?:flight\s*plan[,.]?\s*)?page\s*(3\s*[-–]\s*\d{1,3}|\d{1,3})', re.I)
GET_REF = re.compile(r'\b(\d{2,3})[:+ ](\d{2})(?::(\d{2}))?\b')


def resolve_page(token: str, said_at: int | None, hours: dict[str, int]) -> str | None:
    """``3-84``, ``384`` (dash lost in the 1971 OCR) or ``84``: choose the candidate page nearest after ``said_at``."""
    token = re.sub(r'\s', '', token).replace('–', '-')
    if '-' in token:
        return normalise_page_id(token)
    digits = token
    candidates = {normalise_page_id(f'3-{digits}')}
    if len(digits) == 3 and digits.startswith('3'):
        candidates.add(normalise_page_id(f'3-{digits[1:]}'))
    candidates = {c for c in candidates if c and int(c[2:]) <= 408}
    if not candidates:
        return None
    if said_at is None or len(candidates) == 1:
        return sorted(candidates)[0]
    scored = sorted(candidates, key=lambda c: (hours.get(c, 10 ** 9) < said_at - 3600, abs(hours.get(c, 10 ** 9) - said_at)))
    return scored[0]


def change_kind(text: str) -> str:
    t = text.lower()
    if re.search(r'\b(delete|scratch|cross out|strike|omit|no longer|cancel)', t):
        return 'deleted'
    if re.search(r'\bpad\b', t):
        return 'pad'
    if re.search(r'\b(add|insert|write in|put in|pencil in)\b', t):
        return 'added'
    if re.search(r'\b(change|move|instead of|slip|now at|new time|at \d)', t):
        return 'time_change'
    return 'unknown'


def main_updates(args) -> int:
    hours = page_hours()
    annotations = {}
    for b in read_tsv(FLIGHT_PLAN / 'fp_blocks.tsv'):
        if b.get('origin') == 'annotation':
            annotations.setdefault(b['page_id'], []).append(b['block_id'])
    sources = []
    for edition in ('tec', 'cm', 'lm'):
        for r in read_tsv(SCRAPED / f'transcript_{edition}.tsv'):
            sources.append(('transcript_' + edition, r['event_id'], r.get('get_seconds'), r.get('text_ocr') or ''))
    for name in ('afj', 'alsj'):
        for r in journal_rows(name):
            if r['kind'] in ('utterance', 'comment', 'pao'):
                sources.append(('journal_' + name, r['block_key'], r.get('get_seconds'), r.get('text') or ''))
                for link in re.findall(r'fplan/([1-6]-\d{3})\.gif', r.get('media') or r.get('html') or ''):
                    sources.append(('journal_' + name + '_link', r['block_key'], r.get('get_seconds'),
                                    f'[link] page {link}'))
    rows, previous_cue = [], None
    for source, key, get_s, text in sources:
        said = int(get_s) if get_s not in ('', None) else None
        cue = re.search(r'flight\s*plan|page\s*3\s*-|\[link\]', text, re.I)
        if not cue:
            continue
        for n, m in enumerate(PAGE_REF.finditer(text)):
            if 'flight' not in text.lower() and '-' not in m.group(1) and not text.startswith('[link]'):
                continue
            page = resolve_page(m.group(1), said, hours)
            if not page:
                continue
            window = text[max(0, m.start() - 160):m.end() + 240]
            gm = GET_REF.search(text[m.end():m.end() + 200])
            cited = f'{int(gm.group(1)):03d}:{gm.group(2)}' + (f':{gm.group(3)}' if gm.group(3) else '') if gm else ''
            if text.startswith('[link]'):
                kind, basis = 'reference', 'journal link'
            else:
                kind, basis = change_kind(window), 'rule'
            rows.append({'update_id': f'upd:{source}:{key}:{n}', 'cited_page_id': page, 'cited_get': cited,
                         'change_text': squash(window), 'utterance_event_ids': key,
                         'source_get': format_get(said) if said is not None else '', 'source': source,
                         'annotation_block_ids': ';'.join(annotations.get(page, [])), 'kind': kind,
                         'kind_basis': basis})
    rows.sort(key=lambda r: (parse_get(r['source_get']) if r['source_get'] else -10 ** 9, r['update_id']))
    write_tsv(FLIGHT_PLAN / 'fp_updates.tsv', UPDATE_FIELDS, rows)
    kinds = {}
    for r in rows:
        kinds[r['kind']] = kinds.get(r['kind'], 0) + 1
    print(f'updates: {len(rows)} page citations {kinds}')
    return 0


def record_review() -> int:
    queue = read_tsv(WORK / 'fp_actuals_queue.tsv')
    saved = {r['activity_id']: r for r in read_tsv(REVIEW / 'fp_actuals.tsv')}
    n = 0
    for r in queue:
        if r.get('choice') and r['activity_id'] not in saved:
            saved[r['activity_id']] = {**{k: r.get(k, '') for k in REVIEW_FIELDS}, 'date': r.get('date') or today()}
            n += 1
    write_tsv(REVIEW / 'fp_actuals.tsv', REVIEW_FIELDS, [saved[k] for k in sorted(saved)])
    return n


def main_actual(args) -> int:
    if args.record:
        print(f'recorded {record_review()} actual decisions')
    anchors = build_anchors()
    print(f'anchors: {len(anchors)} ({sum(a["basis"] == "mission_report" for a in anchors)} Mission Report, '
          f'{sum(a["basis"] == "afj_index" for a in anchors)} AFJ index, '
          f'{sum(a["basis"] == "utterance" for a in anchors)} utterance)')
    activities = read_tsv(FLIGHT_PLAN / 'fp_activities.tsv')
    events = read_tsv(FLIGHT_PLAN / 'fp_events.tsv')
    decisions = {r['activity_id']: r for r in read_tsv(REVIEW / 'fp_actuals.tsv')}
    rows, queue = [], []
    planned = [{'activity_id': a['activity_id'], 'title': a['title'], 'vehicle': a['vehicle'],
                'planned_s': int(a['planned_start_s']) if a['planned_start_s'] else None,
                'category': a['category']} for a in activities]
    planned += [{'activity_id': e['event_id'], 'title': e['name'], 'vehicle': e['vehicle'],
                 'planned_s': int(e['planned_get_s']) if e['planned_get_s'] else None, 'category': 'event'}
                for e in events]
    matched_anchor, lane_points = set(), {'CSM': [], 'LM': []}
    for p in planned:
        if p['planned_s'] is None:
            continue
        window = 24 * 3600 if p['category'] in ('sleep', 'eat', 'eva') else 2 * 3600
        best = None
        for key, _a, planned_re, vehicle, _l, _t in CANON:
            if not re.search(planned_re, p['title'].upper()):
                continue
            for a in anchors:
                if a['key'] == key and abs(int(a['get_seconds']) - p['planned_s']) <= window:
                    d = abs(int(a['get_seconds']) - p['planned_s'])
                    if best is None or d < best[0]:
                        best = (d, a)
        if best:
            a = best[1]
            slip = int(a['get_seconds']) - p['planned_s']
            matched_anchor.add(a['anchor_id'])
            lane_points.setdefault(p['vehicle'] if p['vehicle'] in lane_points else 'CSM', []).append(
                (p['planned_s'], slip))
            rows.append({**p, 'status': 'done' if abs(slip) <= 300 else 'done_shifted',
                         'actual_start': format_get(int(a['get_seconds'])), 'start_basis': a['basis'],
                         'uncertainty_s': 1 if a['basis'] == 'mission_report' else 5, 'evidence': a['evidence'],
                         'slip_s': slip, 'review_state': 'auto_rule'})
        else:
            rows.append({**p, 'status': 'unconfirmed', 'review_state': 'pending'})
    issues = []
    for lane, pts in lane_points.items():
        issues += [f'{lane}: {i}' for i in check_monotonic(pts)]
    maps = {lane: slip_map(pts) for lane, pts in lane_points.items()}
    for r in rows:
        if r['status'] == 'unconfirmed' and r['planned_s'] is not None:
            slip, distance = maps.get(r['vehicle'], maps['CSM'])(r['planned_s'])
            r.update(actual_start=format_get(int(round(r['planned_s'] + slip))), start_basis='slip_interpolated',
                     uncertainty_s=uncertainty(distance), slip_s=int(round(slip)))
            queue.append({'activity_id': r['activity_id'], 'issue': 'no_evidence', 'planned': format_get(r['planned_s']),
                          'estimate': r['actual_start'], 'title': r['title']})
        d = decisions.get(r['activity_id'])
        if d and d.get('choice'):
            r['status'] = d['choice']
            if d.get('actual_start'):
                r.update(actual_start=d['actual_start'], start_basis='human')
            r['review_state'] = 'human'
        r['planned_start'] = format_get(r['planned_s']) if r['planned_s'] is not None else ''
    for a in anchors:
        if a['anchor_id'] in matched_anchor or not planned:
            continue
        if a['basis'] == 'afj_index' and a['level'] == 2:
            rows.append({'activity_id': f'A15-UNPLANNED-{a["anchor_id"]}', 'title': a['label'], 'vehicle': a['vehicle'],
                         'status': 'unplanned', 'actual_start': a['get'], 'start_basis': a['basis'],
                         'evidence': a['evidence'], 'review_state': 'proposed'})
    rows.sort(key=lambda r: (parse_get(r.get('actual_start') or r.get('planned_start') or '') or 0, r['activity_id']))
    write_tsv(FLIGHT_PLAN / 'fp_actual.tsv', ACTUAL_FIELDS, rows)
    write_tsv(WORK / 'fp_actuals_queue.tsv', ('activity_id', 'issue', 'planned', 'estimate', 'title', 'choice',
                                              'actual_start', 'reviewer', 'date', 'note', 'methods'),
              queue + [{'activity_id': '-', 'issue': i} for i in issues])
    if not planned:
        print('fp_activities/fp_events are empty (structure step pending OCR): anchors only; fp_actual.tsv empty')
    counts = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    print(f'actual: {len(rows)} rows {counts}; slip issues {len(issues)}; review queue {len(queue)}')
    import md_fp_export
    data = md_fp_export.export()
    print(f'flightPlanData.json: {len(data["pages"])} pages, {sum(len(p["activities"]) for p in data["phases"])} activities')
    return 0
