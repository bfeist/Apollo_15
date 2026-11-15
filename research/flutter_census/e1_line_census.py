"""E1: census of narrow lines on all 30 channels of one window and their
agreement with a reference (or with the cross-channel consensus).

usage: e1_line_census.py TAPE T0 DUR [--ref CH:HZ] [--out file.json]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, corr, demod, find_lines, frac_dev, inventory, read, slope, lagcorr


def track_all(tape, t0, dur, channels=None, max_lines=40, min_snr=10., band=(2., 20.)):
    inv = inventory(tape)
    channels = channels or sorted(inv)
    traces = []
    for ch in channels:
        x = read(tape, ch, t0, dur)
        if not np.any(x):
            continue
        lines = find_lines(x, min_snr_db=min_snr)
        lines.sort(key=lambda r: -r['snr_db'])
        lines = lines[:max_lines]
        hzs = sorted(r['hz'] for r in lines)
        for r in lines:
            nb = [abs(h - r['hz']) for h in hzs if h != r['hz']]
            gap = min(nb) if nb else 100.
            cap = float(np.clip(.45 * gap, 1.5, 25.))
            amp, fr = demod(x, SR, r['hz'], cap)
            e, ok, c = frac_dev(fr, amp, band=band, capture=cap)
            traces.append(dict(ch=ch, hz=r['hz'], centre=c, snr_db=r['snr_db'],
                               mains_offset_hz=r['mains_offset_hz'], capture=cap,
                               support=float(ok.mean()), e=e, ok=ok))
    return traces


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tape'); ap.add_argument('t0', type=float); ap.add_argument('dur', type=float)
    ap.add_argument('--ref', default=None, help='CH:HZ reference line')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    t = time.time()
    tr = track_all(a.tape, a.t0, a.dur)
    el = time.time() - t
    edge = 4 * RATE
    sl = slice(edge, -edge)
    if a.ref:
        ch, hz = a.ref.split(':'); ch = int(ch); hz = float(hz)
        ref = min((r for r in tr if r['ch'] == ch), key=lambda r: abs(r['hz'] - hz))
        refe = ref['e'][sl]
        print(f"reference CH{ref['ch']} {ref['hz']:.2f} Hz support {ref['support']:.2f} "
              f"rms {1e2*refe.std():.3f}%")
    else:
        ref = None
    rows = []
    for r in tr:
        e = r['e'][sl]
        row = dict(ch=r['ch'], hz=round(r['hz'], 3), snr_db=round(r['snr_db'], 1),
                   mains_off=round(r['mains_offset_hz'], 3), capture=r['capture'],
                   support=round(r['support'], 3), rms_pct=round(1e2 * float(e.std()), 4))
        if ref is not None and r is not ref:
            lag, rl, r0 = lagcorr(refe, e, 2.0)
            row.update(r0=round(r0, 3), best_lag=lag, r_lag=round(rl, 3),
                       gain=round(slope(e, refe), 3))
        rows.append(row)
    rows.sort(key=lambda r: -abs(r.get('r0', 0)))
    print(f'{len(tr)} line traces in {el:.1f}s')
    for r in rows[:60]:
        print(r)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(dict(tape=a.tape, t0=a.t0, dur=a.dur, ref=a.ref,
                                               seconds=el, rows=rows), indent=1))


if __name__ == '__main__':
    main()
