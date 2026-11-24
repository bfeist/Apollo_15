"""Step 5: reconcile the witnesses into utterance events.

The printed TEC, CM and LM rows form the event skeleton; PAO edition rows
labelled PAO form the skeleton for announcements. Journal utterances are
linked to printed rows per (journal, edition) by a monotonic alignment: each
journal row's candidates lie within a GET window, are scored by ordered token
similarity of the *spoken* spans only, and the best order-preserving chain is
kept (weighted longest increasing subsequence). Printed event IDs are reused,
so pilot catalog IDs (``AS15_TEC-p0849-r03``) stay valid.

The witnesses break utterances differently: a journal often joins two printed rows of one
speaker, or splits one printed row in two. After the one-to-one chain, each link is grown
over unlinked neighbouring rows of the same speaker while the joined text matches clearly
better (``grow_link``). Linked rows that share a journal row form a *group* (``group_id``, the
first printed event), and the wording review compares whole groups, not single rows.

Journal readings in the wording queue drop every square bracket, the editors' additions that
are not in the audio, and unit conversions in any bracket (``journal_reading``).

Outputs (under ``.local/mission_data``, not committed):
* ``scraped_data/journal_links.tsv``: one row per accepted journal→printed link.
* ``scraped_data/utterance_events.tsv``: one row per event and its witnesses.
* ``scraped_data/time_offsets.tsv``: printed-minus-journal GET by chapter, to
  detect clock spans needing a constant shift (none is assumed).
* ``.local/mission_data/review/wording.tsv``: readings that disagree (the review queue). Its
  ``proposed`` reading is the journal, which curates the PDFs (``proposed_reading``); the ASR
  is never a wording source.
* Choices made by listening are recorded in ``MISSION_DATA/review/wording_choices.tsv``
  (``record_wording_choices``) and reused on every later build.
"""
from __future__ import annotations

import bisect
import re
import difflib
from collections import defaultdict
from statistics import median

from md_common import REVIEW, SCRAPED, WORK, today, format_get, read_tsv, short_hash, tokens, write_tsv
from md_speakers import load_map, speaker_for

LINK_FIELDS = ('block_key', 'source', 'edition', 'event_id', 'score', 'similarity', 'dt', 'method')
EVENT_FIELDS = ('event_id', 'type', 'get', 'get_seconds', 'get_basis', 'printed_get', 'who', 'speaker_label',
                'printed_edition', 'printed_text', 'afj_key', 'alsj_key', 'pao_key', 'link_score', 'status',
                'flags', 'group_id')
OFFSET_FIELDS = ('source', 'chapter', 'edition', 'links', 'median_dt', 'min_dt', 'max_dt', 'nonzero_share')
WORDING_FIELDS = ('wording_key', 'event_id', 'group_events', 'joined', 'get', 'who', 'kind', 'status', 'proposed', 'printed',
                  'afj', 'alsj', 'choice', 'reviewer', 'date', 'note', 'audio_locator')
GROW_GAP_S = 90      # a neighbouring row joins a link only within this many seconds
GROW_GAP_PAO_S = 900  # ... PAO rows: their times are tape-segment headers, minutes apart
GROW_GAIN = 0.05     # ... when the joined text is at least this much more similar
GROW_SKIP = 2        # ... past at most this many interleaved rows of another speaker linked elsewhere
OVERLAP_MAX = 15     # words a PAO tape segment may repeat from the one before
SPIKE_S = 120        # a printed stamp out of order with neighbours this close is a misread
SHIFT_GAIN = 0.1     # two neighbouring links join when their joined readings are this much more similar
GROW_COVER = 0.6     # ... and this share of the neighbour's words is found, in order, in the other side
AUTO_KINDS = {'ocr_noise', 'garble_filled'}  # the proposed journal reading is accepted without review
WINDOW = {'tec': 150, 'cm': 150, 'lm': 150, 'pao': 1800}
EDITION_KEY = {'AS15_TEC': 'tec', 'AS15_CM': 'cm', 'AS15_LM': 'lm', 'AS15_PAO': 'pao'}


def spoken_text(spans: list[dict]) -> str:
    """The spoken spans, with spoken literal notation such as CB(16) kept whole."""
    from md_editorial import rule_judgement
    out = []
    for i, s in enumerate(spans):
        if s['kind'] == 'spoken':
            out.append(s['text'])
        elif s['bracket'] == '(' and rule_judgement(spans, i)['class'] == 'literal':
            out.append(s['source_chars'])
        else:
            out.append(' ')
    return ''.join(out)


