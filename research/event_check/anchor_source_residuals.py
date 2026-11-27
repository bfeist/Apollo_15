"""Per SG source: median 07a event residual (GET - curve) on a tape's anchors edition. Read-only."""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path[:0] = ['scripts/30-track/07_retime', 'scripts/00_common']
from sg_inputs import read_tsv  # noqa: E402

EV = Path('D:/Apollo_15_30-Track/stage_evidence/retiming')
for tape in sys.argv[1:]:
    ed = sorted((EV / tape / 'sg_anchors').glob('anchors-*'), key=lambda p: p.stat().st_mtime)[-1]
    by = defaultdict(list)
    for e in read_tsv(ed / 'events.tsv'):
        if e['residual_s'] not in ('', None) and float(e['weight'] or 0) > 0:
            by[e['source']].append(float(e['residual_s']))
    print(tape, ed.name, {s: (len(v), round(float(np.median(v)), 3)) for s, v in sorted(by.items()) if len(v) >= 30})
