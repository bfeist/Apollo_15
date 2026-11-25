"""Spot check of the SG respeed with Quindar tones: their pitch before and after, against the spec.

The Quindar tones keyed the uplink: 2525 Hz on key and 2475 Hz on unkey, 250 ms sine bursts
(https://apollojournals.org/alsj/quindar.html; the generator tolerance is not published). A reel
played back fast raises both. This script detects the bursts on a source reel and its respeeded
output with the stage 06 detector (``quindar_speed.detect`` and its quality gates), pairs them
through the reel's drift map, and reports their frequencies and implied playback rate against
the drift curve, which was fitted to transcript times only. The tones are an independent check.

    uv run --with matplotlib python research/sg_quindar_check.py 572-AAA 582-AAA
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from _bootstrap import install  # noqa: E402
install()

import argparse  # noqa: E402
import json  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from quindar_speed import detect, quality_events  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SOURCE = Path('F:/Apollo_15_data/raw/audio/Internet Archive')
RESPEED = Path('F:/Apollo_15_data/processing/sg/respeed')
REPORT = REPO / 'reports/sg_quindar'
KEY, UNKEY = 2525.0, 2475.0


def scan(path: str) -> list[dict]:
    """Quindar candidates on the channel mean, in 60 s blocks with 1 s context."""
    events = []
    with sf.SoundFile(path) as f:
        sr, n = f.samplerate, f.frames
        for a in np.arange(0, n / sr, 60.):
            left, right = max(0., a - 1), min(n / sr, a + 61)
            f.seek(int(round(left * sr)))
            x = f.read(int(round((right - left) * sr)), dtype='float32', always_2d=True).mean(axis=1)
            events.extend(detect(x.astype(np.float64), sr, left, a, min(a + 60, n / sr)))
    return events


def classify(e):
    """Key or unkey from the measured frequency, midway between the nominal pair."""
    e['tone'] = 'key' if e['frequency_hz'] >= (KEY + UNKEY) / 2 * e.get('_rate', 1.0) else 'unkey'
    e['nominal_hz'] = KEY if e['tone'] == 'key' else UNKEY
    return e


def drift_rate(meta, t):
    scale, c = meta['drift_scale_s'], meta['drift_coefficients_s']
    return 1 + sum((d + 1) * k * (t / scale) ** d / scale for d, k in enumerate(c))


def drift(meta, t):
    scale, c = meta['drift_scale_s'], meta['drift_coefficients_s']
    return sum(k * (t / scale) ** (d + 1) for d, k in enumerate(c))


def summary(values):
    v = np.asarray(values, float)
    return dict(n=len(v), median=float(np.median(v)), mean=float(np.mean(v)),
                p05=float(np.percentile(v, 5)), p95=float(np.percentile(v, 95)),
                mad=float(1.4826 * np.median(np.abs(v - np.median(v)))))


def check(reel: str, src_events, out_events, meta) -> dict:
    src = quality_events(src_events)
    out = quality_events(out_events)
    for e in src:
        e['_rate'] = drift_rate(meta, e['start_seconds'])
        classify(e)
    for e in out:
        classify(e)
    # Pair: a source burst at t should appear in the output at u = t + D(t).
    starts = np.array([e['start_seconds'] for e in out])
    pairs = []
    for e in src:
        if not len(starts):
            break
        u = e['start_seconds'] + drift(meta, e['start_seconds'])
        j = int(np.argmin(np.abs(starts - u)))
        if abs(starts[j] - u) < 0.05 and out[j]['tone'] == e['tone']:
            pairs.append((e, out[j], starts[j] - u))
    res = {'reel': reel, 'candidates_source': len(src_events), 'candidates_output': len(out_events),
           'quality_source': len(src), 'quality_output': len(out), 'paired': len(pairs),
           'curve': {'degree': meta['degree'], 'rate_start_ppm': meta['rate_start_ppm'],
                     'rate_end_ppm': meta['rate_end_ppm'], 'rate_from': meta['rate_from']}}
    for name, events in (('source', src), ('output', out)):
        for tone in ('key', 'unkey'):
            f = [e['frequency_hz'] for e in events if e['tone'] == tone]
            if f:
                nominal = KEY if tone == 'key' else UNKEY
                res[f'{name}_{tone}_hz'] = summary(f)
                res[f'{name}_{tone}_offset_ppm'] = summary([(x / nominal - 1) * 1e6 for x in f])
    if pairs:
        # Tone-implied playback rate of the source vs the curve's rate at the same instant.
        tone_rate = [(a['frequency_hz'] / a['nominal_hz'] - 1) * 1e6 for a, _, _ in pairs]
        curve_rate = [(drift_rate(meta, a['start_seconds']) - 1) * 1e6 for a, _, _ in pairs]
        ratio = [(a['frequency_hz'] / b['frequency_hz'] - 1) * 1e6 for a, b, _ in pairs]
        res['paired_tone_rate_minus_curve_ppm'] = summary(np.subtract(tone_rate, curve_rate))
        res['paired_source_over_output_minus_curve_ppm'] = summary(np.subtract(ratio, curve_rate))
        res['paired_position_error_ms'] = summary([1000 * d for _, _, d in pairs])
        res['_pairs'] = [(a['start_seconds'], a['tone'], a['frequency_hz'], b['frequency_hz'], c)
                         for (a, b, _), c in zip(pairs, curve_rate)]
    res['_src'], res['_out'] = src, out
    return res


def plot(res, src, out, meta):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    grid = np.linspace(0, meta['source_duration_s'], 300)
    ax = axes[0]
    for tone, color in (('key', 'tab:red'), ('unkey', 'tab:blue')):
        nominal = KEY if tone == 'key' else UNKEY
        s = [(e['start_seconds'], (e['frequency_hz'] / nominal - 1) * 1e6) for e in src if e['tone'] == tone]
        if s:
            ax.scatter(*np.array(s).T / [[60], [1]], s=10, c=color, label=f'source {tone} ({nominal:.0f} Hz)')
    ax.plot(grid / 60, (drift_rate(meta, grid) - 1) * 1e6, 'k-', lw=2, label='drift curve rate (from transcripts)')
    ax.axhline(0, color='grey', lw=0.8)
    ax.set_ylabel('offset from spec (ppm)')
    ax.set_title(f'{res["reel"]}: Quindar pitch before respeed vs the transcript-fitted curve')
    ax.legend(fontsize=8, loc='lower right')
    ax = axes[1]
    for tone, color in (('key', 'tab:red'), ('unkey', 'tab:blue')):
        nominal = KEY if tone == 'key' else UNKEY
        s = [(e['start_seconds'], (e['frequency_hz'] / nominal - 1) * 1e6) for e in out if e['tone'] == tone]
        if s:
            ax.scatter(*np.array(s).T / [[60], [1]], s=10, c=color, label=f'respeeded {tone}')
    ax.axhline(0, color='grey', lw=0.8)
    ax.set_ylabel('offset from spec (ppm)')
    ax.set_xlabel('time (min)')
    ax.set_title('after respeed')
    ax.legend(fontsize=8, loc='lower right')
    for a in axes:
        a.set_ylim(-6000, 12000)
    fig.tight_layout()
    REPORT.mkdir(parents=True, exist_ok=True)
    fig.savefig(REPORT / f'{res["reel"]}.png', dpi=110)
    plt.close(fig)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('reels', nargs='+')
    args = p.parse_args(argv)
    paths = [str(SOURCE / f'{r}.wav') for r in args.reels] + [str(RESPEED / f'{r}.wav') for r in args.reels]
    with ProcessPoolExecutor(max_workers=len(paths)) as pool:
        scanned = list(pool.map(scan, paths))
    REPORT.mkdir(parents=True, exist_ok=True)
    results = []
    for i, reel in enumerate(args.reels):
        meta = json.loads((RESPEED / f'{reel}.json').read_text(encoding='utf-8'))
        src, out = scanned[i], scanned[i + len(args.reels)]
        res = check(reel, src, out, meta)
        try:
            plot(res, res['_src'], res['_out'], meta)
        except ImportError:
            pass
        (REPORT / f'{reel}.json').write_text(json.dumps({k: v for k, v in res.items() if not k.startswith('_')}, indent=1),
                                            encoding='utf-8')
        with open(REPORT / f'{reel}_pairs.tsv', 'w', encoding='utf-8') as f:
            f.write('source_s\ttone\tsource_hz\toutput_hz\tcurve_rate_ppm\n')
            for row in res.get('_pairs', []):
                f.write('\t'.join(f'{x:.4f}' if isinstance(x, float) else str(x) for x in row) + '\n')
        results.append(res)
        print(json.dumps({k: v for k, v in res.items() if not k.startswith('_')}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
