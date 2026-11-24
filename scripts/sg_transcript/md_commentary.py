"""Step 6: merge the commentary.

AFJ comments, ALSJ commentary, and editorial notes moved out of utterances
(span action ``to_commentary``) are sorted by GET, then source (AFJ, ALSJ,
other), then document order, as Apollo 11's ``merge_scraped_commentary.py``
did. Commentary keeps its editorial text and links; links are made absolute
and only simple inline markup is kept. "Comm break" notes keep their own kind.

PAO announcements are published as ``P`` utterances (the Apollo 11, 13 and 17
convention), so they are not repeated here.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from bs4 import BeautifulSoup, NavigableString, Tag

from md_common import AFJ_BASE, ALSJ_BASE, MISSION_DATA, SCRAPED, WORK, absolute, format_get, nfc, read_tsv, squash, \
    write_tsv
from md_speakers import load_map, speaker_for

FIELDS = ('commentary_id', 'get', 'get_seconds', 'get_basis', 'source', 'who', 'kind', 'text', 'html', 'block_key',
          'chapter')
KEEP_TAGS = {'a', 'i', 'em', 'b', 'strong', 'sup', 'sub'}
SOURCE_ORDER = {'AFJ': 0, 'ALSJ': 1}


def clean_html(html: str, base: str) -> str:
    soup = BeautifulSoup(html or '', 'lxml')
    root = soup.body or soup
    for tag in root.find_all(['script', 'style', 'img', 'audio', 'iframe']):
        tag.decompose()
    for tag in root.find_all(True):
        if tag.name not in KEEP_TAGS:
            tag.unwrap()
            continue
        attrs = {}
        if tag.name == 'a':
            href = tag.get('href')
            if not href or href.startswith('#') and not tag.get_text(strip=True):
                tag.unwrap()
                continue
            attrs = {'href': absolute(base, href), 'target': '_blank'}
        tag.attrs = attrs
    out = ''.join(str(c) for c in root.children)
    return nfc(re.sub(r'\s+', ' ', out)).strip()


def strip_outer_brackets(text: str) -> str:
    text = text.strip()
    if text.startswith('[') and text.endswith(']'):
        depth = 0
        for i, ch in enumerate(text):
            depth += ch == '['
            depth -= ch == ']'
            if depth == 0 and i < len(text) - 1:
                return text
        return text[1:-1].strip()
    return text


def kind_of(text: str, attribution: str) -> str:
    low = text.casefold()
    if re.match(r'^(?:very )?(?:long |short |brief )?comm(?:unications?)? break', low) or low.startswith('comm break'):
        return 'comm_break'
    if attribution or re.match(r'^\[?\s*[A-Z][a-z]+(?:, from the \d{4}| -| \(\d{4}\))', text):
        return 'review_quote'
    return 'comment'


def main(args=None) -> int:
    mapping = load_map()
    rows = []
    for source, base in (('afj', AFJ_BASE), ('alsj', ALSJ_BASE)):
        blocks = read_tsv(SCRAPED / f'journal_{source}.tsv')
        # Comments before a chapter's first timed block take that block's GET.
        next_get: dict[int, tuple] = {}
        upcoming = None
        for i in range(len(blocks) - 1, -1, -1):
            b = blocks[i]
            if b['get_basis'] in ('anchor', 'stamp'):
                upcoming = (b['get_seconds'], b['chapter'])
            next_get[i] = upcoming
        for i, b in enumerate(blocks):
            if b['kind'] != 'comment':
                continue
            text = b['text']
            html = clean_html(b['html'], base + b['chapter'])
            if source == 'alsj':
                text = strip_outer_brackets(text)
                html = strip_outer_brackets(html)
            if not text or text.startswith(('Previous', 'Next', 'Index')):
                continue
            seconds, basis = b['get_seconds'], b['get_basis']
            if seconds in ('', None):
                upcoming = next_get.get(i)
                if not upcoming or upcoming[1] != b['chapter']:
                    continue
                seconds, basis = upcoming[0], 'next'
            attribution = b['attribution']
            who = ''
            if attribution:
                name = re.split(r'[ ,(]', attribution.strip())[0]
                entry = speaker_for(name, mapping)
                who = entry['who'] if entry and entry['kind'] in ('crew', 'capcom') else attribution
            rows.append({'get_seconds': int(seconds), 'get': format_get(int(seconds)), 'get_basis': basis,
                         'source': source.upper(), 'who': who, 'kind': kind_of(text, attribution), 'text': text,
                         'html': html, 'block_key': b['block_key'], 'chapter': b['chapter'], '_order': (source, i)})
    # Editorial notes moved out of utterances.
    blocks = {}
    for source in ('afj', 'alsj'):
        for b in read_tsv(SCRAPED / f'journal_{source}.tsv'):
            blocks[b['block_key']] = b
    spans = {}
    for source in ('afj', 'alsj'):
        for s in read_tsv(SCRAPED / f'journal_spans_{source}.tsv'):
            spans[s['span_key']] = s
    for n, d in enumerate(read_tsv(WORK / 'editorial_spans.tsv')):
        if d['action'] != 'to_commentary':
            continue
        b = blocks.get(d['block_key'])
        if not b or b['get_seconds'] in ('', None):
            continue
        rows.append({'get_seconds': int(b['get_seconds']), 'get': b['get'], 'get_basis': b['get_basis'],
                     'source': b['source'], 'who': '', 'kind': 'editorial_note',
                     'text': strip_outer_brackets(spans[d['span_key']]['source_chars'].strip('()')),
                     'html': '', 'block_key': d['block_key'], 'chapter': b['chapter'],
                     '_order': (b['source'].lower(), int(b['seq']) + 0.5)})
    rows.sort(key=lambda r: (r['get_seconds'], SOURCE_ORDER.get(r['source'], 2), r['_order']))
    seen = Counter()
    for r in rows:
        base = f'C{r["get"].replace(":", "")}'
        seen[base] += 1
        r['commentary_id'] = f'{base}-{seen[base]:02d}'
    write_tsv(SCRAPED / 'commentary_merged.tsv', FIELDS, rows)
    kinds = Counter(r['kind'] for r in rows)
    print(f'commentary: {len(rows)} rows {dict(kinds)}')
    return 0
