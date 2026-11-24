"""Flight plan step 3: rules, boxes, columns, ruler strips, ruler fit and page type (§3.1–3.2).

Geometry is found with OpenCV on the default witness (the AFJ GIF) before any
OCR. A timeline page has one or more *strips*, each with its own GET ruler
(3-143 has 111:00–111:30 on the left and 111:30–112:00 on the right). Strips
are located from the time-label column and the vertical rule beside it; ticks
are counted on whichever side of the rule they are drawn.

The ruler fit needs label text. Before OXR exists it uses the Virtual AGC text
layer (the same scan as the AFJ PDF, mapped onto GIF pixels through the image
placement) and the footer time range; step 4 refits it from OXR readings.
Five-digit DAP configuration words beside the ruler are never read as times.
"""
from __future__ import annotations

import json
import re

import numpy as np

from md_common import LOCAL, RAW_ROOT, WORK, parse_get, read_tsv, write_tsv
from md_flightplan import FLIGHT_PLAN, FP_RAW, VAGC_PDF, page_sort_key

GEOMETRY_FIELDS = ('page_id', 'witness', 'width', 'height', 'ink_fraction', 'page_type', 'page_type_basis',
                   'vehicle', 'strips', 'columns', 'footer_y', 'footer_range', 'cdt_top', 'cdt_expected',
                   'cdt_check', 'header_tokens')
RULE_FIELDS = ('page_id', 'witness', 'kind', 'x0', 'y0', 'x1', 'y1')
RULER_FIELDS = ('page_id', 'witness', 'source', 'strip', 'y_px', 'label', 'get_seconds', 'slope_s_per_px',
                'intercept_s', 'residual_s', 'inlier', 'tick_s', 'fit_rms_s', 'fit_max_s', 'fit_basis')
LAUNCH_GMT_S = 13 * 3600 + 34 * 60
CDT_OFFSET_S = -5 * 3600


def rules_path():
    """Line and box geometry: cheap to regenerate, so kept out of Git."""
    return LOCAL / 'flight_plan' / 'fp_rules.tsv'


# ------------------------------------------------------------------ helpers
def ink(gray: np.ndarray) -> np.ndarray:
    import cv2
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return binary


def line_segments(binary: np.ndarray, vertical: bool, min_len: int, max_thick: int = 9) -> list[tuple]:
    import cv2
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len) if vertical else (min_len, 1))
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    n, _, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if vertical and w <= max_thick and h >= min_len:
            out.append((int(x), int(y), int(x + w - 1), int(y + h - 1)))
        elif not vertical and h <= max_thick and w >= min_len:
            out.append((int(x), int(y), int(x + w - 1), int(y + h - 1)))
    return sorted(out)


def boxes(binary: np.ndarray, hsegs, vsegs) -> list[tuple]:
    """Closed rectangles drawn with rules (checklist boxes, PAD forms, footer cells)."""
    import cv2
    mask = np.zeros_like(binary)
    for x0, y0, x1, y1 in list(hsegs) + list(vsegs):
        mask[y0:y1 + 1, x0:x1 + 1] = 255
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < 20 or h < 12:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.contourArea(c) > 0.6 * w * h:
            out.append((int(x), int(y), int(x + w - 1), int(y + h - 1)))
    return sorted(set(out))


def ticks_on(binary: np.ndarray, x0: int, x1: int, y0: int, y1: int, side: str) -> list[tuple[int, int]]:
    """(y, length) of short horizontal ticks attached to one side of a vertical rule."""
    out, run = [], []
    for y in range(y0, y1 + 1):
        row = binary[y, max(0, x0 - 40):x0][::-1] if side == 'left' else binary[y, x1 + 1:x1 + 41]
        length = 0
        for v in row > 0:
            if not v:
                break
            length += 1
        if 5 <= length <= 38:
            run.append((y, length))
        elif run:
            out.append((int(round(np.mean([r[0] for r in run]))), max(r[1] for r in run)))
            run = []
    if run:
        out.append((int(round(np.mean([r[0] for r in run]))), max(r[1] for r in run)))
    # Hatched bars (MSFN stripes) give ticks every few pixels: not a ruler.
    gaps = np.diff([t[0] for t in out])
    if len(gaps) and np.median(gaps) < 6:
        return []
    return out


