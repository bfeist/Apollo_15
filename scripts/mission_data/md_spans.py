"""§4 steps 2–6: judge every editorial span and record one decision per span.

Three independent witnesses: pattern rules, alignment with the linked 1971
transcript row, and (``--ai``) the language-model label. Their judgements are
combined, and agreement is applied automatically. Disagreement, low AI
confidence and any restoration go to the review queue. Human decisions in
``MISSION_DATA/review/editorial_decisions.tsv`` are kept for unchanged spans
— the span key includes a hash of the span text, so an edited span returns
to the queue.

Provisional thresholds (until the calibration corpus in
tests/test_mission_editorial.py measures them per class): pause, garble,
non-speech, unit and literal rule labels apply without an AI label when the
transcript alignment does not report a restoration. Every other class needs
the AI to agree.
"""
from __future__ import annotations

import difflib
from collections import Counter, defaultdict

from md_common import REVIEW, SCRAPED, WORK, read_tsv, today, tokens, write_tsv
from md_editorial import rule_judgement

DECISION_FIELDS = ('span_key', 'source', 'block_key', 'get', 'class', 'action', 'marker', 'status', 'agreed',
                   'rule', 'rule_class', 'rule_action', 'pdf', 'ai_class', 'ai_action', 'ai_confidence', 'ai_backend',
                   'reviewer', 'date', 'note', 'span', 'utterance', 'printed')
QUEUE_FIELDS = ('span_key', 'get', 'speaker', 'span', 'utterance', 'previous', 'next', 'printed', 'rule_class',
                'rule_action', 'pdf', 'ai_class', 'ai_action', 'ai_confidence', 'reason', 'class', 'action',
                'reviewer', 'note')
RULE_ONLY = {'pause', 'garble', 'nonspeech', 'unit', 'literal'}
AI_MIN_CONFIDENCE = 0.8
REMOVING = {'remove', 'annotate', 'to_commentary', 'garble'}


def alignment_judgement(spans: list[dict], index: int, printed: list[str]) -> str:
    """``insertion`` | ``restoration`` | ``n/a`` for editorial span ``index`` against printed tokens."""
    span_tokens = tokens(spans[index]['text'])
    if not printed or not span_tokens:
        return 'n/a'
    seq, owner = [], []
    for k, s in enumerate(spans):
        t = tokens(s['text'])
        seq.extend(t)
        owner.extend([k] * len(t))
    matcher = difflib.SequenceMatcher(None, seq, printed, autojunk=False)
    matched = set()
    for block in matcher.get_matching_blocks():
        matched.update(range(block.a, block.a + block.size))
    mine = [pos for pos, k in enumerate(owner) if k == index]
    hit = sum(pos in matched for pos in mine)
    if hit == 0:
        return 'insertion'
    if hit >= max(1, len(mine) * 0.6):
        return 'restoration'
    return 'n/a'


def combine(rule: dict, pdf: str, ai: dict | None, ai_other: dict | None) -> tuple[str, str, str, list[str], str]:
    """(class, action, status, agreed methods, reason)."""
    agreed = []
    r_cls, r_act = rule['class'], rule['action']
    if r_act:
        agreed.append('rule')
    pdf_ok = pdf != 'restoration'
    if pdf == 'insertion' and r_act in REMOVING:
        agreed.append('pdf')
    if pdf == 'restoration' and r_act == 'keep':
        agreed.append('pdf')
    if ai_other and ai and (ai_other['action'] != ai['action']):
        return r_cls, r_act, 'review', agreed, 'hgx_strata_disagree'
    if ai:
        same = ai['action'] == r_act
        if same:
            agreed.append('ai')
        if not r_act:
            if ai['confidence'] >= AI_MIN_CONFIDENCE and pdf_ok and not (
                    pdf == 'insertion' and ai['action'] == 'keep'):
                return ai['class'], ai['action'], 'auto', ['ai'] + (['pdf'] if pdf == 'insertion' else []), ''
            return ai['class'], ai['action'], 'review', agreed, 'ai_only_low_confidence_or_pdf'
        if not same:
            return r_cls, r_act, 'review', agreed, 'rule_ai_disagree'
        if ai['confidence'] < AI_MIN_CONFIDENCE:
            return r_cls, r_act, 'review', agreed, 'low_ai_confidence'
        if not pdf_ok and r_act != 'keep':
            return r_cls, r_act, 'review', agreed, 'restoration'
        return r_cls, r_act, 'auto', agreed, ''
    if r_act and r_cls in RULE_ONLY and pdf_ok:
        return r_cls, r_act, 'auto', agreed, ''
    if not pdf_ok:
        return r_cls, r_act, 'review', agreed, 'restoration'
    return r_cls, r_act, 'review', agreed, 'needs_ai' if r_act else 'no_rule'


def load_context():
    blocks, spans = {}, defaultdict(list)
    order = []
    for source in ('afj', 'alsj'):
        for b in read_tsv(SCRAPED / f'journal_{source}.tsv'):
            blocks[b['block_key']] = b
            if b['kind'] in ('utterance', 'pao', 'event'):
                order.append(b['block_key'])
        for s in read_tsv(SCRAPED / f'journal_spans_{source}.tsv'):
            spans[s['block_key']].append(s)
    printed_text = {}
    for edition in ('tec', 'cm', 'lm', 'pao'):
        for r in read_tsv(SCRAPED / f'transcript_{edition}.tsv'):
            printed_text[r['event_id']] = r['text_ocr']
    links = {l['block_key']: l['event_id'] for l in read_tsv(SCRAPED / 'journal_links.tsv')}
    return blocks, spans, order, printed_text, links


