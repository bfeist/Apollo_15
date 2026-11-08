"""Bounded two-path subtraction of an evidenced earlier repeat, before de-flutter.

This is an Audition experiment, not a tape-wide production gate. The explicit
repeat interval is established from within-target matches before subtraction.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.interpolate import CubicSpline
from scipy.linalg import solve

if __package__:
    from .crosstalk_offsets import denoised_sources, peak, profile
    from .crosstalk_probe import CONTEXT, RATE, read_window
else:
    from crosstalk_offsets import denoised_sources, peak, profile
    from crosstalk_probe import CONTEXT, RATE, read_window


def design(audio, taps=33):
    return np.lib.stride_tricks.sliding_window_view(np.pad(audio, taps // 2), taps)


def fit(matrix, target, ridge=.03):
    scale = np.sqrt(np.mean(matrix ** 2, axis=0) + 1e-20)
    normalized = matrix / scale
    return solve(normalized.T @ normalized + ridge * len(target) * np.eye(matrix.shape[1]),
                 normalized.T @ target, assume_a='pos') / scale


def trajectory(target, donor, nominal, radius, polarity=None):
    """Fractional local alignment, constrained to a continuous signed path.

    Flat/weak evidence holds a path; it does not authorize subtraction. The
    renderer separately restricts support to the evidenced repeat interval.
    """
    n, r, window, hop = round(nominal * RATE), round(radius * RATE), 640, 160
    centers = np.arange(CONTEXT, len(target) - CONTEXT, hop)
    profiles = []
    for center in centers:
        first = center - window // 2
        reference = target[first:first + window]
        audio = donor[first + n - r:first + n + r + window]
        dots = signal.correlate(audio, reference, mode='valid', method='fft')
        energy = np.r_[0., np.cumsum(audio * audio)]
        profiles.append(dots / np.sqrt(np.maximum(
            (energy[window:] - energy[:-window]) * np.dot(reference, reference), 1e-30)))
    correlations = np.array(profiles)
    evidence = abs(correlations) if polarity is None else np.maximum(polarity * correlations, 0)
    lags = np.arange(-r, r + 1) + n
    previous = np.zeros(len(lags))
    back = []
    for row in evidence:
        options = np.full((25, len(lags)), -1e9)
        indices = np.arange(len(lags))
        for j, shift in enumerate(range(-12, 13)):
            valid = (indices + shift >= 0) & (indices + shift < len(lags))
            options[j, valid] = previous[indices[valid] + shift] - .0015 * shift * shift
        best = np.argmax(options, axis=0)
        back.append(indices + best - 12)
        previous = options[best, indices] + np.maximum(row * row - .08, 0)
        previous -= previous.max()
    index = int(np.argmax(previous))
    choices = []
    for predecessor in back[::-1]:
        choices.append(index)
        index = predecessor[index]
    choices = np.array(choices[::-1])
    fractions = []
    for row, index in zip(evidence, choices):
        fraction = 0.
        if 0 < index < len(row) - 1:
            left, middle, right = row[index - 1:index + 2]
            if middle >= left and middle >= right and left - 2 * middle + right < 0:
                fraction = float(np.clip(.5 * (left - right) / (left - 2 * middle + right), -.5, .5))
        fractions.append(fraction)
    values = lags[choices] + fractions
    positions = np.arange(len(target))
    curve = np.interp(positions, centers, values)
    aligned = CubicSpline(positions, donor)(np.clip(positions + curve, 0, len(donor) - 1))
    return aligned, dict(time=((centers - CONTEXT) / RATE).tolist(),
                         donor_minus_target_seconds=(values / RATE).tolist(),
                         correlation=correlations[np.arange(len(centers)), choices].tolist())


def preliminary_path(target, donor):
    matrix = design(donor, 17)
    result, weights = np.zeros(len(target)), np.zeros(len(target))
    for center in range(CONTEXT, len(target) - CONTEXT, 800):
        indices = np.arange(center - 1600, center + 1600)
        window = np.hanning(len(indices))
        coefficients = fit(matrix[indices], target[indices])
        result[indices] += window * (matrix[indices] @ coefficients)
        weights[indices] += window
    return result / np.maximum(weights, 1e-20)


def subtract(target, main, echo, calibration, support):
    """Fit both paths, subtract only echo; freeze B and cross-check local C."""
    main_matrix, echo_matrix = design(main), design(echo)
    joint = np.column_stack([main_matrix, echo_matrix])
    positions = np.arange(len(target))
    phase = positions % 160
    train = calibration & (phase >= 20) & (phase < 60)
    check = calibration & (phase >= 100) & (phase < 140)
    check_joint = fit(joint[train], target[train])
    check_main = fit(main_matrix[train], target[train])
    before = np.sum((target[check] - main_matrix[check] @ check_main) ** 2)
    after = np.sum((target[check] - joint[check] @ check_joint) ** 2)
    fixed_accepted = bool(after < .966 * before and before-after > .002 * np.sum(target[check]**2))
    coefficients = fit(joint[calibration], target[calibration])
    fixed = support * (echo_matrix @ coefficients[33:]) * fixed_accepted
    adaptive, weights = np.zeros(len(target)), np.zeros(len(target))
    rows = []
    active = np.flatnonzero(support)
    for center in range(int(active[0]), int(active[-1]) + 1, 400):
        indices = np.arange(center - 1200, center + 1200)
        matrix, audio = joint[indices], target[indices]
        # Alternate 10 ms blocks, with a 2.5 ms guard around boundaries so
        # adjacent 33-tap regressors cannot straddle train/check partitions.
        phase = indices % 160
        train = (phase >= 20) & (phase < 60)
        check = (phase >= 100) & (phase < 140)
        local = fit(matrix[train], audio[train])
        kept = matrix[:, :33] @ local[:33]
        main_only = matrix[:, :33] @ fit(matrix[train, :33], audio[train])
        removed = matrix[:, 33:] @ local[33:]
        before = np.sum((audio[check] - main_only[check]) ** 2)
        after = np.sum((audio[check] - kept[check] - removed[check]) ** 2)
        change = float(10 * np.log10(max(after, 1e-30) / max(before, 1e-30)))
        confidence = float(np.clip((-change - .15) / 1.5, 0, 1))
        if before - after <= .002 * np.sum(audio[check] ** 2):
            confidence = 0.
        local = fit(matrix, audio)
        window = np.hanning(len(indices))
        adaptive[indices] += window * confidence * (matrix[:, 33:] @ local[33:])
        weights[indices] += window
        rows.append(dict(time=(center - CONTEXT) / RATE, check_residual_db=change,
                         confidence=confidence, coefficients=local.tolist()))
    adaptive = support * adaptive / np.maximum(weights, 1e-20)
    return fixed, adaptive, dict(fixed_coefficients=coefficients.tolist(),
                                 fixed_accepted=fixed_accepted, local=rows)


def envelope(times, start, end, fade=.08):
    weight = np.minimum(np.clip((times - start) / fade, 0, 1),
                        np.clip((end - times) / fade, 0, 1))
    return .5 - .5 * np.cos(np.pi * weight)


def render(folder, output):
    paths, provenance = denoised_sources(folder)
    output.mkdir(exist_ok=False)
    start, duration, gain = 13555., 30., 1.3996783018483525
    kernel = signal.firwin(257, [300, 3000], fs=RATE, pass_zero=False)
    donor = read_window(paths[19], start, duration)
    x = signal.fftconvolve(donor, kernel, mode='same')
    times = (np.arange(len(x)) - CONTEXT) / RATE
    core = slice(CONTEXT, -CONTEXT)
    summaries, directions = {}, {}
    for channel in (17, 20):
        destination = output / f'CH{channel}'
        destination.mkdir()
        raw = read_window(paths[channel], start, duration)
        target = signal.fftconvolve(raw, kernel, mode='same')
        main, main_track = trajectory(target - preliminary_path(target, x), x, -1.377, .025)
        echo, echo_track = trajectory(target, x, .00125 if channel == 17 else 0.,
                                      .00035, polarity=-1 if channel == 17 else 1)
        # Same earlier-copy direction throughout. This bounded experiment covers
        # one independently evidenced phrase, not every matching donor event.
        support = envelope(times, 14.10, 15.48)
        calibration = (times >= 14.25) & (times < 14.75)
        fixed, adaptive, fits = subtract(target, main, echo, calibration, support)
        versions = dict(A=raw, B=raw-fixed, C=raw-adaptive, removed_B=fixed, removed_C=adaptive)
        files, decoded = {}, {}
        for name, samples in versions.items():
            audio = gain * samples[core]
            if not np.isfinite(audio).all() or np.max(abs(audio)) >= 1:
                raise ValueError('Nonfinite or clipped export')
            path = destination / f'{name}.wav'
            sf.write(path, audio, RATE, subtype='PCM_24')
            decoded[name], rate = sf.read(path)
            assert rate == RATE and len(decoded[name]) == round(duration * RATE)
            files[name] = dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               peak=float(np.max(abs(decoded[name]))))
        evidence = []
        for offset in np.arange(14.3, 15.41, .1):
            section = signal.fftconvolve(read_window(paths[channel], start + offset, .2), kernel, mode='same')
            match = peak(profile(section, section), 1.32, 1.42)
            k = CONTEXT + round(match['lag_seconds'] * RATE)
            match.update(earlier_canonical_seconds=start + float(offset),
                         later_over_earlier_db=float(10 * np.log10(
                             np.sum(section[k:k+1600] ** 2) / np.sum(section[CONTEXT:-CONTEXT] ** 2))))
            evidence.append(match)
        metrics = {}
        for label, a, b in [('calibration', 14.25, 14.75), ('withheld_tail', 14.80, 15.40),
                            ('stronger_main', 15.70, 17.00), ('marked_12_seconds', 11.5, 12.5)]:
            selected = (times >= a) & (times < b)
            metrics[label] = {name: float(10 * np.log10(
                np.sum((target[selected] - prediction[selected]) ** 2) / np.sum(target[selected] ** 2)))
                for name, prediction in [('B', fixed), ('C', adaptive)]}
        inactive = support[core] == 0
        checks = dict(frames=round(duration*RATE), sample_rate=RATE, channels=1, subtype='PCM_24',
                      baseline_max_quantization_error=float(np.max(abs(decoded['A'] - raw[core]*gain))),
                      unchanged_outside_support=all(np.array_equal(decoded['A'][inactive], decoded[n][inactive])
                                                    for n in ('B','C')),
                      subtraction_identity_error={n:float(np.max(abs(decoded['A']-decoded[n]-decoded[f'removed_{n}'])))
                                                  for n in ('B','C')})
        assert checks['unchanged_outside_support']
        assert checks['baseline_max_quantization_error'] <= 2**-23
        assert max(checks['subtraction_identity_error'].values()) <= 2**-22
        report = dict(tape='T510083',target=channel,donor=19,start=start,duration=duration,common_gain=gain,
                      operation_order='completed denoised -> crosstalk; no de-flutter or extra output cleanup',
                      direction='earlier', signed_main_to_echo_seconds_approx=-1.37,
                      direction_evidence=evidence, direction_scope='fixed T510083/channel direction for subsequent research; listening acceptance pending',
                      application_interval=[13569.10,13570.48], calibration_interval=[13569.25,13569.75],
                      main_tracking=main_track,echo_tracking=echo_track,fits=fits,metrics=metrics,
                      files=files,verification=checks,sources={c:provenance[c] for c in (channel,19)},
                      limitations=['Only the clearly paired phrase at excerpt 14.1–15.48 s is changed. The marked 12 s is untouched.',
                                   'B freezes transfer coefficients; its local timing still uses evaluation audio.',
                                   'C refits locally and uses withheld blocks as an adaptive screen, not an independent event qualification.',
                                   'Energy reduction is not a measurement of perceptual echo removal or proof of preserved overlapping speech.',
                                   'The later main occurrence is bit-identical by explicit bypass. Wider intervals remain unqualified.'])
        (destination/'review.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        directions[f'T510083/CH{channel:02}'] = dict(
            remove='earlier', status='measured on identified repeated phrase; listening acceptance pending',
            reuse='Keep this direction fixed for subsequent tape/channel tests; bypass ambiguous windows.',
            evidence=evidence, report=str((destination/'review.json').resolve()))
        summaries[channel]=dict(metrics=metrics,verification=checks)
    (output/'summary.json').write_text(json.dumps(summaries,indent=2),encoding='utf-8')
    (output/'directions.json').write_text(json.dumps(directions,indent=2),encoding='utf-8')
    print(json.dumps(summaries,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    render(args.input,args.output)
