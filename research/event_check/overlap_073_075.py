"""Tie T510073 (speech-matched to NARA 608) to T510075's time code through their overlap.

Read-only. For each word sequence heard on both tapes in the overlap:

  D = GET of the T510073 word (07a anchors curve, which is placed on the NARA voice clock)
      - GET of the T510075 word read straight from T510075's IRIG-B time code (no row correction)

Reported per T510075 channel with its stagger group. One T510075 row sits on the time-code row; for
it D = (NARA voice lateness). The other row differs by the 1.38 s stagger. Also prints the launch
countdown on T510073 on the same NARA-placed clock, per row.

  uv run python research/event_check/overlap_073_075.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/00_common'),
                str(ROOT / 'scripts/30-track/06_transcribe'), str(ROOT / 'scripts/sg-audio/02_sg-transcript')]
from sg_curve import Curve  # noqa: E402
from sg_inputs import load_words, read_tsv, tape_channels  # noqa: E402

EV = Path('D:/Apollo_15_30-Track/stage_evidence/retiming')
CACHE = EV / '_cache' / 'words'
RANGE_ZERO = 206 * 86400 + 13 * 3600 + 34 * 60
N = 4


def stagger(tape):
    ed = sorted((EV / tape / 'sg_anchors').glob('anchors-*'))[-1]
    out = defaultdict(list)
    for r in read_tsv(ed / 'stagger.tsv'):
        if r['measured'] in ('True', True) and r['group'] not in ('', None):
            out[int(r['channel'])].append((int(r['group']), float(r['tape_level_s'])))
    res = {}
    for c, v in out.items():
        g = max({x[0] for x in v}, key=[x[0] for x in v].count)
        res[c] = (g, float(np.median([x[1] for x in v if x[0] == g])))
    return ed, res


def irig_clock(tape):
    f = np.asarray(json.loads((EV / 'irig' / tape / 'decode' / 'CH30.json').read_text())['frames'], float)
    d = f[:, 1] - f[:, 0]
    f = f[np.abs(d - np.median(d)) < 600]
    keep = np.ones(len(f), bool)  # drop misread labels against a running fit
    for _ in range(3):
        p = np.polyfit(f[keep, 0], f[keep, 1], 3)
        r = f[:, 1] - np.polyval(p, f[:, 0])
        keep = np.abs(r - np.median(r[keep])) < 2.0
    t, c = f[keep, 0], f[keep, 1]

    def gmt(x):  # local linear interpolation between strict frames
        return np.interp(x, t, c)
    return gmt


def grams(words, times):
    out = defaultdict(list)
    for i in range(len(words) - N + 1):
        if times[i + N - 1] - times[i] < 3.0:
            out[' '.join(words[i:i + N])].append(times[i])
    return out


def main():
    ed73, st73 = stagger('A15_T510073')
    _, st75 = stagger('A15_T510075')
    curve = Curve.from_json(json.loads((ed73 / 'curve.json').read_text()))
    meta = json.loads((ed73 / 'curve.json').read_text())
    print('T510073 anchors', ed73.name, 'reference group', meta['reference_group'], 'IA offsets b', meta['b'])
    gmt75 = irig_clock('A15_T510075')

    # T510073 pooled on its reference-row GET (NARA-placed)
    pool73 = defaultdict(list)
    countdown = defaultdict(list)
    for ch in tape_channels('A15_T510073'):
        if ch['problem'] or ch['channel'] not in st73:
            continue
        g, s = st73[ch['channel']]
        w = load_words(Path(ch['identity_dir']), CACHE)
        t = ch['origin_s'] + w.s
        get = curve.get(t - s)  # placed on the reference row
        m = (get > 16000) & (get < 18500)
        for k, v in grams(list(w.tok[m]), get[m]).items():
            pool73[k].extend(v)
        m = np.abs(get) < 15
        raw = curve.get(t)  # same clock, without the row correction
        for tok, gg in zip(w.tok[m], raw[m]):
            if tok.isdigit() and 1 <= int(tok) <= 10:
                countdown[(g, s)].append(gg + int(tok))  # lag of the number call behind T-N
    print('\nLaunch countdown on T510073 (seconds behind the tick, NARA-placed clock, no row correction):')
    for (g, s), v in sorted(countdown.items()):
        v = np.asarray(v)
        v = v[np.abs(v - np.median(v)) < 0.7]
        print(f'  row g{g} (stagger {s:+.3f}): n={len(v):4d} median {np.median(v):+.3f} s')

    print('\nOverlap: D = GET(T510073, NARA-placed) - GET(T510075, own time code), per T510075 channel')
    rows = defaultdict(list)
    for ch in tape_channels('A15_T510075'):
        if ch['problem'] or ch['channel'] == 30:
            continue
        w = load_words(Path(ch['identity_dir']), CACHE)
        t = ch['origin_s'] + w.s
        get = gmt75(t) - RANGE_ZERO
        m = get < 18500
        diffs = []
        for k, v in grams(list(w.tok[m]), get[m]).items():
            if len(v) != 1 or k not in pool73:
                continue
            o = np.asarray(pool73[k]) - v[0]
            o = o[np.abs(o) < 5]
            if len(o):
                diffs.append(np.median(o))
        if len(diffs) >= 5:
            g = st75.get(ch['channel'], ('-', np.nan))
            rows[g[0]].extend(diffs)
            print(f'  CH{ch["channel"]:02d} g{g[0]} (stagger {g[1]:+.3f}): n={len(diffs):4d} D median {np.median(diffs):+.3f} s')
    for g, v in sorted(rows.items(), key=lambda x: str(x[0])):
        v = np.asarray(v)
        print(f'row g{g}: n={len(v)} D median {np.median(v):+.3f} s, IQR {np.subtract(*np.percentile(v, [75, 25])):.3f}')


if __name__ == '__main__':
    main()
