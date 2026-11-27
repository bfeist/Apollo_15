"""Do retimed channels of one tape agree? Shared 4-word sequences: tape_start_s (after 07b) and
source canonical time (before) of channel B minus channel A. Read-only."""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np


def words(folder):
    rows = list(csv.DictReader(open(Path(folder) / 'timed_transcript.tsv', encoding='utf-8'), delimiter='\t'))
    meta = json.loads((Path(folder) / 'timed_transcript.json').read_text(encoding='utf-8'))
    origin = meta['time_basis']['canonical_origin_s']
    w = [(r['text'].lower(), float(r['tape_start_s']), origin + float(r['source_start_s']), r['get_start_s'])
         for r in rows if r['kind'] == 'word' and r['tape_start_s']]
    return w


def grams(w):
    d = defaultdict(list)
    for i in range(len(w) - 3):
        if w[i + 3][1] - w[i][1] < 3:
            d[' '.join(x[0] for x in w[i:i + 4])].append(w[i])
    return d


a, b = (grams(words(x)) for x in sys.argv[1:3])
after, before = [], []
for k, va in a.items():
    vb = b.get(k)
    if vb and len(va) == 1 and len(vb) == 1:
        if abs(vb[0][2] - va[0][2]) < 4:
            before.append(vb[0][2] - va[0][2])
            after.append(vb[0][1] - va[0][1])
print(f'{len(after)} shared sequences; B-A before (05b canonical): median {np.median(before):+.3f} s; '
      f'after (retimed tape clock): median {np.median(after):+.3f} s, MAD {np.median(np.abs(np.array(after) - np.median(after))):.3f}')
