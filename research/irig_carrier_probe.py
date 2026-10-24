"""Bounded, read-only IRIG reference probe; not a production detector.

Run with Apollo_Audio_Processing's Python environment (jdsp is required).
Only JSON evidence is written. Source audio and the legacy code are unchanged.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy import signal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--legacy-repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--starts', type=float, nargs='+', default=[60, 600, 3600])
    parser.add_argument('--duration', type=float, default=30)
    args = parser.parse_args()
    if (not np.isfinite(args.starts).all() or min(args.starts) < 0
            or not 10 <= args.duration <= 120 or len(args.starts)*args.duration > 300):
        parser.error('Use nonnegative starts, 10-120 s excerpts, and at most 300 s total')
    source = args.input.resolve()
    output = args.output.resolve()
    if output.suffix.lower() != '.json' or output.exists() or output == source:
        parser.error('Output must be a new JSON file')
    sys.path.insert(0, str(args.legacy_repo.resolve()/'processing_scripts'))
    from jdsp.io import WaveSource
    from irig_b import IRIGBDecoder

    before = source.stat()
    wav = WaveSource(str(source))
    sr, frames = wav.rate(), wav._w.getnframes()
    if wav.channels() != 1 or max(args.starts)+args.duration > frames/sr:
        parser.error('Require mono audio covering every requested excerpt')
    result = dict(
        purpose='Known-reference probe, not an Apollo 15 survey or transfer validation',
        source=dict(path=str(source), bytes=before.st_size, mtime_ns=before.st_mtime_ns,
                    sample_rate=sr, frames=frames),
        duration_per_excerpt=args.duration,
        validity_policy='SBS agrees with H:M:S; ranges valid; historical year field zero. '
                        'These checks alone do not authenticate timecode.',
        timing_policy='Original sample positions; zero-phase diagnostic filters. '
                      'Filter/polarity-dependent crossing bias is not calibrated.',
        excerpts=[])
    for start in args.starts:
        start_sample = round(start*sr)
        wav._w.setpos(start_sample)
        x = wav.read(round(args.duration*sr))[0]
        freq, power = signal.welch(x, sr, nperseg=sr*2)
        band = np.flatnonzero((freq > 700) & (freq < 1300))
        peak = band[np.argmax(power[band])]
        row = dict(start_seconds=start, frames=len(x),
                   excerpt_float64_sha256=hashlib.sha256(x.tobytes()).hexdigest(),
                   strongest_700_1300_hz_peak=float(freq[peak]), attempts=[])
        variants = [
            ('raw', x),
            ('highpass_400', signal.sosfiltfilt(signal.butter(
                4, 400, fs=sr, btype='highpass', output='sos'), x)),
            ('bandpass_650_1350', signal.sosfiltfilt(signal.butter(
                4, [650, 1350], fs=sr, btype='bandpass', output='sos'), x)),
        ]
        for name, samples in variants:
            for polarity in (1, -1):
                decoded = []
                decoder = IRIGBDecoder(sr, decoded.append)
                decoder.set_sample_number(start_sample)
                for offset in range(0, len(samples), sr):
                    decoder.process(polarity*samples[offset:offset+sr])
                accepted = []
                for frame in decoded:
                    if not (frame.ok_sbs_tm and frame.years == 0 and 1 <= frame.days <= 366
                            and 0 <= frame.hours < 24 and 0 <= frame.minutes < 60
                            and 0 <= frame.seconds < 60):
                        continue
                    sample = float(frame.absolute_sample.i)+float(frame.absolute_sample.f)
                    accepted.append(dict(day_of_year=int(frame.days), hours=int(frame.hours),
                        minutes=int(frame.minutes), seconds=int(frame.seconds),
                        original_sample=sample, original_seconds=sample/sr))
                longest = current = 0
                previous = None
                for frame in accepted:
                    clock = (frame['day_of_year']*86400+frame['hours']*3600
                             +frame['minutes']*60+frame['seconds'])
                    current = (current+1 if previous is not None and clock-previous[0] == 1
                               and .8 < frame['original_seconds']-previous[1] < 1.25 else 1)
                    longest = max(longest, current)
                    previous = (clock, frame['original_seconds'])
                row['attempts'].append(dict(filter=name, polarity=polarity,
                    decoded_frames=len(decoded), valid_frames=len(accepted),
                    longest_consecutive_valid_frames=longest, frames=accepted))
        result['excerpts'].append(row)
        print(f"{start:g} s: peak {freq[peak]:g} Hz; "
              +', '.join(f"{a['filter']}/{a['polarity']}: {a['valid_frames']} valid"
                         for a in row['attempts']), flush=True)
    # The legacy JWaveSource exposes its underlying file, not a close method.
    wav._w._w.close()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('Source changed during probe')
    result['source_stat_unchanged'] = True
    result['implementation_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result['imported_legacy_code'] = {}
    for name, module in sorted(sys.modules.items()):
        if name == 'irig_b' or name == 'apollo' or name.startswith('jdsp'):
            path = getattr(module, '__file__', None)
            if path and Path(path).is_file():
                result['imported_legacy_code'][name] = dict(path=str(path),
                    sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest())
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(output)


if __name__ == '__main__':
    main()
