"""Render a denoised crosstalk comparison with a locally tracked donor delay."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.interpolate import CubicSpline

from crosstalk_offsets import denoised_sources
from crosstalk_probe import CONTEXT, RATE, read_window


def track(target, donor, center_seconds, *, minimum_correlation=.65, radius_seconds=.008):
    window, hop = round(.06 * RATE), round(.02 * RATE)
    radius = round(radius_seconds * RATE)
    nominal = round(center_seconds * RATE)
    centers, delays, correlations, accepted = [], [], [], []
    for center in range(CONTEXT, len(target) - CONTEXT, hop):
        first = center - window // 2
        y = target[first:first + window]
        x = donor[first + nominal - radius:first + nominal + radius + window]
        dots = signal.correlate(x, y, mode='valid', method='fft')
        energy = np.r_[0., np.cumsum(x * x)]
        energy = energy[window:] - energy[:-window]
        corr = dots / np.sqrt(np.maximum(energy * np.dot(y, y), 1e-30))
        k = int(np.argmax(corr))
        fraction = 0.
        if 0 < k < len(corr) - 1:
            a, b, c = corr[k - 1:k + 2]
            fraction = float(np.clip(.5 * (a - c) / min(a - 2*b + c, -1e-20), -.5, .5))
        other = corr.copy()
        other[max(0, k - 8):k + 9] = 0
        good = (0 < k < len(corr) - 1 and corr[k] >= minimum_correlation
                and corr[k] >= 1.15 * max(np.max(other), 1e-15))
        centers.append(center)
        delays.append(nominal - radius + k + fraction)
        correlations.append(float(corr[k]))
        accepted.append(good)
    centers, delays, accepted = np.array(centers), np.array(delays), np.array(accepted)
    # Reject isolated estimates and abrupt jumps before interpolation.
    neighbors = np.zeros(len(centers), dtype=bool)
    consistent = accepted[1:] & accepted[:-1] & (abs(np.diff(delays)) < .001 * RATE)
    neighbors[1:] |= consistent
    neighbors[:-1] |= consistent
    accepted &= neighbors
    if np.count_nonzero(accepted) < 3:
        raise ValueError('Insufficient delay support')
    points = np.arange(len(target))
    times, values = centers[accepted], delays[accepted]
    curve = np.interp(points, times, values)
    right = np.searchsorted(times, points, side='right')
    left = np.clip(right - 1, 0, len(times) - 1)
    right = np.clip(right, 0, len(times) - 1)
    supported = ((points >= times[0]) & (points <= times[-1])
                 & (times[right] - times[left] <= .10 * RATE))
    fade = signal.convolve(supported.astype(float), np.ones(161) / 161, mode='same')
    return curve, fade, dict(centers=centers.tolist(), lag_samples=delays.tolist(),
                            correlation=correlations, accepted=accepted.tolist())


def render(folder, output, start, duration, target_channel, donor_channel, nominal):
    paths, provenance = denoised_sources(folder)
    raw = read_window(paths[target_channel], start, duration)
    donor = read_window(paths[donor_channel], start, duration)
    kernel = signal.firwin(257, [300, 3000], fs=RATE, pass_zero=False)
    y, x = [signal.fftconvolve(wav, kernel, mode='same') for wav in (raw, donor)]
    curve, gate, tracking = track(y, x, nominal)
    positions = np.arange(len(x))
    spline = CubicSpline(positions, x, extrapolate=False)
    aligned = spline(np.clip(positions + curve, 0, len(x) - 1))
    fixed = spline(np.clip(positions + nominal * RATE, 0, len(x) - 1))
    # Fit one transfer filter in the first ten seconds, keeping later coefficients frozen.
    taps = 17
    design = np.lib.stride_tricks.sliding_window_view(np.pad(aligned, taps // 2), taps)
    calibration = ((positions >= CONTEXT) & (positions < CONTEXT + 10 * RATE) & (gate > .99))
    if np.count_nonzero(calibration) < RATE:
        raise ValueError('Less than one second of calibration support')
    matrix = design[calibration]
    gram = matrix.T @ matrix
    coefficients = np.linalg.solve(gram + .01 * np.trace(gram) / taps * np.eye(taps), matrix.T @ y[calibration])
    prediction = gate * (design @ coefficients)
    fixed_design = np.lib.stride_tricks.sliding_window_view(np.pad(fixed, taps // 2), taps)
    fixed_prediction = gate * (fixed_design @ coefficients)
    section = slice(CONTEXT, -CONTEXT)
    versions = dict(A_denoised=raw[section], B_tracked= (raw - prediction)[section],
                    C_fixed_delay=(raw - fixed_prediction)[section], removed=prediction[section],
                    donor_aligned=aligned[section])
    # One common listening gain, including the donor reference in the peak limit.
    gain = .95 / max(1e-12, *(np.max(abs(wav)) for wav in versions.values()))
    output.mkdir()
    files = {}
    for label, wav in versions.items():
        path = output / f'{label}.wav'
        sf.write(path, wav * gain, RATE, subtype='PCM_24')
        files[label] = dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    check = slice(CONTEXT + 10 * RATE, len(raw) - CONTEXT)
    baseline_power = np.sum(y[check] ** 2)
    metrics = {name: float(10 * np.log10(np.sum((y[check] - p[check]) ** 2) / baseline_power))
               for name, p in [('tracked_residual_db', prediction), ('fixed_residual_db', fixed_prediction)]}
    report = dict(start=start, duration=duration, sample_rate=RATE, target=target_channel, donor=donor_channel,
                  source_kind='completed denoised', sources={c: provenance[c] for c in (target_channel, donor_channel)},
                  common_gain=gain, nominal_seconds=nominal, tracking=tracking,
                  coverage_seconds=float(np.sum(gate[section] > .99) / RATE), coefficients=coefficients.tolist(),
                  calibration_seconds=10, metrics=metrics, files=files,
                  limitation='Exploratory listening test. Delay tracking uses both channels during evaluation. '
                             'Residual energy reduction does not independently prove preservation of wanted speech.')
    (output / 'review.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(dict(metrics=metrics, coverage_seconds=report['coverage_seconds'], common_gain=gain)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--start', type=float, required=True)
    parser.add_argument('--duration', type=float, default=40)
    parser.add_argument('--pair', type=int, nargs=2, required=True)
    parser.add_argument('--nominal', type=float, required=True)
    args = parser.parse_args()
    if args.duration <= 10 or abs(args.nominal) > 1.9:
        parser.error('Require duration above ten seconds and nominal delay within +/-1.9 seconds')
    render(args.input, args.output, args.start, args.duration, *args.pair, args.nominal)
