"""Are the journals' square-bracketed words in the audio? A check against the Whisper words.

For every timing-anchored event (``sg/sg_anchors.tsv``, ok or flagged) with a journal row, the
journal's spoken words and its bracketed words are aligned, in order, with the SG-reel ASR words
around the anchor. A bracket is judged only where the spoken words on both sides of it are found
in the ASR (a closed gap). It is ``heard`` when at least half its words fill that gap, ``absent``
when the gap holds none of them. Short spoken spans (1-4 words) are scored the same way as the
recall baseline of the ASR.

Run: uv run python research/mission_timing/journal_bracket_audio_check.py [--examples N]
Writes .local/mission_data/sg/journal_bracket_audio.tsv.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'sg_transcript'))
from md_common import LOCAL, MISSION_DATA, SCRAPED, read_tsv, tokens, write_tsv  # noqa: E402
from md_editorial import rule_judgement  # noqa: E402
from md_reconcile import comparable  # noqa: E402
from md_sg import SG_STORE, load_asr, reel_identity  # noqa: E402

PAD_S = 4.0
WORD_S = 0.45
FIELDS = ('span_key', 'source', 'bracket', 'rule_class', 'verdict', 'heard_share', 'reel', 'source_s', 'span',
          'asr_gap', 'utterance')


def sequence(spans):
    """Comparable tokens tagged with the span index they come from."""
    out = []
    for i, s in enumerate(spans):
        text = s['text'] if s['kind'] == 'spoken' else s['text']
        out += [(t, i) for t in comparable(tokens(text))]
    return out


def judge(spans, asr):
    seq = sequence(spans)
    words = [w['tok'] for w in asr]
    if not seq or not words:
        return []
    matcher = difflib.SequenceMatcher(None, [t for t, _ in seq], words, autojunk=False)
    where = {}
    for m in matcher.get_matching_blocks():
        for k in range(m.size):
            where[m.a + k] = m.b + k
    out = []
    for i, s in enumerate(spans):
        idx = [k for k, (_, si) in enumerate(seq) if si == i]
        if not idx:
            continue
        before = [k for k in range(idx[0]) if seq[k][1] != i and spans[seq[k][1]]['kind'] == 'spoken']
        after = [k for k in range(idx[-1] + 1, len(seq)) if spans[seq[k][1]]['kind'] == 'spoken']
        left = next((where[k] for k in reversed(before) if k in where), None) if before else -1
        right = next((where[k] for k in after if k in where), None) if after else len(words)
        if left is None or right is None or (left == -1 and right == len(words)):
            continue  # the gap is not closed by found spoken words
        gap = words[left + 1:right]
        found = sum(1 for k in idx if k in where)
        share = found / len(idx)
        if s['kind'] == 'spoken':
            out.append((i, 'spoken', share, gap))
        else:
            verdict = 'heard' if share >= 0.5 else ('absent' if found == 0 else 'partial')
            out.append((i, verdict, share, gap))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--examples', type=int, default=8)
    args = parser.parse_args()
    reels = {r['reel']: r for r in read_tsv(MISSION_DATA / 'sg_reels.tsv')}
    events = {e['event_id']: e for e in read_tsv(SCRAPED / 'utterance_events.tsv')}
    spans = defaultdict(list)
    for source in ('afj', 'alsj'):
        for s in read_tsv(SCRAPED / f'journal_spans_{source}.tsv'):
            spans[s['block_key']].append(s)
    by_reel = defaultdict(list)
    for a in read_tsv(LOCAL / 'sg' / 'sg_anchors.tsv'):
        ev = events.get(a['event_id'])
        if a['status'] in ('ok', 'flagged') and ev and not ev.get('group_id'):
            by_reel[a['reel']].append((a, ev))
    rows, spoken = [], Counter()
    for reel, items in sorted(by_reel.items()):
        segments = load_asr(reel_identity(SG_STORE / reels[reel]['asr_settings_id'], reel))
        words = sorted((w for seg in segments for w in seg['words']), key=lambda w: w['s'])
        starts = [w['s'] for w in words]
        import bisect
        for a, ev in items:
            for field in ('afj_key', 'alsj_key'):
                key = ev[field]
                block = sorted(spans.get(key, []), key=lambda s: int(s['index']))
                if not any(s['kind'] == 'editorial' for s in block):
                    continue
                n = len(comparable(tokens(' '.join(s['text'] for s in block))))
                t0 = float(a['source_s']) - PAD_S
                t1 = max(float(a['source_end_s']), float(a['source_s']) + n * WORD_S) + PAD_S
                asr = words[bisect.bisect_left(starts, t0):bisect.bisect_right(starts, t1)]
                for i, verdict, share, gap in judge(block, asr):
                    if verdict == 'spoken':
                        # Baseline on spans as short as most brackets (1-4 words), judged the same way.
                        if 1 <= len(comparable(tokens(block[i]['text']))) <= 4:
                            spoken['found' if share >= 0.5 else 'missed'] += 1
                        continue
                    s = block[i]
                    rows.append({'span_key': s['span_key'], 'source': field[:-4].upper(), 'bracket': s['bracket'],
                                 'rule_class': rule_judgement(block, i)['class'], 'verdict': verdict,
                                 'heard_share': round(share, 2), 'reel': reel, 'source_s': a['source_s'],
                                 'span': s['source_chars'], 'asr_gap': ' '.join(gap),
                                 'utterance': ''.join(x['source_chars'] for x in block)})
    write_tsv(LOCAL / 'sg' / 'journal_bracket_audio.tsv', FIELDS, rows)
    total = spoken['found'] + spoken['missed']
    print(f'short spoken spans heard by the same test (ASR recall baseline): {spoken["found"]}/{total} '
          f'= {spoken["found"] / max(1, total):.1%}')
    table = defaultdict(Counter)
    for r in rows:
        table[(r['source'], r['bracket'])][r['verdict']] += 1
        table[(r['source'], r['bracket'], r['rule_class'])][r['verdict']] += 1
    for k in sorted(table, key=lambda k: (len(k), k)):
        c = table[k]
        n = sum(c.values())
        print(f'{"/".join(k):32} n={n:5}  heard {c["heard"] / n:6.1%}  partial {c["partial"] / n:6.1%}  '
              f'absent {c["absent"] / n:6.1%}')
    heard = [r for r in rows if r['bracket'] == '[' and r['verdict'] == 'heard']
    print(f'\n[ ] spans judged heard ({len(heard)}), examples:')
    for r in heard[:args.examples]:
        print(f'  {r["source"]} {r["rule_class"]:10} {r["span"]!r:40} gap={r["asr_gap"]!r}\n      {r["utterance"][:150]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
