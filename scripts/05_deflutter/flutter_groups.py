"""Bounded playback-motion transfer, independent of content/head alignment.

Each member must have its own distinct reference tone on original audio. No
cross-correlation lag search is allowed. Groups are rechecked during monitoring and expire on
support gaps or motion disagreement. A group is numeric evidence, not listening.
"""
import numpy as np

from flutter_tracking import RATE

SETTINGS = dict(window_seconds=3., minimum_windows_per_split=2,
                reserved_final_map_seconds=.5,
                minimum_phase_correlation=.90, amplitude_gain_range=[.8, 1.2],
                maximum_residual_speed_rms=.003, maximum_amplitude_coupling=.5,
                minimum_frequency_separation_hz=100.)


def compare_motion(applied, independent, support, amplitude, lo, hi):
    result = []
    width = round(SETTINGS['window_seconds']*RATE)
    for a in range(lo, hi-width+1, width):
        b = a+width-round(SETTINGS['reserved_final_map_seconds']*RATE)
        if np.min(support[a:b]) < .95:
            result.append(dict(start=a/RATE, end=b/RATE, passed=False, reason='unsupported_gap'))
            continue
        x, y, am = applied[a:b], independent[a:b], amplitude[a:b]
        if min(np.std(x), np.std(y)) < .0002:
            result.append(dict(start=a/RATE, end=b/RATE, passed=False, reason='insufficient_motion'))
            continue
        corr = float(np.corrcoef(x, y)[0, 1])
        gain = float(np.dot(x, y)/max(np.dot(x, x), 1e-20))
        residual = float(np.std(y-x))
        coupling = float(abs(np.corrcoef(x, am)[0, 1])) if np.std(am) > 1e-10 else 0.
        passed = (corr >= SETTINGS['minimum_phase_correlation']
                  and SETTINGS['amplitude_gain_range'][0] <= gain <= SETTINGS['amplitude_gain_range'][1]
                  and residual <= SETTINGS['maximum_residual_speed_rms']
                  and coupling <= SETTINGS['maximum_amplitude_coupling'])
        result.append(dict(start=a/RATE, end=b/RATE, phase_correlation=corr, amplitude_gain=gain,
                           residual_speed_rms=residual, amplitude_coupling=coupling, passed=bool(passed)))
    return result
