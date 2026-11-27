"""After a 07a fit: NARA voice-row review (docs/23). Read-only.

For every fitted tape, the fit's residuals (GET - curve) of NARA events in 30-minute GET bins,
pooled per NARA tape. A bin whose median sits near +/-0.34..0.68 s while its neighbours are near 0
is where that NARA tape's voice changes head row (as 646-AAA at 225-226 h): add an interval to
catalog/nara_voice/nara_voice_offsets.json and rebuild from the reference step.

  uv run python research/event_check/nara_voice_check.py <sg_fit edition folder>
"""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/00_common')]
from sg_inputs import read_json  # noqa: E402

fit = Path(sys.argv[1])
bins = defaultdict(list)
plans = sorted((fit / 'tapes').glob('*/curve.json'))
if not plans:
    sys.exit(f'No tapes/*/curve.json under {fit}: not a fit edition (see fit_review.py, which finds it).')
for plan in plans:
    model = read_json(plan)
    for a in model['anchors']:
        if a['ref_kind'] == 'nara' and a['kind'] != 'irig':
            bins[int(a['get_s'] // 1800)].append(a['residual_s'])
print('GET bin (h)   n    median residual (s)')
flag = []
for k in sorted(bins):
    v = np.asarray(bins[k])
    if len(v) < 30:
        continue
    med = float(np.median(v))
    mark = '  <-- review' if abs(med) > 0.25 else ''
    if mark:
        flag.append(k)
    print(f'{k * 0.5:7.1f}  {len(v):5d}   {med:+.3f}{mark}')
print(f'{len(flag)} bins to review' if flag else 'no NARA bin is off by a voice-row step')