def rule_ticks(binary, seg) -> tuple[list[tuple[int, int]], str]:
    x0, y0, x1, y1 = seg
    left, right = ticks_on(binary, x0, x1, y0, y1, 'left'), ticks_on(binary, x0, x1, y0, y1, 'right')
    return (left, 'left') if len(left) > len(right) else (right, 'right')


# ------------------------------------------------------------- ruler labels
LABEL_FULL = re.compile(r'^(\d{1,3}):(\d{2})$')
LABEL_MIN = re.compile(r'^:(\d{2})$')
DAP_CODE = re.compile(r'^\(?[\dX]{5}\)?$')


def clean_token(token: str) -> str:
    return (token.strip().replace('O', '0').replace('o', '0').replace('l', '1').replace('I', '1')
            .replace(';', ':').rstrip(',.-—'))


def label_value(text: str) -> tuple[str, int | None, int] | None:
    """('full', hour, minute) or ('min', None, minute) for a ruler label; None otherwise (codes, times with seconds)."""
    raw = text.strip()
    if DAP_CODE.match(raw.replace('O', '0')):
        return None
    token = clean_token(raw)
    m = LABEL_FULL.match(token)
    if m and int(m.group(2)) < 60:
        return 'full', int(m.group(1)), int(m.group(2))
    m = LABEL_MIN.match(token)
    if m and int(m.group(1)) < 60:
        return 'min', None, int(m.group(1))
    return None


def strip_clusters(tokens: list[tuple], height: int, footer_y: float | None) -> list[dict]:
    """Columns of ruler labels: tokens with label values, clustered by right edge (x1 within 45 px)."""
    limit = footer_y or 0.9 * height
    cands = [(t, label_value(t[3])) for t in tokens if t[1] < limit and t[1] > 0.04 * height]
    cands = [(t, v) for t, v in cands if v]
    clusters = []
    for t, v in sorted(cands, key=lambda c: c[0][2]):
        for c in clusters:
            if abs(np.median(c['x1']) - t[2]) <= 45:
                c['items'].append((t, v))
                c['x1'].append(t[2])
                break
        else:
            clusters.append({'items': [(t, v)], 'x1': [t[2]]})
    out = []
    for c in clusters:
        fulls = [v for _, v in c['items'] if v[0] == 'full']
        if len(c['items']) >= 2 and fulls:
            out.append({'label_x1': float(np.median(c['x1'])), 'items': sorted(c['items'], key=lambda i: i[0][1])})
    return sorted(out, key=lambda c: c['label_x1'])


def ruler_labels(items: list[tuple], start_hour: int | None) -> list[tuple[float, str, int]]:
    """Cluster items (token, value) top to bottom -> (y, label, GET seconds). ``:MM`` takes the running hour."""
    out, hour = [], start_hour
    for t, (kind, h, minute) in items:
        if kind == 'full':
            hour = h
        elif hour is None:
            continue
        seconds = hour * 3600 + minute * 60
        if out and seconds <= out[-1][2]:
            if kind == 'min':
                hour += 1
                seconds += 3600
            else:
                continue  # a full label that steps backwards is a misreading; keep it out of the fit
        out.append((t[1], t[3], seconds))
    return out


def fit_ruler(points: list[tuple[float, int]], tolerance_s: float = 45.0) -> dict | None:
    """Robust linear fit GET = intercept + slope*y. Needs two points; keeps the largest consistent set."""
    if len(points) < 2:
        return None
    pts = np.array(points, dtype=float)
    best = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            dy = pts[j, 0] - pts[i, 0]
            if abs(dy) < 5:
                continue
            slope = (pts[j, 1] - pts[i, 1]) / dy
            if slope <= 0:
                continue
            intercept = pts[i, 1] - slope * pts[i, 0]
            resid = np.abs(pts[:, 1] - (intercept + slope * pts[:, 0]))
            inl = resid <= tolerance_s
            score = (int(inl.sum()), -float(resid[inl].sum()))
            if best is None or score > best[0]:
                best = (score, inl)
    if best is None or best[0][0] < 2:
        return None
    inl = best[1]
    slope, intercept = np.polyfit(pts[inl, 0], pts[inl, 1], 1)
    if slope <= 0:
        return None
    resid = pts[:, 1] - (intercept + slope * pts[:, 0])
    return {'slope': float(slope), 'intercept': float(intercept), 'inliers': inl.tolist(),
            'residuals': resid.tolist(), 'rms': float(np.sqrt(np.mean(resid[inl] ** 2))),
            'max': float(np.max(np.abs(resid[inl])))}


