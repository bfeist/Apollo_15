"""P2: production census consensus vs the saved, listening-approved v5 map.

Guide section 6.3: where saved v5 spans exist the consensus should agree with
the v5 curve at lag 0 (r >= 0.8) and its held-out beep ratio should not be
worse than v5's by more than 0.05. Uses the production detector on one 64 s
read per window and W3's saved-v5 extraction. Research only.

    uv run python research/flutter_census/p2_v5_consistency.py OUT A15_T510083:14034:15
"""
import json
import os
import sys
from pathlib import Path

for _name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS'):
    os.environ[_name] = '1'
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]/'scripts'))
from _bootstrap import install
install()
sys.path.insert(0, str(HERE))

import numpy as np

import flutter_census as census
import flutter_tracking as ft
from flutter_detection import read_block
from flutter_planning import inventory
from w3_run import v5_window_speed

ORIGINALS = Path('D:/Apollo_15_30-Track/orig_concatenated')


def beep_ratio(x, curve, sr):
    events = [e for e in ft.beeps(x, sr) if 5 < e['start'] < len(x)/sr-5]
    if not events:
        return None, 0
    base = ft.evaluate_events(x, sr, np.zeros_like(curve), events)
    after = ft.evaluate_events(x, sr, curve, events)
    ratios = [np.median([m['after_rms_hz'] for m in a['margins']])/max(np.median([m['after_rms_hz'] for m in b['margins']]), 1e-9)
              for a, b in zip(after, base) if a['margins'] and b['margins']]
    return (round(float(np.median(ratios)), 4) if ratios else None), len(ratios)


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in sys.argv[2:]:
        tape, t0, channel = item.split(':')
        t0, channel = float(t0), int(channel)
        inv = inventory(ORIGINALS/tape)
        settings = census.make_settings()
        read, _ = read_block(inv['channels'], t0, t0+64., settings['sample_rate'])
        result, curves = census.analyse_block(read, [r['channel'] for r in inv['channels']], tape,
                                              t0, t0+64., settings)
        window = result['windows'][0]
        full = np.zeros(64*census.RATE)
        edge = 4*census.RATE
        full[edge:-edge] = curves[window['curve_index']]
        v5, info = v5_window_speed(tape, t0, 64., channel)
        row = dict(tape=tape, t0=t0, channel=channel, eligible=window['eligible'], v5_spans=info.get('spans'))
        if v5 is not None:
            v5 = np.asarray(v5, float)[:len(full)]
            both = (abs(v5) > 0) & (full != 0)
            row['overlap_seconds'] = round(float(both.sum()/census.RATE), 2)
            if both.sum() > 4*census.RATE:
                row['r_lag0'] = round(census.correlation(full[both], v5[both]), 4)
                row['gain_v5_over_census'] = round(float(v5[both].std()/full[both].std()), 4)
            x, _ = read(channel)
            gate = (full != 0).astype(float)
            row['census_beep_ratio'], row['census_beeps'] = beep_ratio(x.astype(float), ft.bound(full, gate), 8000)
            v5_gate = (abs(v5) > 0).astype(float)
            row['v5_beep_ratio'], row['v5_beeps'] = beep_ratio(x.astype(float), ft.bound(v5, v5_gate), 8000)
            mask = gate*v5_gate
            row['census_beep_ratio_v5_span'], row['beeps_in_v5_span'] = beep_ratio(
                x.astype(float), ft.bound(full, mask), 8000)
        rows.append(row)
        print(json.dumps(row, default=float), flush=True)
    (out/'summary.json').write_text(json.dumps(rows, indent=1, default=float), encoding='utf-8')


if __name__ == '__main__':
    main()
