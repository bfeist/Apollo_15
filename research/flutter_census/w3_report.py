"""W3 report assembly: merge base-run results, sweep results, unique-beep-time
clusters and v5 covered/uncovered splits into results.json; print report tables.

usage: w3_report.py --out DIR
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

sys_path = None


def clusters_for_window(o, tol=0.35):
    """Group beep events across channels by canonical start time."""
    evs = []
    for ch, lst in o.get('beeps_by_channel', o.get('_beeps', {})).items():
        for e in lst:
            evs.append((float(e['start']), int(ch)))
    evs.sort()
    out = []
    for start, ch in evs:
        if out and start - out[-1]['t_max'] <= tol:
            c = out[-1]
            c['members'].append((ch, start))
            c['t_max'] = max(c['t_max'], start)
        else:
            out.append(dict(t0=start, t_max=start, members=[(ch, start)]))
    return out


def variant_cluster_stats(o, name, clusters):
    """Per-unique-time median ratio for one variant (margin 0.055)."""
    by_ev = {}
    for e in o.get('beep_eval', {}).get(name, {}).get('events', []):
        if 'error' in e:
            continue
        by_ev[(int(e['ch']), round(float(e['start']), 2))] = e
    if not by_ev:
        return None
    rows = []
    for c in clusters:
        rs = []
        for ch, start in c['members']:
            e = by_ev.get((ch, round(start, 2)))
            if e is not None:
                rs.append(e['ratio_0.055'])
        if rs:
            rows.append(dict(t0=round(c['t0'], 2), n_channels=len(rs),
                             median_ratio=round(float(np.median(rs)), 3)))
    if not rows:
        return None
    med = [r['median_ratio'] for r in rows]
    return dict(n_unique_times=len(rows), median_ratio=round(float(np.median(med)), 3),
                worst_time=round(float(np.max(med)), 3),
                times=rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    outdir = Path(a.out)
    wins = []
    for p in sorted((outdir / 'windows').glob('*.json')):
        o = json.loads(p.read_text(encoding='utf-8'))
        key = f"{o['tape']}_{int(o['t0'])}"
        sw = json.loads((outdir / 'sweep' / f'{key}.json').read_text(encoding='utf-8')) \
            if (outdir / 'sweep' / f'{key}.json').exists() else None
        # unique beep-time clusters from base-run event starts
        evstarts = sorted({(round(e['start'], 2)) for name in o.get('beep_eval', {})
                           for e in o['beep_eval'][name].get('events', []) if 'error' not in e})
        uniq = []
        for s in evstarts:
            if uniq and s - uniq[-1][-1] <= 0.35:
                uniq[-1].append(s)
            else:
                uniq.append([s])
        o['n_unique_beep_times'] = len(uniq)
        o['unique_times'] = [round(float(u[0]), 2) for u in uniq]
        # cluster stats for main variants
        cl = {}
        evmap = {}
        for name in o.get('beep_eval', {}):
            for e in o['beep_eval'][name].get('events', []):
                if 'error' in e:
                    continue
                evmap.setdefault((name, round(float(e['start']), 2)), {})[int(e['ch'])] = e
        for name in ('identity', 'full', 'noband', 'hum', 'full_neg', 'full_shift', 'noband_neg'):
            per_time = []
            for u in uniq:
                rs = [e['ratio_0.055'] for (n2, s2), d in evmap.items() if n2 == name and abs(s2 - u[0]) <= .01
                      for e in d.values()]
                if rs:
                    per_time.append(float(np.median(rs)))
            if per_time:
                cl[name] = dict(n=len(per_time), median_ratio=round(float(np.median(per_time)), 3),
                                worst_time=round(float(max(per_time)), 3))
        o['unique_time_stats'] = cl
        if sw:
            o['sweep'] = {k: sw.get(k) for k in
                          ('status', 'hum_rms_pct', 'lines', 'n_beeps', 'best_alpha', 'best',
                           'sweep', 'v5_channels')}
        wins.append(o)
    (outdir / 'results.json').write_text(json.dumps(dict(windows=wins), indent=1))
    # console tables
    for o in wins:
        key = f"{o['tape']}_{int(o['t0'])}"
        print(f"== {key}  events={o['n_beeps']} unique_times={o['n_unique_beep_times']} "
              f"beep_channels={o['n_beep_channels']}")
        for name, a in sorted(o.get('beep_eval', {}).items()):
            print(f"  {name:14s} n={a['n']:3d} before={a['mean_before_hz']:6.2f} "
                  f"medR={a['median_ratio_0.055']} worstR={a['worst_ratio_0.055']} "
                  f"medAfter={a['median_after_hz_0.055']:7.3f} pass={a['pass_share_0.055']}")
        print('  unique-time medians:', {k: v['median_ratio'] for k, v in o['unique_time_stats'].items()})
        s = o.get('sweep') or {}
        if s.get('best'):
            print(f"  calibrated: alpha={s['best_alpha']} conc={s['best']['conc_mean']} "
                  f"medR={s['best']['median_ratio']} worstR={s['best']['worst_ratio']} "
                  f"medAfter={s['best']['median_after_hz']}")
    print('results.json updated')


if __name__ == '__main__':
    main()