def cdt_expected(get_start: int) -> str:
    seconds = (LAUNCH_GMT_S + CDT_OFFSET_S + get_start) % 86400
    return f'{seconds // 3600:02d}{seconds % 3600 // 60:02d}'


FOOTER_TIME = re.compile(r'(\d{1,3})\s*:\s*(\d{2})\s*-?\s*(\d{1,3})\s*:\s*(\d{2})')


def footer_range(text: str) -> tuple[int, int] | None:
    """The footer TIME field ``104:00 - 105:00``: the last time pair in the footer text (OCR O/D read as 0)."""
    clean = re.sub(r'(?<=\d)[OD]|[OD](?=\d)', '0', text or '')
    matches = list(FOOTER_TIME.finditer(clean))
    if not matches:
        return None
    h0, m0, h1, m1 = (int(v) for v in matches[-1].groups())
    a, b = h0 * 3600 + m0 * 60, h1 * 3600 + m1 * 60
    if m0 >= 60 or m1 >= 60 or b <= a or b - a > 6 * 3600:
        return None
    return a, b


# ------------------------------------------------------------------ page type
def classify(page: dict, geometry: dict, header: str) -> tuple[str, str, str]:
    """(page_type, basis, vehicle) from chapter, title, header tokens and strips. Basis notes rule conflicts."""
    page_id, chapter = page['page_id'], page['chapter']
    title = (page.get('index_title') or '').lower()
    if page['status'] == 'blank' or title.startswith('blank') or geometry['ink_fraction'] < 0.004:
        return 'blank', 'index title' if title.startswith('blank') else 'ink fraction', '-'
    if chapter == 'front':
        return 'front_matter', 'chapter', '-'
    if chapter == '1':
        return 'notes', 'chapter', '-'
    if chapter == '2':
        return 'table', 'chapter', '-'
    if chapter == '5':
        return 'summary_timeline', 'chapter', 'both'
    timeline = bool(geometry.get('strips')) or bool(re.match(r'(csm |lm )?\d{2,3}(:\d{2})? hours', title))
    head = header.upper()
    lm_header = ('CDR' in head and 'LMP' in head) or bool(re.search(r'\bLM\s+FL', head))
    csm_header = bool(re.search(r'\bCSM\s+FL', head))
    n = int(page_id[2:]) if chapter == '3' else 0
    lm_expected = 106 <= n <= 286 and n % 2 == 0
    if not timeline:
        return 'procedure', 'no ruler labels; index title without hour', '-'
    basis = ('strips' if geometry.get('strips') else 'index title hour')
    if lm_header or lm_expected:
        if (lm_header and not lm_expected) or (csm_header and lm_expected):
            return 'lm_timeline', f'rules_disagree: header {head[:40]!r}, page number parity', 'LM'
        return 'lm_timeline', basis + ('; header' if lm_header else '; page number'), 'LM'
    if lm_header is False and csm_header is False and 106 <= n <= 287:
        basis += '; page number'
    return 'csm_timeline', basis, 'CSM'


# ------------------------------------------------------------- text tokens
def vagc_tokens(witness_rows: list[dict]) -> dict[str, list[tuple]]:
    """Virtual AGC words mapped to AFJ GIF pixels: page_id -> [(x0, y_centre, x1, text)]."""
    import pymupdf
    from md_fp_pages import page_words_px
    path = RAW_ROOT / VAGC_PDF
    if not path.is_file():
        return {}
    pages = {}
    for r in witness_rows:
        if r['witness'] == 'vagc_pdf' and r.get('page_id') and r['match'] in ('pixel', 'phash'):
            pages.setdefault(r['page_id'], int(r['key']))
    sizes = {r['page_id']: (int(r['width']), int(r['height'])) for r in witness_rows if r['witness'] == 'afj_gif'}
    out = {}
    with pymupdf.open(path) as doc:
        for page_id, number in pages.items():
            if page_id not in sizes:
                continue
            words, _ = page_words_px(doc[number - 1], sizes[page_id])
            out[page_id] = [(x0, (y0 + y1) / 2, x1, w) for x0, y0, x1, y1, w in words]
    return out


