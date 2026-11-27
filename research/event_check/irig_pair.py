"""Two time-coded tapes covering the same hours: D = GET(tape B, own time code) - GET(tape A, own time code)
for the same words, per channel of B, against A's words on one stagger row. Read-only.

  uv run python research/event_check/irig_pair.py A15_T510075 A15_T510076
"""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from overlap_073_075 import CACHE, RANGE_ZERO, grams, irig_clock, stagger  # noqa: E402
from sg_inputs import load_words, tape_channels  # noqa: E402


def main():
    a, b = sys.argv[1], sys.argv[2]
    _, sta = stagger(a)
    ca, cb = irig_clock(a), irig_clock(b)
    pools = {0: defaultdict(list), 1: defaultdict(list)}
    for ch in tape_channels(a):
        if ch['problem'] or ch['channel'] not in sta:
            continue
        w = load_words(Path(ch['identity_dir']), CACHE)
        get = ca(ch['origin_s'] + w.s) - RANGE_ZERO
        for k, v in grams(list(w.tok), get).items():
            pools[sta[ch['channel']][0]][k].extend(v)
    print(f'D = GET({b}) - GET({a} row gX), both from their own time code')
    for ch in tape_channels(b):
        if ch['problem'] or ch['channel'] == 30:
            continue
        w = load_words(Path(ch['identity_dir']), CACHE)
        get = cb(ch['origin_s'] + w.s) - RANGE_ZERO
        out = []
        for g in (0, 1):
            d = []
            for k, v in grams(list(w.tok), get).items():
                if len(v) == 1 and k in pools[g]:
                    o = v[0] - np.asarray(pools[g][k])
                    o = o[np.abs(o) < 5]
                    if len(o):
                        d.append(np.median(o))
            out.append(f'vs {a[-6:]} g{g}: n={len(d):5d} D {np.median(d) if d else np.nan:+.3f}')
        print(f'  {b[-6:]} CH{ch["channel"]:02d}: ' + ' | '.join(out))


if __name__ == '__main__':
    main()
