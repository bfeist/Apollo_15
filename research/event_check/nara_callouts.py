"""Words of a NARA flight tape near a GET, on its IRIG-B GET (GET = output s + segment constant).

  uv run python research/event_check/nara_callouts.py --tape 608-AAA --get 0 --window 40
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/00_common'),
                str(ROOT / 'scripts/30-track/06_transcribe'), str(ROOT / 'scripts/sg-audio/02_sg-transcript')]
from sg_inputs import load_words, nara_sources, source_get  # noqa: E402

CACHE = Path('D:/Apollo_15_30-Track/stage_evidence/retiming/_cache/words')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tape', default='608-AAA')
    ap.add_argument('--get', type=float, default=0.0, help='GET seconds')
    ap.add_argument('--window', type=float, default=40.0)
    ap.add_argument('--words', default='zero,one,two,three,four,five,liftoff,lift,ignition,sequence,start')
    args = ap.parse_args()
    src = next(s for s in nara_sources() if s['name'] == args.tape)
    print(src['name'], src['respeed'], [(g['key'], g['constant_s']) for g in src['segments']])
    w = load_words(Path(src['identity_dir']), CACHE)
    get, seg = source_get(src, w.s)
    m = np.abs(get - args.get) < args.window
    flags = set(args.words.split(','))
    print(' '.join(f'[{t}@{g - args.get:+.2f}]' if t in flags else t for t, g in zip(w.tok[m], get[m])))


if __name__ == '__main__':
    main()
