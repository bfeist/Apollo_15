"""PAO edition: OCR readings, general matching to the SG reels, and word correction.

The PAO edition ("Apollo 15 Spacecraft Commentary") is the 1971 typed transcript of the SG
reels themselves: PAO announcements and the air-to-ground they carried. Its segment numbers are
the reels' index ranges (``sg_reels.tsv``). Each row has independent readings:

* ``tl``  the PDF's embedded text layer (``transcript_pao.tsv``, step 2)
* ``ia``  the Internet Archive's OCR of the same scan (``AS15_PAO_djvu.xml``, raw folder)
* ``oxr`` optional: local OXR pages written by ``flight_plan.py ocr`` (``OXR_DIR``); pages
  without one use the other readings
* ``asr`` the reel's Whisper words, where general matching places the row on a reel

Readings are aligned per page (tl against each OCR reading) and per row (tl against asr).
Where another reading differs, a *slot* records every reading's phrase. Two agreeing OCR
readings outvote the third. Otherwise the ASR decides when it agrees with one reading, or a
journal vocabulary when only one reading is made of known words. Undecided slots keep the
text layer and go to the review queue. The ASR never supplies words of its own: a correction
always takes one of the printed readings (docs/16 §6, "the typed transcripts win on wording").

Outputs (``.local/mission_data``):
* ``scraped_data/pao_slots.tsv``              every tl/ia disagreement and its decision
* ``scraped_data/transcript_pao_corrected.tsv`` rows with ``text_corrected``
* ``sg/sg_pao_rows.tsv``                      each PAO-edition row placed on a reel
* ``review/pao_words.tsv``                    undecided slots; human choices are saved in
  ``MISSION_DATA/review/pao_word_choices.tsv`` and reused
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from md_common import LOCAL, RAW_ROOT, REVIEW, SCRAPED, WORK, read_tsv, short_hash, today, write_tsv
from md_reconcile import EQUIVALENT

IA_XML = 'original_docs/internet_archive/AS15_PAO_djvu.xml'
# OXR page readings, written by `flight_plan.py ocr` (an optional third reading).
OXR_DIR = LOCAL / 'ocr' / 'pao' / 'oxr_180'
OXR_DPI = 180
SLOT_FIELDS = ('slot_key', 'event_id', 'pdf_page', 'speaker', 'kind', 'tl', 'ia', 'oxr', 'asr', 'choice', 'method',
               'context')
QUEUE_FIELDS = ('slot_key', 'event_id', 'pdf_page', 'speaker', 'kind', 'tl', 'ia', 'oxr', 'asr', 'choice', 'reviewer',
                'date', 'note', 'context', 'scan_url')
CHOICE_FIELDS = ('slot_key', 'event_id', 'choice', 'reviewer', 'date', 'note')
CORRECTED_FIELDS = ('event_id', 'pdf_page', 'segment', 'speaker', 'get', 'text_ocr', 'text_corrected', 'corrections')
ROW_FIELDS = ('event_id', 'speaker', 'segment', 'header_get', 'reel', 'start_s', 'end_s', 'matched', 'tokens',
              'coverage')
WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)*")
SPEAKER_FIX = {'CAFCOM': 'CAPCOM', 'CACPOM': 'CAPCOM', 'CAPOCM': 'CAPCOM', 'CJPCOM': 'CAPCOM', 'CCPAQM': 'CAPCOM',
               'CAPOOM': 'CAPCOM', 'CC': 'CAPCOM', 'FLACON': 'FALCON', 'ENDEAVOUY': 'ENDEAVOUR', 'PAS': 'PAO',
               'FAO': 'PAO', 'SPEAKR': 'SPEAKER'}
SPEAKER_LABELS = {'CAPCOM', 'FALCON', 'SCOTT', 'IRWIN', 'WORDEN', 'SC', 'PAO', 'ENDEAVOUR', 'ENDEAVOR', 'RECOVERY',
                  'SPEAKER', 'OKINAWA', 'MADRID', 'ARIA', 'PHOTO', 'CC', 'CDR', 'LMP', 'CMP'}
NEAR = 0.8          # character similarity for the ASR to side with a reading it does not match exactly
MISALIGNED = 0.5    # tl and ia phrases less similar than this are an alignment slip, not a word reading
ASR_FILLERS = {'uh', 'um', 'hmm', 'mm', 'ah'}


def word_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, token) on the NFKC text; tokens as md_common.tokens (case and apostrophes ignored)."""
    text = unicodedata.normalize('NFKC', text or '')
    return [(m.start(), m.end(), re.sub(r"['\u2019]", '', m.group()).casefold()) for m in WORD.finditer(text)]


