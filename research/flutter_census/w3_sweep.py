"""W3 sweep: for every window, amplitude-calibrate the hum-included consensus
by maximising non-beep spectral concentration, then evaluate beeps through the
calibrated map. Also re-runs the base curves for context. No beeps are used
for any consensus or scaling decision (beeps only as held-out validation).

usage: w3_sweep.py --windows windows.json --out DIR [--jobs 4]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import read  # noqa: E402
import w3_core as W  # noqa: E402
from w3_run import (v5_window_speed, beep_events, eval_curves_on_beeps,
                    aggregate, MARGINS)  # noqa: E402

ALPHAS = (0.9, 1.0, 1.1, 1.25, 1.4, 1.6)
MAX_LINES = 6


def sweep_window(job):
    tape, t0, dur, outdir = job
    outdir = Path(outdir)
    res = dict(tape=tape, t0=t0, dur=dur)
    try:
        clean = W.clean_channels(tape, t0, dur)
        b = W.blind_seed(clean, beep_safe=True)
        seed = b.get('_seed')
        if seed is None:
            res['status'] = 'no_seed'
            return res
        cons, meta = W.build_consensus(clean, seed, 'hum', want_validation=False)
        if cons is None:
            res['status'] = 'no_consensus'
            return res
        res['hum_rms_pct'] = meta.get('consensus_rms_pct')
        res['hum_members'] = meta.get('members')
        # top distinct-channel beep-safe members for the concentration objective
        chosen, seen = [], set()
        for mem in sorted(meta['members'], key=lambda m: -len(m)):
            for ch, hz, cap in mem:
                if ch in seen or W.touches_beep(hz):
                    continue
                seen.add(ch)
                chosen.append((ch, hz))
                break
            if len(chosen) >= MAX_LINES:
                break
        res['lines'] = [[c, round(h, 2)] for c, h in chosen]
        beep_chs = []
        for ch in sorted(clean):
            x = read(tape, ch, t0, dur)
            ev = beep_events(x)
            if ev:
                beep_chs.append((ch, x, ev))
        res['n_beeps'] = sum(len(e) for _, _, e in beep_chs)
        # v5 anchors where they exist
        v5chs, sps = [], {}
        for ch, _, _ in beep_chs:
            sp, info = v5_window_speed(tape, t0, dur, ch)
            if sp is not None and info.get('spans'):
                v5chs.append(ch)
                sps[ch] = sp
        res['v5_channels'] = v5chs
        rows = []
        for a in ALPHAS:
            cache, cs = {}, []
            for ch, hz in chosen:
                if ch not in cache:
                    cache[ch] = W.dewarp(clean[ch], a * cons)
                cs.append(W.concentration(cache[ch], hz))
            curves = {f'a{a}': W.proc_curve(a * cons)}
            curves.update({f'v5_{ch}': sps[ch] for ch in v5chs})
            agg = aggregate({ch: eval_curves_on_beeps(x, curves, e)
                             for ch, x, e in beep_chs})
            row = agg.get(f'a{a}', {})
            rows.append(dict(alpha=a, conc_mean=round(float(np.mean(cs)), 4),
                             conc_per_line=[round(c, 4) for c in cs],
                             n=row.get('n'),
                             median_ratio=row.get('median_ratio_0.055'),
                             worst_ratio=row.get('worst_ratio_0.055'),
                             median_after_hz=row.get('median_after_hz_0.055'),
                             pass_share_all=row.get('pass_share_all')))
        res['sweep'] = rows
        best = max(rows, key=lambda r: r['conc_mean'])
        res['best_alpha'] = best['alpha']
        res['best'] = best
        if v5chs:
            v5row = aggregate({ch: eval_curves_on_beeps(x, {f'v5_{ch}': sps[ch]}, e)
                               for ch, x, e in beep_chs})
            res['v5_eval'] = {f'v5_{ch}': {k: v5row[f'v5_{ch}'].get(k) for k in
                                           ('n', 'median_ratio_0.055', 'worst_ratio_0.055',
                                            'median_after_hz_0.055', 'pass_share_all')}
                              for ch in v5chs if f'v5_{ch}' in v5row}
        res['status'] = 'ok'
    except Exception as ex:
        import traceback
        res['status'] = 'error'
        res['error'] = repr(ex)
        res['trace'] = traceback.format_exc()
    (outdir / 'sweep' / f'{tape}_{int(t0)}.json').parent.mkdir(parents=True, exist_ok=True)
    (outdir / 'sweep' / f'{tape}_{int(t0)}.json').write_text(json.dumps(res, indent=1))
    return {k: v for k, v in res.items() if k not in ('sweep', 'hum_members')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--windows', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args()
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    outdir = Path(a.out)
    wins = json.loads(Path(a.windows).read_text(encoding='utf-8'))
    jobs = [(w['tape'], float(w['t0']), float(w.get('dur', 64.)), str(outdir)) for w in wins]
    with ProcessPoolExecutor(a.jobs) as ex:
        for r in ex.map(sweep_window, jobs):
            print(r.get('tape'), r.get('t0'), r.get('status'),
                  'best_alpha', r.get('best_alpha'),
                  'med_ratio', (r.get('best') or {}).get('median_ratio'),
                  'worst', (r.get('best') or {}).get('worst_ratio'),
                  'v5', r.get('v5_eval'), flush=True)


if __name__ == '__main__':
    main()
