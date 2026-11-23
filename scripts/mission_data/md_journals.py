"""Step 3: extract AFJ and ALSJ chapters into ordered blocks and spans.

Parsing is from the local snapshots only (``.local/mission_data/journals``). The document
tree is linearized into block segments, so wrapped markup cannot merge or drop
blocks. Utterances are emitted as ordered ``spoken``/``editorial`` spans and
never as cleaned text; §4 decides what each editorial span becomes.

Outputs (``.local/mission_data/scraped_data``, not committed):

* ``journal_{afj,alsj}.tsv``: one row per block in document order.
* ``journal_spans_{afj,alsj}.tsv``: one row per span of every utterance.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from md_common import (AFJ_BASE, ALSJ_BASE, SCRAPED, absolute, format_get, nfc, parse_get, short_hash,
                       squash, write_tsv)
from md_editorial import split_spans
from md_snapshot import journal_chapters

BLOCK_FIELDS = ('block_key', 'source', 'chapter', 'seq', 'kind', 'get', 'get_seconds', 'get_basis', 'anchor',
                'stamp', 'speaker_label', 'speaker_note', 'onboard', 'div_class', 'attribution', 'text',
                'media', 'flags', 'html')
SPAN_FIELDS = ('span_key', 'block_key', 'index', 'kind', 'bracket', 'text', 'source_chars')

BLOCK_TAGS = {'p', 'div', 'blockquote', 'table', 'tr', 'td', 'th', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5',
              'h6', 'hr', 'center', 'body', 'html', 'dl', 'dt', 'dd', 'pre', 'form', 'tbody', 'thead'}
SKIP_TAGS = {'script', 'style', 'head', 'title', 'meta', 'link', 'noscript'}
STAMP = re.compile(r'^\s*(-?\d{2,3}:\d{2}:\d{2})\b\s*')
ANCHOR = re.compile(r'^-?\d{7}$')
IMAGE_ID = re.compile(r'\b(AS15-(?:\d{2,3}|M|P)-\d{3,5}[A-Za-z]?(?:HR)?)\b', re.I)
# Speaker label: up to the first colon outside brackets, at most eight words.
SPEAKER = re.compile(r'^((?:[A-Za-z][\w.\'’/&-]*)(?:[ ](?:[\w.\'’/&-]+|\([^)]{1,30}\)|\[[^\]]{1,120}\]))*?)\s*:(?:\s+|(?=[.\w"“]))',
                     re.S)
REVIEW_QUOTE = re.compile(r'^\[?\s*([A-Z][A-Za-z.\' ]{1,40}?)(?:,? (?:from (?:the )?)?(\d{4} (?:review|correspondence|'
                          r'interview|debrief)[^:"“]*))?\s*[-:–]\s*["“]')


@dataclass
class Segment:
    nodes: list = field(default_factory=list)
    classes: tuple = ()
    tags: tuple = ()

    def html(self) -> str:
        return ''.join(str(n) for n in self.nodes).strip()

    def text(self) -> str:
        parts = []
        for n in self.nodes:
            parts.append(str(n) if isinstance(n, NavigableString) else n.get_text())
        return squash(''.join(parts))

    def anchors(self) -> list[str]:
        out = []
        for n in self.nodes:
            if isinstance(n, Tag):
                for a in ([n] if n.name == 'a' else []) + n.find_all('a'):
                    if a.get('name'):
                        out.append(a['name'].strip())
        return out


def _has_block(tag: Tag) -> bool:
    return any(isinstance(d, Tag) and d.name in BLOCK_TAGS for d in tag.descendants)


def linearize(root: Tag, special) -> list:
    """Yield ``Segment`` and special ``(kind, tag, ctx)`` items in document order."""
    out: list = []
    current = Segment()

    def flush(ctx):
        nonlocal current
        if current.nodes and current.text():
            out.append(current)
        current = Segment(classes=ctx[0], tags=ctx[1])

    def walk(node: Tag, ctx):
        nonlocal current
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                if not current.nodes:
                    current.classes, current.tags = ctx
                current.nodes.append(child)
                continue
            if not isinstance(child, Tag) or child.name in SKIP_TAGS:
                continue
            kind = special(child)
            if kind:
                flush(ctx)
                out.append((kind, child, ctx))
                continue
            if child.name in BLOCK_TAGS:
                flush(ctx)
                inner = (ctx[0] + (' '.join(child.get('class', [])) or '',), ctx[1] + (child.name,))
                walk(child, inner)
                flush(ctx)
            elif child.name == 'br' and 'blockquote' not in ctx[1]:
                current.nodes.append(NavigableString(' '))
            elif _has_block(child):
                walk(child, ctx)
            else:
                if not current.nodes:
                    current.classes, current.tags = ctx
                current.nodes.append(child)
    walk(root, ((), ()))
    flush(((), ()))
    return out


def media_links(tag: Tag, base: str) -> list[dict]:
    found, seen = [], set()
    for el in [tag] + (tag.find_all(True) if isinstance(tag, Tag) else []):
        for attr in ('href', 'src'):
            url = el.get(attr) if isinstance(el, Tag) else None
            if not url or url.startswith('#') or url.startswith('mailto:'):
                continue
            url = absolute(base, url)
            if url in seen:
                continue
            seen.add(url)
            lower = url.lower().split('?')[0]
            if 'youtube.com' in lower or 'youtu.be' in lower:
                kind = 'video'
            elif lower.endswith(('.mp3', '.wav')):
                kind = 'audio'
            elif lower.endswith(('.mpg', '.mp4', '.mov', '.rm', '.ram', '.avi')):
                kind = 'video'
            elif lower.endswith(('.jpg', '.jpeg', '.gif', '.png', '.tif')):
                kind = 'image'
            elif lower.endswith('.pdf'):
                kind = 'document'
            else:
                kind = 'link'
            entry = {'kind': kind, 'url': url}
            m = IMAGE_ID.search(url.rsplit('/', 1)[-1])
            if m:
                entry['image_id'] = m.group(1).upper().removesuffix('HR')
            found.append(entry)
    return found


def parse_utterance(text: str, anchors: list[str]) -> dict | None:
    """``GET Name: text`` / ``GET`` + note / untimed ``Name:``. ``None`` when not an utterance."""
    m = STAMP.match(text)
    stamp = m.group(1) if m else ''
    rest = text[m.end():] if m else text
    if stamp:
        rest = re.sub(r'^[.:;,]\s*', '', rest)
    anchor = next((a for a in anchors if ANCHOR.match(a)), '')
    label, note, onboard = '', '', False
    s = SPEAKER.match(rest)
    if s and len(s.group(1).split()) <= 8 and not s.group(1).lstrip().startswith(('[', '(')):
        label = s.group(1)
        rest = rest[s.end():]
        notes = re.findall(r'\[([^\]]*)\]|\(([^)]*)\)', label)
        for bracket, paren in notes:
            value = bracket or paren
            if value.strip().casefold() == 'onboard':
                onboard = True
            else:
                note = (note + '; ' if note else '') + value.strip()
        label = squash(re.sub(r'\[[^\]]*\]|\([^)]*\)', '', label))
        continued = label.endswith(' continued') or bool(re.search(r'\(continued\)', s.group(1)))
        if continued:
            label = label.removesuffix(' continued').strip()
            note = (note + '; ' if note else '') + 'continued'
    elif not stamp:
        return None
    return {'stamp': stamp, 'anchor': anchor, 'speaker_label': label, 'speaker_note': note,
            'onboard': onboard, 'body': rest.strip()}


def _inner_html(seg_html: str) -> str:
    return re.sub(r'\s+', ' ', seg_html).strip()


class ChapterParser:
    def __init__(self, source: str, path: Path):
        self.source, self.path = source, path
        self.chapter = path.name
        self.base = (AFJ_BASE if source == 'afj' else ALSJ_BASE) + path.name
        self.blocks: list[dict] = []
        self.spans: list[dict] = []
        self.last_get: int | None = None
        self.last_anchor = ''
        self.since_anchor = 0
        self.keys: set[str] = set()

    # ---------------------------------------------------------------- keys
    def key(self, anchor: str) -> str:
        if anchor:
            self.last_anchor, self.since_anchor = anchor, 0
            base = f'{self.source}:{self.path.stem}:{anchor}'
        else:
            self.since_anchor += 1
            base = f'{self.source}:{self.path.stem}:{self.last_anchor or "start"}+{self.since_anchor}'
        key, n = base, 1
        while key in self.keys:
            n += 1
            key = f'{base}~{n}'
        self.keys.add(key)
        return key

    def add(self, kind: str, *, anchor='', stamp='', text='', html='', classes=(), media=(), speaker_label='',
            speaker_note='', onboard=False, attribution='', flags=(), spans=None):
        seconds = parse_get(stamp) if stamp else None
        basis = ''
        flags = list(flags)
        anchor_seconds = parse_get(anchor) if anchor else None
        if anchor_seconds is not None:
            if seconds is not None and seconds != anchor_seconds:
                flags.append(f'anchor_stamp_mismatch:{format_get(seconds)}')
            seconds, basis = anchor_seconds, 'anchor'
        elif seconds is not None:
            basis = 'stamp'
        elif stamp:
            flags.append('malformed_stamp')
        if seconds is None and self.last_get is not None:
            seconds, basis = self.last_get, 'inherited'
        if basis in ('anchor', 'stamp'):
            self.last_get = seconds
        block_key = self.key(anchor if basis == 'anchor' else '')
        row = {'block_key': block_key, 'source': self.source.upper(), 'chapter': self.chapter,
               'seq': len(self.blocks), 'kind': kind, 'get': format_get(seconds), 'get_seconds': seconds,
               'get_basis': basis, 'anchor': anchor, 'stamp': stamp, 'speaker_label': speaker_label,
               'speaker_note': speaker_note, 'onboard': 'onboard' if onboard else '',
               'div_class': ' '.join(c for c in classes if c), 'attribution': attribution,
               'text': nfc(text), 'media': json.dumps(list(media), ensure_ascii=False) if media else '',
               'flags': ';'.join(flags), 'html': _inner_html(html)}
        self.blocks.append(row)
        for index, span in enumerate(spans or []):
            self.spans.append({'span_key': f'{block_key}#{index}:{short_hash(span["text"], 8)}',
                               'block_key': block_key, 'index': index, **span})
        return row

    # --------------------------------------------------------------- blocks
    def utterance(self, seg: Segment, parsed: dict, kind: str, classes):
        bracket = '[' if self.source == 'afj' else '('
        spans = split_spans(parsed['body'], self.source)
        if not parsed['speaker_label']:
            kind = 'event'  # timed note without a speaker, e.g. (Camera click), [Music ...]
        self.add(kind, anchor=parsed['anchor'], stamp=parsed['stamp'], text=parsed['body'], html=seg.html(),
                 classes=classes, media=media_links_of(seg, self.base), speaker_label=parsed['speaker_label'],
                 speaker_note=parsed['speaker_note'], onboard=parsed['onboard'], spans=spans,
                 flags=[] if parsed['stamp'] else ['untimed'])
        del bracket

    def commentary(self, seg: Segment, classes, kind='comment'):
        text = seg.text()
        attribution = ''
        bold = next((n for n in seg.nodes if isinstance(n, Tag) and n.name == 'b'), None)
        if bold is not None and seg.text().startswith(squash(bold.get_text())):
            label = squash(bold.get_text()).rstrip(':').strip()
            if 0 < len(label) <= 60:
                attribution = label
        span = next((n for n in seg.nodes if isinstance(n, Tag) and 'attribution' in n.get('class', [])), None)
        if span is None:
            for n in seg.nodes:
                if isinstance(n, Tag):
                    span = n.find(class_='attribution')
                    if span is not None:
                        break
        if span is not None:
            attribution = attribution or squash(span.get_text()).rstrip(':')
        m = REVIEW_QUOTE.match(text)
        if not attribution and m:
            attribution = squash(m.group(1) + (', ' + m.group(2) if m.group(2) else ''))
        self.add(kind, anchor=next((a for a in seg.anchors() if ANCHOR.match(a)), ''), text=text, html=seg.html(),
                 classes=classes, media=media_links_of(seg, self.base), attribution=attribution)


def media_links_of(seg: Segment, base: str) -> list[dict]:
    out = []
    for n in seg.nodes:
        if isinstance(n, Tag):
            out.extend(media_links(n, base))
    return out


def _afj_special(tag: Tag) -> str | None:
    classes = tag.get('class', [])
    if tag.name == 'div' and 'photo_wrapper' in classes:
        return 'photo'
    if tag.name == 'div' and 'video' in classes:
        return 'video'
    if tag.name == 'audio' or (tag.name == 'div' and tag.find('audio') is not None and not tag.get_text(strip=True)):
        return 'audio'
    if tag.name == 'iframe':
        return 'video'
    if tag.name == 'table':
        return 'table'
    return None


def parse_afj(path: Path) -> ChapterParser:
    cp = ChapterParser('afj', path)
    soup = BeautifulSoup(path.read_bytes(), 'lxml')
    body = soup.body or soup
    h2 = soup.find('h2')
    title = squash(h2.get_text()) if h2 else path.stem
    cp.add('heading', text=title, html=str(h2 or ''), classes=('h2',))
    for item in linearize(body, _afj_special):
        if isinstance(item, tuple):
            kind, tag, ctx = item
            media = media_links(tag, cp.base)
            caption = tag.find(class_='caption')
            text = squash(caption.get_text()) if caption else squash(tag.get_text())
            if kind == 'table':
                if not media and not text:
                    continue
                kind = 'table'
            cp.add(kind, anchor=next((a.get('name', '') for a in tag.find_all('a') if ANCHOR.match(a.get('name', ''))),
                                     ''), text=text, html=str(tag), classes=ctx[0], media=media)
            continue
        seg = item
        classes = seg.classes
        own = classes[-1] if classes else ''
        text = seg.text()
        if any(c in ('comment',) for c in classes):
            cp.commentary(seg, classes)
            continue
        if own == 'pao':
            parsed = parse_utterance(text, seg.anchors())
            cp.add('pao', anchor=(parsed or {}).get('anchor', ''), stamp=(parsed or {}).get('stamp', ''),
                   text=(parsed or {}).get('body', text) if parsed and parsed['stamp'] else text, html=seg.html(),
                   classes=classes, media=media_links_of(seg, cp.base), speaker_label='PAO',
                   spans=split_spans(parsed['body'] if parsed and parsed['stamp'] else text, 'afj'))
            continue
        if 'div' in seg.tags and own in ('', 'cc', 'onboard'):
            parsed = parse_utterance(text, seg.anchors())
            if parsed:
                cp.utterance(seg, parsed, 'utterance', classes)
                continue
        if 'h1' in seg.tags or 'h2' in seg.tags or 'h3' in seg.tags or 'h4' in seg.tags or 'h5' in seg.tags:
            continue
        if own in ('indented', 'unindented') or 'div' not in seg.tags:
            media = media_links_of(seg, cp.base)
            if media and all(m['kind'] == 'audio' for m in media) and 'MP3' in text:
                cp.add('audio', text=text, html=seg.html(), classes=classes, media=media)
            elif text.startswith('Last updated') or 'Copyright' in text:
                continue
            else:
                cp.commentary(seg, classes, kind='comment')
            continue
        cp.commentary(seg, classes, kind='comment')
    return cp


def _alsj_special(tag: Tag) -> str | None:
    if tag.name == 'table':
        return 'table'
    if tag.name == 'iframe':
        return 'video'
    return None


def parse_alsj(path: Path) -> ChapterParser:
    cp = ChapterParser('alsj', path)
    raw = path.read_bytes()
    soup = BeautifulSoup(raw, 'lxml')
    body = soup.body or soup
    title_tag = soup.find(['h1', 'h2', 'h3']) or soup.find('title')
    cp.add('heading', text=squash(title_tag.get_text()) if title_tag else path.stem, html=str(title_tag or ''),
           classes=('title',))
    started = False
    for item in linearize(body, _alsj_special):
        if isinstance(item, tuple):
            kind, tag, ctx = item
            media = media_links(tag, cp.base)
            if started and media:
                cp.add('table' if kind == 'table' else kind, text=squash(tag.get_text()), html=str(tag),
                       classes=ctx[0], media=media)
            continue
        seg = item
        text = seg.text()
        if 'blockquote' in seg.tags:
            if started:
                cp.commentary(seg, seg.classes)
            continue
        parsed = parse_utterance(text, seg.anchors()) if STAMP.match(text) else None
        if parsed:
            started = True
            cp.utterance(seg, parsed, 'utterance', seg.classes)
            continue
        if not started:
            continue
        media = media_links_of(seg, cp.base)
        if 'MP3 Audio Clip' in text or (media and all(m['kind'] == 'audio' for m in media) and len(text) < 80):
            cp.add('audio', anchor=next((a for a in seg.anchors() if ANCHOR.match(a)), ''), text=text,
                   html=seg.html(), media=media)
        elif re.match(r'^(Previous|Next|Journal Home|Apollo 15 Journal Home|Return to)', text) or \
                'Copyright' in text or text.startswith(('Last revised', 'Corrected Transcript')):
            continue
        elif text.startswith('[') or text.startswith('('):
            cp.commentary(seg, seg.classes)
        elif media and len(text) < 120:
            cp.add('media', text=text, html=seg.html(), media=media)
        else:
            cp.commentary(seg, seg.classes, kind='comment')
    return cp


def extract(source: str) -> tuple[list[dict], list[dict]]:
    blocks, spans = [], []
    for path in journal_chapters(source):
        cp = parse_afj(path) if source == 'afj' else parse_alsj(path)
        blocks.extend(cp.blocks)
        spans.extend(cp.spans)
    return blocks, spans


def main(args=None) -> int:
    for source in ('afj', 'alsj'):
        blocks, spans = extract(source)
        write_tsv(SCRAPED / f'journal_{source}.tsv', BLOCK_FIELDS, blocks)
        write_tsv(SCRAPED / f'journal_spans_{source}.tsv', SPAN_FIELDS, spans)
        kinds = {}
        for b in blocks:
            kinds[b['kind']] = kinds.get(b['kind'], 0) + 1
        editorial = sum(s['kind'] == 'editorial' for s in spans)
        print(f'{source}: {len(blocks)} blocks {kinds}; {editorial} editorial spans')
    return 0
