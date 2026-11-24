"""Step 2: extract the 1971 NASA transcripts from their embedded text layers.

TEC, CM and LM share one typewritten layout (``DD HH MM SS  SPEAKER  text``).
Their row extractor is the pilot extractor
(``research/mission_timing/index_technical_transcript.py``) with three
changes: the CC–LM/CC–CM link context is detected from every page's banners
rather than seeded, the extractor runs over the whole edition, and pages
without a readable stamp grid are recorded rather than stopping the run.
Event IDs keep the pilot form ``AS15_TEC-p0849-r03`` so existing catalog
entries stay valid. A page's first row is often printed above the old body line (y 109); it is
kept when its stamp is readable and numbered ``r00`` (``r00b``, ...) so the rows below keep their IDs.

The PAO edition is mission commentary: tape segments headed by a GET or clock
time with speaker paragraphs. Its rows get the segment's header time
(``get_basis=header``), which is never a timing anchor.

Raw readings are never corrected in place. Digit substitutions (O→0, l→1,
...) are applied only to compute ``get`` and are flagged ``inferred_stamp_digits``.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

from md_common import RANGE_ZERO, SCRAPED, format_get, write_json, write_tsv
from md_snapshot import load_raw_manifest, verify_raw

EDITIONS = {'tec': 'AS15_TEC', 'cm': 'AS15_CM', 'lm': 'AS15_LM', 'pao': 'AS15_PAO'}
FILES = {'tec': 'original_docs/AS15_TEC.PDF', 'cm': 'original_docs/AS15_CM.PDF', 'lm': 'original_docs/AS15_LM.PDF',
         'pao': 'original_docs/AS15_PAO.PDF'}
URLS = {k: 'https://apollojournals.org/alsj/a15/' + v.split('/')[-1] for k, v in FILES.items()}
STAMP_DIGITS = str.maketrans({'O': '0', 'o': '0', 'D': '0', 'Q': '0', 'l': '1', 'I': '1', 'i': '1', '|': '1',
                              ']': '1', 'B': '8', 'S': '5', 'Z': '2'})
SPEAKERS = {'CC', 'CDR', 'LMP', 'CMP', 'SC', 'CDR-EVA', 'LMP-EVA', 'CDR-LM', 'LMP-LM', 'CDR-SEVA', 'CMP-EVA',
            'LCC', 'CT', 'MS', 'PAO', 'RECOVERY', 'SWIM', 'SWIM 1', 'SWIM 2', 'HORNET', 'OKINAWA', 'AIR BOSS',
            'PHOTO', 'MCC', 'CDR/LMP', 'LMP/CDR', 'CDR/CMP', 'CMP/CDR'}
FIELDS = ('event_id', 'edition', 'pdf_page', 'printed_page_ocr', 'transcript_tape_ocr', 'timed_row', 'stamp_ocr',
          'get', 'get_seconds', 'get_basis', 'speaker_ocr', 'speaker', 'text_ocr', 'search_text', 'flags', 'link_context',
          'x0_pt', 'y0_pt', 'x1_pt', 'y1_pt', 'scan_url')
PAGE_FIELDS = ('edition', 'pdf_page', 'status', 'rows', 'timed_rows', 'invalid_stamps', 'unrecognized_speakers',
               'regressions', 'big_jumps', 'dictionary_rate', 'words', 'needs_ocr', 'reasons')
CM_LINK = re.compile(r'SIMULTANEOUS.{0,40}(?:LINK|COMM).{0,40}(?:IN USE|BETW)', re.I)
LM_RESUMED = re.compile(r'TRANSCRIPT OF COMM.{0,60}RESUMED|BETW\S* CC AND \S{1,4}\s*RESUMED|\bRESUMED\b', re.I)
SECURED = re.compile(r'SIMULTANEOUS.{0,80}SECURED', re.I)


def normalize_speaker(raw: str) -> str:
    """Best reading of an OCR speaker token (``I_MP-EVA`` -> ``LMP-EVA``); '' when unreadable."""
    raw = (raw or '').strip()
    if raw in SPEAKERS:
        return raw
    head, _, tail = raw.partition('-')
    if re.fullmatch(r'C[DO0][RHK]?', head):
        role = 'CDR'
    elif re.fullmatch(r'C[MN].{0,2}', head) and not head.startswith('CC'):
        role = 'CMP'
    elif re.fullmatch(r'(?:L|I|_|1|\|)[^-\s]{0,3}[PF]', head) and not head.startswith('C'):
        role = 'LMP'
    elif head in ('CC', 'CO', 'C0'):
        role = 'CC'
    else:
        return ''
    if not tail:
        return role
    if re.fullmatch(r'S?E[VY]A', tail):
        suffix = 'SEVA' if tail.startswith('S') else 'EVA'
    elif re.fullmatch(r'(?:L|I|_|1)[^\s]{0,3}', tail):
        suffix = 'LM'
    else:
        return ''
    return f'{role}-{suffix}' if role != 'CC' else ''


def get_seconds(stamp: str) -> int | None:
    parts = stamp.translate(STAMP_DIGITS).split()
    if len(parts) != 4 or any(not re.fullmatch(r'[0-9]{2}', part) for part in parts):
        return None
    day, hour, minute, second = map(int, parts)
    if hour >= 24 or minute >= 60 or second >= 60:
        return None
    return day * 86400 + 3600 * hour + 60 * minute + second


def normalize_text(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    text = re.sub(r"['’]", '', text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def page_lines(page) -> list[list[tuple]]:
    words = sorted(page.get_text('words'), key=lambda word: (word[1], word[0]))
    lines = []
    for word in words:
        if not lines or word[1] - lines[-1][0][1] > 3:
            lines.append([])
        lines[-1].append(word)
    return [sorted(line, key=lambda word: word[0]) for line in lines]


def find_stamp_columns(lines: list[list[tuple]]) -> list[float]:
    samples = []
    for line in lines:
        for index in range(len(line) - 4):
            words = line[index:index + 4]
            x = [word[0] for word in words]
            if all(12 <= b - a <= 24 for a, b in zip(x, x[1:])) and get_seconds(
                    ' '.join(word[4] for word in words)) is not None:
                samples.append(x)
                break
    return [median(column) for column in zip(*samples)]


def header_value(lines: list[list[tuple]], label: str) -> str:
    for line in lines:
        if min(word[1] for word in line) > 118:
            break
        for index, word in enumerate(line):
            if word[4].casefold() == label and index + 1 < len(line):
                following = line[index + 1]
                if following[0] - word[0] < 65:
                    return following[4]
    return ''


def link_update(line_text: str, context: str) -> str | None:
    if SECURED.search(line_text):
        return 'single'
    if CM_LINK.search(line_text):
        return 'cc_cm'
    if LM_RESUMED.search(line_text) and re.search(r'TRANSCRIPT|BETW|CC AND', line_text, re.I):
        return 'cc_lm'
    return None


def extract_page(page, link_context: str, edition: str, scan_url: str) -> tuple[list[dict], str]:
    """Pilot extractor, generalized: banners update ``link_context`` as they are read."""
    lines = page_lines(page)
    columns = find_stamp_columns(lines)
    if not columns:
        for line in lines:
            update = link_update(' '.join(w[4] for w in line), link_context)
            if update:
                link_context = update
        raise LookupError(link_context)
    stamp_x, *_, last_stamp_x = columns
    text_x = stamp_x + 133
    printed_page = header_value(lines, 'page')
    transcript_tape = header_value(lines, 'tape')
    page_number = page.number + 1
    rows, pending = [], []
    row_number, top_rows = 0, 0

    def append_row(words, stamp, speaker, line, top=False):
        nonlocal row_number, top_rows
        if stamp and top:
            # A timed row above the old body line (y 109). Numbered r00, r00b, ... so that the
            # pilot IDs of the rows below it keep their numbers.
            top_rows += 1
            marker = 'r00' + ('' if top_rows == 1 else chr(ord('a') + top_rows - 1))
        elif stamp:
            row_number += 1
            marker = f'r{row_number:02}'
        else:
            marker = f'u{round(min(word[1] for word in line)):03}'
        flags = []
        seconds = get_seconds(stamp)
        if not stamp:
            flags.append('untimed_continuation')
        elif seconds is None:
            flags.append('invalid_stamp_ocr')
        elif stamp != stamp.translate(STAMP_DIGITS):
            flags.append('inferred_stamp_digits')
        speaker_read = normalize_speaker(speaker) if stamp else ''
        if stamp and speaker not in SPEAKERS:
            flags.append('inferred_speaker' if speaker_read else 'unrecognized_speaker_ocr')
        if not printed_page.isdigit():
            flags.append('unreliable_printed_page_ocr')
        rows.append({
            'event_id': f'{EDITIONS[edition]}-p{page_number:04}-{marker}', 'edition': EDITIONS[edition],
            'pdf_page': page_number, 'printed_page_ocr': printed_page, 'transcript_tape_ocr': transcript_tape,
            'timed_row': (0 if top else row_number) if stamp else '', 'stamp_ocr': stamp,
            'get': format_get(seconds) if seconds is not None else '', 'get_seconds': seconds,
            'get_basis': 'printed' if seconds is not None else '', 'speaker_ocr': speaker,
            'speaker': speaker_read, 'flags': flags,
            'link_context': link_context, 'words': words.copy(), 'line': line.copy(),
            'scan_url': f'{scan_url}#page={page_number}'})

    for line in lines:
        line_text = ' '.join(word[4] for word in line)
        update = link_update(line_text, link_context)
        if update:
            link_context = update
            continue
        stamp_words = []
        for column in columns:
            matches = [word for word in line if abs(word[0] - column) <= 7]
            stamp_words.append(min(matches, key=lambda word: abs(word[0] - column)) if matches else None)
        speaker_words = [word for word in line if 14 <= word[0] - last_stamp_x <= 36]
        top = min(word[1] for word in line) < 109
        stamp = ' '.join(word[4] if word else '?' for word in stamp_words)
        timed = bool(speaker_words) and sum(word is not None for word in stamp_words) >= 3
        # The page header (tape, page, day; often garbled OCR) sits above y 109, but so does the first
        # row on many pages. A line up there is a row only with a readable stamp; its wrapped lines
        # follow it. Other lines up there are header.
        if top and timed and get_seconds(stamp) is None:
            timed = False
        # A line above the first row continues the previous page's last row when it is set in the
        # text column below the header band and is lower-case prose (not a CONFIDENTIAL stamp or
        # a heading such as EVA-1 FINAL PREPARATION).
        carried = (top and not rows and min(word[1] for word in line) >= 80
                   and abs(line[0][0] - text_x) <= 12 and re.search(r'[a-z]', line_text))
        is_body = not top or timed or carried or (rows and rows[-1]['timed_row'] == 0)
        if is_body and timed:
            if pending:
                append_row(pending, '', '', pending)
                pending.clear()
            speaker = ' '.join(word[4] for word in speaker_words)
            left = [word for word in line if word[0] >= stamp_x - 7]
            words = strip_end_of_tape([word for word in left if word[0] >= text_x - 10])
            append_row(words, stamp, speaker, left, top)
        elif is_body:
            words = strip_end_of_tape([word for word in line if text_x - 10 <= word[0] < page.rect.width - 52])
            if not words or re.search(r'AIR-TO-GROUND|END OF TAPE|TRANSCRIPT OF COMMUNICATIONS', line_text, re.I):
                continue
            if rows:
                rows[-1]['words'].extend(words)
                rows[-1]['line'].extend(words)
                if 'has_untimed_wrap' not in rows[-1]['flags']:
                    rows[-1]['flags'].append('has_untimed_wrap')
            else:
                pending.extend(words)
    if pending:
        append_row(pending, '', '', pending)

    for row in rows:
        words, line = row.pop('words'), row.pop('line')
        row['text_ocr'] = ' '.join(word[4] for word in words)
        row['search_text'] = normalize_text(row['text_ocr'])
        for index, (field, reducer) in enumerate(zip(('x0_pt', 'y0_pt', 'x1_pt', 'y1_pt'), (min, min, max, max))):
            row[field] = round(reducer(word[index] for word in line), 1)
        if row['text_ocr'].startswith(('- -', '--')) or row['text_ocr'].endswith(('- -', '--')):
            row['flags'].append('interrupted_or_continued_call')
    return rows, link_context


# --------------------------------------------------------------------- PAO
PAO_HEADER = re.compile(r'APOLLO\s+15\s+MISSION\s+COMMENTARY', re.I)
PAO_SPEAKER = re.compile(r'^(?:PAO|CAP ?COM|CAPCOM|SC|CC|CDR|CMP|LMP|FALCON|ENDEAVOUR|HOUSTON|PHOTO|RECOVERY|SWIM ?\d?|'
                         r'HORNET|OKINAWA|SPEAKER|[A-Z][A-Z-]{1,12})$')
CDT = timezone(timedelta(hours=-5))


DEAD_AIR = re.compile(r'\(?\s*(?:ALL\s+)?DEAD\s+AIR\s*\.?\s*\)?\s*\.?', re.I)


def _like(text: str, target: str, ratio: float) -> bool:
    letters = re.sub(r'[^A-Z]', '', text.upper())
    return bool(letters) and difflib.SequenceMatcher(None, letters, target).ratio() >= ratio


def end_of_tape_at(texts: list[str], k: int, any_case: bool = False) -> int:
    """Number of words of a typed END OF TAPE marker starting at ``texts[k]`` (0 if none).

    The marker's OCR varies (EXD OF TAPE, _ND OF TAPE, ENI, OF TAPE, END OF TA?E, END OF TAP E,
    _1_ OF TAP1!); an upper-case OF between an END-like and a TAPE-like word identifies it.
    ``any_case`` also accepts a lower-case of (a line that is only the marker).
    """
    if k < 0 or k + 2 >= len(texts):
        return 0
    of = texts[k + 1].strip('.,')
    if of != 'OF' and not (any_case and of.upper() == 'OF'):
        return 0
    end = texts[k]
    if not (_like(end, 'END', 0.6) or (not re.search(r'[A-Za-z]', end) and len(end) <= 4)):
        return 0
    if k + 3 < len(texts) and len(texts[k + 3].strip('.,!')) <= 2 \
            and _like(texts[k + 2] + texts[k + 3], 'TAPE', 0.9):
        return 4  # END OF TAP E
    return 3 if _like(texts[k + 2], 'TAPE', 0.7) else 0


def strip_end_of_tape(words: list) -> list:
    """The line's words without a trailing typed END OF TAPE marker (never communication)."""
    texts = [w[4] for w in words]
    for k in (len(texts) - 3, len(texts) - 4):
        n = end_of_tape_at(texts, k, any_case=k == 0)
        if n and k + n == len(texts):
            return words[:k]
    return words


