"""Flight plan step 5: structure OCR blocks into activities, events, coverage, tables and glossary (§3.4–3.5).

Rules label what they can (ruler labels, DAP codes, page furniture, checklist
references, PAD fields, boxed event labels). GLM-5.3-Flash on HGX (or Strata)
labels the rest from text only: block IDs, kinds, categories and groups. The
model never supplies text or times; code validates every answer and builds
titles from block readings and times from printed values or the ruler.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
from html.parser import HTMLParser

from md_common import AI_CACHE, REVIEW, WORK, format_get, parse_get, read_tsv, write_tsv
from md_flightplan import FLIGHT_PLAN, page_sort_key

KINDS = ('activity', 'event_marker', 'uplink', 'call', 'pad_field', 'note', 'checklist_ref', 'procedure_step',
         'code', 'annotation', 'noise')
CATEGORIES = ('sleep', 'eat', 'exercise', 'burn', 'navigation', 'photography', 'sim_bay', 'eva', 'lrv', 'alsep',
              'systems', 'comm', 'tv', 'transfer', 'housekeeping', 'none')
PROMPT_VERSION = 'fp-structure-v1'
CACHE = AI_CACHE / 'flightplan_structure.jsonl'
FP_DIAG = AI_CACHE / 'diagnostics_flightplan'

BLOCK_FIELDS = ('block_id', 'page_id', 'witness', 'bbox_px', 'bbox_norm', 'column', 'origin', 'text', 'readings',
                'kind', 'category', 'activity_group', 'continues_from_previous_page', 'expansion', 'confidence',
                'label_basis', 'get_planned', 'get_basis', 'get_tolerance_s')
ACTIVITY_FIELDS = ('activity_id', 'title', 'category', 'crew', 'vehicle', 'planned_start', 'planned_end',
                   'planned_start_s', 'planned_end_s', 'start_basis', 'parent_id', 'phase', 'block_ids', 'page_ids',
                   'procedure_page_ids')
EVENT_FIELDS = ('event_id', 'name', 'planned_get', 'planned_get_s', 'get_basis', 'vehicle', 'source', 'page_id',
                'activity_id', 'tig', 'bt', 'dv', 'ullage', 'ha', 'hp', 'raw')
COVERAGE_FIELDS = ('kind', 'vehicle', 'start', 'end', 'start_s', 'end_s', 'page_id', 'source')
TABLE_FIELDS = ('table_id', 'row', 'col', 'text', 'bbox', 'page_id')
GLOSSARY_FIELDS = ('term', 'expansion', 'page_id')

PAD_RE = re.compile(r'^(TIG|BT|ΔV[RTC]?|DV[RTC]?|ULLAGE|HA|HP|GET|NOUN|N\d\d|R\d|PITCH|YAW|ROLL|WT|GETI)\b\s*[:=]?', re.I)
CHECKLIST_RE = re.compile(r'\bPAGE\s+[A-Z]{1,3}/\d+-\d+|\bCHECKLIST\b|\bCUE CARD\b', re.I)
DAP_RE = re.compile(r'^\(?[\dX]{5}\)?(\s*\(?[\dX]{5}\)?)*$')
RULER_LABEL_RE = re.compile(r'^(\d{1,3})?:\d{2}$')
PRINTED_TIME_RE = re.compile(r'\b(?:GET|TIG|GETI|T/D|TD)\s*[:=]?\s*(\d{1,3}:\d{2}:\d{2}(?:\.\d+)?)', re.I)
ANY_TIME_RE = re.compile(r'\d{1,3}:\d{2}:\d{2}(?:\.\d+)?')
EVENT_WORDS = ('PDI', 'TOUCHDOWN', 'LIFTOFF', 'LIFT-OFF', 'UNDOCK', 'SEP', 'DOCKING', 'DOCK', 'JETT', 'JETTISON',
               'LOI', 'DOI', 'TEI', 'TLI', 'TPI', 'CSI', 'CDH', 'MCC', 'CIRC', 'AOS', 'LOS', 'INSERTION', 'EI',
               'SPLASHDOWN', 'SPS', 'BURN', 'EVA', 'EGRESS', 'INGRESS', 'DEPRESS', 'REPRESS', 'PLANE CHANGE', 'LAUNCH')


# --------------------------------------------------------------- glossary
def parse_glossary(blocks: list[dict]) -> list[dict]:
    """Abbreviation pages xi–xxii: ``TERM   expansion`` lines; symbols (xxv) kept by text only."""
    out, seen = [], set()
    for b in blocks:
        for line in re.split(r'\n|(?<=[a-z\)])\s{2,}(?=[A-Z0-9])', b['text']):
            m = re.match(r'^([A-Z0-9ΔΣ][A-Z0-9/&\-\.ΔΣ\(\)]{0,11})\s+(?:-\s+)?([A-Za-z].{2,})$', line.strip())
            if not m:
                continue
            term, expansion = m.group(1).strip(), m.group(2).strip()
            if term.isdigit() or (term, expansion) in seen:
                continue
            seen.add((term, expansion))
            out.append({'term': term, 'expansion': expansion, 'page_id': b['page_id']})
    return out


def glossary_for(text: str, glossary: dict[str, list[str]]) -> dict[str, list[str]]:
    words = set(re.findall(r'[A-Z0-9Δ][A-Z0-9/\-Δ]*', text.upper()))
    return {t: glossary[t] for t in sorted(words) if t in glossary}


# ------------------------------------------------------------- rule labels
def in_box(bbox, boxes) -> tuple | None:
    x0, y0, x1, y1 = bbox
    for b in boxes:
        if b[0] - 3 <= x0 and b[1] - 3 <= y0 and x1 <= b[2] + 3 and y1 <= b[3] + 3 and (b[2] - b[0]) < 0.6 * 1200:
            return b
    return None


def rule_label(block: dict, page: dict, geometry: dict, boxes) -> tuple[str, str] | None:
    """(kind, basis) when a deterministic rule decides, else None."""
    text = block['text'].strip()
    y0 = float(block['bbox_px'].split(',')[1])
    height = float(geometry.get('height') or 1)
    footer_y = float(geometry.get('footer_y') or height * 0.9)
    if not text or y0 >= footer_y - 2 or y0 < 0.045 * height and 'FLIGHT PLAN' in text.upper():
        return 'noise', 'page furniture'
    if block['column'] == 'ruler':
        if DAP_RE.match(text.replace('O', '0')):
            return 'code', 'DAP configuration word'
        if RULER_LABEL_RE.match(text.replace('O', '0')) or re.search(r'\d{4}\s*CDT', text):
            return 'noise', 'ruler label'
    if DAP_RE.match(text.replace('O', '0')):
        return 'code', 'DAP configuration word'
    if CHECKLIST_RE.search(text) and len(text.split()) <= 8:
        return 'checklist_ref', 'checklist reference'
    if PAD_RE.match(text):
        return 'pad_field', 'PAD field'
    bbox = tuple(float(v) for v in block['bbox_px'].split(','))
    if in_box(bbox, boxes) and text.upper() == text and len(text.split()) <= 6 and any(
            w in text.upper() for w in EVENT_WORDS):
        return 'event_marker', 'boxed event label'
    if block['column'] == 'MCC-H' and re.match(r'^(UPLINK|UPDATE)\b', text, re.I):
        return 'uplink', 'MCC-H heading'
    if block['column'] == 'MCC-H' and re.search(r'\bGO\s*/\s*NO[\s-]*GO\b|\bSTAY\b', text, re.I):
        return 'call', 'Go/No-Go call'
    return None


# ------------------------------------------------------------ AI contract
def response_format(block_ids: list[str]) -> dict:
    row = {'type': 'object', 'properties': {
        'block_id': {'type': 'string', 'enum': block_ids},
        'kind': {'type': 'string', 'enum': list(KINDS)},
        'category': {'type': 'string', 'enum': list(CATEGORIES)},
        'activity_group': {'type': 'string', 'enum': block_ids},
        'continues_from_previous_page': {'type': 'boolean'},
        'expansion': {'type': 'string'},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
    }, 'required': ['block_id', 'kind', 'category', 'activity_group', 'continues_from_previous_page', 'expansion',
                    'confidence'], 'additionalProperties': False}
    return {'type': 'json_schema', 'json_schema': {'name': 'flight_plan_blocks', 'strict': True, 'schema': {
        'type': 'object', 'properties': {'rows': {'type': 'array', 'items': row, 'minItems': len(block_ids),
                                                  'maxItems': len(block_ids)}},
        'required': ['rows'], 'additionalProperties': False}}}


SYSTEM = ('You structure OCR blocks from the Apollo 15 Final Flight Plan (21 June 1971). You label blocks only. '
          'You never write, correct or invent text, and you never give times. Return only the requested JSON.')
GUIDE = """Kinds: activity (a crew task in a timeline column), event_marker (a boxed or ruled key event such as PDI,
LM LUNAR TOUCHDOWN), uplink (MCC-H uplink/update heading or item), call (MCC-H Go/No-Go, Stay/No-Stay), pad_field
(a field of a PAD or maneuver form, e.g. TIG, BT), note (explanatory text), checklist_ref (reference to a checklist page),
procedure_step (a step inside a procedure), code (configuration words such as (22112)), annotation (hand-written
addition: a value written into an empty PAD box or differing from the printed value), noise (OCR debris).
Categories: sleep, eat, exercise, burn, navigation, photography, sim_bay, eva, lrv, alsep, systems, comm, tv, transfer,
housekeeping, none.
activity_group: the block_id of the first block of the activity this block belongs to (itself if it starts one).
Blocks of one group must be consecutive within one column. continues_from_previous_page: true only when this
block's activity carries on from the "previous_page_tail" activities. expansion: a short glossary expansion for display
taken from "glossary", or "" ."""


def build_prompt(page: dict, footer: dict, items: list[dict], glossary: dict, tail: list[dict]) -> str:
    payload = {'page': {'page_id': page['page_id'], 'page_type': page.get('page_type'), 'vehicle': page.get('vehicle'),
                        'footer': footer}, 'glossary': glossary, 'previous_page_tail': tail, 'blocks': items}
    return GUIDE + '\n\nLabel every item of "blocks", once each, in order. Return {"rows": [...]}.\n\n' + json.dumps(
        payload, ensure_ascii=False, indent=0)


def validate_rows(rows, items: list[dict], glossary: dict) -> tuple[dict[str, dict], list[str]]:
    """Check an AI answer. Returns (accepted rows by block_id, errors). Rejected rows are absent."""
    errors, out = [], {}
    by_id = {i['block_id']: i for i in items}
    order = [i['block_id'] for i in items]
    if not isinstance(rows, list):
        return {}, ['rows is not a list']
    seen = {}
    for r in rows:
        if not isinstance(r, dict):
            errors.append(f'row is not an object: {r!r}')
            continue
        bid = r.get('block_id')
        if bid not in by_id:
            errors.append(f'unknown block_id {bid!r}')
            continue
        seen[bid] = seen.get(bid, 0) + 1
    for bid, count in seen.items():
        if count > 1:
            errors.append(f'block_id {bid} appears {count} times')
    for bid in order:
        if bid not in seen:
            errors.append(f'block_id {bid} missing')
    allowed_expansions = {e for v in glossary.values() for e in v}
    for r in rows:
        if not isinstance(r, dict) or r.get('block_id') not in by_id or seen.get(r['block_id']) != 1:
            continue
        bid, item = r['block_id'], by_id[r['block_id']]
        if r.get('kind') not in KINDS or r.get('category') not in CATEGORIES:
            errors.append(f'{bid}: bad enum')
            continue
        group = r.get('activity_group')
        if group not in by_id:
            errors.append(f'{bid}: unknown activity_group {group!r}')
            continue
        try:
            conf = float(r.get('confidence'))
        except (TypeError, ValueError):
            errors.append(f'{bid}: bad confidence')
            continue
        expansion = str(r.get('expansion') or '')
        # The model never supplies a time or new text: an expansion must come from the glossary.
        if ANY_TIME_RE.search(expansion) or re.search(r'\d{1,3}:\d{2}', expansion):
            errors.append(f'{bid}: expansion supplies a time')
            continue
        if expansion and expansion not in allowed_expansions:
            errors.append(f'{bid}: expansion not from the glossary')
            continue
        for key in r:
            if key not in ('block_id', 'kind', 'category', 'activity_group', 'continues_from_previous_page',
                           'expansion', 'confidence'):
                errors.append(f'{bid}: unexpected field {key}')
                break
        else:
            out[bid] = {'kind': r['kind'], 'category': r['category'], 'activity_group': group,
                        'continues_from_previous_page': bool(r.get('continues_from_previous_page')),
                        'expansion': expansion, 'confidence': round(conf, 3)}
    # A group must be contiguous in its column.
    for bid, row in list(out.items()):
        group = row['activity_group']
        column = by_id[bid]['column']
        if by_id[group]['column'] != column:
            errors.append(f'{bid}: group {group} is in another column')
            out.pop(bid)
            continue
        lane = [b for b in order if by_id[b]['column'] == column]
        a, b = sorted((lane.index(group), lane.index(bid)))
        between = lane[a:b + 1]
        if any(out.get(x, {}).get('activity_group', group) != group for x in between):
            errors.append(f'{bid}: group {group} is not contiguous in column {column}')
            out.pop(bid)
    return out, errors


class StructureCache:
    def __init__(self):
        self.rows = {}
        if CACHE.is_file():
            for line in CACHE.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.rows[r['key']] = r

    def save(self):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_name(CACHE.name + '.tmp')
        tmp.write_text(''.join(json.dumps(self.rows[k], ensure_ascii=False, sort_keys=True) + '\n'
                               for k in sorted(self.rows)), encoding='utf-8')
        tmp.replace(CACHE)


def input_key(prompt: str, backend: str, model: str) -> str:
    return hashlib.sha256((PROMPT_VERSION + SYSTEM + backend + model + prompt).encode('utf-8')).hexdigest()[:24]


def ai_label(backend: str, model: str, page: dict, footer: dict, items: list[dict], glossary: dict, tail,
             cache: StructureCache, budget: list[int]) -> tuple[dict, list[str]]:
    import md_ai
    prompt = build_prompt(page, footer, items, glossary, tail)
    key = input_key(prompt, backend, model)
    if key in cache.rows:
        return validate_rows(cache.rows[key]['rows'], items, glossary)
    if budget[0] <= 0:
        raise md_ai.BackendError('request budget exhausted; checkpoints retained')
    budget[0] -= 1
    schema = response_format([i['block_id'] for i in items])
    old_system = md_ai.SYSTEM
    md_ai.SYSTEM = SYSTEM
    try:
        content, stats = md_ai.stream(backend, prompt, schema)
    finally:
        md_ai.SYSTEM = old_system
    FP_DIAG.mkdir(parents=True, exist_ok=True)
    (FP_DIAG / f'{backend}_{key}.json').write_text(json.dumps(
        {'system': SYSTEM, 'prompt': prompt, 'response': content, 'stats': stats}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    if stats['finish_reason'] != 'stop':
        return {}, [f'finish_reason={stats["finish_reason"]}']
    try:
        rows = json.loads(content[content.find('{'):]).get('rows')
    except (json.JSONDecodeError, AttributeError) as exc:
        return {}, [f'invalid json: {exc}']
    accepted, errors = validate_rows(rows, items, glossary)
    if not errors:
        cache.rows[key] = {'key': key, 'backend': backend, 'model': model, 'prompt_version': PROMPT_VERSION,
                           'page_id': page['page_id'], 'rows': rows}
        cache.save()
    return accepted, errors


# ------------------------------------------------------------- HTML tables
class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_cell = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.cell, self.in_cell = [], True

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.in_cell = False
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def table_cells(markup: str) -> list[list[str]]:
    parser = _Table()
    parser.feed(html.unescape(markup) if '&lt;' in markup else markup)
    return parser.rows


# --------------------------------------------------------- burn PAD fields
def burn_fields(text: str) -> dict:
    out = {}
    for key, pattern in (('tig', r'TIG\s*[:=]?\s*(\d{1,3}:\d{2}:\d{2}(?:\.\d+)?)'),
                         ('bt', r'BT\s*[:=]?\s*([\d:]+(?:\s*MIN)?[\s,]*[\d.]*\s*(?:SEC)?)'),
                         ('dv', r'(?:ΔV[RTC]?|DV[RTC]?)\s*[:=]?\s*([\d,.]+\s*(?:FT/SEC|FPS)?)'),
                         ('ullage', r'ULLAGE\s*[:=]?\s*([^;]+?(?:SEC|$))'),
                         ('ha', r'\bHA\s*[:=]?\s*([\d.]+\s*(?:NM)?)'), ('hp', r'\bHP\s*[:=]?\s*([\d.]+\s*(?:NM)?)')):
        m = re.search(pattern, text, re.I)
        if m:
            out[key] = ' '.join(m.group(1).split()).rstrip(',')
    return out


def printed_get(text: str) -> int | None:
    m = PRINTED_TIME_RE.search(text.replace('O', '0'))
    if not m:
        return None
    value = m.group(1)
    whole = parse_get(value.split('.')[0])
    return whole


def slug(text: str) -> str:
    words = re.findall(r'[A-Z0-9]+', text.upper())[:4]
    return '-'.join(words) or 'X'


def hhmmss(seconds: int) -> str:
    return format_get(seconds).replace(':', '').rjust(7, '0')


# ---------------------------------------------------------------------- main
def main(args) -> int:
    pages = {p['page_id']: p for p in read_tsv(FLIGHT_PLAN / 'fp_pages.tsv')}
    geometry = {g['page_id']: g for g in read_tsv(FLIGHT_PLAN / 'fp_geometry.tsv')}
    footers = {f['page_id']: f for f in read_tsv(FLIGHT_PLAN / 'fp_footers.tsv')}
    ocr_blocks = read_tsv(FLIGHT_PLAN / 'fp_ocr_blocks.tsv')
    if not ocr_blocks:
        raise SystemExit('fp_ocr_blocks.tsv is empty; run `flightplan ocr` first (needs OXR)')
    boxes_by_page = {}
    from md_fp_geometry import RULE_FIELDS  # noqa: F401  (documented dependency)
    from md_fp_geometry import rules_path
    for r in read_tsv(rules_path()):
        if r['kind'] == 'box':
            boxes_by_page.setdefault(r['page_id'], []).append(tuple(int(r[k]) for k in ('x0', 'y0', 'x1', 'y1')))
    by_page = {}
    for b in ocr_blocks:
        by_page.setdefault(b['page_id'], []).append(b)

    glossary_rows = parse_glossary([b for b in ocr_blocks if b['page_id'] in
                                    ('xi', 'xii', 'xiii', 'xiv', 'xv', 'xvi', 'xvii', 'xviii', 'xix', 'xx', 'xxi',
                                     'xxii', 'xxv')])
    glossary = {}
    for g in glossary_rows:
        glossary.setdefault(g['term'], []).append(g['expansion'])
    write_tsv(FLIGHT_PLAN / 'fp_glossary.tsv', GLOSSARY_FIELDS, glossary_rows)

    decisions = {r['key']: r for r in read_tsv(REVIEW / 'fp_structure.tsv')}
    selected = [p.strip() for p in args.pages.split(',')] if args.pages else sorted(by_page, key=page_sort_key)
    model = None
    budget = [args.budget]
    cache = StructureCache()
    if not args.no_ai:
        import md_ai
        model = md_ai.probe(args.backend)
    out_blocks, queue, tables = [], [], []
    tails: dict[str, list[dict]] = {}
    stopped = None
    for page_id in sorted(by_page, key=page_sort_key):
        page = {**pages.get(page_id, {'page_id': page_id}), **{k: v for k, v in geometry.get(page_id, {}).items()
                                                              if k in ('page_type', 'vehicle')}}
        g = geometry.get(page_id, {})
        blocks = by_page[page_id]
        labels = {}
        for b in blocks:
            rule = rule_label(b, page, g, boxes_by_page.get(page_id, []))
            if rule:
                labels[b['block_id']] = {'kind': rule[0], 'category': 'none', 'activity_group': b['block_id'],
                                         'continues_from_previous_page': False, 'expansion': '', 'confidence': 1.0,
                                         'label_basis': 'rule: ' + rule[1]}
            # Hand-written annotation cue: a value in a PAD box that the independent ALSJ scan does not show.
            readings = json.loads(b['readings'] or '{}')
            if 'oxr_alsj' in readings and b['agreement'] == 'disagree' and ANY_TIME_RE.search(b['text']) and \
                    not ANY_TIME_RE.search(readings['oxr_alsj']):
                labels[b['block_id']] = {'kind': 'annotation', 'category': 'none', 'activity_group': b['block_id'],
                                         'continues_from_previous_page': False, 'expansion': '',
                                         'confidence': 0.8, 'label_basis': 'rule: absent from ALSJ scan'}
        if page.get('page_type') == 'table':
            for b in blocks:
                if b['type'] == 'Table' or '<td' in b['text']:
                    for r, cells in enumerate(table_cells(b['text'])):
                        for c, cell in enumerate(cells):
                            tables.append({'table_id': page_id, 'row': r, 'col': c, 'text': cell,
                                           'bbox': b['bbox_px'], 'page_id': page_id})
        pending = [b for b in blocks if b['block_id'] not in labels]
        if pending and page_id in selected and not args.no_ai and stopped is None:
            lane_tail = tails.get(page.get('vehicle') or '-', [])
            items = [{'block_id': b['block_id'], 'column': b['column'], 'bbox_px': b['bbox_px'], 'text': b['text'],
                      'get': format_get(int(b['get_planned'])) if b['get_planned'] else ''} for b in pending]
            gl = glossary_for(' '.join(b['text'] for b in pending), glossary)
            try:
                accepted, errors = ai_label(args.backend, model, page, footers.get(page_id, {}), items, gl,
                                            lane_tail, cache, budget)
                if errors:
                    for item in items:
                        if item['block_id'] in accepted:
                            continue
                        one, errs = ai_label(args.backend, model, page, footers.get(page_id, {}), [item],
                                             glossary_for(item['text'], glossary), lane_tail, cache, budget)
                        accepted.update(one)
                        if errs:
                            queue.append({'key': item['block_id'], 'page_id': page_id, 'issue': 'ai_rejected',
                                          'detail': '; '.join(errs)[:300], 'text': item['text']})
                for bid, row in accepted.items():
                    labels[bid] = {**row, 'label_basis': f'ai:{args.backend}:{model}'}
            except Exception as exc:  # budget, backend or HTTP: keep checkpoints and stop asking
                stopped = str(exc)
                print(f'  AI stopped at {page_id}: {exc}')
        for b in blocks:
            label = labels.get(b['block_id'])
            decision = decisions.get(b['block_id'])
            if decision and decision.get('kind'):
                label = {**(label or {}), 'kind': decision['kind'], 'category': decision.get('category') or
                         (label or {}).get('category', 'none'), 'label_basis': 'human'}
                label.setdefault('activity_group', b['block_id'])
            if label is None:
                label = {'kind': '', 'category': '', 'activity_group': '', 'label_basis': 'pending'}
            row = {**{k: b.get(k, '') for k in ('block_id', 'page_id', 'witness', 'bbox_px', 'bbox_norm', 'column',
                                                 'text', 'readings', 'get_planned', 'get_basis', 'get_tolerance_s')},
                   **{k: label.get(k, '') for k in ('kind', 'category', 'activity_group', 'expansion', 'confidence',
                                                     'label_basis')},
                   'continues_from_previous_page': 'yes' if label.get('continues_from_previous_page') else '',
                   'origin': 'annotation' if label.get('kind') == 'annotation' else 'printed'}
            pt = printed_get(b['text'])
            if pt is not None and row['origin'] == 'printed':
                ruler_s = int(b['get_planned']) if b['get_planned'] else None
                if ruler_s is None or abs(pt - ruler_s) <= 1800:
                    row.update(get_planned=pt, get_basis='printed', get_tolerance_s=1)
                else:  # a misread digit (e.g. 704:40:57 for 104:40:57): keep the ruler time and ask
                    queue.append({'key': b['block_id'], 'page_id': page_id, 'issue': 'printed_time_far_from_ruler',
                                  'detail': f'printed {format_get(pt)} vs ruler {format_get(ruler_s)}',
                                  'text': b['text']})
            out_blocks.append(row)
        tails[page.get('vehicle') or '-'] = [{'block_id': r['block_id'], 'text': r['text'], 'column': r['column']}
                                             for r in out_blocks if r['page_id'] == page_id and r['kind'] == 'activity'][-4:]
    write_tsv(FLIGHT_PLAN / 'fp_blocks.tsv', BLOCK_FIELDS, out_blocks)
    write_tsv(FLIGHT_PLAN / 'fp_tables.tsv', TABLE_FIELDS, tables)
    activities, events, coverage = build_layer1(out_blocks, pages, geometry, footers)
    write_tsv(FLIGHT_PLAN / 'fp_activities.tsv', ACTIVITY_FIELDS, activities)
    write_tsv(FLIGHT_PLAN / 'fp_events.tsv', EVENT_FIELDS, events)
    write_tsv(FLIGHT_PLAN / 'fp_coverage.tsv', COVERAGE_FIELDS, coverage)
    write_tsv(WORK / 'fp_structure_queue.tsv', ('key', 'page_id', 'issue', 'detail', 'text'), queue)
    pending = sum(1 for b in out_blocks if b['label_basis'] == 'pending')
    print(f'structure: {len(out_blocks)} blocks ({pending} pending), {len(activities)} activities, {len(events)} events, '
          f'{len(coverage)} coverage intervals, {len(glossary_rows)} glossary terms; queue {len(queue)}')
    import md_fp_assemble
    md_fp_assemble.assemble()
    return 1 if stopped or pending else 0


def crew_for(column: str, vehicle: str, page_id: str) -> str:
    if column in ('CDR', 'LMP', 'CMP'):
        return column
    if column == 'MCC-H':
        return 'MCC'
    n = int(page_id[2:]) if page_id.startswith('3-') else 0
    if vehicle == 'CSM' and 106 <= n <= 287:
        return 'CMP'
    if vehicle == 'LM':
        return 'CDR;LMP'
    return 'CDR;CMP;LMP'


def build_layer1(blocks: list[dict], pages: dict, geometry: dict, footers: dict):
    """Activities (merged across pages when continued), events with burn fields, MSFN coverage."""
    groups: dict[str, list[dict]] = {}
    for b in blocks:
        if b['kind'] in ('activity', 'procedure_step', 'uplink', 'call') and b['activity_group']:
            groups.setdefault(b['activity_group'], []).append(b)
    order = sorted(groups, key=lambda g: (page_sort_key(groups[g][0]['page_id']), groups[g][0]['block_id']))
    activities, last_in_lane, used_ids = [], {}, set()
    for gid in order:
        members = groups[gid]
        first = members[0]
        page = pages.get(first['page_id'], {})
        vehicle = (geometry.get(first['page_id'], {}).get('vehicle') or '-')
        lane = (vehicle, first['column'])
        start = int(first['get_planned']) if first['get_planned'] else None
        title = ' '.join(m['text'] for m in members if m['kind'] == 'activity') or first['text']
        if first['continues_from_previous_page'] == 'yes' and lane in last_in_lane:
            act = last_in_lane[lane]
            act['block_ids'] += ';' + ';'.join(m['block_id'] for m in members)
            if first['page_id'] not in act['page_ids'].split(';'):
                act['page_ids'] += ';' + first['page_id']
            continue
        if lane in last_in_lane and start is not None and not last_in_lane[lane]['planned_end_s']:
            last_in_lane[lane]['planned_end_s'] = start
            last_in_lane[lane]['planned_end'] = format_get(start)
        base = f'A15-FP-{hhmmss(start) if start is not None else "0000000"}-{vehicle}-{slug(title)}'
        activity_id, k = base, 2
        while activity_id in used_ids:
            activity_id, k = f'{base}-{k}', k + 1
        used_ids.add(activity_id)
        refs = sorted(set(re.findall(r'PAGE\s+([1-6]-\d{1,3})', ' '.join(m['text'] for m in members), re.I)))
        act = {'activity_id': activity_id, 'title': title, 'category': first['category'],
               'crew': crew_for(first['column'], vehicle, first['page_id']), 'vehicle': vehicle,
               'planned_start': format_get(start) if start is not None else '', 'planned_start_s': start,
               'planned_end': '', 'planned_end_s': '', 'start_basis': first['get_basis'],
               'parent_id': 'A15-FP-PHASE-' + slug(page.get('phase') or 'none'), 'phase': page.get('phase', ''),
               'block_ids': ';'.join(m['block_id'] for m in members), 'page_ids': first['page_id'],
               'procedure_page_ids': ';'.join(refs)}
        activities.append(act)
        last_in_lane[lane] = act
    events = []
    by_page = {}
    for b in blocks:
        by_page.setdefault(b['page_id'], []).append(b)
    for page_id, bl in by_page.items():
        vehicle = geometry.get(page_id, {}).get('vehicle') or '-'
        pad_text = ' '.join(b['text'] for b in bl if b['kind'] == 'pad_field')
        for b in bl:
            if b['kind'] != 'event_marker':
                continue
            get_s = int(b['get_planned']) if b['get_planned'] else None
            basis = b['get_basis']
            fields = burn_fields(pad_text) if re.search(r'PDI|LOI|DOI|TEI|TLI|BURN|MCC|CIRC|TPI|CSI|SPS', b['text']) else {}
            if fields.get('tig'):
                tig = parse_get(fields['tig'].split('.')[0])
                if tig is not None and get_s is not None and abs(tig - get_s) < 1800:
                    get_s, basis = tig, 'printed'
            events.append({'event_id': f'{b["block_id"]}', 'name': b['text'], 'planned_get': format_get(get_s),
                           'planned_get_s': get_s, 'get_basis': basis, 'vehicle': vehicle, 'source': b['block_id'],
                           'page_id': page_id, 'raw': b['text'], **fields})
    coverage = []
    for page_id, bl in by_page.items():
        for b in bl:
            if re.fullmatch(r'M\s*S\s*F\s*N', b['text'].upper().strip()):
                coverage.append({'kind': 'msfn', 'vehicle': geometry.get(page_id, {}).get('vehicle') or '-',
                                 'start': '', 'end': '', 'page_id': page_id, 'source': b['block_id']})
    return activities, events, coverage
