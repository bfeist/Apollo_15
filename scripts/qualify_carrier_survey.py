"""Bounded known-positive injection and negative-control carrier qualification.

Reports measurements; never turns development tests into rollout approval.
"""
import argparse
import hashlib
from pathlib import Path
import time

import numpy as np
from scipy import signal
import soundfile as sf

from carrier_survey import SETTINGS, screen, source_record, write_json
from flutter_policy import digest
from irig_clock import decode_excerpt, legacy_decoder, legacy_hashes


def qualify(path, start, duration, output, legacy_repo):
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new qualification report path')
    if not np.isfinite([start, duration]).all() or start < 0 or not 10 <= duration <= 60:
        raise ValueError('Use a finite nonnegative start and 10-60 second known-positive excerpt')
    source = source_record(path)
    sr = source['sample_rate']
    with sf.SoundFile(path) as stream:
        stream.seek(round(start*sr))
        x = stream.read(round(duration*sr), dtype='float64')
    if len(x) != round(duration*sr):
        raise ValueError('Known-positive source is too short')
    started, cpu = time.perf_counter(), time.process_time()
    decoder = legacy_decoder(legacy_repo)
    attempts = decode_excerpt(x, sr, round(start*sr), decoder)
    strongest = max(a['longest_run'] for a in attempts)
    # Normalize only this controlled fixture, retaining its original hash.
    filtered = signal.sosfiltfilt(signal.butter(4, 400, fs=sr, btype='highpass', output='sos'), x)
    filtered /= max(float(np.sqrt(np.mean(filtered**2))), 1e-12)
    rng = np.random.default_rng(510083)
    injections = []
    for speed in (.85, 1., 1.15):
        shifted = np.interp(np.arange(round(4*sr))*speed, np.arange(len(filtered)), filtered)
        for amplitude in (.001, .01, .1):
            for noise in (.001, .01):
                background = rng.normal(0, noise, 8*sr)
                # Exactly a two-second island, including an off-grid onset.
                a = round(3.37*sr)
                background[a:a+2*sr] += amplitude*shifted[:2*sr]
                windows = [screen(background[i*sr:(i+2)*sr], sr) for i in range(7)]
                found = any(w['status'] == 'carrier_candidate' for w in windows[2:6])
                injections.append(dict(speed_factor=speed, signal_rms=amplitude, noise_rms=noise,
                                       island_seconds=2., detected=found, windows=windows))
    t = np.arange(10*sr)/sr
    controls = dict(pure_tone=.1*np.sin(2*np.pi*1000*t),
                    amplitude_only=.1*(1+.7*np.sin(2*np.pi*7*t))*np.sin(2*np.pi*1000*t),
                    hum=.1*np.sin(2*np.pi*60*t),
                    noise=rng.normal(0, .01, len(t)),
                    speech_like=.05*np.sin(2*np.pi*(180*t+10*np.sin(t)))*(.5+.5*np.sin(2*np.pi*3*t)))
    negatives = []
    for name, samples in controls.items():
        decoded = decode_excerpt(samples, sr, 0, decoder)
        negatives.append(dict(name=name, carrier=screen(samples[:2*sr], sr),
                              longest_decoded_run=max(a['longest_run'] for a in decoded),
                              clock_identity_expected=False))
    if source_record(path) != source:
        raise ValueError('Known-positive original changed')
    result = dict(status='measured_requires_review', source=source, start_seconds=start, duration_seconds=duration,
        excerpt_float64_sha256=hashlib.sha256(x.tobytes()).hexdigest(), settings=SETTINGS,
        implementation_sha256=digest(Path(__file__).with_name('carrier_survey.py')),
        legacy_code=legacy_hashes(), positive_longest_decoded_run=strongest, positive_attempts=attempts,
        injections=injections, negative_controls=negatives, passed_basic_controls=strongest >= 4 and all(n['longest_decoded_run'] == 0 for n in negatives),
        remaining='Review sensitivity by signal/noise level; add actual speech backgrounds and fresh Apollo 15 holdouts before acceptance',
        performance=dict(wall_seconds=time.perf_counter()-started, cpu_seconds=time.process_time()-cpu,
                         source_decoded_read_bytes=x.nbytes))
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)
    print(f'Positive sequential frames: {strongest}; injections detected: {sum(i["detected"] for i in injections)}/{len(injections)}; basic controls: {result["passed_basic_controls"]}')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--positive', type=Path, required=True)
    p.add_argument('--start', type=float, default=600.)
    p.add_argument('--duration', type=float, default=30.)
    p.add_argument('--legacy-repo', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    try:
        qualify(args.positive, args.start, args.duration, args.output, args.legacy_repo)
    except (ValueError, OSError, ImportError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    main()
