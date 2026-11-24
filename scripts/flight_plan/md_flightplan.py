"""Flight-plan ingestion (docs/17_2026-11-23_flight_plan_ingestion.md).

Steps of the ``flight_plan.py`` command: snapshot, pages, geometry, ocr,
structure, updates, actual, media, toc, validate. Each step reads only its predecessors'
outputs and rewrites its tables byte-identically for unchanged inputs.

Layers are kept apart: the printed plan (``fp_*`` tables), Woods's hand-written
annotations (``origin = annotation`` blocks, evidence only) and the as-flown
timeline (``fp_actual.tsv``).
"""
from __future__ import annotations

import re
import urllib.error
from pathlib import Path

from bs4 import BeautifulSoup

from md_common import (AFJ_BASE, ALSJ_BASE, JOURNALS, MISSION_DATA, RAW_ROOT, REVIEW, Fetcher, absolute,
                       read_tsv, sha256_file, today, write_tsv)

FLIGHT_PLAN = MISSION_DATA / 'flight_plan'
FP_RAW = 'original_docs/flight_plan'
AFJ_PDF = f'{FP_RAW}/pdf/a15flightplan.pdf'
VAGC_PDF = f'{FP_RAW}/pdf/Apollo15FlightPlan.pdf'
AFJ_PDF_URL = AFJ_BASE + 'pdf/a15flightplan.pdf'
VAGC_PDF_URL = 'https://www.ibiblio.org/apollo/Documents/Apollo15FlightPlan.pdf'
MEDIA_PREFIX = 'A15/flightplan'

# Companion documents in a15docs.html (§1): ground truth, planned EVA detail, replanning clues.
COMPANIONS = (
    ('original_docs/ap15mr.pdf', ALSJ_BASE + 'ap15mr.pdf', 'mission_report'),
    ('original_docs/a15-mission-operations-report.pdf', AFJ_BASE + 'pdf/a15-mission-operations-report.pdf',
     'mission_report'),
    ('original_docs/a15lsp.pdf', ALSJ_BASE + 'a15lsp.pdf', 'procedures'),
    ('original_docs/a15lmact.pdf', AFJ_BASE + 'pdf/a15lmact.pdf', 'procedures'),
    ('original_docs/A15_PressKit.pdf', ALSJ_BASE + 'A15_PressKit.pdf', 'press_kit'),
    ('original_docs/a15-csm-gnc-mocr-log.pdf', AFJ_BASE + 'pdf/a15-csm-gnc-mocr-log.pdf', 'console_log'),
    ('original_docs/a15-csm-g+n-ssr-log.pdf', AFJ_BASE + 'pdf/a15-csm-g+n-ssr-log.pdf', 'console_log'),
    ('original_docs/a15-csm-g+n-ssr-log-cleaned.pdf', AFJ_BASE + 'pdf/a15-csm-g+n-ssr-log-cleaned.pdf',
     'console_log'),
    ('original_docs/a15-csm-propulsion-ssr-log.pdf', AFJ_BASE + 'pdf/a15-csm-propulsion-ssr-log.pdf',
     'console_log'),
)

# Searches for a first-generation scan (§1, §8.3). Recorded so they are not repeated.
SEARCHES = (
    ('2026-11-23', 'Internet Archive', 'title and full-text "apollo 15 flight plan"', 'no result'),
    ('2026-11-23', 'NTRS', '"apollo 15 flight plan"', 'only the Preliminary Science Report and unrelated papers'),
    ('2026-11-23', 'JSC History Portal', 'mission transcripts page', 'no flight plan'),
    ('', 'UHCL JSC History Collection', 'MSC flight-plan series finding aids', 'still to search'),
    ('', 'NARA Fort Worth', 'MSC flight-plan records', 'still to search'),
    ('', 'Sundance / Space Center Houston', 'document scans', 'still to search'),
)


# ------------------------------------------------------------------ page IDs
ROMAN = ('i', 'ii', 'iii', 'iv', 'v', 'vi', 'vii', 'viii', 'ix', 'x', 'xi', 'xii', 'xiii', 'xiv', 'xv', 'xvi',
         'xvii', 'xviii', 'xix', 'xx', 'xxi', 'xxii', 'xxiii', 'xxiv', 'xxv')
SPECIAL = ('cover', 'sigpage', 'acknow')


def normalise_page_id(text: str) -> str | None:
    """``5-9`` -> ``5-009``, ``3-126`` -> ``3-126``, ``XI`` -> ``xi``; None when not a page number."""
    text = (text or '').strip().lower()
    if text in SPECIAL or text in ROMAN:
        return text
    m = re.fullmatch(r'([1-6])\s*-\s*(\d{1,3})', text)
    return f'{m.group(1)}-{int(m.group(2)):03d}' if m else None