def drop_end_of_tape(texts: list[str]) -> list[str]:
    """A word stream (an OCR page) without its END OF TAPE markers."""
    out, k = [], 0
    while k < len(texts):
        n = end_of_tape_at(texts, k)
        if n:
            k += n
        else:
            out.append(texts[k])
            k += 1
    return out


def pao_header(line_text: str) -> dict:
    get = re.search(r'(?:GET\s*(\d{1,3}):(\d{2}))|(?:(\d{1,3}):(\d{2})\s*GET)', line_text)
    date = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2})', line_text)
    clock = re.search(r'(\d{1,2}):?(\d{2})\s*,?\s*(?:CDT|CST|cst|cdt)|(?:CDT|CST|cst|cdt)\s*,?\s*(\d{1,2}):?(\d{2})',
                      line_text)
    tape = re.search(r'(?:MC)?\s?(\d{1,4}/\d{1,3})\s*$', line_text.strip())
    out = {'tape': tape.group(0).strip() if tape else '', 'get_seconds': None, 'basis': ''}
    if get:
        h, m = (get.group(1), get.group(2)) if get.group(1) else (get.group(3), get.group(4))
        out['get_seconds'], out['basis'] = int(h) * 3600 + int(m) * 60, 'header'
    elif date and clock:
        h, m = (clock.group(1), clock.group(2)) if clock.group(1) else (clock.group(3), clock.group(4))
        try:
            local = datetime(1900 + int(date.group(3)), int(date.group(1)), int(date.group(2)), int(h), int(m),
                             tzinfo=CDT)
            out['get_seconds'] = round((local - RANGE_ZERO).total_seconds())
            out['basis'] = 'header_clock'
        except ValueError:
            pass
    return out