# Spellings the journals restore by listening where the 1971 typists normalized (Yeah -> Yes).
EQUIVALENT = {'yeah': 'yes', 'yep': 'yes', 'yup': 'yes', 'rog': 'roger', 'ok': 'okay', 'oh': '', 'uh': '', 'um': ''}


def comparable(words: list[str]) -> list[str]:
    out = [EQUIVALENT.get(w, w) for w in words]
    return [w for w in out if w]


def similarity(a: list[str], b: list[str]) -> float:
    a, b = comparable(a), comparable(b)
    if not a and not b:
        return 0.6  # both unintelligible ('...' / [garble]); time and order decide
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def role_group(who: str) -> str:
    if not who:
        return ''
    if who == 'CC':
        return 'ground'
    if who in ('CDR', 'LMP', 'CMP', 'SC') or '/' in who:
        return 'crew'
    return who


class Fenwick:
    """Prefix maximum with argmax, for the weighted increasing chain."""

    def __init__(self, n):
        self.n = n
        self.tree = [(-1.0, -1)] * (n + 1)

    def update(self, i, value):
        i += 1
        while i <= self.n:
            if value[0] > self.tree[i][0]:
                self.tree[i] = value
            i += i & -i

    def query(self, i):  # max over [0, i)
        best = (0.0, -1)
        while i > 0:
            if self.tree[i][0] > best[0]:
                best = self.tree[i]
            i -= i & -i
        return best


def chain(candidates: list[list[tuple[int, float]]], n_printed: int) -> dict[int, tuple[int, float]]:
    """Max-weight assignment journal j -> printed p with p strictly increasing in j."""
    fen = Fenwick(n_printed)
    nodes = []  # (j, p, score, parent)
    for j, cands in enumerate(candidates):
        staged = []
        for p, score in cands:
            prev_total, parent = fen.query(p)
            staged.append((p, prev_total + score, len(nodes) + len(staged), parent, score))
        for p, total, node_id, parent, score in staged:
            nodes.append((j, p, score, parent, total))
            fen.update(p, (total, node_id))
    if not nodes:
        return {}
    best = fen.query(n_printed)
    out = {}
    node = best[1]
    while node >= 0:
        j, p, score, parent, _ = nodes[node]
        out[j] = (p, score)
        node = parent
    return out


NONSPEECH_PAREN = re.compile(r'\(([^()]{1,60})\)')
SIC = re.compile(r'[\[(]\s*sic\s*[\])]', re.I)  # an editor's mark, also typed in some PDF rows


def without_nonspeech(text: str) -> str:
    """The printed text without non-speech markers such as (laughter) or (Cough), or [sic].

    The journals bracket the same sounds as editorial spans, which are never compared as words,
    so a printed marker would otherwise read as a wording difference.
    """
    from md_editorial import SOUNDS, marker_for

    def drop(m):
        inner = m.group(1).strip(' .!')
        return ' ' if marker_for(inner) or SOUNDS.match(inner) else m.group(0)
    return NONSPEECH_PAREN.sub(drop, SIC.sub(' ', text or ''))


WRAP_HYPHEN = re.compile(r'(?<=[a-z])- (?=[a-z])')  # 'measure- ment', 'com- fortably': a line-end break


def printed_tokens(text: str) -> list[str]:
    return tokens(WRAP_HYPHEN.sub('', without_nonspeech(text)))


def load_printed(edition: str) -> list[dict]:
    rows = read_tsv(SCRAPED / f'transcript_{edition}.tsv')
    out = []
    last_speaker = ''
    for row in rows:
        if edition == 'pao':
            speaker = (row['speaker'] or '').upper()
            if not speaker:
                if last_speaker == 'PAO' and out:
                    out[-1]['text_ocr'] += ' ' + row['text_ocr']
                    out[-1]['tokens'] += printed_tokens(row['text_ocr'])
                continue
            last_speaker = 'PAO' if speaker in ('PAO', 'PAS', 'FAO') else speaker
            if last_speaker != 'PAO':
                continue
            row['speaker'] = 'PAO'
        elif row['timed_row'] == '':
            if out and 'untimed_continuation' in row['flags']:
                out[-1]['text_ocr'] += ' ' + row['text_ocr']
                out[-1]['tokens'] += printed_tokens(row['text_ocr'])
            continue
        if row['get_seconds'] in ('', None):
            continue
        row['t'] = int(row['get_seconds'])
        row['tokens'] = printed_tokens(row['text_ocr'])
        out.append(row)
    # Chain in GET order: separately printed CC–CM sections are not in time order with the CC–LM pages.
    # A single misread stamp (00 00 01 0B read as :08 for :03) would swap two printed rows and break
    # the chain, so a lone row out of order with both print neighbours sorts at its predecessor.
    for prev, row, nxt in zip(out, out[1:], out[2:]):
        row['_order_t'] = row['t']
        if prev['t'] <= nxt['t'] <= prev['t'] + SPIKE_S and not prev['t'] <= row['t'] <= nxt['t']:
            row['_order_t'] = prev['t']
    return sorted(out, key=lambda r: r.get('_order_t', r['t']))