def norm(tokens) -> str:
    return ' '.join(EQUIVALENT.get(t, t) or t for t in tokens)


# --------------------------------------------------------------- readings
def segment_of(text: str):
    m = re.fullmatch(r'(?:MC)?\s*(\d+)\s*/\s*\d+', (text or '').strip())
    return int(m.group(1)) if m else None


def load_rows() -> list[dict]:
    """PAO-edition utterance rows in document order; an untimed continuation joins the row before it."""
    rows = []
    segment = None
    for r in read_tsv(SCRAPED / 'transcript_pao.tsv'):
        seg = segment_of(r['transcript_tape_ocr'])
        segment = seg if seg is not None else segment
        speaker = SPEAKER_FIX.get((r['speaker'] or '').upper(), (r['speaker'] or '').upper())
        if not speaker and rows and rows[-1]['pdf_page'] == r['pdf_page']:
            rows[-1]['parts'].append(r['text_ocr'])
            continue
        rows.append({'event_id': r['event_id'], 'pdf_page': r['pdf_page'], 'segment': segment, 'speaker': speaker,
                     'get': r['get'], 't': int(r['get_seconds']) if r['get_seconds'] else None,
                     'scan_url': r['scan_url'], 'parts': [r['text_ocr']]})
    for row in rows:
        row['text'] = ' '.join(row['parts'])
        row['spans'] = word_spans(row['text'])
        row['tokens'] = [t for _, _, t in row['spans']]
    return rows


def ia_pages() -> list[list[str]]:
    """Words of each page of the Internet Archive OCR, in reading order."""
    path = RAW_ROOT / IA_XML
    if not path.is_file():
        raise RuntimeError(f'{path} is missing; run `sg_transcript.py snapshot raw`')
    pages = []
    for _event, element in ET.iterparse(path, events=('end',)):
        if element.tag == 'OBJECT':
            pages.append([w.text or '' for w in element.iter('WORD')])
            element.clear()
    return pages


def vocabulary() -> set[str]:
    """Words of the journals (modern, listened transcriptions) and their commentary."""
    vocab = Counter()
    for source in ('afj', 'alsj'):
        for b in read_tsv(SCRAPED / f'journal_{source}.tsv'):
            vocab.update(t for _, _, t in word_spans(b['text']))
    return {w for w, n in vocab.items() if n >= 2}


def known(tokens, vocab) -> bool:
    return all(t.isdigit() or t in vocab for t in tokens)


def oxr_pages() -> dict[int, list[str]]:
    """Words of each OXR-read page, by PDF page (optional; written by ``flight_plan.py ocr``)."""
    out = {}
    if OXR_DIR.is_dir():
        for path in sorted(OXR_DIR.glob('p[0-9][0-9][0-9][0-9].json')):
            pages = json.loads(path.read_text(encoding='utf-8')).get('pages') or []
            blocks = sorted(pages[0].get('blocks') or [], key=lambda b: b.get('idx', 0)) if pages else []
            out[int(path.stem[1:])] = [w for b in blocks if b.get('format', 'text') == 'text'
                                       for w in str(b.get('content') or '').split()]
    return out


def reading_tokens(words: list[str]) -> list[tuple[str, str]]:
    """(source word, token) pairs. Speaker-column labels are dropped: the rows exclude them."""
    out = []
    for w in words:
        if w.strip('.:,') in SPEAKER_LABELS:
            continue
        out.extend((w, t) for _, _, t in word_spans(w))
    return out


