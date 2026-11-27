"""Per NARA tape: GET(IA reel) - GET(NARA voice) for the same SG word sequences, from the 07a reference. Read-only."""
import json
import sys
from collections import defaultdict

import numpy as np

REF = sys.argv[1] if len(sys.argv) > 1 else 'D:/Apollo_15_30-Track/stage_evidence/retiming/sg_reference/reference-115b2747e3bc0e36'
z = np.load(f'{REF}/reference.npz')
man = json.load(open(f'{REF}/manifest.json'))
kind = np.array([s['kind'] for s in man['sources']])
name = np.array([s['name'] for s in man['sources']])
tok, get, src, seg = z['tok'], z['get'], z['src'], z['seg']
N = 4
grams = {'nara': defaultdict(list), 'ia': defaultdict(list)}
for i in np.unique(src):
    m = src == i
    t, g, k = tok[m], get[m], seg[m]
    o = np.argsort(g)
    t, g, k = t[o], g[o], k[o]
    for j in range(len(t) - N + 1):
        if g[j + N - 1] - g[j] < 3:
            grams[kind[i]][' '.join(t[j:j + N])].append((g[j], i, k[j]))
by_tape = defaultdict(list)
by_seg = defaultdict(list)
for key, nv in grams['nara'].items():
    iv = grams['ia'].get(key)
    if not iv or len(nv) > 3:
        continue
    for gn, i, _ in nv:
        d = [gi - gn for gi, _, kk in iv if abs(gi - gn) < 5]
        if d:
            by_tape[name[i]].append(np.median(d))
            for gi, ii, kk in iv:
                if abs(gi - gn) < 5:
                    by_seg[(name[i], man['segments'][kk]['key'])].append(gi - gn)
for t in sorted(by_tape):
    v = np.asarray(by_tape[t])
    segs = {k[1]: (len(x), round(float(np.median(x)), 3)) for k, x in by_seg.items() if k[0] == t and len(x) >= 20}
    print(f'{t}: n={len(v):5d} median IA-NARA {np.median(v):+.3f} s; per IA segment {segs}')