def load_journal(source: str) -> tuple[list[dict], dict[str, list[dict]]]:
    blocks = read_tsv(SCRAPED / f'journal_{source}.tsv')
    spans: dict[str, list[dict]] = defaultdict(list)
    for s in read_tsv(SCRAPED / f'journal_spans_{source}.tsv'):
        spans[s['block_key']].append(s)
    rows = [b for b in blocks if b['kind'] in ('utterance', 'pao') and b['get_seconds'] not in ('', None)]
    for b in rows:
        b['t'] = int(b['get_seconds'])
        b['tokens'] = tokens(spoken_text(spans[b['block_key']]))
    return rows, spans


def link(journal: list[dict], printed: list[dict], edition: str, source: str, mapping) -> list[dict]:
    times = [p.get('_order_t', p['t']) for p in printed]
    window = WINDOW[edition]
    candidates = []
    for j in journal:
        jwho = role_group((speaker_for(j['speaker_label'], mapping) or {}).get('who', ''))
        lo = bisect.bisect_left(times, j['t'] - window)
        hi = bisect.bisect_right(times, j['t'] + window)
        cands = []
        for p in range(lo, hi):
            row = printed[p]
            sim = similarity(j['tokens'], row['tokens'])
            dt = row['t'] - j['t']
            pwho = role_group((speaker_for(row.get('speaker') or row.get('speaker_ocr', ''), mapping) or {})
                              .get('who', ''))
            penalty = 0.25 if jwho and pwho and jwho != pwho else 0.0
            short = min(len(j['tokens']), len(row['tokens'])) <= 2
            accept = (sim >= 0.5 and (not short or abs(dt) <= 30)) or (sim >= 0.34 and abs(dt) <= 20) or \
                (short and sim >= 0.99 and abs(dt) <= 8)
            if sim == 0.6 and not j['tokens'] and abs(dt) > 3:
                accept = False
            if accept:
                score = sim - penalty - abs(dt) / (window * 4) + (0.05 if dt == 0 else 0)
                if score > 0:
                    cands.append((p, score))
        candidates.append(cands)
    best = chain(candidates, len(printed))
    out = []
    for j, (p, score) in best.items():
        row = printed[p]
        out.append({'block_key': journal[j]['block_key'], 'source': source.upper(), 'edition': row['edition'],
                    'event_id': row['event_id'], 'score': round(score, 3),
                    'similarity': round(similarity(journal[j]['tokens'], row['tokens']), 3),
                    'dt': row['t'] - journal[j]['t'], 'method': 'order_text_time'})
    return out


def covered(part: list[str], whole: list[str]) -> float:
    """Share of ``part``'s tokens matched, in order, inside ``whole``."""
    a, b = comparable(part), comparable(whole)
    if not a:
        return 1.0
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(m.size for m in blocks) / len(a)


def grow_link(journal_side: list[list[str]], printed_side: list[list[str]], journal_next, printed_next):
    """Grow one link over neighbouring rows while the joined readings match clearly better.

    ``journal_side``/``printed_side`` hold the token lists of the rows already in the link, in order.
    ``journal_next(end, added)``/``printed_next(end, added)`` return ``(id, tokens)`` for the next
    eligible neighbour at ``end`` ('before' or 'after') given the rows already added on that side,
    or None. Returns the ids added on each side as
    ``([(end, id), ...], [(end, id), ...])``.
    """
    def joined(side):
        return [t for row in side for t in row]

    added_j, added_p = [], []
    score = similarity(joined(journal_side), joined(printed_side))
    grown = True
    while grown:
        grown = False
        for side, other, nxt, added in ((printed_side, journal_side, printed_next, added_p),
                                        (journal_side, printed_side, journal_next, added_j)):
            for end in ('after', 'before'):
                found = nxt(end, added)
                if not found:
                    continue
                ident, toks = found
                trial = side + [toks] if end == 'after' else [toks] + side
                new = similarity(joined(trial), joined(other))
                if new >= score + GROW_GAIN and covered(toks, joined(other)) >= GROW_COVER:
                    side[:] = trial
                    added.append((end, ident))
                    score, grown = new, True
    return added_j, added_p, score