def extract_pao_page(page, state: dict, scan_url: str) -> list[dict]:
    rows = []
    page_number = page.number + 1
    for line in page_lines(page):
        text = ' '.join(w[4] for w in line)
        if PAO_HEADER.search(text):
            header = pao_header(text)
            state.update(segment=state.get('segment', 0) + 1, tape=header['tape'], get=header['get_seconds'],
                         basis=header['basis'], row=0)
            continue
        line = strip_end_of_tape(line)
        if not line or DEAD_AIR.fullmatch(' '.join(w[4] for w in line)):
            continue  # typist's notes, not communication
        first = line[0]
        # Speaker column: one or two upper-case words near x≈97 followed by text at x≥170.
        speaker_words = [w for w in line if 85 <= w[0] <= 165]
        rest = [w for w in line if w[0] > 165]
        label = ' '.join(w[4] for w in speaker_words)
        if speaker_words and 85 <= first[0] <= 115 and PAO_SPEAKER.match(label) and label.isupper() and (rest or
                                                                                                         len(line) == len(speaker_words)):
            state['row'] = state.get('row', 0) + 1
            flags = [] if state.get('get') is not None else ['no_header_time']
            rows.append({'event_id': f'AS15_PAO-p{page_number:04}-s{state.get("segment", 0):02}r{state["row"]:02}',
                         'edition': 'AS15_PAO', 'pdf_page': page_number, 'printed_page_ocr': '',
                         'transcript_tape_ocr': state.get('tape', ''), 'timed_row': '', 'stamp_ocr': '',
                         'get': format_get(state.get('get')), 'get_seconds': state.get('get'),
                         'get_basis': state.get('basis', ''), 'speaker_ocr': label,
                         'speaker': label.replace('CAP COM', 'CAPCOM'),
                         'words': list(rest), 'flags': flags, 'link_context': 'pao',
                         'scan_url': f'{scan_url}#page={page_number}', 'line': list(line)})
        elif rows:
            rows[-1]['words'].extend(line)
            rows[-1]['line'].extend(line)
        elif state.get('segment') and min(w[1] for w in line) > 60:
            # Text continuing a paragraph from the previous page: an untimed continuation row.
            rows.append({'event_id': f'AS15_PAO-p{page_number:04}-u{round(line[0][1]):03}', 'edition': 'AS15_PAO',
                         'pdf_page': page_number, 'printed_page_ocr': '', 'transcript_tape_ocr': state.get('tape', ''),
                         'timed_row': '', 'stamp_ocr': '', 'get': format_get(state.get('get')),
                         'get_seconds': state.get('get'), 'get_basis': state.get('basis', ''), 'speaker_ocr': '',
                         'speaker': '', 'words': list(line), 'flags': ['untimed_continuation'],
                         'link_context': 'pao', 'scan_url': f'{scan_url}#page={page_number}', 'line': list(line)})
    for row in rows:
        words, line = row.pop('words'), row.pop('line')
        row['text_ocr'] = ' '.join(w[4] for w in words)
        row['search_text'] = normalize_text(row['text_ocr'])
        for index, (field, reducer) in enumerate(zip(('x0_pt', 'y0_pt', 'x1_pt', 'y1_pt'), (min, min, max, max))):
            row[field] = round(reducer(w[index] for w in line), 1)
    return rows


