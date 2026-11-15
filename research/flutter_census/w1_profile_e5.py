"""W1: cProfile e5_window_eval.evaluate on one window and report a time breakdown."""
import argparse, cProfile, json, pstats, sys, time, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import e5_window_eval

BUCKETS = [
    ('io_read', ['read']),
    ('welch_spectrogram_stft', ['welch', 'spectrogram', '_spectral_helper', 'stft']),
    ('exact_mains', ['exact_mains']),
    ('remove_stationary', ['remove_stationary']),
    ('demod_resample_mix', ['demod', 'resample_poly', 'upfirdn']),
    ('frac_dev_filtering', ['frac_dev', 'sosfiltfilt', 'sosfilt', 'butter']),
    ('dewarp_resample', ['dewarp']),
    ('concentration', ['concentration']),
    ('interp_ndimage', ['interp', 'gaussian_filter', 'median_filter', 'binary_erosion',
                        'uniform_filter', 'maximum_filter', 'minimum_filter']),
    ('corr_lagcorr', ['corr', 'lagcorr', 'fftconvolve']),
    ('find_peaks', ['find_peaks']),
]


def bucketize(stats):
    # aggregate ttot (self time) and cum for functions whose name matches a bucket
    rows = []
    for (fn, ln, name), (cc, nc, ttot, ctot, callers) in stats.stats.items():
        rows.append((f'{fn}:{ln}({name})', ttot, ctot, nc))
    out = {}
    seen = set()
    for label, keys in BUCKETS:
        ttot = ctot = nc = 0
        hits = []
        for r in rows:
            if any(k in r[0] for k in keys):
                # avoid double counting across buckets: assign to first matching bucket
                if r[0] in seen:
                    continue
                seen.add(r[0])
                ttot += r[1]; ctot += r[2]; nc += r[3]
                hits.append(r)
        out[label] = dict(self_s=round(ttot, 2), cum_s=round(ctot, 2), calls=nc)
    return out, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tape', default='A15_T510077')
    ap.add_argument('--t0', type=float, default=19715.)
    ap.add_argument('--dur', type=float, default=64.)
    ap.add_argument('--out', default='reports/flutter_census_20261115/w1_profiling')
    a = ap.parse_args()
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    pr = cProfile.Profile()
    t = time.time()
    pr.enable()
    res = e5_window_eval.evaluate(a.tape, a.t0, a.dur)
    pr.disable()
    wall = time.time() - t
    res.pop('_cons', None)
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(40)
    text = s.getvalue()
    (outdir / 'e5_profile_top40.txt').write_text(text, encoding='utf-8')
    pr.dump_stats(outdir / 'e5_profile.stats')
    buckets, rows = bucketize(pr)
    # top self-time functions for the report
    top_self = sorted(rows, key=lambda r: -r[1])[:25]
    total_self = sum(r[1] for r in rows)
    res2 = dict(tape=a.tape, t0=a.t0, dur=a.dur, wall_s=round(wall, 1),
                total_profiled_self_s=round(total_self, 1),
                status=res.get('status'), buckets=buckets,
                top_self=[dict(func=r[0][:120], self_s=round(r[1], 2), cum_s=round(r[2], 2),
                               calls=r[3]) for r in top_self],
                e5_summary={k: res.get(k) for k in ('n_lines', 'n_families', 'n_measured_families',
                                                    'n_validated_families', 'consensus_rms_pct',
                                                    'flutter_peak_hz', 'blind_seconds', 'seconds')})
    (outdir / 'e5_profile.json').write_text(json.dumps(res2, indent=1))
    print(json.dumps({k: res2[k] for k in ('wall_s', 'status', 'e5_summary')}, indent=1))
    print('buckets:')
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]['self_s']):
        if v['self_s'] > 0.2:
            print(f"  {k:28s} self {v['self_s']:7.2f}s  cum {v['cum_s']:7.2f}s  calls {v['calls']}")


if __name__ == '__main__':
    main()