def page_sort_key(page_id: str) -> tuple:
    if page_id in SPECIAL:
        return (0, SPECIAL.index(page_id))
    if page_id in ROMAN:
        return (1, ROMAN.index(page_id))
    chapter, number = page_id.split('-')
    return (2 + int(chapter), int(number))


# --------------------------------------------------------------- index parse
def parse_afj_index(html: bytes) -> list[dict]:
    """One row per linked page, in index order: page_id, index_title, phase, chapter, gif_url."""
    soup = BeautifulSoup(html, 'html.parser')
    rows, phase, chapter = [], '', ''
    for node in soup.find_all(['h3', 'h4', 'li']):
        if node.name in ('h3', 'h4'):
            title = ' '.join(node.get_text(' ').split())
            if title.startswith('Introductory'):
                chapter, phase = 'front', title
            elif title.startswith('Chapter'):
                chapter, phase = title.split()[1], title
            elif node.name == 'h4' and chapter == '3':
                phase = title
            continue
        link = node.find('a', href=True)
        if not link or not re.search(r'fplan/[^/]+\.gif$', link['href']):
            continue
        page_id = Path(link['href']).stem.lower()
        rows.append({'page_id': page_id, 'index_title': ' '.join(link.get_text(' ').split()),
                     'phase': phase, 'chapter': page_id.split('-')[0] if '-' in page_id else 'front',
                     'gif_url': absolute(AFJ_BASE + 'fplanindex.html', link['href'])})
    return rows


def unlinked_chapters(html: bytes) -> list[str]:
    """Chapter headings listed without page links (Chapters 4–6)."""
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for h in soup.find_all('h4'):
        title = ' '.join(h.get_text(' ').split())
        if title.startswith('Chapter') and not (h.find_next_sibling() and h.find_next_sibling().name == 'ul'):
            out.append(title)
    return out


def parse_alsj_excerpt(html: bytes) -> list[dict]:
    """``Page 3-126, 104:00-105:00, title <a href="a15fp019.jpg">`` -> rows in page order."""
    text = BeautifulSoup(html, 'html.parser').decode()
    rows = []
    for m in re.finditer(r'Page\s+([1-6]-\d+),\s*([\d:]+\s*-\s*[\d:]+),\s*(.*?)<a\s+href="(a15fp\d+\.jpg)"', text,
                         flags=re.S | re.I):
        label, span, title, jpg = m.groups()
        rows.append({'page_id': normalise_page_id(label), 'alsj_span': re.sub(r'\s+', '', span),
                     'alsj_title': ' '.join(BeautifulSoup(title, 'html.parser').get_text(' ').split()),
                     'jpg': jpg, 'jpg_url': ALSJ_BASE + jpg})
    return rows


# ------------------------------------------------------------------ snapshot
def snapshot(args) -> int:
    from md_snapshot import load_raw_manifest, pdf_pages, record_raw, save_raw_manifest, snapshot_journals

    fetcher = Fetcher(delay=args.delay)
    print(snapshot_journals(fetcher, refresh=args.refresh))
    afj_index = (JOURNALS / 'ap15fj' / 'fplanindex.html').read_bytes()
    alsj_index = (JOURNALS / 'alsj15' / 'a15.fltplan.html').read_bytes()
    pages = parse_afj_index(afj_index)
    excerpt = parse_alsj_excerpt(alsj_index)
    print(f'AFJ index: {len(pages)} GIFs; ALSJ excerpt: {len(excerpt)} JPGs')
    rows = load_raw_manifest()
    jobs = [(f'{FP_RAW}/afj_gif/{Path(p["gif_url"]).name}', p['gif_url'], 'flight_plan', 'afj_gif') for p in pages]
    jobs += [(f'{FP_RAW}/alsj_jpg/{e["jpg"]}', e['jpg_url'], 'flight_plan', 'alsj_jpg') for e in excerpt]
    jobs += [(AFJ_PDF, AFJ_PDF_URL, 'flight_plan', 'afj_pdf'), (VAGC_PDF, VAGC_PDF_URL, 'flight_plan', 'vagc_pdf')]
    jobs += [(rel, url, kind, '') for rel, url, kind in COMPANIONS]
    if args.limit:
        jobs = jobs[:args.limit]
    done = 0
    try:
        for relative, url, kind, note in jobs:
            target = RAW_ROOT / relative
            previous = rows.get(relative)
            if target.is_file() and previous and not args.refresh:
                continue
            temporary = target.with_name(target.name + '.new')
            try:
                digest, size, final = fetcher.download(url, temporary)
            except urllib.error.HTTPError as exc:
                print(f'  {url}: HTTP {exc.code}; skipped')
                continue
            if target.is_file() and sha256_file(target) != digest:
                dated = target.with_name(f'{target.stem}_{(previous or {}).get("fetch_date", "previous")}'
                                         f'{target.suffix}')
                target.rename(dated)
                moved = str(Path(relative).with_name(dated.name)).replace('\\', '/')
                if previous:
                    rows[moved] = {**previous, 'path': moved, 'note': 'superseded upstream'}
            temporary.replace(target)
            count = pdf_pages(target) if target.suffix.lower() == '.pdf' else '1'
            record_raw(rows, relative, url, final, kind, note=note, count=count)
            done += 1
            if done % 25 == 0:
                save_raw_manifest(rows)
                print(f'  {done} downloaded')
    finally:
        save_raw_manifest(rows)
    print(f'downloaded {done}; manifest has {sum(r["kind"] == "flight_plan" for r in rows.values())} flight-plan rows')
    write_tsv(FLIGHT_PLAN / 'fp_searches.tsv', ('date', 'where', 'query', 'result'),
              [dict(zip(('date', 'where', 'query', 'result'), s)) for s in SEARCHES])
    return 0


