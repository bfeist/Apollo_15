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

if __package__:
    from .crosstalk_offsets import denoised_sources, peak, profile
    from .crosstalk_probe import CONTEXT, RATE, read_window
else:
    from crosstalk_offsets import denoised_sources, peak, profile
    from crosstalk_probe import CONTEXT, RATE, read_window


# Share the reviewed numerical implementation with official stage 04.
from crosstalk_model import design, fit, trajectory, preliminary_path, subtract, envelope


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