def header_text(tokens: list[tuple], height: int) -> str:
    return ' '.join(t[3] for t in sorted(tokens, key=lambda t: (t[1], t[0])) if t[1] < 0.12 * height)


def footer_text(tokens: list[tuple], footer_y: int | None, height: int) -> str:
    y = footer_y if footer_y else 0.85 * height
    return ' '.join(t[3] for t in sorted(tokens, key=lambda t: (round(t[1] / 6), t[0])) if t[1] >= y - 4)


# ---------------------------------------------------------------- analysis
def analyse(gray: np.ndarray) -> dict:
    binary = ink(gray)
    h, w = binary.shape
    vsegs = line_segments(binary, True, max(40, h // 10))
    hsegs = line_segments(binary, False, max(30, w // 30))
    long_h = [s for s in hsegs if s[2] - s[0] > 0.3 * w and s[1] > 0.7 * h]
    footer_y = min((s[1] for s in long_h), default=None)
    columns = sorted({s[0] for s in vsegs if (s[3] - s[1]) > 0.5 * h and (footer_y is None or s[1] < footer_y)})
    return {'binary': binary, 'width': w, 'height': h, 'ink_fraction': float((binary > 0).mean()),
            'vsegs': vsegs, 'hsegs': hsegs, 'boxes': boxes(binary, hsegs, vsegs), 'footer_y': footer_y,
            'columns': columns}


def find_strips(g: dict, tokens: list[tuple], frange: tuple[int, int] | None) -> list[dict]:
    """Ruler strips from label columns; each gets its rule, ticks and a fit (labels plus the footer ends)."""
    clusters = strip_clusters(tokens, g['height'], g['footer_y'])
    long_v = [s for s in g['vsegs'] if (s[3] - s[1]) > 0.35 * g['height']]
    strips = []
    for k, c in enumerate(clusters):
        x = c['label_x1']
        # The ruler is the rule beside the label column with the most ticks (the nearest one when none has ticks).
        near = [(rule_ticks(g['binary'], s), s) for s in long_v if -100 <= s[0] - x <= 60]
        near.sort(key=lambda n: (-len(n[0][0]), abs(n[1][0] - x)))
        seg = near[0][1] if near else None
        ticks, side = near[0][0] if near else ([], '')
        tick_px = float(np.median(np.diff([t[0] for t in ticks]))) if len(ticks) > 2 else 0.0
        strips.append({'strip': k, 'label_x1': round(x, 1), 'rule_x': seg[0] if seg else None,
                       'y0': seg[1] if seg else None, 'y1': seg[3] if seg else None, 'tick_side': side,
                       'ticks': len(ticks), 'tick_px': round(tick_px, 2), 'items': c['items']})
    n = len(strips)
    for k, s in enumerate(strips):
        start_hour = None
        if frange:
            start = frange[0] + k * (frange[1] - frange[0]) // max(n, 1)
            start_hour = start // 3600
        labels = ruler_labels(s.pop('items'), start_hour)
        points = [(y, sec) for y, _, sec in labels]
        ends = []
        if frange and s['y0'] is not None:
            span = (frange[1] - frange[0]) / max(n, 1)
            ends = [(s['y0'], frange[0] + k * span), (s['y1'], frange[0] + (k + 1) * span)]
        fit = fit_ruler(points + ends) if ends else fit_ruler(points)
        basis = ('labels' if points else '') + ('+footer ends' if ends and fit else '')
        if fit is None and points:
            fit, basis = fit_ruler(points), 'labels'
        s.update(labels=labels, ends=ends, fit=fit, fit_basis=basis.strip('+'))
    return strips


def strip_json(strips: list[dict]) -> str:
    keep = []
    for s in strips:
        fit = s['fit']
        keep.append({k: s[k] for k in ('strip', 'label_x1', 'rule_x', 'y0', 'y1', 'tick_side', 'ticks', 'tick_px')} |
                    {'slope': round(fit['slope'], 5) if fit else None,
                     'intercept': round(fit['intercept'], 1) if fit else None,
                     'rms_s': round(fit['rms'], 1) if fit else None, 'basis': s['fit_basis']})
    return json.dumps(keep, sort_keys=True)


def ruler_rows(page_id: str, witness: str, source: str, strip: dict) -> list[dict]:
    fit = strip['fit']
    rows = []
    points = [(y, label, sec) for y, label, sec in strip['labels']] + [(y, '<rule end>', sec) for y, sec in strip['ends']]
    tick_s = round(fit['slope'] * strip['tick_px'], 1) if fit and strip['tick_px'] else ''
    for k, (y, label, seconds) in enumerate(points):
        resid = (seconds - (fit['intercept'] + fit['slope'] * y)) if fit else None
        rows.append({'page_id': page_id, 'witness': witness, 'source': source if label != '<rule end>' else f'{source}_footer',
                     'strip': strip['strip'], 'y_px': round(y, 1), 'label': label, 'get_seconds': int(seconds),
                     'slope_s_per_px': round(fit['slope'], 5) if fit else '',
                     'intercept_s': round(fit['intercept'], 1) if fit else '',
                     'residual_s': round(resid, 1) if resid is not None else '',
                     'inlier': ('yes' if abs(resid) <= 45 else 'no') if resid is not None else '',
                     'tick_s': tick_s, 'fit_rms_s': round(fit['rms'], 1) if fit else '',
                     'fit_max_s': round(fit['max'], 1) if fit else '', 'fit_basis': strip['fit_basis']})
    return rows


def strip_for_x(strips: list[dict], x: float) -> dict | None:
    """The strip whose label column is the last one left of (or at) ``x``."""
    chosen = None
    for s in sorted(strips, key=lambda s: s['label_x1']):
        if x >= s['label_x1'] - 90:
            chosen = s
    return chosen or (strips[0] if strips else None)


# ---------------------------------------------------------------------- main
def main(args) -> int:
    from md_fp_pages import load_gray
    pages = {p['page_id']: p for p in read_tsv(FLIGHT_PLAN / 'fp_pages.tsv')}
    if not pages:
        raise SystemExit('fp_pages.tsv missing; run `flightplan pages` first')
    witness_rows = read_tsv(FLIGHT_PLAN / 'fp_witnesses.tsv')
    selected = [p.strip() for p in args.pages.split(',')] if getattr(args, 'pages', None) else sorted(
        pages, key=page_sort_key)
    chosen = set(selected)
    tokens = vagc_tokens(witness_rows)
    geometry_rows = {r['page_id']: r for r in read_tsv(FLIGHT_PLAN / 'fp_geometry.tsv') if r['page_id'] not in chosen}
    rule_rows = [r for r in read_tsv(rules_path()) if r['page_id'] not in chosen]
    ruler_out = [r for r in read_tsv(FLIGHT_PLAN / 'fp_ruler.tsv')
                 if r['page_id'] not in chosen or r['source'] not in ('vagc_text', 'vagc_text_footer')]
    queue = []
    for page_id in selected:
        page = pages[page_id]
        if page['status'] not in ('present', 'blank'):
            continue
        gray = load_gray(RAW_ROOT / f'{FP_RAW}/afj_gif/{page_id}.gif')
        g = analyse(gray)
        toks = tokens.get(page_id, [])
        header = header_text(toks, g['height'])
        # The footer TIME field bounds the fit; the index title hour is only a label and is never used.
        frange = footer_range(footer_text(toks, g['footer_y'], g['height']))
        strips = find_strips(g, toks, frange) if page['chapter'] == '3' else []
        g['strips'] = strips
        page_type, basis, vehicle = classify(page, g, header)
        if not page_type.endswith('timeline'):
            strips = []
        row = {'page_id': page_id, 'witness': 'afj_gif', 'width': g['width'], 'height': g['height'],
               'ink_fraction': round(g['ink_fraction'], 5), 'page_type': page_type, 'page_type_basis': basis,
               'vehicle': vehicle, 'footer_y': g['footer_y'] or '', 'columns': ';'.join(map(str, g['columns'])),
               'strips': strip_json(strips) if strips else '',
               'footer_range': f'{frange[0]}-{frange[1]}' if frange else '', 'header_tokens': header[:200]}
        if basis.startswith('rules_disagree'):
            queue.append({'key': page_id, 'issue': 'page_type', 'detail': basis})
        for kind, segs in (('vrule', g['vsegs']), ('hrule', g['hsegs']), ('box', g['boxes'])):
            rule_rows.extend({'page_id': page_id, 'witness': 'afj_gif', 'kind': kind, 'x0': s[0], 'y0': s[1],
                              'x1': s[2], 'y1': s[3]} for s in segs)
        if page_type.endswith('timeline'):
            for s in strips:
                ruler_out.extend(ruler_rows(page_id, 'afj_gif', 'vagc_text', s))
                fit = s['fit']
                if not fit:
                    queue.append({'key': f'{page_id}#{s["strip"]}', 'issue': 'ruler_fit_pending',
                                  'detail': f'{len(s["labels"])} text-layer labels; refit after OXR'})
                elif s['tick_px'] and fit['max'] > s['tick_px'] * fit['slope']:
                    queue.append({'key': f'{page_id}#{s["strip"]}', 'issue': 'ruler_residual',
                                  'detail': f'max {fit["max"]:.0f}s > one tick {s["tick_px"] * fit["slope"]:.0f}s'})
            if not strips:
                queue.append({'key': page_id, 'issue': 'ruler_fit_pending', 'detail': 'no label column in text layer'})
            m = re.search(r'(\d{4})\s*C\s*D\s*T', header.replace('O', '0'))
            if m:
                row['cdt_top'] = m.group(1)
            first = strips[0]['fit'] if strips and strips[0]['fit'] else None
            start = frange[0] if frange else (
                int(round((first['intercept'] + first['slope'] * strips[0]['y0']) / 600) * 600)
                if first and strips[0]['y0'] is not None else None)
            if start is not None:
                row['cdt_expected'] = cdt_expected(start)
                if row.get('cdt_top'):
                    row['cdt_check'] = 'ok' if row['cdt_top'] == row['cdt_expected'] else 'mismatch'
                    if row['cdt_check'] == 'mismatch':
                        queue.append({'key': page_id, 'issue': 'cdt_check',
                                      'detail': f'header {row["cdt_top"]} CDT; GET start gives {row["cdt_expected"]} '
                                                f'(text-layer reading)'})
        geometry_rows[page_id] = row
    write_tsv(FLIGHT_PLAN / 'fp_geometry.tsv', GEOMETRY_FIELDS,
              sorted(geometry_rows.values(), key=lambda r: page_sort_key(r['page_id'])))
    write_tsv(rules_path(), RULE_FIELDS,
              sorted(rule_rows, key=lambda r: (page_sort_key(r['page_id']), r['kind'], int(r['y0']), int(r['x0']))))
    write_tsv(FLIGHT_PLAN / 'fp_ruler.tsv', RULER_FIELDS,
              sorted(ruler_out, key=lambda r: (page_sort_key(r['page_id']), r['source'], int(r['strip']),
                                               float(r['y_px']))))
    write_tsv(WORK / 'fp_geometry_queue.tsv', ('key', 'issue', 'detail'), queue)
    types, fitted, strips_total = {}, 0, 0
    for r in geometry_rows.values():
        types[r['page_type']] = types.get(r['page_type'], 0) + 1
        for s in json.loads(r['strips'] or '[]'):
            strips_total += 1
            fitted += s['slope'] is not None
    issues = {}
    for q in queue:
        issues[q['issue']] = issues.get(q['issue'], 0) + 1
    print(f'geometry: {len(geometry_rows)} pages {types}; strips {strips_total}, fitted {fitted}; queue {issues}')
    import md_fp_assemble
    md_fp_assemble.assemble()
    return 0
