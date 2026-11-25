"""Measure a channel's gain without letting isolated endpoint impulses set it."""
from dataclasses import asdict, dataclass

import numpy as np

from normalization import DEFAULT_MAX_BOOST_DB, DEFAULT_TARGET_DBFS, gain_for_peak


@dataclass(frozen=True)
class Settings:
    target_dbfs: float = DEFAULT_TARGET_DBFS
    max_boost_db: float = DEFAULT_MAX_BOOST_DB
    edge_seconds: float = 5.
    min_impulse_ratio: float = 2.
    max_impulse_ms: float = 40.
    bridge_ms: float = 20.


def configuration():
    return asdict(Settings())


def peak(samples):
    if not np.isfinite(samples).all():
        raise ValueError('Non-finite normalization input')
    return float(np.max(np.abs(samples), initial=0.))


def endpoint_peak(samples, interior_peak, rate, start, settings):
    """Only exclude bursts well above the interior peak and short in context."""
    if not interior_peak:
        return peak(samples), []
    magnitude = np.abs(samples)
    above = np.flatnonzero(magnitude > interior_peak)
    gap = max(1, round(rate*settings.bridge_ms/1000))
    max_frames = max(1, round(rate*settings.max_impulse_ms/1000))
    groups = np.split(above, np.flatnonzero(np.diff(above) > gap)+1)
    ignored = []
    for group in groups:
        if not group.size:
            continue
        a, b = int(group[0]), int(group[-1])+1
        event_peak = float(np.max(magnitude[a:b]))
        if b-a <= max_frames and event_peak >= interior_peak*settings.min_impulse_ratio:
            ignored.append(dict(source_frames=[start+a,start+b], input_peak=event_peak))
            magnitude[a:b] = 0
    return float(np.max(magnitude, initial=0.)), ignored


def measure(audio, settings, check_stop=lambda: None):
    """Scan decoded audio in bounded memory and return a constant-gain record."""
    rate, frames = audio.samplerate, audio.frames
    edge = round(settings.edge_seconds*rate)
    # A short clip cannot provide enough independent interior reference.
    if frames < 2*edge+rate:
        edge = 0
    first, last = edge, frames-edge
    interior_peak = 0.
    audio.seek(first)
    for offset in range(first, last, rate*10):
        check_stop()
        count = min(rate*10, last-offset)
        samples = audio.read(count, dtype='float32')
        if len(samples) != count:
            raise ValueError('Truncated normalization input')
        interior_peak = max(interior_peak, peak(samples))

    program_peak = raw_peak = interior_peak
    ignored = []
    if edge:
        for start in (0, last):
            audio.seek(start)
            samples = audio.read(edge, dtype='float32')
            if len(samples) != edge:
                raise ValueError('Truncated normalization endpoint')
            raw_peak = max(raw_peak, peak(samples))
            accepted_peak, events = endpoint_peak(samples, interior_peak, rate, start, settings)
            program_peak = max(program_peak, accepted_peak)
            ignored.extend(events)
    result = gain_for_peak(program_peak, settings.target_dbfs, settings.max_boost_db)
    result.update(method='constant_endpoint_aware_sample_peak', settings=asdict(settings),
                  unfiltered_input_peak=raw_peak, interior_input_peak=interior_peak,
                  scanned_edge_frames=edge, ignored_endpoint_impulses=ignored,
                  program_output_peak=program_peak*result['gain'])
    return result
