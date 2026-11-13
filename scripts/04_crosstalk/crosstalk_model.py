"""Two-path echo subtraction promoted from the November 8 Audition trial.

Only the echo prediction is subtracted. Callers must establish repeat support
and a fixed tape/channel direction; correlation alone is not echo evidence.
"""
import numpy as np
from scipy import signal
from scipy.interpolate import CubicSpline
from scipy.linalg import solve

RATE = 8000
CONTEXT = 2 * RATE


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


