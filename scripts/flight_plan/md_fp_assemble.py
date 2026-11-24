"""Assemble ``fp_pages.tsv`` from every flight-plan table available so far.

Each step calls :func:`assemble` last, so the page table always reflects the
latest completed step. Inputs, all optional except the index:

* the AFJ index and ALSJ excerpt (page list, titles, phases)
* ``fp_witnesses.tsv`` and ``fp_lep.tsv`` (step 2)
* ``fp_geometry.tsv`` (step 3: page type, vehicle, ruler span)
* ``fp_footers.tsv`` (step 4: printed footer and header fields)
* ``fp_blocks.tsv`` (step 5: annotation flags)
* ``fp_media.tsv`` (step 8: hosted paths)
* ``MISSION_DATA/review/fp_pages.tsv`` (recorded witness and status decisions)
"""
from __future__ import annotations

from md_common import JOURNALS, REVIEW, read_tsv, write_tsv
from md_flightplan import (FLIGHT_PLAN, FP_RAW, page_sort_key, parse_afj_index, parse_alsj_excerpt,
                           unlinked_chapters)
from md_snapshot import load_raw_manifest

PAGE_FIELDS = ('page_id', 'seq', 'chapter', 'phase', 'index_title', 'printed_title', 'page_type', 'vehicle',
               'pair_page_id', 'get_start', 'get_end', 'flight_day', 'rev', 'edition', 'page_date', 'lep_date',
               'cdt_top', 'afj_gif_url', 'afj_gif_sha256', 'afj_pdf_page', 'vagc_pdf_page', 'alsj_jpg_url',
               'alsj_jpg_sha256', 'alsj_homography', 'hosted_full', 'hosted_medium', 'hosted_thumbnail',
               'witness_choice', 'has_annotations', 'status')
CHAPTER_TITLES = {'1': 'Chapter 1 - Flight Plan Notes', '2': 'Chapter 2 - Charts and Tables',
                  '4': 'Chapter 4 - Consumables Analysis', '5': 'Chapter 5 - Abbreviated Timeline',
                  '6': 'Chapter 6 - Alternate Mission Timelines'}


def pair_of(page_id: str) -> str:
    """LM (even) and CSM (odd) pages for the same hour, 3-106 to 3-287."""
    if not page_id.startswith('3-'):
        return ''
    n = int(page_id[2:])
    if 106 <= n <= 287:
        return f'3-{n + 1:03d}' if n % 2 == 0 else f'3-{n - 1:03d}'
    return ''


def assemble(index_rows=None, excerpt=None, unlinked=None) -> list[dict]:
    if index_rows is None:
        html = (JOURNALS / 'ap15fj' / 'fplanindex.html').read_bytes()
        index_rows, unlinked = parse_afj_index(html), unlinked_chapters(html)
    if excerpt is None:
        excerpt = parse_alsj_excerpt((JOURNALS / 'alsj15' / 'a15.fltplan.html').read_bytes())
    manifest = load_raw_manifest()
    pages: dict[str, dict] = {}

    def page(page_id: str) -> dict:
        if page_id not in pages:
            chapter = page_id.split('-')[0] if '-' in page_id else 'front'
            pages[page_id] = {'page_id': page_id, 'chapter': chapter, 'phase': CHAPTER_TITLES.get(chapter, ''),
                              'status': 'missing', 'pair_page_id': pair_of(page_id)}
        return pages[page_id]

    for row in index_rows:
        p = page(row['page_id'])
        p.update(phase=row['phase'], index_title=row['index_title'], afj_gif_url=row['gif_url'], status='present')
        m = manifest.get(f'{FP_RAW}/afj_gif/{row["page_id"]}.gif')
        p['afj_gif_sha256'] = m['sha256'] if m else ''
        if row['index_title'].lower().startswith('blank'):
            p['status'] = 'blank'
    for e in excerpt:
        p = page(e['page_id'])
        p['alsj_jpg_url'] = e['jpg_url']
        m = manifest.get(f'{FP_RAW}/alsj_jpg/{e["jpg"]}')
        p['alsj_jpg_sha256'] = m['sha256'] if m else ''
        if p['status'] == 'missing':
            p.update(status='alsj_only', index_title=p.get('index_title') or e['alsj_title'])
    for row in read_tsv(FLIGHT_PLAN / 'fp_lep.tsv'):
        p = page(row['page_id'])
        p['lep_date'] = row['lep_date']
    for w in read_tsv(FLIGHT_PLAN / 'fp_witnesses.tsv'):
        if not w['page_id'] or w['page_id'] == 'pdf_note' or w['match'] in ('unmatched', 'ambiguous'):
            continue
        p = page(w['page_id'])
        if w['witness'] in ('afj_pdf', 'vagc_pdf'):
            field = w['witness'].replace('_pdf', '_pdf_page')
            # A page scanned twice into the PDF (3-302) keeps its first PDF page.
            if not p.get(field) or int(w['key']) < int(p[field]):
                p[field] = w['key']
        elif w['witness'] == 'alsj_jpg' and 'H=' in (w['note'] or ''):
            p['alsj_homography'] = w['note'].split('H=')[-1]
    for g in read_tsv(FLIGHT_PLAN / 'fp_geometry.tsv'):
        if g['page_id'] in pages:
            p = pages[g['page_id']]
            for key in ('page_type', 'vehicle', 'cdt_top'):
                p[key] = g.get(key, '') or p.get(key, '')
            if g.get('page_type') == 'blank' and p['status'] == 'present':
                p['status'] = 'blank'
    for f in read_tsv(FLIGHT_PLAN / 'fp_footers.tsv'):
        if f['page_id'] in pages:
            p = pages[f['page_id']]
            for key in ('printed_title', 'get_start', 'get_end', 'flight_day', 'rev', 'edition', 'page_date',
                        'cdt_top'):
                if f.get(key):
                    p[key] = f[key]
    annotated = {b['page_id'] for b in read_tsv(FLIGHT_PLAN / 'fp_blocks.tsv') if b.get('origin') == 'annotation'}
    for media in read_tsv(FLIGHT_PLAN / 'fp_media.tsv'):
        if media['page_id'] in pages and media['witness'] == 'default':
            pages[media['page_id']][f'hosted_{media["size"]}'] = media['media_path']
    decisions = {r['page_id']: r for r in read_tsv(REVIEW / 'fp_pages.tsv')}
    for page_id, p in pages.items():
        if p['status'] in ('present', 'blank'):
            p['witness_choice'] = 'afj_gif: default witness (decision 8.1)'
        elif p['status'] == 'alsj_only':
            p['witness_choice'] = 'alsj_jpg: only available scan'
        if page_id.startswith(('4-', '5-', '6-')) and p['status'] == 'missing':
            p['phase'] = p['phase'] or CHAPTER_TITLES.get(page_id[0], '')
        p['has_annotations'] = 'yes' if page_id in annotated else ('' if not annotated else 'no')
        d = decisions.get(page_id)
        if d:
            if d.get('witness_choice'):
                p['witness_choice'] = d['witness_choice']
            if d.get('status'):
                p['status'] = d['status']
    ordered = sorted(pages.values(), key=lambda r: page_sort_key(r['page_id']))
    for seq, p in enumerate(ordered):
        p['seq'] = seq
    write_tsv(FLIGHT_PLAN / 'fp_pages.tsv', PAGE_FIELDS, ordered)
    counts = {}
    for p in ordered:
        counts[p['status']] = counts.get(p['status'], 0) + 1
    print(f'fp_pages.tsv: {len(ordered)} pages {counts}; unlinked chapters: {unlinked or []}')
    return ordered
