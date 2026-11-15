"""W2 summary: aggregate per-channel lag tables across windows, odd/even and
head structure, two-component tests, stability, phase-error math -> JSON+MD."""
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from common import inventory

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('reports/flutter_census_20261115/w2_lag')
WINDOWS = ['A15_T510077_19715', 'A15_T510077_44595', 'A15_T510077_3822', 'A15_T510077_57328',
           'A15_T510083_13920', 'A15_T510083_27105', 'A15_T510102_3682']
R_MIN = 0.7  # reliable-curve threshold


def head_of(tape, ch):
    p = inventory(tape)[ch]['path']
    return p.split('_')[2] if '_HR' in p else '?'


summary = {}
for w in WINDOWS:
    o = json.loads((OUT / f'{w}.json').read_text())
    tape = o['tape']
    rows = [r for r in o['per_channel'] if r['r'] >= R_MIN]
    weak = [r for r in o['per_channel'] if r['r'] < R_MIN]
    lags = np.array([r['lag_s'] for r in rows])
    gains = np.array([r['gain'] for r in rows if r['gain'] is not None])
    odd = [r for r in rows if r['ch'] % 2 == 1]
    even = [r for r in rows if r['ch'] % 2 == 0]
    hs = sorted({head_of(tape, r['ch']) for r in rows})
    two = [(r['ch'], r['two_comp']) for r in o['per_channel'] if 'two_comp' in r]
    summary[w] = dict(
        tape=tape, t0=o['t0'], n_curves=o['n_curves'], n_reliable=len(rows),
        weak_channels=[r['ch'] for r in weak],
        lag_med_ms=round(1000 * float(np.median(lags)), 3),
        lag_iqr_ms=[round(1000 * float(np.percentile(lags, 25)), 3),
                    round(1000 * float(np.percentile(lags, 75)), 3)],
        lag_abs_max_ms=round(1000 * float(np.max(np.abs(lags))), 3),
        lag_std_ms=round(1000 * float(lags.std()), 3),
        gain_med=round(float(np.median(gains)), 3),
        gain_iqr=[round(float(np.percentile(gains, 25)), 3), round(float(np.percentile(gains, 75)), 3)],
        odd_lag_ms=round(1000 * float(np.median([r['lag_s'] for r in odd])), 3) if odd else None,
        even_lag_ms=round(1000 * float(np.median([r['lag_s'] for r in even])), 3) if even else None,
        heads=hs, ref_vs_ensemble=o.get('ref_vs_ensemble'),
        two_comp=two, per_channel=o['per_channel'])
    s = summary[w]
    print(w, 'rel', s['n_reliable'], '/30 med', s['lag_med_ms'], 'ms IQR', s['lag_iqr_ms'],
          'max', s['lag_abs_max_ms'], 'g', s['gain_med'], 'odd', s['odd_lag_ms'],
          'even', s['even_lag_ms'], 'weak', s['weak_channels'])
    for ch, tc in two:
        print('   two-comp ch', ch, 'best_d', tc['best_d'], 'r1', tc['r1'], 'r2', tc['r2'],
              's2/s1', tc['s2_vs_s1'], 'b_1369', tc['b_1369'], 'r_1369', tc['r_1369'])

# ---- per-channel stability across windows ----
print('\n-- per-channel lag (ms) across windows (r>=%.2f only; . = weak) --' % R_MIN)
stab = {c: {} for c in range(1, 31)}
for w in WINDOWS:
    for r in summary[w]['per_channel']:
        if r['r'] >= R_MIN:
            stab[r['ch']][w] = r['lag_s'] * 1000
hdr = 'ch ' + ' '.join(f'{w.split("_")[2]:>7}' for w in WINDOWS) + '   med_ms  n'
print(hdr)
meds = {}
for c in range(1, 31):
    vals = [stab[c].get(w) for w in WINDOWS]
    cells = ' '.join('      .' if v is None else f'{v:7.2f}' for v in vals)
    vv = [v for v in vals if v is not None]
    meds[c] = float(np.median(vv)) if vv else None
    print(f'{c:2d} {cells}   {"   --" if not vv else f"{meds[c]:6.2f}"} {len(vv)}')
arr = np.array([m for m in meds.values() if m is not None])
print('per-channel median-lag over windows: median %.3f ms, std %.3f ms, |max| %.3f ms'
      % (np.median(arr), arr.std(), np.max(np.abs(arr))))
json.dump(dict(summary={w: {k: v for k, v in s.items() if k != 'per_channel'}
                        for w, s in summary.items()},
               per_channel_medians_ms={str(c): meds[c] for c in meds}),
          open(OUT / 'w2_summary.json', 'w'), indent=1)
print('wrote', OUT / 'w2_summary.json')