def page_slots(rows: list[dict], readings: dict[str, list[str]]) -> list[dict]:
    """Spans of a page's text layer where another reading differs, with every reading's phrase.

    ``readings`` maps a reading (``ia``, ``oxr``) to the page's words. A span comes from any
    reading's disagreement inside one row. Each reading's phrase for it lies between the words
    aligned to the span's neighbours. It is None when they are not aligned, or when the phrase is
    far longer than the span (an alignment slip, not a word reading).
    """
    stream = [(k, i) for k, row in enumerate(rows) for i in range(len(row['tokens']))]
    tl = [rows[k]['tokens'][i] for k, i in stream]
    aligned, spans = {}, defaultdict(dict)
    for name, words in readings.items():
        pairs = reading_tokens(words)
        sm = difflib.SequenceMatcher(None, tl, [t for _, t in pairs], autojunk=False)
        equal = {blk.a + d: blk.b + d for blk in sm.get_matching_blocks() for d in range(blk.size)}
        aligned[name] = (pairs, equal)
        for op, a0, a1, b0, b1 in sm.get_opcodes():
            if op == 'equal':
                continue
            if a0 == a1:  # words the text layer lacks: only inside a row (headers are not rows)
                if a0 == 0 or a0 >= len(stream) or stream[a0 - 1][0] != stream[a0][0]:
                    continue
            elif stream[a1 - 1][0] != stream[a0][0]:
                continue  # across a row boundary: leave the text layer
            if b1 - b0 > (a1 - a0) + 3 or (a0 == a1 and b1 - b0 > 2):
                continue  # a block inserted by the reading (stray header, other column)
            spans[(a0, a1)][name] = (b0, b1)
    slots = []
    for (a0, a1), own in sorted(spans.items()):
        phrases = {}
        for name, (pairs, equal) in aligned.items():
            if name in own:
                b0, b1 = own[name]
            else:  # between the words aligned to the span's neighbours
                before, after = equal.get(a0 - 1), equal.get(a1)
                if before is None or after is None or after - before - 1 > (a1 - a0) + 3:
                    phrases[name] = None
                    continue
                b0, b1 = before + 1, after
            part = pairs[b0:b1]
            phrases[name] = ([t for _, t in part], ' '.join(dict.fromkeys(w for w, _ in part)))
        if all(v is None or v[0] == tl[a0:a1] for v in phrases.values()):
            continue
        k, i = stream[a0]
        slots.append({'row': k, 'i0': i, 'i1': i + (a1 - a0), 'tl': tl[a0:a1], 'readings': phrases})
    return slots


# ------------------------------------------------------- general matching
def match_reel_rows(segments: list[dict], rows: list[dict], cand_idx: list[int]) -> tuple[list[dict], dict]:
    """Place PAO-edition rows on one reel. Returns row locations and tl-token -> (segment, asr index)."""
    grams = defaultdict(set)
    for k in cand_idx:
        t = rows[k]['tokens']
        for i in range(len(t) - 2):
            grams[tuple(t[i:i + 3])].add(k)
    links = {}  # (row, token index) -> (segment index, asr token index)
    hits = defaultdict(list)  # row -> [(start, end, matched tokens)]
    for seg in segments:
        asr = [(w['tok'], w) for w in seg['words'] if w['tok'] not in ASR_FILLERS]
        toks = [norm([t]) for t, _ in asr]
        if len(toks) < 3:
            continue
        votes = Counter()
        for i in range(len(toks) - 2):
            for k in grams.get(tuple(t for t, _ in asr[i:i + 3]), ()):
                votes[k] += 1
        good = sorted(k for k, v in votes.items() if v >= 2 or (v >= 1 and len(toks) < 8))
        if not good:
            continue
        lo, hi = good[0], good[-1]
        if hi - lo > 12:  # keep the densest run of rows
            best = max(good, key=lambda k: sum(votes[j] for j in good if k <= j <= k + 12))
            lo, hi = best, min(good[-1], best + 12)
        run = [k for k in cand_idx if lo <= k <= hi]
        stream = [(k, i) for k in run for i in range(len(rows[k]['tokens']))]
        printed = [norm([rows[k]['tokens'][i]]) for k, i in stream]
        sm = difflib.SequenceMatcher(None, toks, printed, autojunk=False)
        for b in sm.get_matching_blocks():
            if b.size < 3:
                continue
            for d in range(b.size):
                k, i = stream[b.b + d]
                links.setdefault((k, i), (seg['index'], b.a + d, asr))
            for d in range(b.size):
                k, i = stream[b.b + d]
                w = asr[b.a + d][1]
                hits[k].append((w['s'], w['e'], 1))
    placed = []
    for k, hs in hits.items():
        n = sum(h[2] for h in hs)
        placed.append({'row': k, 'start_s': round(min(h[0] for h in hs), 3), 'end_s': round(max(h[1] for h in hs), 3),
                       'matched': n, 'tokens': len(rows[k]['tokens']),
                       'coverage': round(min(1.0, n / max(1, len(rows[k]['tokens']))), 3)})
    return placed, links


def asr_phrase(links: dict, row: int, i0: int, i1: int):
    """ASR tokens between the ASR words aligned to the slot's neighbours, if both are on one segment."""
    before, after = links.get((row, i0 - 1)), links.get((row, i1))
    if not before or not after or before[0] != after[0] or after[1] <= before[1]:
        return None
    asr = before[2]
    gap = after[1] - before[1] - 1
    if gap > (i1 - i0) + 3:
        return None
    return [t for t, _ in asr[before[1] + 1:after[1]]]


