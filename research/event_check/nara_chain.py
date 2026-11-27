"""GET(NARA tape B voice) - GET(NARA tape A voice) for the same words where consecutive tapes overlap. Read-only."""
import json
import sys
from collections import defaultdict

import numpy as np

REF = sys.argv[1] if len(sys.argv) > 1 else 'D:/Apollo_15_30-Track/stage_evidence/retiming/sg_reference/reference-115b2747e3bc0e36'
z = np.load(f'{REF}/reference.npz')
man = json.load(open(f'{REF}/manifest.json'))
kind = [s['kind'] for s in man['sources']]
name = [s['name'] for s in man['sources']]
tok, get, src = z['tok'], z['get'], z['src']
N = 4
per = {}
for i in np.unique(src):
    if kind[i] != 'nara':
        continue
    m = src == i
    t, g = tok[m], get[m]
    o = np.argsort(g)
    t, g = t[o], g[o]
    d = defaultdict(list)
    for j in range(len(t) - N + 1):
        if g[j + N - 1] - g[j] < 3:
            d[' '.join(t[j:j + N])].append(g[j])
    per[name[i]] = d
names = sorted(per)
for a, b in zip(names[:-1], names[1:]):
    diffs = []
    for k, va in per[a].items():
        vb = per[b].get(k)
        if vb and len(va) == 1 and len(vb) == 1 and abs(vb[0] - va[0]) < 5:
            diffs.append(vb[0] - va[0])
    v = np.asarray(diffs)
    print(f'{b} - {a}: n={len(v):4d} median {np.median(v) if len(v) else float("nan"):+.3f} s, '
          f'MAD {np.median(np.abs(v - np.median(v))) if len(v) else float("nan"):.3f}')