# ------------------------------------------------------------- QA metrics
_WORDS: set[str] | None = None


def dictionary() -> set[str]:
    """Vocabulary from the journals: what a good text layer should mostly contain."""
    global _WORDS
    if _WORDS is None:
        from md_common import read_tsv
        vocab: Counter = Counter()
        for source in ('afj', 'alsj'):
            for row in read_tsv(SCRAPED / f'journal_{source}.tsv'):
                if row['kind'] in ('utterance', 'pao'):
                    vocab.update(normalize_text(row['text']).split())
        _WORDS = {w for w, n in vocab.items() if n >= 2}
    return _WORDS


def page_quality(edition: str, page_number: int, rows: list[dict], status: str) -> dict:
    words = [w for r in rows for w in r['search_text'].split() if not w.isdigit()]
    vocab = dictionary()
    rate = (sum(w in vocab for w in words) / len(words)) if words and vocab else 0.0
    timed = [r for r in rows if r['timed_row'] != '' or r['get_basis'] == 'header']
    invalid = sum('invalid_stamp_ocr' in r['flags'] for r in rows)
    unrecognized = sum('unrecognized_speaker_ocr' in r['flags'] for r in rows)
    regress, jumps = 0, 0
    seconds = [r['get_seconds'] for r in rows if r['get_seconds'] is not None and r['timed_row'] != '']
    gaps = [b - a for a, b in zip(seconds, seconds[1:])]
    pace = median([g for g in gaps if g > 0]) if any(g > 0 for g in gaps) else 0
    for g in gaps:
        regress += g < 0
        jumps += pace > 0 and g > max(600, 40 * pace)
    reasons = []
    if status != 'ok':
        reasons.append(status)
    if invalid:
        reasons.append('stamp_parse')
    if regress:
        reasons.append('stamp_regression')
    if jumps:
        reasons.append('stamp_jump')
    if unrecognized and edition != 'pao':
        reasons.append('speaker_column')
    if words and rate < 0.80:
        reasons.append('low_dictionary_rate')
    needs = bool(reasons) and status != 'no_text_layer_expected'
    return {'edition': EDITIONS[edition], 'pdf_page': page_number, 'status': status, 'rows': len(rows),
            'timed_rows': len(timed), 'invalid_stamps': invalid, 'unrecognized_speakers': unrecognized,
            'regressions': regress, 'big_jumps': jumps, 'dictionary_rate': round(rate, 3), 'words': len(words),
            'needs_ocr': 'yes' if needs else '', 'reasons': ';'.join(reasons)}