# ---------------------------------------------------------------- decide
def similar(a, b) -> float:
    return difflib.SequenceMatcher(None, ''.join(a), ''.join(b), autojunk=False).ratio()


def decide(slot: dict, asr, vocab: set) -> tuple[str, str, str]:
    """(kind, choice, method). choice names a printed reading (tl, ia or oxr); '' keeps tl for review.

    Order: word splits; alignment slips dropped; two agreeing OCR readings outvote the third;
    the ASR sides with a reading it equals or clearly resembles; a journal vocabulary picks the
    only known reading; an unwitnessed word that only one reading has keeps the text layer.
    """
    tl = slot['tl']
    others = {n: v[0] for n, v in slot['readings'].items() if v is not None}
    for name, toks in sorted(others.items()):
        if toks != tl and ''.join(toks) == ''.join(tl):
            return 'split', (name if len(toks) < len(tl) else 'tl'), 'joined'
    kind = 'insert' if not tl else ('delete' if any(not t for t in others.values()) else 'replace')
    others = {n: t for n, t in others.items()
              if not (t and tl and max(len(t), len(tl)) > 2 and similar(t, tl) < MISALIGNED)}
    if not others:
        return 'misaligned', 'tl', 'default'
    reading = {'tl': tl, **others}
    value = {n: norm(t) for n, t in reading.items()}
    if len(reading) == 3:
        if value['ia'] == value['oxr'] != value['tl']:
            return kind, 'oxr', 'ocr_majority'
        if value['tl'] in (value['ia'], value['oxr']):
            return kind, 'tl', 'ocr_majority'
    if asr:
        a = norm(asr)
        for n in ('tl', 'oxr', 'ia'):
            if n in value and value[n] == a:
                return kind, n, 'asr'
        close = sorted(((similar(asr, t), n) for n, t in reading.items()), reverse=True)
        if close[0][0] >= NEAR and close[0][0] >= close[1][0] + 0.15:
            return kind, close[0][1], 'asr_near'
    if kind == 'replace':
        ok = {n for n, t in reading.items() if known(t, vocab)}
        if 'tl' not in ok and ok and len({value[n] for n in ok}) == 1:
            return kind, max(ok), 'vocab'  # 'oxr' before 'ia' when both agree
        if ok == {'tl'}:
            return kind, 'tl', 'vocab'
    if kind in ('insert', 'delete') and asr is None:
        return kind, 'tl', 'default'
    return kind, '', 'review'


def corrected_text(row: dict, chosen: list[dict]) -> str:
    """Apply the slots decided for another reading to the row's text, keeping its other characters."""
    text = unicodedata.normalize('NFKC', row['text'])
    spans = row['spans']
    for s in sorted(chosen, key=lambda s: -s['i0']):
        if s['i0'] < s['i1']:
            start, end = spans[s['i0']][0], spans[s['i1'] - 1][1]
            text = text[:start] + s['words'] + text[end:]
        else:  # insertion before token i0
            at = spans[s['i0']][0]
            text = text[:at] + s['words'] + ' ' + text[at:]
    return text


