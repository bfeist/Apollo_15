"""Sweep E5 over evenly spaced windows of several tapes; compare with the saved
v5 use_reference union at the same canonical times."""
import json, sys, os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import V5, inventory
from e5_window_eval import _job


def v5_union(tape):
    p = json.loads((V5 / tape / 'plan' / 'plan.json').read_text(encoding='utf-8'))
    sp = []
    for t in p['regions'][0]['targets']:
        for s in t['reference_spans']:
            if s['action'] == 'use_reference':
                sp.append((s['start_seconds'], s['end_seconds']))
    return sorted(sp), p['inventory']['duration_seconds']


def main(out, tapes, n=8, jobs=12, dur=64.):
    Path(out).mkdir(parents=True, exist_ok=True)
    work, meta = [], {}
    for tape in tapes:
        spans, D = v5_union(tape)
        meta[tape] = dict(duration=D, v5_union_pct=100 * sum(b - a for a, b in _merge(spans)) / D, spans=spans)
        for i in range(n):
            t0 = round(D * (i + .5) / n)
            work.append((tape, float(t0), dur, out))
    os.environ['OMP_NUM_THREADS'] = '1'
    res = []
    with ProcessPoolExecutor(jobs) as ex:
        for r in ex.map(_job, work):
            sp = meta[r['tape']]['spans']
            r['v5_overlap_s'] = sum(max(0, min(b, r['t0'] + dur) - max(a, r['t0'])) for a, b in _merge(sp))
            res.append(r)
            print(r['tape'], r['t0'], r.get('status'), 'validated', r.get('n_validated_families'),
                  r.get('validated_channels'), 'v5', round(r['v5_overlap_s']), r.get('seconds'), flush=True)
    summ = {}
    for tape in tapes:
        rr = [r for r in res if r['tape'] == tape]
        summ[tape] = dict(v5_union_pct=round(meta[tape]['v5_union_pct'], 2), windows=len(rr),
                          eligible_windows=sum(bool(r.get('eligible')) for r in rr),
                          any_validated=sum((r.get('n_validated_families') or 0) >= 1 for r in rr),
                          v5_windows=sum(r['v5_overlap_s'] > 0 for r in rr),
                          flutter_peak_hz=[r.get('flutter_peak_hz') for r in rr],
                          consensus_rms_pct=[r.get('consensus_rms_pct') for r in rr])
    Path(out, 'summary.json').write_text(json.dumps(summ, indent=1))
    for k, v in summ.items():
        print(k, v)


def _merge(sp):
    out = []
    for a, b in sorted(sp):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2].split(','), int(sys.argv[3]) if len(sys.argv) > 3 else 8)
