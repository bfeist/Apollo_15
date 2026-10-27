"""Continuous reference-span maps and frozen original-coordinate final checks."""
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage
from scipy.integrate import cumulative_trapezoid

from flutter_plan_store import checked_artifact
from flutter_tracking import evaluate_events, RATE
from flutter_policy import LIMITS
from continuous_quality import IMPROVEMENT_LIMITS


def detail(folder, summary):
    _, raw = checked_artifact(Path(folder), summary['detail_file'], summary['detail_sha256'])
    return json.loads(raw)


def rising(values):
    return .5-.5*np.cos(np.pi*np.clip(values, 0., 1.))


def saved_continuous_map(folder, summary, channel, start, frames, sr):
    record = detail(folder, summary)
    target = next(t for t in record['targets'] if t['channel'] == channel)
    last = (frames-1)/sr
    tt = np.arange(math.floor(last*RATE)+1, dtype=float)/RATE
    if last > tt[-1]+1e-12:
        tt = np.r_[tt, last]
    times = start+tt
    speed, envelope = np.zeros(len(tt)), np.zeros(len(tt))
    markers, evidence, switches = [], [], 0
    previous = None
    for span in target['reference_spans']:
        a, b = max(start, span['start_seconds']), min(start+frames/sr, span['end_seconds'])
        if b <= a:
            continue
        selected = span.get('selected')
        lo, hi = np.searchsorted(times, [a, b])
        if hi <= lo:
            continue
        marker = dict(start_seconds=a-start, end_seconds=b-start, region='continuous',
                      selected=selected, reason=span['reason'],
                      quality_level=span.get('quality_level'),
                      kind=('group_numeric_pass' if span.get('evidence_kind') == 'transfer_group' else 'numeric_pass') if selected else 'bypassed')
        if selected:
            stream = record['maps']['streams'][selected]
            path = (Path(folder)/stream['file']).resolve()
            if Path(folder).resolve() not in path.parents or path.stat().st_size != stream['frames']*16:
                raise ValueError('Continuous map stream changed')
            data = np.memmap(path, dtype='<f8', mode='r', shape=(stream['frames'], 2))
            positions = (times[lo:hi]-stream['start_seconds'])*RATE
            left = max(0, int(np.floor(positions.min()))-1)
            right = min(stream['frames'], int(np.ceil(positions.max()))+2)
            if right <= left:
                raise ValueError('Reference span has no stream coverage')
            local = np.asarray(data[left:right])
            if not np.isfinite(local).all() or np.max(abs(local[:, 0])) > .05 or np.any((local[:, 1] < 0) | (local[:, 1] > 1)):
                raise ValueError('Invalid continuous speed/support samples')
            grid = np.arange(left, right)
            e = np.interp(positions, grid, local[:, 0], left=0., right=0.)
            gate = np.interp(positions, grid, local[:, 1], left=0., right=0.)
            # Taper only physical support/lifetime edges, never monitor or I/O
            # boundaries. The padding is the actual span edge.
            distance = ndimage.distance_transform_edt(np.r_[False, gate >= .95, False])[1:-1]
            fade = rising(distance/(.5*RATE))
            gate *= fade
            speed[lo:hi], envelope[lo:hi] = e*gate, gate
            marker.update(reference_channel=span.get('reference_channel'), method=span.get('method'))
            if previous is not None and previous != selected:
                switches += 1
            previous = selected
            evidence.append(dict(selected=selected, stream_sha256=stream['sha256'],
                                 start_seconds=a, end_seconds=b, applied=True))
            for edge, low, high in (('acquisition', a, min(b, a+.5)),
                                    ('end_of_support', max(a, b-.5), b)):
                markers.append(dict(start_seconds=low-start, end_seconds=high-start,
                    kind='fade', region='continuous', selected=selected,
                    reason='Physical reference lifetime '+edge))
        elif 'support' in span['reason'].lower() or 'carrier' in span['reason'].lower():
            markers.append(dict(start_seconds=a-start, end_seconds=b-start, kind='support_gap',
                                region='continuous', selected=None, reason=span['reason']))
        markers.append(marker)
    # One integration over the entire requested channel. This intentionally
    # preserves a constant displacement through bypasses instead of resetting it.
    edge = rising(np.minimum(tt, last-tt)/.5)
    speed *= edge
    envelope *= edge
    area = float(np.trapezoid(envelope, tt))
    dc = float(np.trapezoid(speed, tt)/area) if area else 0.
    speed -= dc*envelope
    if np.max(abs(speed)) > .05:
        raise ValueError('Continuous duration anchoring exceeds speed bound')
    delay = cumulative_trapezoid(speed, tt, initial=0.)
    delay[-1] = 0.
    nominal = tt+delay
    if not np.isfinite(nominal).all() or np.any(np.diff(nominal) <= 0):
        raise ValueError('Continuous timing map is not finite/monotonic')
    stats = dict(rate=RATE, frames=frames, sample_rate=sr, last_sample_seconds=last,
                 dc_speed_removed=dc, maximum_displacement_seconds=float(np.max(abs(delay))),
                 maximum_speed_correction=float(np.max(abs(speed))),
                 speed_limit_saturation_fraction=float(np.mean(abs(speed) >= .049)),
                 reference_switches=switches, fade_count=sum(m['kind'] == 'fade' for m in markers),
                 blend_count=0, nonzero_speed_fraction=float(np.mean(abs(speed)>1e-12)),
                 integration='One integration of persistent reference streams; fades only at lifetime/support edges',
                 bypass='Zero local speed correction; constant displacement retained for continuity')
    return tt, nominal, speed, stats, markers, evidence