def grow_links(links: list[dict], printed: dict, journals: dict, mapping) -> list[dict]:
    """Extra links where the journal and the printed edition break an utterance differently.

    ``group_printed``: one journal row also covers a neighbouring printed row.
    ``group_journal``: a neighbouring journal row also belongs to one printed row.
    Onboard (CM/LM recorder) and air-to-ground journal rows never join: they are separate audio.
    Only rows not linked to that journal already, of the same speaker and within ``GROW_GAP_S``.
    """
    position = {e: {r['event_id']: i for i, r in enumerate(rows)} for e, rows in printed.items()}
    order = {s: {b['block_key']: i for i, b in enumerate(rows)} for s, (rows, _) in journals.items()}
    taken = {(l['source'], l['event_id']) for l in links} | {l['block_key'] for l in links}

    def who_printed(row):
        return (speaker_for(row.get('speaker') or row.get('speaker_ocr', ''), mapping) or {}).get('who', '')

    def who_journal(block):
        return (speaker_for(block['speaker_label'], mapping) or {}).get('who', '')

    extra = []
    for l in sorted(links, key=lambda l: -l['score']):
        edition = EDITION_KEY[l['edition']]
        gap = GROW_GAP_PAO_S if edition == 'pao' else GROW_GAP_S  # PAO rows carry tape-header times
        source, rows = l['source'].lower(), printed[edition]
        blocks = journals[source][0]
        p0, j0 = position[edition][l['event_id']], order[source][l['block_key']]
        block = blocks[j0]
        who = who_journal(block) or who_printed(rows[p0])

        def step(end, added, start, index_of, items, foreign):
            """The next item past the rows already added at ``end``, skipping up to GROW_SKIP rows of
            another speaker that belong elsewhere (an interleaved simultaneous conversation)."""
            ids = [i for e, i in added if e == end]
            edge = index_of[ids[-1]] if ids else start
            d = 1 if end == 'after' else -1
            q = edge + d
            for _ in range(GROW_SKIP):
                if not (0 <= q < len(items) and foreign(items[q])):
                    break
                q += d
            return (items[q], items[edge]) if 0 <= q < len(items) else (None, None)

        def printed_next(end, added):
            row, edge = step(end, added, p0, position[edition], rows,
                             lambda r: (l['source'], r['event_id']) in taken and who_printed(r) not in ('', who))
            if row is None or (l['source'], row['event_id']) in taken or abs(row['t'] - edge['t']) > gap:
                return None
            if who and who_printed(row) and who_printed(row) != who:
                return None
            return row['event_id'], row['tokens']

        def journal_next(end, added):
            b, edge = step(end, added, j0, order[source], blocks,
                           lambda b: b['block_key'] in taken and who_journal(b) != who_journal(block))
            if b is None or abs(b['t'] - edge['t']) > gap:
                return None
            if b['block_key'] in taken or b['kind'] != block['kind'] or b['onboard'] != block['onboard'] \
                    or who_journal(b) != who_journal(block):
                return None
            return b['block_key'], b['tokens']

        added_j, added_p, score = grow_link([block['tokens']], [rows[p0]['tokens']], journal_next, printed_next)
        for _, event_id in added_p:
            taken.add((l['source'], event_id))
            row = rows[position[edition][event_id]]
            extra.append({**l, 'event_id': event_id, 'dt': row['t'] - block['t'], 'similarity': round(score, 3),
                          'method': 'group_printed'})
        for _, key in added_j:
            taken.add(key)
            b = blocks[order[source][key]]
            extra.append({**l, 'block_key': key, 'dt': rows[p0]['t'] - b['t'], 'similarity': round(score, 3),
                          'method': 'group_journal'})
    # A shifted break: two neighbouring links whose words cross between them (the journal ends one
    # utterance a few words later or earlier). Joined, they match clearly better than apart.
    by_pair = defaultdict(list)
    for l in links:
        by_pair[(l['source'], l['edition'])].append(l)
    for (source, edition_name), pair_links in by_pair.items():
        edition = EDITION_KEY[edition_name]
        if edition == 'pao':
            continue
        rows, blocks = printed[edition], journals[source.lower()][0]
        found = sorted((position[edition][l['event_id']], order[source.lower()][l['block_key']], l) for l in pair_links)
        for (p1, j1, l1), (p2, j2, l2) in zip(found, found[1:]):
            if p2 != p1 + 1 or j2 != j1 + 1 or abs(rows[p2]['t'] - rows[p1]['t']) > GROW_GAP_S:
                continue
            if len({who_journal(blocks[j1]), who_journal(blocks[j2]), who_printed(rows[p1]), who_printed(rows[p2])}
                   - {''}) > 1:
                continue
            a, b = (comparable(blocks[j]['tokens']) for j in (j1, j2))
            c, d = (comparable(rows[p]['tokens']) for p in (p1, p2))
            if not (a and b and c and d):
                continue
            apart = (similarity(a, c) * (len(a) + len(c)) + similarity(b, d) * (len(b) + len(d))) / (
                len(a) + len(b) + len(c) + len(d))
            joined = similarity(a + b, c + d)
            if joined >= apart + SHIFT_GAIN:
                extra.append({**l1, 'event_id': rows[p2]['event_id'], 'dt': rows[p2]['t'] - blocks[j1]['t'],
                              'similarity': round(joined, 3), 'method': 'group_shifted'})
    return extra


