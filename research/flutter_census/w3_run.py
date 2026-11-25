"""W3 driver: beep scan, per-window evaluation (consensus variants + beep
validation through flutter.positions + saved v5 comparison + amplitude
scaling), report. Research only; writes only into the given output dir.

usage:
  w3_run.py scan  --tape TAPE --t0s 100,200,... [--dur 64] [--out DIR] [--jobs 6]
  w3_run.py run   --windows windows.json [--out DIR] [--jobs 6]
  w3_run.py report [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'scripts' / '30-track' / '05_deflutter'))
from common import RATE, SR, V5, corr, inventory, lagcorr, read  # noqa: E402
import flutter_tracking as ft  # noqa: E402
from flutter import positions  # noqa: E402
import w3_core as W  # noqa: E402

MARGINS = (.025, .04, .055)
PASS_HZ, PASS_CENTS = 6., 5.
SCALES = (0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75)


def rising(values):
    return .5 - .5 * np.cos(np.pi * np.clip(values, 0., 1.))


# ------------------------------------------------------------------ v5 maps
def v5_window_speed(tape, t0, dur, ch, pad=2.0):
    """Extract the saved v5 continuous speed correction (canonical seconds)
    for one channel over [t0, t0+dur]. Returns (speed or None, spans info).
    Replicates saved_continuous_map span gating and lifetime fades; no
    full-channel duration anchoring (noted in the report)."""
    folder = V5 / tape / 'plan'
    pj = folder / 'plan.json'
    if not pj.exists():
        return None, dict(reason='no_plan')
    plan = json.loads(pj.read_text(encoding='utf-8'))
    summary = plan['regions'][0]
    detail_path = folder / summary['detail_file']
    h = hashlib.sha256()
    with open(detail_path, 'rb') as fh:
        for blk in iter(lambda: fh.read(1 << 20), b''):
            h.update(blk)
    if h.hexdigest() != summary['detail_sha256']:
        raise ValueError('v5 detail hash mismatch')
    detail = json.loads(detail_path.read_text(encoding='utf-8'))
    target = next((t for t in detail['targets'] if t['channel'] == ch), None)
    if target is None:
        return None, dict(reason='no_target')
    n = int(round(dur * RATE))
    speed = np.zeros(n)
    spans = []
    lo_all, hi_all = t0 - pad, t0 + dur + pad
    for span in target['reference_spans']:
        if not span.get('selected'):
            continue
        a, b = max(span['start_seconds'], lo_all), min(span['end_seconds'], hi_all)
        if b <= a:
            continue
        stream = detail['maps']['streams'][span['selected']]
        path = (folder / stream['file']).resolve()
        if path.stat().st_size != stream['frames'] * 16:
            raise ValueError('Continuous map stream changed')
        data = np.memmap(path, dtype='<f8', mode='r', shape=(stream['frames'], 2))
        tpad = np.arange(a, b, 1. / RATE)
        pos = (tpad - stream['start_seconds']) * RATE
        left = max(0, int(np.floor(pos.min())) - 1)
        right = min(stream['frames'], int(np.ceil(pos.max())) + 2)
        if right <= left:
            continue
        local = np.asarray(data[left:right])
        grid = np.arange(left, right)
        e = np.interp(pos, grid, local[:, 0], left=0., right=0.)
        gate = np.interp(pos, grid, local[:, 1], left=0., right=0.)
        distance = ndimage.distance_transform_edt(np.r_[False, gate >= .95, False])[1:-1]
        gate = gate * rising(distance / (.5 * RATE))
        j0 = int(np.ceil((a - t0) * RATE))
        j1 = int(np.ceil((b - t0) * RATE))
        j0, j1 = max(j0, 0), min(j1, n)
        if j1 > j0:
            speed[j0:j1] = np.interp(t0 + np.arange(j0, j1) / RATE, tpad, e * gate)
        spans.append(dict(stream=span['selected'], start=a, end=b,
                          in_window_s=round(b - a, 2)))
    return (speed if len(spans) else None), dict(spans=spans)


# ------------------------------------------------------------- beep helpers
def beep_events(x):
    ev = ft.beeps(x, SR)
    return [dict(start=float(e['start']), end=float(e['end']),
                 amplitude_over_floor=float(e['amplitude_over_floor'])) for e in ev]


def eval_curves_on_beeps(x, curves, events):
    """curves: name -> (e200 array) or None. identity = zeros. Returns
    per-name list of evaluate_events outputs."""
    out = {}
    for name, e in curves.items():
        if e is None:
            continue
        try:
            pos = positions(e, SR, len(x))
            out[name] = ft.evaluate_events(x, SR, None, events, sample_positions=pos)
        except Exception as ex:  # keep failures explicit
            out[name] = [dict(error=repr(ex))]
    return out


def aggregate(res_by_ch):
    """Aggregate evaluate_events results per curve name."""
    acc = {}
    for ch, byname in res_by_ch.items():
        for name, evs in byname.items():
            a = acc.setdefault(name, dict(n=0, events=[]))
            for ev in evs:
                if 'error' in ev:
                    a['events'].append(dict(ch=ch, error=ev['error']))
                    continue
                ms = {m['margin_seconds']: m for m in ev['margins']}
                if not ms:
                    continue
                row = dict(ch=ch, start=round(ev['start'], 2),
                           before_rms_hz=round(max(m['before_rms_hz'] for m in ms.values()), 3))
                for g, m in ms.items():
                    row[f'after_hz_{g}'] = round(m['after_rms_hz'], 3)
                    row[f'ratio_{g}'] = round(m['residual_ratio'], 3)
                    row[f'cents_{g}'] = round(m['after_rms_cents'], 3)
                    row[f'pass_{g}'] = bool(m['after_rms_hz'] <= PASS_HZ and m['after_rms_cents'] <= PASS_CENTS)
                row['pass_all'] = all(row[f'pass_{g}'] for g in MARGINS)
                a['n'] += 1
                a['events'].append(row)
    for name, a in acc.items():
        ok = [e for e in a['events'] if 'error' not in e]
        a['n_errors'] = len(a['events']) - len(ok)
        a['n'] = len(ok)
        for g in MARGINS:
            r = [e[f'ratio_{g}'] for e in ok]
            a[f'median_ratio_{g}'] = round(float(np.median(r)), 3) if r else None
            a[f'worst_ratio_{g}'] = round(float(np.max(r)), 3) if r else None
            a[f'median_after_hz_{g}'] = round(float(np.median([e[f'after_hz_{g}'] for e in ok])), 3) if r else None
            a[f'pass_share_{g}'] = round(float(np.mean([e[f'pass_{g}'] for e in ok])), 3) if r else None
        a['pass_share_all'] = round(float(np.mean([e['pass_all'] for e in ok])), 3) if ok else None
        a['mean_before_hz'] = round(float(np.mean([e['before_rms_hz'] for e in ok])), 3) if ok else None
    return acc


# ------------------------------------------------------------------- worker
def run_window(job):
    tape, t0, dur, outdir = job
    t = time.time()
    out = dict(tape=tape, t0=t0, dur=dur)
    outdir = Path(outdir)
    try:
        from common import inventory
        chans = sorted(inventory(tape))
        xs = {ch: read(tape, ch, t0, dur) for ch in chans}
        xs = {k: v for k, v in xs.items() if v.any()}
        clean = {ch: W.remove_stationary(x, W.exact_mains(x)) for ch, x in xs.items()}
        # beeps on ORIGINAL audio (pipeline detector; held out from consensus)
        beeps_by_ch = {}
        for ch, x in xs.items():
            ev = beep_events(x)
            if ev:
                beeps_by_ch[ch] = ev
        out['n_beep_channels'] = len(beeps_by_ch)
        out['n_beeps'] = sum(len(v) for v in beeps_by_ch.values())
        out['beep_channels'] = sorted(beeps_by_ch)

        # blind seeds: exact-e5 (unfiltered candidates) and beep-safe
        b_full = W.blind_seed(clean, beep_safe=False)
        seed_full = b_full.get('_seed')
        b_nb = W.blind_seed(clean, beep_safe=True)
        seed_nb = b_nb.get('_seed')
        out['blind'] = {k: v for k, v in b_full.items() if k != '_seed'}
        out['blind_noband'] = {k: v for k, v in b_nb.items() if k != '_seed'}

        variants = {}
        cons_curves = {}
        if seed_full is not None:
            c, m = W.build_consensus(clean, seed_full, 'full')
            variants['full'] = m
            cons_curves['full'] = c
        if seed_nb is not None:
            c, m = W.build_consensus(clean, seed_nb, 'noband')
            variants['noband'] = m
            cons_curves['noband'] = c
            c2, m2 = W.build_consensus(clean, seed_nb, 'hum')
            variants['hum'] = m2
            cons_curves['hum'] = c2
        out['variants'] = variants
        for name, c in cons_curves.items():
            if c is not None:
                np.save(outdir / f'{tape}_{int(t0)}_cons_{name}.npy', c.astype(np.float32))
        out['status'] = 'ok' if cons_curves else 'no_consensus'

        # beep evaluation
        proc = {n: W.proc_curve(c) for n, c in cons_curves.items() if c is not None}
        curves = dict(identity=np.zeros(int(round(dur * RATE))))
        if 'noband' in proc:
            curves['noband'] = proc['noband']
            curves['noband_neg'] = -proc['noband']
        if 'full' in proc:
            curves['full'] = proc['full']
            curves['full_neg'] = -proc['full']
            curves['full_shift'] = np.roll(proc['full'], int(1.5 * RATE))
        if 'hum' in proc:
            curves['hum'] = proc['hum']

        # amplitude scaling on non-beep census lines (no beeps used)
        if cons_curves.get('noband') is not None:
            sc = tune_scale(clean, cons_curves['noband'], variants['noband'])
            if sc is not None:
                out['scaling'] = sc
                if sc.get('best') is not None:
                    curves['noband_scaled'] = W.proc_curve(sc['best'] * cons_curves['noband'])

        # v5 saved maps for this window
        v5_info = {}
        for ch in sorted(beeps_by_ch):
            sp, info = v5_window_speed(tape, t0, dur, ch)
            if sp is not None:
                curves_v5 = dict(curves)
                v5_info[ch] = info
                curves_v5['v5'] = sp
                res = eval_curves_on_beeps(xs[ch], curves_v5, beeps_by_ch[ch])
                res_by_ch = {ch: res}
                agg = aggregate(res_by_ch)
                merge_agg(out, agg)
                # curve comparison on covered samples
                cmp = v5_compare(sp, cons_curves.get('noband'), t0, dur)
                if cmp:
                    v5_info[ch].update(cmp)
            else:
                v5_info[ch] = info
        out['v5'] = v5_info
        # beep eval for channels without v5 coverage (dedup: already done above)
        done = {ch for ch in v5_info if v5_info[ch].get('spans')}
        for ch in sorted(beeps_by_ch):
            if ch in done:
                continue
            res = eval_curves_on_beeps(xs[ch], curves, beeps_by_ch[ch])
            merge_agg(out, aggregate({ch: res}))

        # consensus curve agreement across variants
        agree = {}
        for a in ('full', 'noband', 'hum'):
            for b in ('noband', 'hum'):
                if a >= b:
                    continue
                ca, cb = cons_curves.get(a), cons_curves.get(b)
                if ca is not None and cb is not None:
                    agree[f'{a}_vs_{b}'] = dict(
                        r=round(corr(ca[W.EDGE], cb[W.EDGE]), 3),
                        gain=round(float(np.dot(ca[W.EDGE], cb[W.EDGE]) / max(np.dot(cb[W.EDGE], cb[W.EDGE]), 1e-30)), 3))
        out['variant_agreement'] = agree
        out['seconds'] = round(time.time() - t, 1)
        (outdir / 'windows' / f'{tape}_{int(t0)}.json').parent.mkdir(parents=True, exist_ok=True)
        (outdir / 'windows' / f'{tape}_{int(t0)}.json').write_text(json.dumps(out, indent=1))
        return out
    except Exception as ex:
        import traceback
        out.update(status='error', error=repr(ex), trace=traceback.format_exc(),
                   seconds=round(time.time() - t, 1))
        return out


def merge_agg(out, agg):
    for name, a in agg.items():
        tgt = out.setdefault('beep_eval', {}).setdefault(name, dict(n=0, events=[]))
        tgt['n'] += a['n']
        tgt['n_errors'] = tgt.get('n_errors', 0) + a.get('n_errors', 0)
        tgt['events'] += a['events']


def finalize_agg(out):
    """Recompute summary statistics after merging per-channel results."""
    for name, a in out.get('beep_eval', {}).items():
        ok = [e for e in a['events'] if 'error' not in e]
        a['n'] = len(ok)
        for g in MARGINS:
            r = [e[f'ratio_{g}'] for e in ok]
            a[f'median_ratio_{g}'] = round(float(np.median(r)), 3) if r else None
            a[f'worst_ratio_{g}'] = round(float(np.max(r)), 3) if r else None
            a[f'median_after_hz_{g}'] = round(float(np.median([e[f'after_hz_{g}'] for e in ok])), 3) if r else None
            a[f'pass_share_{g}'] = round(float(np.mean([e[f'pass_{g}'] for e in ok])), 3) if r else None
        a['pass_share_all'] = round(float(np.mean([e['pass_all'] for e in ok])), 3) if ok else None
        a['mean_before_hz'] = round(float(np.mean([e['before_rms_hz'] for e in ok])), 3) if ok else None


def tune_scale(clean, cons, meta, max_lines=4, scales=SCALES):
    """Pick a consensus amplitude scale by maximising spectral concentration
    of non-beep census lines (no beeps used)."""
    chosen, seen_ch = [], set()
    for mem in sorted(meta.get('members', []), key=lambda m: -len(m)):
        for ch, hz, cap in mem:
            if ch in seen_ch or W.touches_beep(hz):
                continue
            seen_ch.add(ch)
            chosen.append((ch, hz))
            break
        if len(chosen) >= max_lines:
            break
    if not chosen:
        return None
    per = {a: [] for a in scales}
    for a in scales:
        cache = {}
        for ch, hz in chosen:
            if ch not in cache:
                cache[ch] = W.dewarp(clean[ch], a * cons)
            per[a].append(W.concentration(cache[ch], hz))
    means = {a: round(float(np.mean(v)), 4) for a, v in per.items()}
    best = max(means, key=means.get)
    return dict(alphas=list(scales), mean_conc=means, best=float(best),
                lines=[[c, round(h, 2)] for c, h in chosen])


def v5_compare(sp, cons, t0, dur, edge_s=4.0):
    if cons is None:
        return None
    n = int(round(dur * RATE))
    tt = np.arange(n) / RATE
    edge = slice(int(edge_s * RATE), n - int(edge_s * RATE))
    sos = signal.butter(3, [2., 30.], fs=RATE, btype='bandpass', output='sos')
    a = signal.sosfiltfilt(sos, sp)[edge]
    b = signal.sosfiltfilt(sos, cons)[edge]
    m = np.abs(a) > 1e-6
    if m.sum() < 200:
        return dict(covered_s=0.)
    r = corr(a[m], b[m])
    g = float(np.dot(a[m], b[m]) / max(np.dot(b[m], b[m]), 1e-30))
    lag, rl, r0 = lagcorr(a[m], b[m], maxlag_s=2.0)
    return dict(covered_s=round(float(np.mean(np.abs(sp) > 1e-12) * dur), 2),
                r_bp=round(r, 3), gain_bp=round(g, 3), lag_s=round(lag, 2), lag_r=round(rl, 3),
                rms_v5_pct=round(100 * float(a.std()), 3), rms_cons_pct=round(100 * float(b.std()), 3))


# -------------------------------------------------------------------- scan
def scan_window(job):
    tape, t0, dur, outdir = job
    from common import inventory
    found = {}
    for ch in sorted(inventory(tape)):
        try:
            x = read(tape, ch, t0, dur)
        except Exception:
            continue
        if not x.any():
            continue
        ev = beep_events(x)
        if ev:
            found[str(ch)] = [dict(start=round(e['start'] + t0, 2), end=round(e['end'] + t0, 2)) for e in ev]
    # v5 selected overlap
    ov = 0.
    pj = V5 / tape / 'plan' / 'plan.json'
    if pj.exists():
        plan = json.loads(pj.read_text(encoding='utf-8'))
        for tgt in plan['regions'][0]['targets']:
            for s in tgt['reference_spans']:
                if s.get('action') == 'use_reference':
                    ov += max(0., min(s['end_seconds'], t0 + dur) - max(s['start_seconds'], t0))
    d = Path(outdir) / 'scan'
    d.mkdir(parents=True, exist_ok=True)
    r = dict(tape=tape, t0=t0, dur=dur, n_beep_channels=len(found),
             n_beeps=sum(len(v) for v in found.values()), beeps=found,
             v5_overlap_s=round(ov, 1))
    (d / f'{tape}_{int(t0)}.json').write_text(json.dumps(r, indent=1))
    return r


def grid(tape, n):
    from common import inventory
    D = inventory(tape) and max(c['frames'] for c in inventory(tape).values()) / SR
    return [round(D * (i + .5) / n) for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('scan')
    s.add_argument('--tape', required=True)
    s.add_argument('--t0s', required=True, help='comma-separated start seconds (or "grid:N")')
    s.add_argument('--dur', type=float, default=64.)
    s.add_argument('--out', required=True)
    s.add_argument('--jobs', type=int, default=6)
    r = sub.add_parser('run')
    r.add_argument('--windows', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--jobs', type=int, default=6)
    p = sub.add_parser('report')
    p.add_argument('--out', required=True)
    a = ap.parse_args()
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    if a.cmd == 'scan':
        if 'grid:' in a.t0s:
            parts = a.t0s.split(',')
            t0s = [t for t in parts if not t.startswith('grid:')]
            for p in parts:
                if p.startswith('grid:'):
                    t0s += [str(v) for v in grid(a.tape, int(p.split(':')[1]))]
            t0s = [float(v) for v in t0s]
        else:
            t0s = [float(v) for v in a.t0s.split(',')]
        jobs = [(a.tape, float(t0), a.dur, str(outdir)) for t0 in t0s]
        with ProcessPoolExecutor(a.jobs) as ex:
            for r_ in ex.map(scan_window, jobs):
                print(r_['tape'], r_['t0'], 'beep_channels', r_['n_beep_channels'],
                      'beeps', r_['n_beeps'], 'v5_ov_s', r_['v5_overlap_s'], flush=True)
    elif a.cmd == 'run':
        wins = json.loads(Path(a.windows).read_text(encoding='utf-8'))
        jobs = [(w['tape'], float(w['t0']), float(w.get('dur', 64.)), str(outdir)) for w in wins]
        with ProcessPoolExecutor(a.jobs) as ex:
            for o in ex.map(run_window, jobs):
                finalize_agg(o)
                key = f"{o['tape']}_{int(o['t0'])}"
                (outdir / 'windows' / f'{key}.json').parent.mkdir(parents=True, exist_ok=True)
                (outdir / 'windows' / f'{key}.json').write_text(json.dumps(o, indent=1))
                print(key, o.get('status', 'ok'), 'beeps', o.get('n_beeps'),
                      'variants', {k: v.get('n_measured_families') for k, v in o.get('variants', {}).items()},
                      'noband', (o.get('beep_eval', {}).get('noband') or {}).get('median_ratio_.055'),
                      o.get('seconds'), flush=True)
    elif a.cmd == 'report':
        make_report(outdir)


def make_report(outdir):
    """Assemble results.json from per-window files."""
    wins = []
    for p in sorted((outdir / 'windows').glob('*.json')):
        o = json.loads(p.read_text(encoding='utf-8'))
        finalize_agg(o)
        wins.append(o)
    (outdir / 'results.json').write_text(json.dumps(dict(windows=wins), indent=1))
    print('results.json written,', len(wins), 'windows')


if __name__ == '__main__':
    main()
