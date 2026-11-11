"""Acquisition bandwidth alternatives that preserve low-carrier FM sidebands.

A wider capture is a candidate, never an eligibility decision. Independent
target calibration and competing-tone checks must choose between alternatives.
"""
import math


def capture_candidates(carrier_hz, sample_rate, modulation_band):
    if (not math.isfinite(carrier_hz) or not math.isfinite(sample_rate)
            or sample_rate <= 0 or not 0 < carrier_hz < sample_rate/2):
        raise ValueError('Carrier must lie inside the sampled frequency band')
    if (len(modulation_band) != 2 or not all(math.isfinite(v) for v in modulation_band)
            or not 0 < modulation_band[0] < modulation_band[1]):
        raise ValueError('Use a finite increasing positive modulation band')
    # Keep the existing conservative carrier fraction while respecting the
    # distance to Nyquist, which matters for candidates near the recorded edge.
    ceiling = min(180., carrier_hz*.45, .9*(sample_rate/2-carrier_hz))
    narrow = min(ceiling, max(12., carrier_hz*.09))
    # The 121-tap/1kHz capture FIR has a broad transition. A 12Hz nominal
    # cutoff attenuates 6.5Hz and 13Hz FM sidebands (~.84/.45 gain). Giving the
    # declared modulation band a modest guard restores useful flat passband.
    wider = min(ceiling, max(narrow, modulation_band[1]+5.))
    return tuple(sorted({round(narrow, 6), round(wider, 6)}))