def main(args) -> int:
    step = args.fp_command
    if step == 'snapshot':
        return snapshot(args)
    if step == 'pages':
        import md_fp_pages
        return md_fp_pages.main(args)
    if step == 'geometry':
        import md_fp_geometry
        return md_fp_geometry.main(args)
    if step == 'ocr':
        import md_fp_ocr
        return md_fp_ocr.main(args)
    if step == 'structure':
        import md_fp_structure
        return md_fp_structure.main(args)
    if step == 'updates':
        import md_fp_actual
        return md_fp_actual.main_updates(args)
    if step == 'actual':
        import md_fp_actual
        return md_fp_actual.main_actual(args)
    if step == 'media':
        import md_fp_media
        return md_fp_media.main(args)
    if step == 'toc':
        import md_toc
        return md_toc.main(args)
    if step == 'validate':
        import md_validate
        return md_validate.main(args)
    raise SystemExit(f'unknown flight_plan step {step}')


def add_steps(fp) -> None:
    """Register the steps on a subparsers object whose ``dest`` is ``fp_command``."""
    s = fp.add_parser('snapshot', help='1: fetch indexes, GIFs, JPGs, PDFs and companion documents')
    s.add_argument('--refresh', action='store_true')
    s.add_argument('--delay', type=float, default=1.0)
    s.add_argument('--limit', type=int, help='stop after this many downloads (testing)')
    fp.add_parser('pages', help='2: witness matching, LEP and index -> fp_pages.tsv')
    s = fp.add_parser('geometry', help='3: rules, columns, ruler fit, page type')
    s.add_argument('--pages', help='comma list of page_ids (default: all)')
    s = fp.add_parser('ocr', help='4: OXR readings, Virtual AGC layer, combined blocks')
    s.add_argument('--pages', help='comma list of page_ids (default: all with a witness)')
    s.add_argument('--witness', choices=('afj', 'alsj', 'both'), default='both')
    s.add_argument('--scale', type=float, default=2.0, help='Lanczos upscale before OXR (1 = native)')
    s.add_argument('--url', default='http://127.0.0.1:8010/v1')
    s.add_argument('--vagc-only', action='store_true', help='only import the Virtual AGC text layer')
    s.add_argument('--force', action='store_true', help='run even if a 06a transcription run is active')
    s = fp.add_parser('structure', help='5: AI structuring with validation')
    s.add_argument('--backend', choices=('hgx', 'strata'), default='hgx')
    s.add_argument('--budget', type=int, default=100)
    s.add_argument('--pages', help='comma list of page_ids')
    s.add_argument('--no-ai', action='store_true', help='rules only')
    s = fp.add_parser('updates', help='6: read-ups from transcripts and journals')
    s.add_argument('--backend', choices=('hgx', 'strata'), default='hgx')
    s.add_argument('--budget', type=int, default=0, help='AI requests for change kinds (0 = rules only)')
    s = fp.add_parser('actual', help='7: event index, anchors, slip map -> fp_actual.tsv')
    s.add_argument('--backend', choices=('hgx', 'strata'), default='hgx')
    s.add_argument('--budget', type=int, default=0, help='AI requests for unresolved activities')
    s.add_argument('--record', action='store_true', help='first save decided review rows to MISSION_DATA/review')
    s = fp.add_parser('media', help='8: hosted page images')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--limit', type=int)
    fp.add_parser('toc', help='9: table of contents, mission stages and crew status')
    fp.add_parser('validate', help='10: flight-plan and TOC checks')
