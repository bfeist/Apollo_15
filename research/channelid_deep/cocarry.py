"""Co-carry census of one tape: which delivered channels carry the same speech (stage 10 research).

For every transcribed content word on channel i, find the channels that have the same word within
+-WINDOW s. Writes the matrix C[i, j] = share of i's content words also heard on j, and per channel
the distribution of how many channels each of its words is heard on. No LLM, no ordering model.

    uv run python research/channelid_deep/cocarry.py A15_T510092
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts' / '30-track' / '10_channelid'))
import channelid_evidence as ce  # noqa: E402

TRANSCRIPTS = Path('D:/Apollo_15_30-Track/timing_transcripts/asr-c1198b48a6d3')
OUT = Path('D:/Apollo_15_30-Track/channel_id/deep/cocarry')
WINDOW = 2.0


def words_of(tape):
    out = {}
    for ch, (folder, meta) in sorted(ce.tape_sources(TRANSCRIPTS, tape).items()):
        rate = float(meta['source']['sample_rate'])
        origin = float(meta['source'].get('canonical_origin_s') or 0.0)
        ws = []
        for r in ce.read_rows(folder):
            if r.get('sf') is None or ce.is_noise(r):
                continue
            for w in r.get('words') or []:
                if w.get('sf') is None:
                    continue
                for cw in ce.content_words(w['w']):
                    ws.append((origin + w['sf'] / rate, cw))
        out[ch] = ws
    return out


def census(tape):
    words = words_of(tape)
    index = collections.defaultdict(set)
    for ch, ws in words.items():
        for t, w in ws:
            index[(w, int(t // WINDOW))].add(ch)
    chans = sorted(words)
    pos = {c: i for i, c in enumerate(chans)}
    n = len(chans)
    C = np.zeros((n, n))
    width = np.zeros((n, n + 1))
    for ch, ws in words.items():
        i = pos[ch]
        for t, w in ws:
            b = int(t // WINDOW)
            seen = index.get((w, b - 1), set()) | index.get((w, b), set()) | index.get((w, b + 1), set())
            for c in seen:
                C[i, pos[c]] += 1
            width[i, len(seen) - 1] += 1
        if ws:
            C[i] /= len(ws)
            width[i] /= len(ws)
    return chans, C, width, {c: len(words[c]) for c in chans}


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    tapes = (argv or sys.argv[1:])
    OUT.mkdir(parents=True, exist_ok=True)
    for tape in tapes:
        chans, C, width, nw = census(tape)
        (OUT / f'{tape}.json').write_text(json.dumps(dict(tape=tape, channels=chans, words=nw, cocarry=C.round(4).tolist(),
                                                        width=width.round(4).tolist())), encoding='utf-8')
        print(f'{tape}: share (%) of row channel words also heard on column channel')
        print('     words ' + ' '.join(f'{c:3d}' for c in chans) + '   own  2-4  5+')
        for i, c in enumerate(chans):
            row = ' '.join(' ..' if i == j else f'{int(round(100 * C[i, j])):3d}' for j in range(len(chans)))
            own, mid, wide = width[i, 0], width[i, 1:4].sum(), width[i, 4:].sum()
            print(f'CH{c:02d} {nw[c]:6d} {row}  {100 * own:4.0f} {100 * mid:4.0f} {100 * wide:3.0f}')


if __name__ == '__main__':
    main()
