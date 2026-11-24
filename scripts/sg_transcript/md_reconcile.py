"""Step 5: reconcile the witnesses into utterance events.

The printed TEC, CM and LM rows form the event skeleton; PAO edition rows
labelled PAO form the skeleton for announcements. Journal utterances are
linked to printed rows per (journal, edition) by a monotonic alignment: each
journal row's candidates lie within a GET window, are scored by ordered token
similarity of the *spoken* spans only, and the best order-preserving chain is
kept (weighted longest increasing subsequence). Printed event IDs are reused,
so pilot catalog IDs (``AS15_TEC-p0849-r03``) stay valid.

Outputs (under ``.local/mission_data``, not committed):
* ``scraped_data/journal_links.tsv``: one row per accepted journal→printed link.
* ``scraped_data/utterance_events.tsv``: one row per event and its witnesses.
* ``scraped_data/time_offsets.tsv``: printed-minus-journal GET by chapter, to
  detect clock spans needing a constant shift (none is assumed).
* ``.local/mission_data/review/wording.tsv``: readings that disagree (the review queue).
* Choices made by listening are recorded in ``MISSION_DATA/review/wording_choices.tsv``
  (``record_wording_choices``) and reused on every later build.
"""
from __future__ import annotations

import bisect
import difflib
from collections import defaultdict
from statistics import median

from md_common import REVIEW, SCRAPED, WORK, today, format_get, read_tsv, short_hash, tokens, write_tsv
from md_speakers import load_map, speaker_for

LINK_FIELDS = ('block_key', 'source', 'edition', 'event_id', 'score', 'similarity', 'dt', 'method')
EVENT_FIELDS = ('event_id', 'type', 'get', 'get_seconds', 'get_basis', 'printed_get', 'who', 'speaker_label',
                'printed_edition', 'printed_text', 'afj_key', 'alsj_key', 'pao_key', 'link_score', 'status',
                'flags')
OFFSET_FIELDS = ('source', 'chapter', 'edition', 'links', 'median_dt', 'min_dt', 'max_dt', 'nonzero_share')
WORDING_FIELDS = ('wording_key', 'event_id', 'get', 'who', 'kind', 'status', 'printed', 'afj', 'alsj', 'choice',
                  'reviewer', 'date', 'note', 'audio_locator')
WINDOW = {'tec': 150, 'cm': 150, 'lm': 150, 'pao': 1800}
EDITION_KEY = {'AS15_TEC': 'tec', 'AS15_CM': 'cm', 'AS15_LM': 'lm', 'AS15_PAO': 'pao'}


def spoken_text(spans: list[dict]) -> str:
    return ''.join(s['text'] if s['kind'] == 'spoken' else ' ' for s in spans)


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
                    out[-1]['tokens'] += tokens(row['text_ocr'])
                continue
            last_speaker = 'PAO' if speaker in ('PAO', 'PAS', 'FAO') else speaker
            if last_speaker != 'PAO':
                continue
            row['speaker'] = 'PAO'
        elif row['timed_row'] == '':
            if out and 'untimed_continuation' in row['flags']:
                out[-1]['text_ocr'] += ' ' + row['text_ocr']
                out[-1]['tokens'] += tokens(row['text_ocr'])
            continue
        if row['get_seconds'] in ('', None):
            continue
        row['t'] = int(row['get_seconds'])
        row['tokens'] = tokens(row['text_ocr'])
        out.append(row)
    # Chain in GET order: separately printed CC–CM sections are not in time order with the CC–LM pages.
    return sorted(out, key=lambda r: r['t'])


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
    times = [p['t'] for p in printed]
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
    write_tsv(SCRAPED / 'journal_links.tsv', LINK_FIELDS, links)

    # Clock-offset census by chapter: printed GET minus journal GET of confident links.
    by_chapter = defaultdict(list)
    block_index = {b['block_key']: b for rows, _ in journals.values() for b in rows}
    for l in links:
        if l['similarity'] >= 0.8 and l['edition'] != 'AS15_PAO':
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
                'status': 'printed_only', 'flags': row['flags'], '_tokens': row['tokens']}
    for l in links:
        ev = events[l['event_id']]
        block = block_index[l['block_key']]
        field = 'afj_key' if l['source'] == 'AFJ' else 'alsj_key'
        if ev[field]:
            continue  # a second journal row on the same printed row: keep the first, leave the other journal-only
        ev[field] = l['block_key']
        ev['link_score'] = max(float(ev['link_score'] or 0), l['score'])
        ev['status'] = 'linked'
        block['_linked'] = l['event_id']
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
                'pao_key': '', 'link_score': '', 'status': 'journal_only', 'flags': b['flags'], '_tokens': []}
    # Journal GET is the published time when a journal witnesses the event (§6: never pooled as timing).
    for ev in events.values():
        key = ev['afj_key'] or ev['alsj_key']
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
    wording = []
    for ev in ordered:
        if ev['status'] != 'linked':
            continue
        readings = {'printed': ev['_tokens']}
        for field, source in (('afj_key', 'afj'), ('alsj_key', 'alsj')):
            if ev[field]:
                readings[source] = block_index[ev[field]]['tokens']
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
        wording.append({'wording_key': key, 'event_id': ev['event_id'], 'get': ev['get'], 'who': ev['who'],
                        'kind': worst, 'status': 'human' if row.get('choice') else ('auto' if worst == 'ocr_noise' else 'review'),
                        'printed': ev['printed_text'],
                        'afj': block_index[ev['afj_key']]['text'] if ev['afj_key'] else '',
                        'alsj': block_index[ev['alsj_key']]['text'] if ev['alsj_key'] else '',
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
