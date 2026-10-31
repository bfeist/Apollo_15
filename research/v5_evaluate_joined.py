"""Audit exact joined maps for a bounded continuous plan; no parameters are fitted."""
import argparse, json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from channel_review import prepare, continuous_map, joined_map_checks

p = argparse.ArgumentParser()
p.add_argument('plan', type=Path)
p.add_argument('output', type=Path)
a = p.parse_args()
plan = json.loads(a.plan.read_text(encoding='utf-8-sig'))
start = plan['parameters']['start']; duration = plan['parameters']['duration']
rows = []
for channel in plan['inventory']['channels']:
    if channel['role'] == 'timing_only':
        continue
    n = channel['channel']
    try:
        path, _, row, regions, start1, _, frames = prepare(a.plan, n, start, duration)
        t, nominal, speed, stats, markers, evidence = continuous_map(path.parent, regions, n, start1, frames, row['sample_rate'])
        checks = joined_map_checks(path.parent, regions, row, n, start1, t, nominal, speed)
        target = next(x for x in regions[0]['targets'] if x['channel'] == n)
        spans = target['reference_spans']
        rows.append(dict(channel=n, spans=spans, stats=stats, checks=checks,
                         monotonic=bool(np.all(np.diff(nominal)>0)), endpoint_error_seconds=float(nominal[-1]-t[-1]),
                         markers=markers))
    except Exception as e:
        rows.append(dict(channel=n, error=repr(e)))
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(dict(plan=str(a.plan), methodology=plan['methodology'], start=start, duration=duration,
    rows=rows, note='Checks use only saved reserved events. An absent measurement does not establish improvement.'), indent=2, allow_nan=False), encoding='utf-8')
print(a.output)
