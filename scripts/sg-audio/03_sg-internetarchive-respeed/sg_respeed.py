"""SG reels, step 5 applied: respeed each reel by its slow drift curve.

The drift fit (``sg_drift_fit.py``, ``catalog/sg_drift/sg_drift_maps.json``) gives each reel
one slow curve D(t): the reel played back at rate 1 + D'(t) relative to mission time, about
0.5 % fast. This step resamples every reel so that output time u = t + D(t) runs at mission
rate. Only D is applied: the per-segment constants (recorder gaps, known edits) place the
output on GET but never change the audio, and no anchor is joined to its neighbour.

The resampler evaluates a Kaiser-windowed sinc (64 taps, cutoff 0.97 of the output Nyquist,
2,048-phase table) at each output sample's exact source position, from one global inverse
map; 512-sample halos cover block joins. It is a numba kernel: against exact sinusoids to
10 kHz its error is -108 dB (stage 06b's 255-tap FIR, 8x oversampling and cubic spline:
-96 dB), and it runs about 70 times faster, so a reel is limited by disk speed.

Each reel's source is checked against the checksum the map was fitted for. Outputs, under
``F:/Apollo_15_data/processing/sg/respeed/`` (``--output-root``):

* ``<reel>.wav``   the source's rate, channels and sample format (24-bit PCM). Resampling a
  source that touches full scale overshoots it slightly; those samples are clipped back to
  full scale and counted (``clipped_samples``)
* ``<reel>.json``  provenance: source and map checksums, the curve, the output-time segments
  with their GET placement (GET = output time + constant), render statistics and a decoded
  checksum of the output

An output whose provenance matches the current source and map is kept; one made from other
inputs is never overwritten (pass ``--replace`` to move it aside first). Example:

    uv run python scripts/run.py sg_respeed.py --reel all --workers 12
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

from numba import njit, prange

from sg_sources import SG_ROOT

REPO = Path(__file__).resolve().parents[3]  # scripts/sg-audio/<stage>/
MAPS = REPO / 'catalog/sg_drift/sg_drift_maps.json'
OUTPUT_ROOT = Path(os.environ.get('A15_SG_RESPEED_ROOT', 'F:/Apollo_15_data/processing/sg/respeed'))
VERSION = 'sg-respeed-v3'
GRID_S = 0.1          # inverse-map grid
BLOCK_S = 5.0         # output block
HALO = 512            # input samples on each side of a block
HALF = 32             # sinc taps on each side
PHASES = 2048         # kernel table resolution per sample
BETA = 9.0            # Kaiser window


def kernel_table(cutoff: float) -> np.ndarray:
    """Windowed-sinc kernel sampled at 1/PHASES; ``cutoff`` as a fraction of Nyquist."""
    x = np.arange(-HALF * PHASES, HALF * PHASES + 1) / PHASES
    return np.r_[cutoff * np.sinc(cutoff * x) * np.kaiser(len(x), BETA), 0.0]


@njit(parallel=True, fastmath=True, cache=True)
def sinc_at(x, pos, table):
    """x (frames, channels) evaluated at fractional sample positions ``pos``."""
    n, ch = x.shape
    y = np.zeros((len(pos), ch), np.float32)
    for j in prange(len(pos)):
        base = int(np.floor(pos[j]))
        frac = pos[j] - base
        for k in range(-HALF + 1, HALF + 1):
            i = base + k
            if i < 0 or i >= n:
                continue
            d = (k - frac) * PHASES + HALF * PHASES
            di = int(d)
            w = table[di] + (table[di + 1] - table[di]) * (d - di)
            for c in range(ch):
                y[j, c] += w * x[i, c]
    return y


def file_sha256(path: Path, block: int = 16 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(block):
            h.update(chunk)
    return h.hexdigest()


def drift(reel: dict, t: np.ndarray) -> np.ndarray:
    scale = reel.get('drift_scale_s') or reel['duration_s']
    return sum(c * (t / scale) ** (d + 1) for d, c in enumerate(reel['drift_coefficients_s']))


def inverse_map(reel: dict, duration: float) -> tuple[np.ndarray, np.ndarray]:
    """(input seconds, output seconds) on a fine grid; output = input + D(input)."""
    t = np.unique(np.r_[np.arange(0., duration, GRID_S), duration])
    u = t + drift(reel, t)
    if not np.isfinite(u).all() or np.any(np.diff(u) <= 0):
        raise ValueError(f'{reel["reel"]}: non-monotone drift map')
    return t, u


def render(source: Path, output: Path, t: np.ndarray, u: np.ndarray, progress) -> dict:
    """Every output block uses one global inverse map; one fixed kernel for the whole file."""
    speed = np.diff(t) / np.diff(u)  # input seconds per output second (< 1: the output is longer)
    cutoff = min(1., 1. / float(speed.max())) * .97
    table = kernel_table(cutoff)
    peak, digest, frames_out, clipped = 0., hashlib.sha256(), 0, 0
    min_step, max_step, previous = float('inf'), 0., None
    started = last = time.monotonic()
    with sf.SoundFile(source) as f:
        sr, n, ch, subtype = f.samplerate, f.frames, f.channels, f.subtype
        if subtype != 'PCM_24':
            raise ValueError(f'unexpected source format {subtype}')
        full = 2 ** 23
        if abs(t[-1] - n / sr) > .5 / sr:
            raise ValueError('map and source duration disagree')
        count = int(round(u[-1] * sr))
        block = int(round(BLOCK_S * sr))
        fmt = 'RF64' if count * ch * 3 > 4_000_000_000 else 'WAV'
        with sf.SoundFile(output, 'w', samplerate=sr, channels=ch, subtype='PCM_24', format=fmt) as out:
            for start in range(0, count, block):
                times = np.arange(start, min(count, start + block)) / sr
                pos = np.clip(np.interp(times, u, t) * sr, 0., n - 1.)
                steps = np.diff(np.r_[previous, pos]) if previous is not None else np.diff(pos)
                if len(steps):
                    min_step, max_step = min(min_step, float(steps.min())), max(max_step, float(steps.max()))
                previous = pos[-1]
                left = max(0, int(np.floor(pos[0])) - HALO)
                right = min(n, int(np.ceil(pos[-1])) + HALO + 1)
                f.seek(left)
                x = f.read(right - left, dtype='float32', always_2d=True)
                y = sinc_at(x, pos - left, table)
                if not np.isfinite(y).all():
                    raise ValueError('non-finite output')
                peak = max(peak, float(np.abs(y).max()))
                q = np.round(y.astype(np.float64) * full)
                clipped += int(np.count_nonzero((q > full - 1) | (q < -full)))
                q = np.clip(q, -full, full - 1).astype(np.int32)
                digest.update(q.tobytes())
                out.write(q << 8)  # soundfile takes the top 24 bits of int32 for PCM_24
                frames_out += len(y)
                now = time.monotonic()
                if now - last >= 60:
                    progress(f'{100 * frames_out / count:.0f}% ({now - started:.0f} s)')
                    last = now
    return dict(frames=frames_out, sample_rate=sr, channels=ch, format=fmt, subtype='PCM_24',
                peak_before_clipping=round(peak, 6), clipped_samples=clipped,
                decoded_int24_sha256=digest.hexdigest(), minimum_input_sample_step=min_step,
                maximum_input_sample_step=max_step, antialias_cutoff_fraction_of_nyquist=cutoff,
                seconds=round(time.monotonic() - started, 1),
                resampler=f'{2 * HALF}-tap Kaiser (beta {BETA:g}) windowed sinc, {PHASES}-phase table; '
                          f'{HALO}-sample halo; {BLOCK_S:g} s blocks; global inverse map on a {GRID_S:g} s grid')


def provenance(reel: dict, maps_sha: str, source: Path) -> dict:
    return {'reel': reel['reel'], 'source': str(source), 'source_sha256': reel['sha256'], 'maps_file': str(MAPS),
            'maps_sha256': maps_sha, 'version': VERSION, 'rate_from': reel['rate_from'], 'degree': reel['degree'],
            'drift_coefficients_s': reel['drift_coefficients_s'],
            'drift_scale_s': reel.get('drift_scale_s') or reel['duration_s'], 'status': reel['status']}


def output_segments(reel: dict) -> list[dict]:
    """Each segment in output time, with its GET placement: GET = output seconds + constant."""
    out = []
    for s in reel.get('segments', []):
        a, b = (float(x) for x in (s['source_start_s'], s['source_end_s']))
        out.append({**s, 'output_start_s': round(a + float(drift(reel, np.array(a))), 3),
                    'output_end_s': round(b + float(drift(reel, np.array(b))), 3),
                    'get_equals_output_plus_s': s['constant_s']})
    return out


def run_reel(reel: dict, maps_sha: str, sg_root: Path, root: Path, replace: bool) -> str:
    name = reel['reel']
    source = sg_root / reel['file']
    wav, meta = root / f'{name}.wav', root / f'{name}.json'
    want = provenance(reel, maps_sha, source)
    if meta.is_file() and wav.is_file():
        have = json.loads(meta.read_text(encoding='utf-8'))
        if all(have.get(k) == v for k, v in want.items() if k not in ('source', 'maps_file')):
            return f'{name}: up to date'
        if not replace:
            return f'{name}: SKIPPED, existing output was made from other inputs (use --replace)'
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        for p in (wav, meta):
            p.rename(p.with_name(f'{p.stem}.superseded-{stamp}{p.suffix}'))
    if not source.is_file():
        return f'{name}: FAILED, source missing: {source}'
    sha = file_sha256(source)
    if sha != reel['sha256']:
        return f'{name}: FAILED, source checksum {sha[:12]} is not the mapped {reel["sha256"][:12]}'
    info = sf.info(source)
    t, u = inverse_map(reel, info.frames / info.samplerate)
    partial = wav.with_name(wav.stem + '.partial.wav')
    stats = render(source, partial, t, u, lambda m: print(f'  {name}: {m}', flush=True))
    partial.replace(wav)
    meta.write_text(json.dumps({
        **want, 'rendered': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'source_frames': info.frames, 'source_duration_s': info.frames / info.samplerate,
        'output_duration_s': stats['frames'] / stats['sample_rate'], 'added_s': round(float(u[-1] - t[-1]), 3),
        'rate_start_ppm': reel.get('rate_start_ppm'), 'rate_end_ppm': reel.get('rate_end_ppm'),
        'note': reel.get('note', ''), 'segments': output_segments(reel),
        'time_domain': 'output seconds = reel seconds + D(reel seconds); GET = output seconds + segment constant',
        'render': stats}, indent=1) + '\n', encoding='utf-8')
    return (f'{name}: done, +{u[-1] - t[-1]:.1f} s, {stats["clipped_samples"]} clipped samples, '
            f'{stats["seconds"]:.0f} s')


def _threads(n: int) -> None:
    import numba
    numba.set_num_threads(n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--reel', nargs='+', required=True, help="reel names, or 'all'")
    p.add_argument('--workers', type=int, default=4, help='reels at once (each uses its share of the cores)')
    p.add_argument('--sg-root', type=Path, default=SG_ROOT)
    p.add_argument('--output-root', type=Path, default=OUTPUT_ROOT)
    p.add_argument('--replace', action='store_true', help='move aside outputs made from other inputs and redo them')
    args = p.parse_args(argv)
    maps = json.loads(MAPS.read_text(encoding='utf-8'))
    maps_sha = hashlib.sha256(MAPS.read_bytes()).hexdigest()
    reels = [r for r in maps['reels'] if 'all' in args.reel or r['reel'] in args.reel]
    reels = [r for r in reels if r['status'] in ('fitted', 'rate_only') and r.get('drift_coefficients_s')]
    args.output_root.mkdir(parents=True, exist_ok=True)
    print(f'{len(reels)} reels -> {args.output_root} ({args.workers} workers)', flush=True)
    failed = 0
    threads = max(1, (os.cpu_count() or 1) // args.workers)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_threads, initargs=(threads,)) as pool:
        jobs = {pool.submit(run_reel, r, maps_sha, args.sg_root, args.output_root, args.replace): r['reel']
                for r in reels}
        for job in as_completed(jobs):
            try:
                message = job.result()
            except Exception as error:  # noqa: BLE001 - report and continue with the other reels
                message = f'{jobs[job]}: FAILED, {error}'
            failed += 'FAILED' in message or 'SKIPPED' in message
            print(message, flush=True)
    print(f'finished: {len(reels) - failed} ok, {failed} failed or skipped')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