def marked(spans: list[dict], index: int) -> str:
    return ''.join(('⟦' + s['source_chars'] + '⟧') if k == index else s['source_chars'] for k, s in enumerate(spans))


def main(args=None) -> int:
    if args is not None and getattr(args, 'record', False):
        print(f'recorded {record_queue_decisions()} decisions')
    blocks, spans, order, printed_text, links = load_context()
    previous = {r['span_key']: r for r in read_tsv(REVIEW / 'editorial_decisions.tsv')}
    position = {k: i for i, k in enumerate(order)}
    items = []
    for key in order:
        block_spans = spans.get(key, [])
        printed = printed_text.get(links.get(key, ''), '')
        for index, span in enumerate(block_spans):
            if span['kind'] != 'editorial':
                continue
            i = position[key]
            context = [blocks[order[k]]['text'][:300] for k in (i - 1, i + 1) if 0 <= k < len(order)]
            items.append({'span_key': span['span_key'], 'block_key': key, 'index': index, 'spans': block_spans,
                          'utterance': marked(block_spans, index), 'span': span['source_chars'], 'printed': printed,
                          'context': context})
    ai_backend = getattr(args, 'backend', 'hgx') if args is not None else 'hgx'
    labels = {}
    if args is not None and getattr(args, 'ai', False):
        import md_ai
        wanted = []
        for item in items:
            rule = rule_judgement(item['spans'], item['index'])
            if rule['class'] not in RULE_ONLY or not rule['action']:
                wanted.append(item)
        try:
            md_ai.label_spans(wanted, ai_backend, budget=args.budget, window=args.window)
        except md_ai.BackendError as exc:
            print(f'AI stopped: {exc}')
    import md_ai
    hgx, strata = md_ai.cached_labels('hgx'), md_ai.cached_labels('strata')
    decisions, queue = [], []
    for item in items:
        rule = rule_judgement(item['spans'], item['index'])
        pdf = alignment_judgement(item['spans'], item['index'], tokens(item['printed']))
        ai = hgx.get(item['span_key']) or strata.get(item['span_key'])
        other = strata.get(item['span_key']) if item['span_key'] in hgx else None
        cls, action, status, agreed, reason = combine(rule, pdf, ai, other)
        prior = previous.get(item['span_key'])
        reviewer, date, note = '', '', rule.get('note', '')
        if prior and prior.get('reviewer'):
            cls, action, status = prior['class'], prior['action'], 'human'
            reviewer, date, note = prior['reviewer'], prior['date'], prior['note']
            agreed = sorted(set(agreed) | {'human'})
        block = blocks[item['block_key']]
        marker = rule['marker'] if (cls, action) == ('nonspeech', 'keep') else ''
        if (cls, action) == ('nonspeech', 'keep') and not marker:
            from md_editorial import marker_for
            marker = marker_for(item['spans'][item['index']]['text']) or ''
        row = {'span_key': item['span_key'], 'source': block['source'], 'block_key': item['block_key'],
               'get': block['get'], 'class': cls, 'action': action, 'marker': marker, 'status': status,
               'agreed': ','.join(agreed), 'rule': rule['rule'], 'rule_class': rule['class'],
               'rule_action': rule['action'], 'pdf': pdf, 'ai_class': (ai or {}).get('class', ''),
               'ai_action': (ai or {}).get('action', ''), 'ai_confidence': (ai or {}).get('confidence', ''),
               'ai_backend': f'{ai["backend"]}:{ai["model"]}' if ai else '', 'reviewer': reviewer, 'date': date,
               'note': note, 'span': item['span'], 'utterance': item['utterance'][:600],
               'printed': item['printed'][:400]}
        decisions.append(row)
        if status == 'review':
            i = position[item['block_key']]
            queue.append({**{f: row[f] for f in QUEUE_FIELDS if f in row}, 'speaker': block['speaker_label'],
                          'previous': blocks[order[i - 1]]['text'][:200] if i > 0 else '',
                          'next': blocks[order[i + 1]]['text'][:200] if i + 1 < len(order) else '',
                          'reason': reason, 'class': '', 'action': '', 'reviewer': '', 'note': ''})
    write_tsv(WORK / 'editorial_spans.tsv', DECISION_FIELDS, decisions)
    write_tsv(WORK / 'editorial_queue.tsv', QUEUE_FIELDS, queue)
    status = Counter(d['status'] for d in decisions)
    reasons = Counter(q['reason'] for q in queue)
    print(f'spans: {len(decisions)} editorial spans {dict(status)}; queue reasons {dict(reasons)}')
    print('  Queue: .local/mission_data/review/editorial_queue.tsv. Fill class, action and reviewer, then run '
          '`mission_data.py spans --record` to save them in MISSION_DATA/review/editorial_decisions.tsv.')
    return 0


HUMAN_FIELDS = ('span_key', 'class', 'action', 'marker', 'reviewer', 'date', 'note', 'span', 'get')


def record_queue_decisions() -> int:
    """Append decided queue rows (reviewer and action filled) to MISSION_DATA/review/editorial_decisions.tsv."""
    decided = [q for q in read_tsv(WORK / 'editorial_queue.tsv') if q['reviewer'] and q['action']]
    rows = {r['span_key']: r for r in read_tsv(REVIEW / 'editorial_decisions.tsv')}
    for q in decided:
        rows[q['span_key']] = {'span_key': q['span_key'], 'class': q['class'] or q['rule_class'], 'action': q['action'],
                               'marker': '', 'reviewer': q['reviewer'], 'date': today(), 'note': q['note'],
                               'span': q['span'], 'get': q['get']}
    write_tsv(REVIEW / 'editorial_decisions.tsv', HUMAN_FIELDS, [rows[k] for k in sorted(rows)])
    return len(decided)