def editorial_classes() -> dict[str, dict]:
    """Span key -> its row in the editorial table (class, action and marker decided for it)."""
    return {r['span_key']: r for r in read_tsv(WORK / 'editorial_spans.tsv')}


def journal_reading(spans: list[dict], decided: dict[str, dict]) -> str:
    """The journal row as shown for wording review: only what the journal says was spoken.

    Square brackets are the editors' additions and never in the audio, so every ``[...]`` span is
    dropped (``[garble]`` shows as ``...``, a non-speech marker such as [Laughter] as the approved
    marker). Unit conversions are dropped in any bracket. Other parenthesized spans stay visible.
    """
    from md_editorial import GARBLE, marker_for, rule_judgement, tidy
    spans = sorted(spans, key=lambda s: int(s.get('index') or 0))
    out = []
    for i, span in enumerate(spans):
        if span['kind'] == 'spoken':
            out.append(span['source_chars'])
            continue
        row = decided.get(span.get('span_key', '')) or {}
        rule = rule_judgement(spans, i)
        cls, action = row.get('class') or rule['class'], row.get('action') or rule['action']
        if span['bracket'].startswith('['):
            if cls == 'garble':
                out.append(f' {GARBLE} ')
            elif (cls, action) == ('nonspeech', 'keep'):
                out.append(f' {row.get("marker") or marker_for(span["text"]) or ""} ')
            else:
                out.append(' ')
        elif 'unit' in (cls, rule['class']):
            out.append(' ')
        else:
            out.append(span['source_chars'])
    return tidy(''.join(out))


def join_printed(rows: list[tuple[str, list[str]]]) -> tuple[str, list[str]]:
    """Text and tokens of consecutive printed rows, without the overlap PAO tapes repeat.

    Each PAO tape segment opens with the last words of the one before ("... in trajectory toward
    the moon." / "trajectory toward the moon. It will ..."); the repeat is dropped once.
    """
    text, words = '', []
    for row_text, row_words in rows:
        k = next((k for k in range(min(OVERLAP_MAX, len(words), len(row_words)), 1, -1)
                  if words[-k:] == row_words[:k]), 0)
        if k:
            parts, seen = row_text.split(), 0
            while parts and seen < k:
                seen += len(tokens(parts.pop(0)))
            row_text = ' '.join(parts)
        text = f'{text} {row_text}'.strip()
        words = words + row_words[k:]
    return text, words


def joined_note(ids: list[str], keys: list[tuple[str, str]]) -> str:
    """How a group joins rows, for the reviewer: '2 printed rows + 1 AFJ row'."""
    parts = [f'{len(ids)} printed row{"s" * (len(ids) > 1)}']
    for source in ('afj', 'alsj'):
        n = sum(s == source for s, _ in keys)
        if n:
            parts.append(f'{n} {source.upper()} row{"s" * (n > 1)}')
    return ' + '.join(parts)


def wording_kind(printed: list[str], journal: list[str]) -> str:
    if printed == journal:
        return 'agree'
    if not printed:
        return 'printed_garble_or_empty'
    a, b = ' '.join(printed), ' '.join(journal)
    char = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    if char >= 0.93:
        return 'ocr_noise'
    return 'substantive'


# Garble markers. The TEC, CM and LM legends: "three dots (...) designate those portions of the
# communications that could not be transcribed because of garbling" (*** is VOX clipping, not
# garble). The PAO edition prints (garble); the journals bracket [garble].
GARBLE = 'zgarblez'
PRINTED_GARBLE = re.compile(r'\.{3,}|\((?:garble|garbled)\)', re.I)
JOURNAL_GARBLE = re.compile(r'^\W*garbled?\b', re.I)


def printed_garble_tokens(text: str) -> list[str]:
    return tokens(PRINTED_GARBLE.sub(f' {GARBLE} ', without_nonspeech(text)))


def journal_garble_tokens(spans: list[dict]) -> list[str]:
    return tokens(''.join(s['text'] if s['kind'] == 'spoken' else
                          (f' {GARBLE} ' if JOURNAL_GARBLE.match(s['text'] or '') else ' ') for s in spans))


