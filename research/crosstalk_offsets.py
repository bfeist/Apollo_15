"""Measure local interchannel delays from completed denoised audio."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

if __package__:
    from .crosstalk_probe import CONTEXT, RATE, read_window
else:
    from crosstalk_probe import CONTEXT, RATE, read_window


def denoised_sources(folder):
    paths, provenance = {}, {}
    for manifest in sorted(folder.glob('CH*/denoised.json')):
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        signature = saved['signature']
        path = manifest.with_name('denoised.wav')
        info = sf.info(path)
        channel = signature['source']['channel']
        if (saved['status'] != 'complete' or signature['offset_frames'] != 0
                or info.frames != signature['frames'] or info.samplerate != RATE
                or info.channels != 1):
            raise ValueError(f'Incomplete or unaligned denoised source: {manifest}')
        if channel in paths:
            raise ValueError(f'Duplicate channel {channel}')
        paths[channel] = path
        provenance[channel] = dict(path=str(path.resolve()), frames=info.frames,
                                   manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                                   saved_audio=saved['audio'], signature=signature)
    if not paths:
        raise ValueError('No completed denoised channels')
    return paths, provenance


def profile(target, donor):
    reference = target[CONTEXT:-CONTEXT]
    products = signal.correlate(donor, reference, mode='valid', method='fft')
    energy = np.r_[0., np.cumsum(donor ** 2)]
    energy = energy[len(reference):] - energy[:-len(reference)]
    return products / np.sqrt(np.maximum(np.dot(reference, reference) * energy, 1e-30))


def peak(correlation, low, high):
    first, last = CONTEXT + round(low * RATE), CONTEXT + round(high * RATE)
    values = abs(correlation[first:last + 1])
    k = int(np.argmax(values))
    fraction = 0.
    if 0 < k < len(values) - 1:
        left, middle, right = values[k - 1:k + 2]
        curvature = left - 2 * middle + right
        if curvature < 0:
            fraction = float(.5 * (left - right) / curvature)
    alternatives = values.copy()
    radius = round(.005 * RATE)
    alternatives[max(0, k - radius):k + radius + 1] = 0
    return dict(lag_seconds=(first + k + fraction - CONTEXT) / RATE,
                correlation=float(correlation[first + k]),
                peak_ratio=float(values[k] / max(np.max(alternatives), 1e-15)))


def measure(folder, starts, duration, channels=None):
    paths, provenance = denoised_sources(folder)
    if channels is not None:
        paths = {ch: paths[ch] for ch in channels}
        provenance = {ch: provenance[ch] for ch in channels}
    bands = [(300, 3000), (300, 900), (900, 1800), (1800, 3000)]
    kernels = [signal.firwin(257, band, fs=RATE, pass_zero=False) for band in bands]
    rows = []
    for start in starts:
        audio = {ch: read_window(path, start, duration) for ch, path in paths.items()}
        filtered = {ch: [signal.fftconvolve(x, kernel, mode='same') for kernel in kernels]
                    for ch, x in audio.items()}
        for target in sorted(paths):
            donor = target + 1
            if donor not in paths:
                continue
            profiles = [profile(a, b) for a, b in zip(filtered[target], filtered[donor])]
            for name, low, high in [('early', -1.9, -.8), ('late', .8, 1.9),
                                    ('coincident', -.05, .05)]:
                peaks = [peak(p, low, high) for p in profiles]
                broad = peaks[0]
                supporting = [p for p in peaks[1:] if abs(p['correlation']) >= .15
                              and p['peak_ratio'] >= 1.15
                              and abs(p['lag_seconds'] - broad['lag_seconds']) <= .002]
                center = CONTEXT + round(broad['lag_seconds'] * RATE)
                reference = audio[target][CONTEXT:-CONTEXT]
                aligned = audio[donor][center:center + len(reference)]
                rows.append(dict(start=start, target=target, donor=donor, mode=name,
                                 peaks=peaks, supporting_bands=len(supporting),
                                 donor_over_target_db=float(10 * np.log10(
                                     max(np.dot(aligned, aligned), 1e-30) /
                                     max(np.dot(reference, reference), 1e-30))),
                                 supported=(len(supporting) >= 2 and abs(broad['correlation']) >= .15
                                            and broad['peak_ratio'] >= 1.15)))
    return dict(source_kind='completed_denoised', sources=provenance, duration=duration,
                band_hz=bands, lag_definition='donor sample time minus target sample time',
                limitations='Fractional peak interpolation is an estimate, not certified timing accuracy. '
                             'Repeated windows can belong to the same speech event. '
                             'A copied signal alone does not identify crosstalk.', rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--starts', type=float, nargs='+', required=True)
    parser.add_argument('--span', type=float, default=60)
    parser.add_argument('--duration', type=float, default=2)
    parser.add_argument('--channels', type=int, nargs='+')
    args = parser.parse_args()
    if args.duration <= 0 or args.span < args.duration:
        parser.error('Require positive duration and span at least duration')
    starts = [float(t) for start in args.starts
              for t in np.arange(start, start + args.span - args.duration + 1e-8, args.duration)]
    if args.output.exists():
        parser.error('Choose a fresh output file')
    result = measure(args.input, starts, args.duration, args.channels)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2)


if __name__ == '__main__':
    main()