# ------------------------------------------------------------------ driver
def run(reels: list[dict], load_asr, identity_of) -> list[dict]:
    """Builds the slots, places rows on reels, decides and writes corrected rows. Returns rows."""
    rows = load_rows()
    pages = ia_pages()
    oxr = oxr_pages()
    vocab = vocabulary()
    by_page = defaultdict(list)
    for k, row in enumerate(rows):
        by_page[int(row['pdf_page'])].append(k)
    slots = []
    for page, ks in sorted(by_page.items()):
        readings = {}
        if page - 1 < len(pages):
            readings['ia'] = pages[page - 1]
        if page in oxr:
            readings['oxr'] = oxr[page]
        for s in (page_slots([rows[k] for k in ks], readings) if readings else []):
            s['row'] = ks[s['row']]
            slots.append(s)

    links_all, placed_rows = {}, []
    for reel in reels:
        first, last = reel['segment_first'], reel['segment_last']
        if not first.isdigit():
            continue
        lo, hi = int(first) - 1, (int(last) if last.isdigit() else 10 ** 6) + 1
        cand = [k for k, r in enumerate(rows) if r['segment'] is not None and lo <= r['segment'] <= hi]
        placed, links = match_reel_rows(load_asr(identity_of(reel)), rows, cand)
        for (k, i), v in links.items():
            links_all.setdefault((k, i), v)
        for p in placed:
            r = rows[p['row']]
            placed_rows.append({'event_id': r['event_id'], 'speaker': r['speaker'], 'segment': r['segment'],
                                'header_get': r['get'], 'reel': reel['reel'], **{f: p[f] for f in
                                ('start_s', 'end_s', 'matched', 'tokens', 'coverage')}})

    choices = {c['slot_key']: c for c in read_tsv(REVIEW / 'pao_word_choices.tsv')}
    out_slots, queue, per_row = [], [], defaultdict(list)
    for s in slots:
        row = rows[s['row']]
        asr = asr_phrase(links_all, s['row'], s['i0'], s['i1']) if s['i0'] > 0 else None
        kind, choice, method = decide(s, asr, vocab)
        phrase = {n: (v[1] if v else '') for n, v in s['readings'].items()}
        ia_tokens = ' '.join(s['readings']['ia'][0]) if s['readings'].get('ia') else ''
        key = f'{row["event_id"]}:{s["i0"]}:{short_hash(" ".join(s["tl"]) + "|" + ia_tokens, 8)}'
        human = choices.get(key)
        if human and (human['choice'] == 'tl' or (human['choice'] in ('ia', 'oxr') and phrase.get(human['choice']))):
            choice, method = human['choice'], 'human'
        context = ' '.join(row['tokens'][max(0, s['i0'] - 5):s['i1'] + 5])
        rec = {'slot_key': key, 'event_id': row['event_id'], 'pdf_page': row['pdf_page'], 'speaker': row['speaker'],
               'kind': kind, 'tl': ' '.join(s['tl']), 'ia': phrase.get('ia', ''), 'oxr': phrase.get('oxr', ''),
               'asr': '' if asr is None else ' '.join(asr),
               'choice': choice, 'method': method, 'context': context}
        out_slots.append(rec)
        if method == 'review':
            queue.append({**rec, 'choice': '', 'reviewer': '', 'date': '', 'note': '', 'scan_url': row['scan_url']})
        if choice in ('ia', 'oxr'):
            per_row[s['row']].append({**s, 'words': phrase[choice]})

    corrected = []
    for k, row in enumerate(rows):
        text = corrected_text(row, per_row[k]) if per_row[k] else row['text']
        corrected.append({'event_id': row['event_id'], 'pdf_page': row['pdf_page'], 'segment': row['segment'],
                          'speaker': row['speaker'], 'get': row['get'], 'text_ocr': row['text'],
                          'text_corrected': text, 'corrections': len(per_row[k])})
        row['text_corrected'] = text
        row['ctokens'] = [t for _, _, t in word_spans(text)]
    write_tsv(SCRAPED / 'pao_slots.tsv', SLOT_FIELDS, out_slots)
    write_tsv(SCRAPED / 'transcript_pao_corrected.tsv', CORRECTED_FIELDS, corrected)
    write_tsv(WORK / 'pao_words.tsv', QUEUE_FIELDS, queue)
    from md_sg import OUT
    placed_rows.sort(key=lambda r: (r['reel'], r['start_s'], r['event_id']))
    write_tsv(OUT / 'sg_pao_rows.tsv', ROW_FIELDS, placed_rows)
    methods = Counter((s['method'], s['choice']) for s in out_slots)
    on_reel = {r['event_id'] for r in placed_rows}
    print(f'pao: {len(rows)} rows, {len(on_reel)} placed on a reel; {len(out_slots)} tl/ia slots '
          f'{dict(sorted(methods.items()))}; {sum(1 for c in corrected if c["corrections"])} rows corrected; '
          f'{len(queue)} queued in review/pao_words.tsv')
    return rows


def record_choices() -> int:
    """Save queue rows with a choice (tl or ia) and reviewer to MISSION_DATA/review/pao_word_choices.tsv."""
    decided = [w for w in read_tsv(WORK / 'pao_words.tsv') if w['choice'] and w['reviewer']]
    rows = {r['slot_key']: r for r in read_tsv(REVIEW / 'pao_word_choices.tsv')}
    for w in decided:
        if w['choice'] not in ('tl', 'ia', 'oxr'):
            raise ValueError(f'{w["slot_key"]}: choice must be tl, ia or oxr')
        rows[w['slot_key']] = {'slot_key': w['slot_key'], 'event_id': w['event_id'], 'choice': w['choice'],
                               'reviewer': w['reviewer'], 'date': w['date'] or today(), 'note': w['note']}
    write_tsv(REVIEW / 'pao_word_choices.tsv', CHOICE_FIELDS, [rows[k] for k in sorted(rows)])
    return len(decided)