def fills_garble(printed: list[str], journal: list[str]) -> bool:
    """True when the journal differs from the printed row only by words where the printed row has garble.

    Every difference must replace or sit beside a printed garble marker; any other difference,
    or a row without garble, is not a garble fill.
    """
    a, b = comparable(printed), comparable(journal)
    if GARBLE not in a or a == b:
        return False
    filled = False
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == 'equal':
            continue
        if op == 'insert':
            if not ((i1 > 0 and a[i1 - 1] == GARBLE) or (i1 < len(a) and a[i1] == GARBLE)):
                return False
        elif any(t != GARBLE for t in a[i1:i2]):
            return False
        filled = filled or any(t != GARBLE for t in b[j1:j2])
    return filled


def proposed_reading(readings: dict[str, list[str]]) -> str:
    """The reading used without a human choice: the journals over the printed transcript.

    The AFJ and ALSJ are curated retypings of the 1971 PDFs, so where a journal and the printed
    row differ the journal is proposed. Where the journals differ from each other, the one that
    agrees with the printed row is proposed; otherwise nothing is. The ASR never takes part.
    """
    afj, alsj = readings.get('afj'), readings.get('alsj')
    if afj is not None and alsj is not None and comparable(afj) != comparable(alsj):
        printed = comparable(readings['printed'])
        agreeing = [s for s in ('afj', 'alsj') if comparable(readings[s]) == printed]
        return agreeing[0] if agreeing else ''
    return 'afj' if afj is not None else ('alsj' if alsj is not None else 'printed')