def continuous_holdout_checks(folder, summary, row, channel, start, tt, nominal, speed):
    record = detail(folder, summary)
    target = next(t for t in record['targets'] if t['channel'] == channel)
    observations, selected = [], []
    sr = row['sample_rate']
    displacement = float(np.max(abs(nominal-tt)))
    for event in record.get('final_holdouts', {}).get(str(channel), []):
        a, b = event['start_seconds'], event['end_seconds']
        if a < start+.5 or b > start+tt[-1]-.5:
            continue
        span = next((s for s in target['reference_spans'] if s.get('selected') and
                     s['start_seconds'] <= a and b <= s['end_seconds']), None)
        if span is None:
            continue
        if selected and a-selected[-1] < LIMITS['independent_window_seconds']:
            continue
        selected.append(a)
        low = max(start, a-.5-displacement)
        high = min(start+tt[-1], b+.5+displacement)
        offset = round((low-row['start_seconds'])*sr)
        count = round((high-low)*sr)
        low = row['start_seconds']+offset/sr
        with sf.SoundFile(row['path']) as handle:
            handle.seek(offset)
            x = handle.read(count, dtype='float64')
        output_times = low-start+np.arange(len(x))/sr
        input_times = np.interp(output_times, nominal, tt)
        pos = (input_times-(low-start))*sr
        metrics = evaluate_events(x, sr, None, [dict(event, start=a-low, end=b-low)], sample_positions=pos)
        margins = metrics[0]['margins'] if metrics else []
        restored = len(margins) == 3 and all(
            m['after_rms_hz'] <= LIMITS['max_residual_hz'] and m['after_rms_cents'] <= LIMITS['max_residual_cents']
            and m['after_rms_hz'] <= max(m['before_rms_hz']*1.05, m['before_rms_hz']+.1) for m in margins)
        if span.get('quality_level') == 'supported_improvement':
            passed = len(margins) == 3 and all(m['residual_ratio'] <= IMPROVEMENT_LIMITS['max_event_ratio'] for m in margins)
            passed = passed and np.median([m['residual_ratio'] for m in margins]) <= IMPROVEMENT_LIMITS['median_event_ratio']
        else:
            passed = restored
        observations.append(dict(original_start_seconds=a, status='passed' if passed else 'failed', metrics=metrics,
                                 quality_level=span.get('quality_level'), restoration_target_met=restored))
    no_motion = not bool(np.any(abs(speed)>1e-12))
    status = 'passed' if len(observations) >= 2 or no_motion else 'insufficient_reserved_events'
    if any(event['status'] == 'failed' for event in observations):
        status = 'failed'
    return dict(status=status, reserved_events=observations, no_motion=no_motion,
                restoration_target_met=not no_motion and status == 'passed' and
                    all(s.get('quality_level', 'restoration_target_met') == 'restoration_target_met'
                        for s in target['reference_spans'] if s.get('selected')),
                map='Exact continuous reference-span inverse map after support fades and duration anchoring',
                failed_choice_refitted=False, listening_approved=False)
