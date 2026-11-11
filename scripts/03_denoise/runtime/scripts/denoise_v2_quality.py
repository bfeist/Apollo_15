"""Bounded residual measurements and numerical checks for the v2 rollout."""
import json
import math

import numpy as np
import soundfile as sf
from scipy import fft

from denoise_archive import sha256

REVIEW_TOLERANCE = 2**-22  # Two PCM24 quantization steps at the historical gain.
METRIC_SECONDS = 4
MAX_REEVALUATIONS = 1


def line_centers(left, right, rate):
    frequencies = set(range(60, 661, 60))
    for anchor in (left, right):
        for model in anchor['components']+anchor.get('refinement', []):
            if 'hz' in model and 35 <= model['hz'] < rate/2-15:
                frequencies.add(round(model['hz'], 1))
    return sorted(frequencies)


def residual_windows(raw, corrected, rate, left, right, thresholds, frame=0):
    count = min(len(raw), len(corrected))
    # At least 1 second provides a meaningful local line/floor separation.
    if count < rate:
        return []
    n = METRIC_SECONDS*rate
    spans = [(i, min(i+n, count)) for i in range(0, count, n) if count-i >= rate]
    centers = line_centers(left, right, rate)
    activity, results = [], []
    for a, b in spans:
        width = rate/(b-a)
        taper = np.hanning(b-a)
        spectra = np.abs(fft.rfft(np.stack((raw[a:b], corrected[a:b]))*taper, axis=1))**2
        original, output = spectra
        activity.append(float(np.sum(original[round(350/width):round(2500/width)])))
        lines = []
        for hz in centers:
            i = round(hz/width)
            band, near, far = (round(x/width) for x in (1.25, 5, 12))
            original_line, output_line = spectra[:, i-band:i+band+1].sum(axis=1)
            lo, hi = spectra[:, i-far:i-near], spectra[:, i+near+1:i+far+1]
            floor_before, floor_after = np.median(np.concatenate((lo, hi), axis=1), axis=1)*(2*band+1)
            before = db_ratio(original_line, floor_before)
            after = db_ratio(output_line, floor_after)
            change = db_ratio(output_line, original_line)
            lines.append([hz, round(before, 2), round(after, 2), round(change, 2)])
        outages = []
        for node in (left, right):
            if node.get('upper_bypass'):
                outages.append(b-a)
                continue
            origin = node['context_frames'][0]
            outages.append(sum(max(0, min(frame+b, origin+hi)-max(frame+a, origin+lo))
                               for lo, hi in node.get('reference_off_intervals_frames', ())))
        results.append(dict(relative_frames=[a,b], lines=lines, flags=[], upper_reference_off_frames=outages))
    quiet = np.percentile(activity, 25)
    for level, window in zip(activity, results):
        group = 'quiet' if level <= quiet else 'active'
        window['activity'] = group
        if thresholds is None:
            continue
        limits = thresholds[group]
        for hz, before, after, change in window['lines']:
            if after > limits['minimum_residual_floor_db'] and change > limits['maximum_reinforcement_db']:
                window['flags'].append([hz, 'reinforcement'])
            elif before >= limits['minimum_established_floor_db'] and after > limits['maximum_residual_floor_db'] and change > limits['minimum_attenuation_db']:
                window['flags'].append([hz, 'persistent_residual'])
    return results


def db_ratio(top, bottom):
    return 10*math.log10(max(float(top), 1e-30)/max(float(bottom), 1e-30))


def freeze_thresholds(log, review_start_frame, review_end_frame):
    """Use only the auditioned interval to set initial review flags."""
    evidence = {'quiet': [], 'active': []}
    with log.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['kind'] != 'interval':
                continue
            for window in row['windows']:
                lo, hi = (row['source_frames'][0]+i for i in window['relative_frames'])
                if lo >= review_start_frame and hi <= review_end_frame:
                    evidence[window['activity']].extend(window['lines'])
    result = {}
    for group, lines in evidence.items():
        supported = [line for line in lines if line[1] >= 15]
        if not supported:
            raise ValueError(f'Canary needs measured established lines during {group} audio')
        result[group] = dict(
            minimum_established_floor_db=15.,
            minimum_residual_floor_db=12.,
            maximum_reinforcement_db=max(3., round(float(np.percentile([r[3] for r in supported], 99))+1, 2)),
            maximum_residual_floor_db=max(18., round(float(np.percentile([r[2] for r in supported], 95))+3, 2)),
            minimum_attenuation_db=-6.)
    return result


def compare_review(path, manifest, reference, sample_rate, gain=1.):
    if sha256(reference) != manifest['files'][reference.name]['sha256']:
        raise ValueError('Saved listening reference changed')
    start, end = manifest['source_frames']
    maximum = 0.
    with sf.SoundFile(path) as full, sf.SoundFile(reference) as reviewed:
        if (reviewed.frames, reviewed.samplerate, reviewed.channels) != (end-start, sample_rate, 1):
            raise ValueError('Saved listening reference header mismatch')
        full.seek(start)
        for expected in reviewed.blocks(blocksize=65536, dtype='float64'):
            actual = full.read(len(expected), dtype='float64')*(manifest['common_gain']/gain)
            maximum = max(maximum, float(np.max(abs(actual-expected))))
    if maximum > REVIEW_TOLERANCE:
        raise ValueError(f'Whole-channel review crop differs by {maximum:g} (limit {REVIEW_TOLERANCE:g})')
    return dict(source_frames=[start,end], maximum_error=maximum, tolerance=REVIEW_TOLERANCE,
                reference_sha256=manifest['files'][reference.name]['sha256'])


def review_windows(log, total, rate, review_start):
    """Select whole-channel ends and the reported spots without averaging the lines away."""
    spans = dict(beginning=(0, 16*rate), end=(max(0, total-20*rate), total),
                 original_problem=(review_start+564*rate, review_start+576*rate),
                 late_hum=(review_start+1530*rate, review_start+1580*rate))
    found = {label:[] for label in spans}
    representative = {}
    with log.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['kind'] != 'interval':
                continue
            for window in row['windows']:
                lo, hi = (row['source_frames'][0]+i for i in window['relative_frames'])
                sample = dict(window, source_frames=[lo,hi])
                for label, (a,b) in spans.items():
                    if lo < b and hi > a:
                        found[label].append(sample)
                key = window['activity']
                strength = max((values[1] for values in window['lines']), default=0)
                if key not in representative or strength > representative[key][0]:
                    representative[key] = (strength, sample)
    return dict(windows=found, strongest_established={k:v[1] for k,v in representative.items()},
                line_columns=['frequency_hz', 'original_above_floor_db', 'output_above_floor_db', 'output_change_db'])