def main(args=None) -> int:
    if args is not None and getattr(args, 'record', False):
        print(f'recorded {record_wording_choices()} wording choices')
    mapping = load_map()
    printed = {e: load_printed(e) for e in ('tec', 'cm', 'lm', 'pao')}
    journals = {s: load_journal(s) for s in ('afj', 'alsj')}
    links = []
    for source, (rows, _) in journals.items():
        onboard = [r for r in rows if r['onboard'] and r['kind'] == 'utterance']
        air = [r for r in rows if not r['onboard'] and r['kind'] == 'utterance']
        pao = [r for r in rows if r['kind'] == 'pao']
        plan = [(air, 'tec'), (onboard, 'cm'), (onboard, 'lm'), (pao, 'pao')]
        if source == 'alsj':
            plan.append((air, 'lm'))  # the ALSJ does not mark onboard-recorder lines
        for subset, edition in plan:
            if subset and printed[edition]:
                found = link(subset, printed[edition], edition, source, mapping)
                links.extend(found)
                print(f'  {source}×{edition}: {len(found)}/{len(subset)} linked')
    # One printed event per journal row: keep the best-scoring edition.
    best_link: dict[str, dict] = {}
    for l in links:
        if l['block_key'] not in best_link or l['score'] > best_link[l['block_key']]['score']:
            best_link[l['block_key']] = l
    links = sorted(best_link.values(), key=lambda l: (l['source'], l['block_key']))
    grown = grow_links(links, printed, journals, mapping)
    print(f'  grown over differing utterance breaks: {sum(g["method"] == "group_printed" for g in grown)} printed rows, '
          f'{sum(g["method"] == "group_journal" for g in grown)} journal rows, '
          f'{sum(g["method"] == "group_shifted" for g in grown)} shifted breaks')
    links += grown
    write_tsv(SCRAPED / 'journal_links.tsv', LINK_FIELDS, links)

    # Clock-offset census by chapter: printed GET minus journal GET of confident links.
    by_chapter = defaultdict(list)
    block_index = {b['block_key']: b for rows, _ in journals.values() for b in rows}
    for l in links:
        if l['similarity'] >= 0.8 and l['edition'] != 'AS15_PAO' and l['method'] == 'order_text_time':
            by_chapter[(l['source'], block_index[l['block_key']]['chapter'], l['edition'])].append(l['dt'])
    offsets = [{'source': s, 'chapter': c, 'edition': e, 'links': len(v), 'median_dt': median(v), 'min_dt': min(v),
                'max_dt': max(v), 'nonzero_share': round(sum(d != 0 for d in v) / len(v), 3)}
               for (s, c, e), v in sorted(by_chapter.items())]
    write_tsv(SCRAPED / 'time_offsets.tsv', OFFSET_FIELDS, offsets)
    shifted = [o for o in offsets if abs(o['median_dt']) >= 3 and o['links'] >= 10]
    if shifted:
        print(f'  WARNING: {len(shifted)} chapter/edition spans with |median printed−journal| ≥ 3 s; '
              'see scraped_data/time_offsets.tsv')

    # Build events.
    events: dict[str, dict] = {}
    for edition, rows in printed.items():
        for row in rows:
            events[row['event_id']] = {
                'event_id': row['event_id'], 'type': 'P' if edition == 'pao' else ('O' if edition in ('cm', 'lm') else 'T'),
                'get': format_get(row['t']), 'get_seconds': row['t'],
                'get_basis': 'header' if edition == 'pao' else 'printed', 'printed_get': format_get(row['t']),
                'who': (speaker_for(row.get('speaker') or '', mapping) or {}).get('who', ''),
                'speaker_label': row.get('speaker') or row.get('speaker_ocr', ''), 'printed_edition': row['edition'],
                'printed_text': row['text_ocr'], 'afj_key': '', 'alsj_key': '', 'pao_key': '', 'link_score': '',
                'status': 'printed_only', 'flags': row['flags'], 'group_id': '', '_tokens': row['tokens']}
    links.sort(key=lambda l: l['method'] != 'order_text_time')  # one-to-one links first
    members: dict[str, list[tuple[str, str]]] = defaultdict(list)  # event -> (source, block) of its group links
    for l in links:
        ev = events[l['event_id']]
        block = block_index[l['block_key']]
        field = 'afj_key' if l['source'] == 'AFJ' else 'alsj_key'
        if l['method'] == 'group_journal':
            block['_linked'] = l['event_id']  # a further journal row of this printed row
        if l['method'] != 'order_text_time':
            members[l['event_id']].append((field[:-4], l['block_key']))
        if ev[field] or l['method'] in ('group_journal', 'group_shifted'):
            continue  # a second journal row on the same printed row: keep the first, leave the other journal-only
        ev[field] = l['block_key']
        ev['link_score'] = max(float(ev['link_score'] or 0), l['score'])
        ev['status'] = 'linked'
        if l['method'] == 'order_text_time':
            block['_linked'] = l['event_id']
            ev.setdefault('_journal_time', l['block_key'])  # the AFJ first
            members[l['event_id']].append((field[:-4], l['block_key']))
    # Groups: printed events joined by a shared journal row, with all their journal rows.
    journal_position = {b['block_key']: i for rows, _ in journals.values() for i, b in enumerate(rows)}
    parent = {}

    def root(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    by_block = defaultdict(list)
    for event_id, keys in members.items():
        for _, key in keys:
            by_block[key].append(event_id)
    for ids in by_block.values():
        for other in ids[1:]:
            parent[root(other)] = root(ids[0])
    groups = defaultdict(list)
    for event_id in members:
        groups[root(event_id)].append(event_id)
    for ids in groups.values():
        keys = {k for e in ids for k in members[e]}
        if len(ids) > 1 or len(keys) > len({s for s, _ in keys}):
            ids.sort(key=lambda e: (int(events[e]['get_seconds']), e))
            for e in ids:
                events[e]['group_id'] = ids[0]
            events[ids[0]]['_group'] = (ids, sorted(keys, key=lambda k: (k[0], journal_position[k[1]])))
    for source, (rows, _) in journals.items():
        for b in rows:
            if b.get('_linked'):
                continue
            who = (speaker_for(b['speaker_label'], mapping) or {}).get('who', '') if b['kind'] != 'pao' else 'PAO'
            events[f'J-{b["block_key"]}'] = {
                'event_id': f'J-{b["block_key"]}', 'type': 'P' if b['kind'] == 'pao' else ('O' if b['onboard'] else 'T'),
                'get': b['get'], 'get_seconds': b['t'], 'get_basis': b['get_basis'], 'printed_get': '', 'who': who,
                'speaker_label': b['speaker_label'], 'printed_edition': '', 'printed_text': '',
                'afj_key': b['block_key'] if source == 'afj' else '', 'alsj_key': b['block_key'] if source == 'alsj' else '',
                'pao_key': '', 'link_score': '', 'status': 'journal_only', 'flags': b['flags'], 'group_id': '',
                '_tokens': []}
    # Journal GET is the published time when a journal witnesses the event (§6: never pooled as timing).
    for ev in events.values():
        key = ev.get('_journal_time')
        if key and ev['status'] == 'linked':
            block = block_index[key]
            if block['get_basis'] in ('anchor', 'stamp'):
                ev['get'], ev['get_seconds'], ev['get_basis'] = block['get'], block['t'], 'journal'
            if not ev['who'] or ev['who'] in ('SC',):
                ev['who'] = (speaker_for(block['speaker_label'], mapping) or {}).get('who', ev['who']) \
                    if block['kind'] != 'pao' else 'PAO'
    ordered = sorted(events.values(), key=lambda e: (int(e['get_seconds']), e['event_id']))

    # Wording review: printed tokens against each journal's spoken tokens.
    previous = {r['wording_key']: r for r in read_tsv(REVIEW / 'wording_choices.tsv')}
    decided = editorial_classes()
    wording = []
    for ev in ordered:
        if ev['status'] != 'linked' or (ev['group_id'] and ev['group_id'] != ev['event_id']):
            continue  # a later member of a group is reviewed with the group's first event
        # The unit under review: the group's printed rows and journal rows, or one linked row.
        ids, keys = ev.get('_group') or ([ev['event_id']], [(s, ev[f'{s}_key']) for s in ('afj', 'alsj')
                                                            if ev[f'{s}_key']])
        printed_text, printed_words = join_printed([(events[e]['printed_text'], events[e]['_tokens']) for e in ids])
        readings = {'printed': printed_words}
        spans_of = {}
        for source, key in keys:
            readings.setdefault(source, []).extend(block_index[key]['tokens'])
            spans_of.setdefault(source, []).append(journals[source][1][key])
        kinds = {s: wording_kind(readings['printed'], t) for s, t in readings.items() if s != 'printed'}
        if 'afj' in readings and 'alsj' in readings and readings['afj'] != readings['alsj']:
            kinds['journals'] = 'journals_differ'
        worst = 'substantive' if 'substantive' in kinds.values() or 'journals_differ' in kinds.values() else (
            'printed_garble_or_empty' if 'printed_garble_or_empty' in kinds.values() else (
                'ocr_noise' if 'ocr_noise' in kinds.values() else 'agree'))
        if worst == 'agree':
            continue
        key = f'{ev["event_id"]}:{short_hash("|".join(" ".join(readings.get(s, [])) for s in ("printed", "afj", "alsj")), 8)}'
        row = previous.get(key, {})
        proposed = proposed_reading(readings)
        if worst in ('substantive', 'printed_garble_or_empty') and proposed in ('afj', 'alsj'):
            j_spans = [s for block_spans in spans_of[proposed] for s in block_spans + [{'kind': 'spoken', 'text': ' '}]]
            if fills_garble(printed_garble_tokens(printed_text), journal_garble_tokens(j_spans)):
                worst = 'garble_filled'  # the journal's words where the PDF has garble: accepted
        shown = {s: ' '.join(journal_reading(b, decided) for b in spans_of.get(s, [])) for s in ('afj', 'alsj')}
        wording.append({'wording_key': key, 'event_id': ev['event_id'],
                        'group_events': ';'.join(ids) if len(ids) > 1 else '',
                        'joined': joined_note(ids, keys) if ev.get('_group') else '', 'get': ev['get'], 'who': ev['who'],
                        'kind': worst,
                        'status': 'human' if row.get('choice') else ('auto' if worst in AUTO_KINDS else 'review'),
                        'proposed': proposed, 'printed': printed_text, 'afj': shown['afj'], 'alsj': shown['alsj'],
                        'choice': row.get('choice', ''), 'reviewer': row.get('reviewer', ''),
                        'date': row.get('date', ''), 'note': row.get('note', ''),
                        'audio_locator': f'GET {ev["printed_get"] or ev["get"]}'})
    write_tsv(WORK / 'wording.tsv', WORDING_FIELDS, wording)
    write_tsv(SCRAPED / 'utterance_events.tsv', EVENT_FIELDS, ordered)
    counts = defaultdict(int)
    for ev in ordered:
        counts[ev['status']] += 1
    kinds = defaultdict(int)
    for w in wording:
        kinds[w['kind']] += 1
    print(f'reconcile: {len(ordered)} events {dict(counts)}; {len(links)} links; wording rows {dict(kinds)}')
    return 0


CHOICE_FIELDS = ('wording_key', 'event_id', 'get', 'choice', 'reviewer', 'date', 'note')


def record_wording_choices() -> int:
    """Save queue rows with a choice (printed/afj/alsj) and reviewer to MISSION_DATA/review/wording_choices.tsv."""
    decided = [w for w in read_tsv(WORK / 'wording.tsv') if w['choice'] and w['reviewer']]
    rows = {r['wording_key']: r for r in read_tsv(REVIEW / 'wording_choices.tsv')}
    for w in decided:
        if w['choice'] not in ('printed', 'afj', 'alsj'):
            raise ValueError(f'{w["wording_key"]}: choice must be printed, afj or alsj')
        rows[w['wording_key']] = {'wording_key': w['wording_key'], 'event_id': w['event_id'], 'get': w['get'],
                                  'choice': w['choice'], 'reviewer': w['reviewer'], 'date': w['date'] or today(),
                                  'note': w['note']}
    write_tsv(REVIEW / 'wording_choices.tsv', CHOICE_FIELDS, [rows[k] for k in sorted(rows)])
    return len(decided)
