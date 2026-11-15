"""P1: regression of the production stage-05 detector on saved census windows.

Runs ``flutter_census.analyse_block`` (production code) on exact 64 s windows,
one read per window like E7/E9, and compares its consensus and held-out beep
audit with the saved research outputs. Research only: reads originals, writes
only to the given output directory. Never tune on the confirmation set.

    uv run python research/flutter_census/p1_production_regression.py OUT \
        --windows @reports/flutter_census_20261115/confirm_windows.txt \
        --saved reports/flutter_census_20261115/confirm_cap20 --jobs 2
"""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

for _name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS'):
    os.environ[_name] = '1'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from _bootstrap import install
install()

import numpy as np

import flutter_census as census
from flutter_detection import read_block
from flutter_planning import inventory

ORIGINALS = Path('D:/Apollo_15_30-Track/orig_concatenated')


def one(item):
    tape, t0, saved, out = item
    inv = inventory(ORIGINALS/tape)
    settings = census.make_settings()
    read, stats = read_block(inv['channels'], t0, t0+64., settings['sample_rate'])
    result, curves = census.analyse_block(read, [r['channel'] for r in inv['channels']], tape, t0, t0+64.,
                                          settings)
    window = result['windows'][0]
    row = dict(tape=tape, t0=t0, eligible=window['eligible'], reasons=window.get('reasons'),
               agreeing=len(window.get('agreeing_groups', [])), families=window.get('families'),
               consensus_fm_percent=window.get('consensus_fm_percent'),
               duplicate_groups=result['duplicate_groups'], read_seconds=round(stats['read_seconds'], 2))
    audit = window.get('beep_audit') or {}
    row['beeps'] = {k: v for k, v in audit.items() if k != 'events'}
    if window.get('curve_index') is not None:
        consensus = curves[window['curve_index']].astype(float)
        np.save(Path(out)/f'{tape}_{int(t0)}.npy', consensus.astype(np.float32))
        reference = Path(saved)/f'{tape}_{int(t0)}.npy' if saved else None
        if reference and reference.is_file():
            old = np.load(reference).astype(float)[4*census.RATE:-4*census.RATE]
            n = min(len(old), len(consensus))
            row['r_vs_saved'] = round(census.correlation(consensus[:n], old[:n]), 4)
            row['rms_ratio_vs_saved'] = round(float(consensus[:n].std()/max(old[:n].std(), 1e-12)), 4)
    (Path(out)/f'{tape}_{int(t0)}.json').write_text(json.dumps(dict(row, window=window), indent=1,
                                                               default=float), encoding='utf-8')
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('output', type=Path)
    p.add_argument('--windows', required=True, help='TAPE:T0,... or @file')
    p.add_argument('--saved', type=Path, help='Saved research window folder for comparison')
    p.add_argument('--jobs', type=int, default=2)
    a = p.parse_args()
    text = Path(a.windows[1:]).read_text() if a.windows.startswith('@') else a.windows
    items = [(w.split(':')[0], float(w.split(':')[1]), a.saved, a.output)
             for w in text.replace('\n', ',').split(',') if w.strip()]
    a.output.mkdir(parents=True, exist_ok=True)
    rows = []
    with ProcessPoolExecutor(a.jobs) as pool:
        for row in pool.map(one, items):
            rows.append(row)
            print(json.dumps({k: row.get(k) for k in ('tape', 't0', 'eligible', 'agreeing', 'families',
                  'consensus_fm_percent', 'r_vs_saved', 'rms_ratio_vs_saved', 'beeps')}, default=float), flush=True)
    audits = [r['beeps'] for r in rows if r['beeps'].get('beeps')]
    summary = dict(windows=len(rows), eligible=sum(r['eligible'] for r in rows),
                   eligible_fraction=round(sum(r['eligible'] for r in rows)/max(len(rows), 1), 3),
                   beep_windows=len(audits), beeps=sum(b['beeps'] for b in audits))
    events = []
    for r in rows:
        window = json.loads((a.output/f'{r["tape"]}_{int(r["t0"])}.json').read_text(encoding='utf-8'))['window']
        events += window.get('beep_audit', {}).get('events', [])
    if events:
        table = np.array([[e['identity'], e['curve'], e['wrong_sign'], e['shift']] for e in events])
        ratio = table[:, 1]/np.maximum(table[:, 0], 1e-9)
        summary.update(median_hz=np.round(np.median(table, 0), 2).tolist(),
                       median_ratio=round(float(np.median(ratio)), 4),
                       worse_fraction=round(float(np.mean(ratio > 1)), 4))
    (a.output/'summary.json').write_text(json.dumps(dict(summary, rows=rows), indent=1, default=float),
                                         encoding='utf-8')
    print(json.dumps(summary, default=float))


if __name__ == '__main__':
    main()
