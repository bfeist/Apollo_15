"""Flight plan step 4: OXR readings, the Virtual AGC text layer, and combined blocks (§3.3).

Readings per page:

* ``oxr`` on the AFJ GIF (required), at native size or Lanczos-upscaled
* ``oxr`` on the ALSJ JPG where it exists (an independent scan), mapped onto
  AFJ pixels through the step-2 homography
* ``vagc_text``, the Virtual AGC OCR layer (weak)

Each OXR response is kept under ``.local/mission_data/flight_plan/ocr`` with
its provenance (source checksum, scale, engine revisions). A page is skipped
only when its provenance and outputs match. Run discipline follows the
ingestion guide: retry transient errors three times, stop when the service is
unavailable or after three consecutive failed pages, write
``run-summary.json`` and exit nonzero while pages remain incomplete.

OCR is a GPU job: it refuses to start while a stage-06a transcription run is
active unless ``--force`` is given.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from md_common import LOCAL, RAW_ROOT, WORK, read_tsv, sha256_file, tokens, write_json, write_tsv
from md_flightplan import FLIGHT_PLAN, FP_RAW, normalise_page_id, page_sort_key

OCR_ROOT = LOCAL / 'flight_plan' / 'ocr'
CROPS = LOCAL / 'flight_plan' / 'crops'
OXR_REVISION = 'c1a1dcb312ee9f691404f127785510453c0d9350'
MODEL_REVISION = 'c708407f9d2b4df658f2543d9132b62bb7221df4'
CLIENT_VERSION = 1
READING_FIELDS = ('page_id', 'witness', 'engine', 'revision', 'scale', 'idx', 'type', 'x0', 'y0', 'x1', 'y1', 'text')
BLOCK_FIELDS = ('block_id', 'page_id', 'witness', 'bbox_px', 'bbox_norm', 'column', 'type', 'text', 'readings',
                'agreement', 'get_planned', 'get_basis', 'get_tolerance_s')
FOOTER_FIELDS = ('page_id', 'printed_page', 'printed_title', 'edition', 'page_date', 'get_start', 'get_end',
                 'flight_day', 'rev', 'cdt_top', 'source')
QUEUE_FIELDS = ('key', 'page_id', 'issue', 'readings', 'crop', 'choice', 'reviewer', 'date', 'note')
TIME_RE = re.compile(r'\d{1,3}:\d{2}(?::\d{2}(?:\.\d)?)?')


# ------------------------------------------------------------------ guards
def transcription_running() -> bool:
    """True while a stage-06a run holds the GPU (OCR must not run then)."""
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command',
                              "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                              "Select-Object -ExpandProperty CommandLine"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ''
    return '06a_transcribe' in out


def service_up(base_url: str) -> bool:
    try:
        urllib.request.urlopen(base_url.rsplit('/v1', 1)[0] + '/docs', timeout=5)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


# ------------------------------------------------------------------- OXR I/O
def oxr_request(png: bytes, base_url: str) -> urllib.request.Request:
    boundary = uuid.uuid4().hex
    fields = [('api_key', 'dummy'), ('output_formats', 'markdown'), ('output_formats', 'json'),
              ('feature_config', json.dumps({'keep_header_footer': True})),
              ('element_formats', json.dumps({'image': 'base64', 'table': 'html'})),
              ('extra', json.dumps({'temperature': 0}))]
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields]
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="page.png"\r\n'
              f'Content-Type: image/png\r\n\r\n'.encode(), png, f'\r\n--{boundary}--\r\n'.encode()]
    return urllib.request.Request(base_url + '/parse/sync', data=b''.join(parts),
                                  headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})


def oxr_parse(png: bytes, base_url: str, attempts: int = 3) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(attempts):
        try:
            with opener.open(oxr_request(png, base_url), timeout=900) as response:
                data = json.load(response)
            if data.get('code') != 0 or not isinstance(data.get('data'), dict):
                raise ValueError(f'OXR error {data.get("code")}: {data.get("message")}')
            return data
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'OXR failed after {attempts} attempts: {last}')


def oxr_blocks(response: dict) -> list[dict]:
    structured = response['data'].get('result', {}).get('outputs', {}).get('json') or {}
    pages = structured.get('pages') or []
    if len(pages) != 1:
        raise ValueError('expected one parsed page')
    return pages[0].get('blocks') or []


def prepared_png(path: Path, scale: float) -> bytes:
    image = Image.open(path)
    image.load()
    image = image.convert('L')
    if scale != 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format='PNG', optimize=False)
    return out.getvalue()


def run_page(page_id: str, witness: str, path: Path, scale: float, base_url: str) -> tuple[str, list[dict]]:
    folder = OCR_ROOT / witness / page_id
    provenance = {'engine': 'OXR-1.0', 'oxr_revision': OXR_REVISION, 'model_revision': MODEL_REVISION,
                  'client_version': CLIENT_VERSION, 'source_sha256': sha256_file(path), 'scale': scale,
                  'resample': 'lanczos', 'keep_header_footer': True, 'temperature': 0}
    result = folder / 'result.json'
    if result.is_file():
        saved = json.loads(result.read_text(encoding='utf-8'))
        if saved.get('provenance') == provenance and (folder / 'response.json').is_file():
            blocks = oxr_blocks(json.loads((folder / 'response.json').read_text(encoding='utf-8')))
            return 'cached', scale_blocks(blocks, scale)
    folder.mkdir(parents=True, exist_ok=True)
    result.unlink(missing_ok=True)
    t0 = time.perf_counter()
    response = oxr_parse(prepared_png(path, scale), base_url)
    write_json(folder / 'response.json', response)
    blocks = oxr_blocks(response)
    write_json(result, {'provenance': provenance, 'blocks': len(blocks), 'seconds': round(time.perf_counter() - t0, 1)})
    return 'ran', scale_blocks(blocks, scale)


def scale_blocks(blocks: list[dict], scale: float) -> list[dict]:
    out = []
    for b in blocks:
        x0, y0, x1, y1 = (float(v) / scale for v in b.get('bbox', (0, 0, 0, 0)))
        out.append({'idx': b.get('idx'), 'type': b.get('type', ''), 'bbox': (x0, y0, x1, y1),
                    'text': ' '.join(str(b.get('content') or '').split())})
    return out


# --------------------------------------------------------- Virtual AGC layer
def vagc_lines(page_tokens: list[tuple]) -> list[dict]:
    """Group mapped text-layer words into lines: [{'bbox', 'text'}]."""
    lines = []
    for x0, yc, x1, word in sorted(page_tokens, key=lambda t: (t[1], t[0])):
        for line in lines:
            if abs(line['yc'] - yc) < 6 and x0 - line['bbox'][2] < 25:
                line['words'].append(word)
                line['bbox'] = (line['bbox'][0], min(line['bbox'][1], yc - 6), max(line['bbox'][2], x1),
                                max(line['bbox'][3], yc + 6))
                break
        else:
            lines.append({'yc': yc, 'bbox': (x0, yc - 6, x1, yc + 6), 'words': [word]})
    return [{'bbox': l['bbox'], 'text': ' '.join(l['words'])} for l in lines]


# --------------------------------------------------------------- combining
def overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area


def apply_h(H: list[float], bbox) -> tuple:
    M = np.array(H, dtype=float).reshape(3, 3)
    pts = np.array([[bbox[0], bbox[1], 1], [bbox[2], bbox[1], 1], [bbox[0], bbox[3], 1], [bbox[2], bbox[3], 1]]).T
    q = M @ pts
    q = q[:2] / q[2]
    return float(q[0].min()), float(q[1].min()), float(q[0].max()), float(q[1].max())


def times_in(text: str) -> list[str]:
    return TIME_RE.findall((text or '').replace('O', '0'))


def column_of(bbox, geometry: dict, strips: list[dict], header_cols: list[tuple[float, str]]) -> str:
    """Column by geometry: ruler labels/codes, MCC-H left of the first ruler, then the header-named lanes."""
    xc = (bbox[0] + bbox[2]) / 2
    def region(s):  # the rule may be left of the labels (ticks to the right) or right of them
        rule = s['rule_x'] if s.get('rule_x') is not None else s['label_x1']
        return min(rule, s['label_x1'] - 100) - 5, max(rule, s['label_x1']) + 8
    for s in strips:
        left, right = region(s)
        if left <= bbox[0] and bbox[2] <= right + 4:
            return 'ruler'
    if strips and xc < region(strips[0])[0]:
        return 'MCC-H'
    columns = sorted({float(c) for c in (geometry.get('columns') or '').split(';') if c})
    if header_cols:
        for hx, name in sorted(header_cols, reverse=True):
            left = max([c for c in columns if c <= hx] or [0])
            if xc >= left:
                return name
    if geometry.get('page_type') == 'lm_timeline':
        return 'LM'
    if geometry.get('page_type') == 'csm_timeline':
        return 'NOTES' if columns and xc > columns[-1] else 'CSM'
    return ''


def label_tokens(blocks: list[dict]) -> list[tuple]:
    """(x0, y_centre, x1, word) for the first word of each block, the shape ruler labels take in OXR output."""
    out = []
    for b in blocks:
        words = b['text'].split()
        if not words:
            continue
        x0, y0, x1, y1 = b['bbox']
        share = len(words[0]) / max(1, len(b['text']))
        out.append((x0, (y0 + y1) / 2 if len(words) == 1 else y0 + min(12.0, (y1 - y0) / 2), x0 + (x1 - x0) * share,
                    words[0]))
    return out


HEADER_NAMES = ('MCC-H', 'CDR', 'LMP', 'CMP', 'NOTES', 'CSM', 'LM')


def header_columns(blocks: list[dict], height: int) -> list[tuple[float, str]]:
    out = []
    for b in blocks:
        if b['bbox'][1] > 0.12 * height:
            continue
        for word in b['text'].upper().replace(',', ' ').split():
            word = word.replace('MCCH', 'MCC-H')
            if word in HEADER_NAMES and word not in [n for _, n in out]:
                out.append(((b['bbox'][0] + b['bbox'][2]) / 2, word))
    return out


def parse_footer(text: str) -> dict:
    """``APOLLO 15 | FINAL (7/26) | 6/21/71 | 104:00 - 105:00 | 5/14 | 3-126`` -> fields.

    Fields are removed from the text as they are found, so the edition ``(7/26)`` and the date are
    never read as DAY/REV. DAY/REV is ``5/14``, ``1/TLC`` or ``3-4/TLC``.
    """
    from md_fp_geometry import footer_range
    rest = re.sub(r'\s+', ' ', (text or '').replace('|', ' ')).strip()
    out = {}
    m = re.search(r'FINAL\s*\(\s*\d+\s*/\s*\d+\s*\)', rest, re.I)
    if m:
        out['edition'] = 'FINAL ' + re.sub(r'\s+', '', m.group(0))[5:]
        rest = rest.replace(m.group(0), ' ')
    m = re.search(r'\b(\d{1,2}/\d{1,2}/7\d)\b', rest)
    if m:
        out['page_date'] = m.group(1)
        rest = rest.replace(m.group(0), ' ')
    span = footer_range(rest)
    if span:
        out['get_start'], out['get_end'] = (f'{v // 3600:03d}:{v % 3600 // 60:02d}' for v in span)
        rest = re.sub(r'\d{1,3}\s*:\s*[\dOD]{2}\s*-?\s*\d{1,3}\s*:\s*[\dOD]{2}', ' ', rest)
    m = re.search(r'\b([1-6])\s*-\s*(\d{1,3})\s*$', rest)
    if m:
        out['printed_page'] = normalise_page_id(f'{m.group(1)}-{m.group(2)}')
        rest = rest[:m.start()]
    m = re.search(r'\b(\d{1,2}(?:-\d{1,2})?)\s*/\s*([A-Z]{2,4}|\d{1,3}(?:-\d{1,3})?)\b', rest)
    if m:
        out['flight_day'], out['rev'] = m.group(1), m.group(2)
    return out


# ---------------------------------------------------------------------- main
def selected_pages(args, pages: dict) -> list[str]:
    if args.pages:
        return [p.strip() for p in args.pages.split(',') if p.strip()]
    return sorted((p for p, r in pages.items() if r['status'] in ('present', 'alsj_only')), key=page_sort_key)


def main(args) -> int:
    from md_fp_geometry import ruler_rows, vagc_tokens
    pages = {p['page_id']: p for p in read_tsv(FLIGHT_PLAN / 'fp_pages.tsv')}
    geometry = {g['page_id']: g for g in read_tsv(FLIGHT_PLAN / 'fp_geometry.tsv')}
    witnesses = read_tsv(FLIGHT_PLAN / 'fp_witnesses.tsv')
    if not pages or not geometry:
        raise SystemExit('run `flight_plan.py pages` and `flight_plan.py geometry` first')
    selected = selected_pages(args, pages)
    vagc = vagc_tokens(witnesses)

    oxr: dict[tuple[str, str], list[dict]] = {}
    if not args.vagc_only:
        if transcription_running() and not args.force:
            raise SystemExit('A stage-06a transcription run is active; OCR waits (one GPU model at a time). '
                             'Rerun when it finishes, or pass --force.')
        if not service_up(args.url):
            raise SystemExit(f'OXR is not answering at {args.url}. Start it with ../lsc/scripts/setup_oxr.ps1 '
                             '(Docker container lsc-oxr), then rerun; completed pages are kept.')
        jobs = []
        for page_id in selected:
            if args.witness in ('afj', 'both') and pages[page_id]['status'] in ('present', 'blank'):
                jobs.append((page_id, 'afj_gif', RAW_ROOT / f'{FP_RAW}/afj_gif/{page_id}.gif'))
            if args.witness in ('alsj', 'both') and pages[page_id].get('alsj_jpg_url'):
                jpg = Path(pages[page_id]['alsj_jpg_url']).name
                jobs.append((page_id, 'alsj_jpg', RAW_ROOT / f'{FP_RAW}/alsj_jpg/{jpg}'))
        failures = consecutive = 0
        incomplete = []
        for i, (page_id, witness, path) in enumerate(jobs, 1):
            try:
                state, blocks = run_page(page_id, witness, path, args.scale, args.url)
                oxr[(page_id, witness)] = blocks
                consecutive = 0
                if state == 'ran':
                    print(f'[{i}/{len(jobs)}] {witness} {page_id}: {len(blocks)} blocks', flush=True)
            except Exception as exc:
                failures += 1
                consecutive += 1
                incomplete.append(f'{witness}:{page_id}')
                (OCR_ROOT / witness / page_id).mkdir(parents=True, exist_ok=True)
                write_json(OCR_ROOT / witness / page_id / 'error.json', {'error': str(exc)})
                print(f'[{i}/{len(jobs)}] {witness} {page_id}: ERROR {exc}', flush=True)
                if isinstance(exc, (urllib.error.URLError, ConnectionError)) or consecutive >= 3:
                    incomplete.extend(f'{w}:{p}' for p, w, _ in jobs[i:])
                    break
        write_json(OCR_ROOT / 'run-summary.json', {'selected': len(jobs), 'complete': len(jobs) - len(incomplete),
                                                   'failed_this_run': failures, 'incomplete': incomplete,
                                                   'scale': args.scale})
    else:
        for page_id in selected:
            for witness in ('afj_gif', 'alsj_jpg'):
                folder = OCR_ROOT / witness / page_id
                if (folder / 'response.json').is_file() and (folder / 'result.json').is_file():
                    scale = json.loads((folder / 'result.json').read_text(encoding='utf-8'))['provenance']['scale']
                    oxr[(page_id, witness)] = scale_blocks(
                        oxr_blocks(json.loads((folder / 'response.json').read_text(encoding='utf-8'))), scale)
        incomplete = []

    readings, blocks_out, footers, queue = [], [], [], []
    ruler_out = [r for r in read_tsv(FLIGHT_PLAN / 'fp_ruler.tsv') if not r['source'].startswith('oxr') or r['page_id'] not in selected]
    decisions = {r['key']: r for r in read_tsv(FLIGHT_PLAN.parent / 'review' / 'fp_ocr.tsv')}
    for page_id in selected:
        g = geometry.get(page_id, {})
        width, height = int(g.get('width') or 0), int(g.get('height') or 0)
        lines = vagc_lines(vagc.get(page_id, []))
        for k, l in enumerate(lines):
            readings.append({'page_id': page_id, 'witness': 'afj_gif', 'engine': 'vagc_text', 'revision': '2009-09-07',
                             'scale': 1, 'idx': k, 'type': 'line', 'x0': round(l['bbox'][0], 1),
                             'y0': round(l['bbox'][1], 1), 'x1': round(l['bbox'][2], 1), 'y1': round(l['bbox'][3], 1),
                             'text': l['text']})
        main_blocks = oxr.get((page_id, 'afj_gif'))
        alsj_blocks = oxr.get((page_id, 'alsj_jpg')) or []
        for witness, bl in (('afj_gif', main_blocks or []), ('alsj_jpg', alsj_blocks)):
            for b in bl:
                readings.append({'page_id': page_id, 'witness': witness, 'engine': 'oxr', 'revision': OXR_REVISION[:7],
                                 'scale': args.scale, 'idx': b['idx'], 'type': b['type'],
                                 'x0': round(b['bbox'][0], 1), 'y0': round(b['bbox'][1], 1),
                                 'x1': round(b['bbox'][2], 1), 'y1': round(b['bbox'][3], 1), 'text': b['text']})
        if main_blocks is None:
            continue
        H = pages[page_id].get('alsj_homography')
        H = json.loads(H) if H else None
        alsj_mapped = [{**b, 'bbox': apply_h(H, b['bbox'])} for b in alsj_blocks] if H else []
        hcols = header_columns(main_blocks, height)
        strips = []
        if g.get('page_type', '').endswith('timeline'):
            from md_fp_geometry import analyse, find_strips, footer_range, strip_for_x
            from md_fp_pages import load_gray
            image = analyse(load_gray(RAW_ROOT / f'{FP_RAW}/afj_gif/{page_id}.gif'))
            ftext = ' '.join(b['text'] for b in sorted(main_blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))
                             if b['bbox'][1] >= (image['footer_y'] or 0.85 * height) - 4)
            strips = find_strips(image, label_tokens(main_blocks), footer_range(ftext))
            for s in strips:
                ruler_out.extend(ruler_rows(page_id, 'afj_gif', 'oxr', s))
                if not s['fit']:
                    queue.append({'key': f'{page_id}#{s["strip"]}', 'page_id': page_id, 'issue': 'ruler_fit_failed',
                                  'readings': json.dumps([l[1] for l in s['labels']])})
        order = sorted(main_blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))
        for n, b in enumerate(order):
            block_id = f'{page_id}:b{n:03d}'
            vread = ' '.join(l['text'] for l in lines if overlap(l['bbox'], b['bbox']) > 0.5)
            aread = ' '.join(a['text'] for a in alsj_mapped if overlap(a['bbox'], b['bbox']) > 0.4)
            reads = {'oxr_afj': b['text']}
            if vread:
                reads['vagc_text'] = vread
            if aread:
                reads['oxr_alsj'] = aread
            agree = 'single'
            ta = times_in(b['text'])
            if aread:
                agree = 'agree' if tokens(aread) == tokens(b['text']) else (
                    'times_agree' if times_in(aread) == ta else 'disagree')
            elif vread and ta:
                agree = 'times_agree' if times_in(vread) == ta else 'times_vagc_differs'
            text = b['text']
            decision = decisions.get(block_id)
            if decision and decision.get('choice'):
                text, agree = decision['choice'], 'human'
            elif agree in ('disagree', 'times_vagc_differs') and ta:
                crop = CROPS / page_id / f'{block_id.replace(":", "_")}.png'
                save_crop(RAW_ROOT / f'{FP_RAW}/afj_gif/{page_id}.gif', b['bbox'], crop)
                queue.append({'key': block_id, 'page_id': page_id, 'issue': agree,
                              'readings': json.dumps(reads, ensure_ascii=False), 'crop': str(crop)})
            x0, y0, x1, y1 = b['bbox']
            column = column_of(b['bbox'], g, strips, hcols)
            row = {'block_id': block_id, 'page_id': page_id, 'witness': 'afj_gif',
                   'bbox_px': f'{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}',
                   'bbox_norm': f'{x0 / width:.4f},{y0 / height:.4f},{x1 / width:.4f},{y1 / height:.4f}' if width else '',
                   'column': column, 'type': b['type'], 'text': text,
                   'readings': json.dumps(reads, ensure_ascii=False, sort_keys=True), 'agreement': agree}
            strip = strip_for_x(strips, (x0 + x1) / 2) if strips else None
            fit = strip['fit'] if strip else None
            if fit and column != 'ruler' and y0 < float(g.get('footer_y') or height):
                tick_s = fit['slope'] * strip['tick_px'] if strip['tick_px'] else 60.0
                row.update(get_planned=int(round(fit['intercept'] + fit['slope'] * y0)), get_basis='ruler',
                           get_tolerance_s=round(tick_s / 2))
            blocks_out.append(row)
        footer_blocks = [b for b in main_blocks if b['bbox'][1] > 0.85 * height]
        f = parse_footer(' '.join(b['text'] for b in sorted(footer_blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))))
        title = ' '.join(b['text'] for b in main_blocks if b['bbox'][1] < 0.08 * height and 'FLIGHT PLAN' in b['text'].upper())
        m = re.search(r'(\d{4})\s*CDT', ' '.join(b['text'] for b in main_blocks if b['bbox'][1] < 0.12 * height))
        footers.append({'page_id': page_id, 'printed_title': title, 'cdt_top': m.group(1) if m else '',
                        'source': 'oxr_afj', **f})
        if f.get('printed_page') and f['printed_page'] != page_id:
            queue.append({'key': f'{page_id}:footer', 'page_id': page_id, 'issue': 'footer_page_mismatch',
                          'readings': json.dumps({'oxr_afj': f['printed_page']})})
        elif not f.get('printed_page') and pages[page_id]['chapter'] in ('2', '3'):
            queue.append({'key': f'{page_id}:footer', 'page_id': page_id, 'issue': 'no_footer_reading',
                          'readings': ''})
    write_tsv(FLIGHT_PLAN / 'fp_ocr_readings.tsv', READING_FIELDS,
              merge_rows(FLIGHT_PLAN / 'fp_ocr_readings.tsv', readings, selected,
                         key=lambda r: (page_sort_key(r['page_id']), r['witness'], r['engine'], int(r['idx']))))
    write_tsv(FLIGHT_PLAN / 'fp_ocr_blocks.tsv', BLOCK_FIELDS,
              merge_rows(FLIGHT_PLAN / 'fp_ocr_blocks.tsv', blocks_out, selected,
                         key=lambda r: (page_sort_key(r['page_id']), r['block_id'])))
    write_tsv(FLIGHT_PLAN / 'fp_footers.tsv', FOOTER_FIELDS,
              merge_rows(FLIGHT_PLAN / 'fp_footers.tsv', footers, selected, key=lambda r: page_sort_key(r['page_id'])))
    from md_fp_geometry import RULER_FIELDS
    write_tsv(FLIGHT_PLAN / 'fp_ruler.tsv', RULER_FIELDS,
              sorted(ruler_out, key=lambda r: (page_sort_key(r['page_id']), r['source'], float(r['y_px']))))
    write_tsv(WORK / 'fp_ocr_queue.tsv', QUEUE_FIELDS, queue)
    print(f'ocr: {len(blocks_out)} blocks on {len({b["page_id"] for b in blocks_out})} pages; '
          f'{len(readings)} readings; review queue {len(queue)}')
    import md_fp_assemble
    md_fp_assemble.assemble()
    return 1 if incomplete else 0


def merge_rows(path: Path, new_rows: list[dict], selected: list[str], key) -> list[dict]:
    """Replace rows of the selected pages and keep the others, so partial runs accumulate."""
    chosen = set(selected)
    kept = [r for r in read_tsv(path) if r['page_id'] not in chosen]
    return sorted(kept + new_rows, key=key)


def save_crop(path: Path, bbox, target: Path, pad: int = 6) -> None:
    if target.is_file():
        return
    image = Image.open(path).convert('L')
    x0, y0, x1, y1 = bbox
    box = (max(0, int(x0) - pad), max(0, int(y0) - pad), min(image.width, int(x1) + pad), min(image.height, int(y1) + pad))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.crop(box).save(target)