def extract_edition(edition: str) -> tuple[list[dict], list[dict], dict]:
    import pymupdf
    path = verify_raw(FILES[edition])
    url = URLS[edition]
    rows, pages = [], []
    context = 'single'
    state: dict = {}
    with pymupdf.open(path) as doc:
        for index in range(len(doc)):
            page = doc[index]
            text = page.get_text().strip()
            if not text:
                pages.append(page_quality(edition, index + 1, [], 'no_text_layer'))
                continue
            if edition == 'pao':
                page_rows = extract_pao_page(page, state, url)
                status = 'ok' if page_rows else 'no_rows'
            else:
                try:
                    page_rows, context = extract_page(page, context, edition, url)
                    status = 'ok' if page_rows else 'no_rows'
                except LookupError as exc:
                    context = str(exc)
                    page_rows, status = [], 'no_stamp_columns'
            rows.extend(page_rows)
            pages.append(page_quality(edition, index + 1, page_rows, status))
    prior = None
    stamp_counts = Counter(r['get_seconds'] for r in rows if r['get_seconds'] is not None and r['timed_row'] != '')
    for row in rows:
        current = row['get_seconds']
        if current is not None and row['timed_row'] != '':
            if prior is not None and current < prior:
                row['flags'].append('get_regression_in_print_order')
            if stamp_counts[current] > 1:
                row['flags'].append('shared_whole_second_stamp')
            prior = current
        row['flags'] = ';'.join(row['flags'])
    raw = load_raw_manifest()[FILES[edition]]
    meta = {'edition': EDITIONS[edition], 'source_url': url, 'source_sha256': raw['sha256'],
            'extractor': 'PyMuPDF ' + pymupdf.__version__, 'pages': len(pages), 'rows': len(rows),
            'timed_rows': sum(r['timed_row'] != '' for r in rows),
            'pages_needing_ocr': sum(p['needs_ocr'] == 'yes' for p in pages), 'status': 'embedded_ocr_unreviewed',
            'link_context': 'detected from banners; initial single'}
    return rows, pages, meta


def main(args) -> int:
    editions = ['tec', 'cm', 'lm', 'pao'] if args.edition == 'all' else [args.edition]
    all_pages = []
    for edition in editions:
        rows, pages, meta = extract_edition(edition)
        all_pages.extend(pages)
        if not getattr(args, 'check_layers', False):
            write_tsv(SCRAPED / f'transcript_{edition}.tsv', FIELDS, rows)
            write_json(SCRAPED / f'transcript_{edition}.json', meta)
        flagged = Counter(r for p in pages for r in p['reasons'].split(';') if r)
        print(f'{EDITIONS[edition]}: {meta["pages"]} pages, {meta["rows"]} rows ({meta["timed_rows"]} timed); '
              f'{meta["pages_needing_ocr"]} pages flagged {dict(flagged)}')
    existing = [p for p in __import__('md_common').read_tsv(SCRAPED / 'transcript_pages.tsv')
                if p['edition'] not in {EDITIONS[e] for e in editions}]
    write_tsv(SCRAPED / 'transcript_pages.tsv', PAGE_FIELDS,
              sorted(existing + all_pages, key=lambda p: (p['edition'], int(p['pdf_page']))))
    return 0
