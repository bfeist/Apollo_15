"""Measure Quindar bursts after defluttering; never modify the input recording.

Nominal key/unkey frequencies: 2525/2475 Hz; nominal duration: 0.25 s.
See https://apollojournals.org/alsj/quindar.html . Generator tolerance is unknown.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage, signal

NOMINAL = np.array([2475., 2525.])


def quality_events(events):
    """Conservative, fixed gates applied before fitting a speed model."""
    return [dict(e) for e in events if e['band_power_fraction'] > .5
            and e['amplitude_ratio'] < 2 and e['trim_range_hz'] < 6
            and e['residual_fm_sd_hz'] < 27 and .23 < e['duration_seconds'] < .31]


def robust_fit(design, values):
    coef = np.linalg.lstsq(design, values, rcond=None)[0]
    for _ in range(30):
        residual = values-design@coef
        scale = max(.0005, 1.4826*np.median(abs(residual-np.median(residual))))
        w = np.minimum(1., 1.5*scale/np.maximum(abs(residual), 1e-15))
        new = np.linalg.lstsq(design*np.sqrt(w[:, None]), values*np.sqrt(w), rcond=None)[0]
        if np.max(abs(new-coef)) < 1e-11:
            break
        coef = new
    return coef


def fit_events(events):
    """Jointly identify two tone families and robust constant/linear speed fits.

    speed = measured frequency / nominal frequency. Playback multiplier = 1/speed.
    A linear model is deliberately slow: no attempt to infer flutter between beeps.
    """
    q = quality_events(events)
    if len(q) < 30:
        raise ValueError('Insufficient clean bursts for a tape-level assessment')
    freq = np.array([e['frequency_hz'] for e in q])
    centers = np.percentile(freq, [25, 75])
    for _ in range(30):
        label = np.argmin(abs(freq[:, None]-centers), axis=1)
        if min(np.bincount(label, minlength=2)) < 10:
            raise ValueError('Both Quindar tone families must be supported')
        new = np.array([np.median(freq[label == i]) for i in range(2)])
        if np.max(abs(new-centers)) < 1e-8:
            break
        centers = new
    if not 35 < centers[1]-centers[0] < 70:
        raise ValueError('Two separated Quindar families are not established')
    times = np.array([(e['start_seconds']+e['end_seconds'])/2 for e in q])
    design = np.column_stack([np.ones(len(q)), times/3600])
    for _ in range(10):
        ratios = freq/NOMINAL[label]
        linear = robust_fit(design, ratios)
        residuals = abs(freq[:, None]-(design@linear)[:, None]*NOMINAL)
        new_label = np.argmin(residuals, axis=1)
        if np.array_equal(new_label, label):
            break
        label = new_label
    ratios = freq/NOMINAL[label]
    constant = float(robust_fit(design[:, :1], ratios)[0])
    linear = robust_fit(design, ratios)
    if min(np.bincount(label, minlength=2)) < 10 or not np.all((design@linear > .94)&(design@linear < 1.06)):
        raise ValueError('Unsupported tone family assignment or speed outside +/-6%')
    for i, e in enumerate(q):
        e.update(kind='start' if label[i] else 'end', nominal_hz=float(NOMINAL[label[i]]),
                 recorded_speed_ratio=float(ratios[i]),
                 linear_residual_hz=float(freq[i]-(design@linear)[i]*NOMINAL[label[i]]))
    # Held-out 10-minute blocks, in both directions. No event-level random split.
    blocks = (times//600).astype(int)
    validation = []
    for parity in (0, 1):
        train = blocks % 2 == parity
        test = ~train
        if min(train.sum(), test.sum()) < 10:
            raise ValueError('Insufficient temporal coverage for held-out assessment')
        c = robust_fit(design[train, :1], ratios[train])
        l = robust_fit(design[train], ratios[train])
        validation.append(dict(training_block_parity=parity, training_count=int(train.sum()),
                               test_count=int(test.sum()),
                               constant_rmse_hz=float(np.sqrt(np.mean(((ratios[test]-c[0])*NOMINAL[label[test]])**2))),
                               linear_rmse_hz=float(np.sqrt(np.mean(((ratios[test]-design[test]@l)*NOMINAL[label[test]])**2)))))
    rng = np.random.default_rng(20261017)
    unique = np.unique(blocks)
    estimates = []
    for _ in range(500):
        ids = np.concatenate([np.flatnonzero(blocks == b) for b in rng.choice(unique, len(unique), replace=True)])
        estimates.append([robust_fit(design[ids, :1], ratios[ids])[0], *robust_fit(design[ids], ratios[ids])])
    ci = np.percentile(estimates, [2.5, 97.5], axis=0)
    families = []
    for k in (0, 1):
        ids = label == k
        fit = robust_fit(design[ids], ratios[ids])
        families.append(dict(kind='start' if k else 'end', count=int(ids.sum()),
                             median_frequency_hz=float(np.median(freq[ids])),
                             constant_speed_ratio=float(np.median(ratios[ids])),
                             linear_coefficients=fit.tolist()))
    return dict(events=q, constant_speed_ratio=constant, linear_coefficients=linear.tolist(),
                model_definition='recorded_speed(t) = intercept + slope_per_hour * input_seconds / 3600; playback_multiplier = 1 / recorded_speed',
                bootstrap_95_percent=dict(constant_speed_ratio=ci[:, 0].tolist(),
                                          linear_intercept=ci[:, 1].tolist(),
                                          linear_slope_per_hour=ci[:, 2].tolist(),
                                          caveat='500 resamples of occupied 10-minute blocks; statistical sampling only, excludes generator and recording-chain systematic error'),
                validation=validation, families=families,
                support=dict(first_event_seconds=float(times.min()), last_event_seconds=float(times.max()),
                             maximum_gap_seconds=float(np.max(np.diff(times))), occupied_ten_minute_blocks=len(unique)))


def corrected_time(input_seconds, coefficients):
    """Forward map. Absolute speed correction intentionally changes duration."""
    a, b = coefficients
    t = np.asarray(input_seconds)
    return a*t+b*t*t/7200


def render_excerpt(path, start, duration, coefficients):
    """Render a bounded review clip with the same absolute map as a whole tape.

    Uses the existing 8x FIR/cubic renderer with explicit antialias filtering.
    The source halo preserves interpolation/filter context at excerpt boundaries.
    """
    from flutter import resample_at
    a, b = coefficients
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        if f.channels != 1 or start < 0 or duration <= 0 or start+duration > len(f)/sr:
            raise ValueError('Invalid mono excerpt')
        speeds = a+b*np.array([start, start+duration])/3600
        if not np.isfinite(speeds).all() or min(speeds) < .94 or max(speeds) > 1.06:
            raise ValueError('Review renderer requires speed ratios within +/-6%')
        left = max(0, round(start*sr)-512)
        right = min(len(f), round((start+duration)*sr)+512)
        f.seek(left)
        x = f.read(right-left)
    # For speedups, suppress frequencies that would cross output Nyquist.
    cutoff = min(1., float(min(speeds)))*.97
    x = signal.fftconvolve(x, signal.firwin(255, cutoff, window=('kaiser', 8.6)), mode='same')
    s0 = a+b*start/3600
    out_duration = float(corrected_time(start+duration, coefficients)-corrected_time(start, coefficients))
    u = np.arange(round(out_duration*sr))/sr
    # Stable quadratic inverse also works for a zero slope.
    relative = 2*u/(s0+np.sqrt(s0*s0+2*b*u/3600))
    return resample_at(x, (start+relative)*sr-left), sr


def spans(mask):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    return zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))


def baseband(x, sr):
    if sr < 6000 or sr % 1000:
        raise ValueError('Sample rate must be >=6000 Hz and a multiple of 1000')
    z = signal.resample_poly(x * np.exp(-2j*np.pi*2500*np.arange(len(x))/sr), 1, sr//1000)
    return signal.sosfiltfilt(signal.butter(5, 280, fs=1000, output='sos'), z)


def measure(z, lo, hi):
    """Fit central unwrapped phase; retain residual FM and edge sensitivity."""
    amp = abs(z)
    estimates = []
    for trim in (20, 40, 60):
        zz = z[lo+trim:hi-trim]
        if len(zz) < 40:
            return None
        tt = np.arange(len(zz))/1000
        phase = np.unwrap(np.angle(zz))
        coef = np.polyfit(tt, phase, 1, w=abs(zz))
        estimates.append(float(2500+coef[0]/(2*np.pi)))
    core = slice(lo+30, hi-30)
    phase = np.unwrap(np.angle(z[core]))
    freq = 2500+ndimage.gaussian_filter1d(np.gradient(phase)*1000/(2*np.pi), 3)
    a = amp[core]
    return dict(frequency_hz=estimates[1], trim_frequencies_hz=estimates,
                trim_range_hz=float(np.ptp(estimates)),
                residual_fm_sd_hz=float(np.std(freq)),
                amplitude_ratio=float(np.percentile(a, 90)/max(np.percentile(a, 10), 1e-20)),
                mean_phase_frequency_hz=float(np.mean(freq)))


def detect(x, sr, offset=0., core_start=0., core_end=None, *, max_fm_hz=35., max_amplitude_ratio=3.):
    z = baseband(x, sr)
    amp = ndimage.gaussian_filter1d(abs(z), 2)
    floor = max(float(np.percentile(amp, 25)), 1e-12)
    threshold = max(4*floor, float(np.median(amp))*2)
    mask = ndimage.binary_closing(amp > threshold, iterations=3)
    found = []
    for lo, hi in spans(mask):
        begin = offset+lo/1000
        duration = (hi-lo)/1000
        if not (core_start <= begin < (core_end if core_end is not None else float('inf'))):
            continue
        if not .16 <= duration <= .38:
            continue
        m = measure(z, lo, hi)
        if m is None or not 2250 < m['frequency_hz'] < 2750:
            continue
        if m['amplitude_ratio'] > max_amplitude_ratio or m['residual_fm_sd_hz'] > max_fm_hz:
            continue
        # Reject speech harmonics: most power should belong to the single beep.
        a = x[round((lo+30)*sr/1000):round((hi-30)*sr/1000)]
        purity = float(2*np.mean(abs(z[lo+30:hi-30])**2)/max(np.mean(a*a), 1e-20))
        found.append(dict(start_seconds=begin, end_seconds=offset+hi/1000,
                          duration_seconds=duration, band_power_fraction=purity,
                          amplitude_over_floor=float(np.median(amp[lo+30:hi-30])/floor), **m))
    return found


def scan(path, start=0., duration=None):
    events = []
    then = time.monotonic()
    with sf.SoundFile(path) as f:
        if f.channels != 1:
            raise ValueError('Input must be mono')
        sr = f.samplerate
        end = len(f)/sr if duration is None else min(len(f)/sr, start+duration)
        if not 0 <= start < end:
            raise ValueError('Invalid scan range')
        for index, a in enumerate(np.arange(start, end, 60.)):
            left = max(0., a-1)
            right = min(len(f)/sr, a+61)
            f.seek(round(left*sr))
            x = f.read(round((right-left)*sr))
            events.extend(detect(x, sr, left, a, min(a+60, end)))
            if index % 60 == 0:
                print(f'{a/3600:.1f}/{end/3600:.1f} hours; {len(events)} candidates; {time.monotonic()-then:.1f}s', flush=True)
    return events


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True, help='Post-deflutter mono audio')
    p.add_argument('--output', type=Path, required=True, help='New JSON candidate inventory')
    p.add_argument('--start', type=float, default=0.)
    p.add_argument('--duration', type=float)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError('Output already exists; use a new path')
    before = args.input.stat()
    events = scan(args.input, args.start, args.duration)
    after = args.input.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Input changed during analysis')
    info = sf.info(args.input)
    result = dict(source=str(args.input.resolve()), bytes=before.st_size,
                  mtime_ns=before.st_mtime_ns, sample_rate=info.samplerate,
                  frames=info.frames, duration_seconds=info.duration,
                  scan_start=args.start, scan_end=min(info.duration, args.start+args.duration) if args.duration else info.duration,
                  implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  nominal_hz=NOMINAL.tolist(), events=events)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    print(f'Saved {len(events)} candidates to {args.output}', flush=True)


if __name__ == '__main__':
    main()
