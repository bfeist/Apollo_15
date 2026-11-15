"""Summarize an E7 sweep directory against saved v5 union coverage."""
import json, glob, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from sweep import v5_union, _merge

d = sys.argv[1]
by = defaultdict(list)
for f in glob.glob(d + '/*.json'):
    if f.endswith('summary.json'): continue
    o = json.loads(Path(f).read_text()); by[o['tape']].append(o)
rows = []
for tape in sorted(by):
    sp, D = v5_union(tape); sp = _merge(sp)
    v5 = 100 * sum(b - a for a, b in sp) / D
    ws = by[tape]
    el = [o for o in ws if o.get('eligible')]
    agree = [o.get('n_agreeing', 0) for o in ws]
    rms = [o.get('consensus_rms_pct') for o in el]
    rows.append(dict(tape=tape, v5_union_pct=round(v5, 1), windows=len(ws), eligible=len(el),
                     eligible_pct=round(100 * len(el) / len(ws), 0), median_agreeing_ch=float(np.median(agree)),
                     consensus_rms_pct_median=round(float(np.median(rms)), 2) if rms else None,
                     ineligible_t0=[o['t0'] for o in ws if not o.get('eligible')]))
tot = sum(r['windows'] for r in rows); el = sum(r['eligible'] for r in rows)
print('| tape | saved v5 union % | windows eligible | median agreeing channels | flutter rms % | ineligible window t0 |')
print('|---|---:|---:|---:|---:|---|')
for r in rows:
    print(f"| {r['tape']} | {r['v5_union_pct']} | {r['eligible']}/{r['windows']} | {r['median_agreeing_ch']:.0f} | {r['consensus_rms_pct_median']} | {', '.join(str(int(t)) for t in r['ineligible_t0'])} |")
print(f'TOTAL eligible {el}/{tot} = {100*el/tot:.1f}%; median v5 union {np.median([r["v5_union_pct"] for r in rows]):.1f}%')
Path(d, 'summary.json').write_text(json.dumps(rows, indent=1))
