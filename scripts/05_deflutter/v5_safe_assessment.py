"""Complete assessment while explicitly rejecting unsafe duration-anchored maps.

The legacy assessment and map engine remain frozen for checkpoint compatibility.
Valid maps receive the same reserved-event assessment. A speed-bound failure is
reported on its channel, with no replacement or clipping of the failed map.
"""
import hashlib
from pathlib import Path

import numpy as np

from continuous_maps import saved_continuous_map, continuous_holdout_checks, detail
from flutter_plan_store import load_saved_plan
from flutter_policy import digest
from flutter_policy import LIMITS
from flutter_planning import write_json


def assessment_implementation_digest():
    from v5_compatibility import ASSESSMENT_DIGESTS

    names = ('v5_safe_assessment.py', 'continuous_maps.py', 'flutter_policy.py')
    content = b''.join(Path(__file__).with_name(name).read_bytes() for name in names)
    normalized = hashlib.sha256(content.replace(b'\r\n', b'\n')).hexdigest()
    return ASSESSMENT_DIGESTS.get(normalized) or hashlib.sha256(content).hexdigest()


def rejected_map(row, target, start, end, error):
    spans, gaps, cursor = [], [], start
    for span in target['reference_spans']:
        low, high = max(start, span['start_seconds']), min(end, span['end_seconds'])
        if high <= low:
            continue
        if low > cursor+1e-7:
            gaps.append(dict(start_seconds=cursor, end_seconds=low))
        if low < cursor-1e-7:
            raise ValueError('Overlapping v5 correction spans')
        cursor = high
        selected = bool(span.get('selected'))
        spans.append(dict(start_seconds=low, end_seconds=high, seconds=high-low,
            proposed_correction=selected,
            outcome='rejected_invalid_map' if selected else 'insufficient_evidence',
            planning_quality=span.get('quality_level'), reason=span['reason'],
            reserved_event_count=0, event_metrics=[], remaining_worst_rms_hz=None,
            worst_ratio=None))
    if cursor < end-1e-7:
        gaps.append(dict(start_seconds=cursor, end_seconds=end))
    return dict(channel=row['channel'], available_seconds=end-start,
        proposed_correction_seconds=sum(span['seconds'] for span in spans if span['proposed_correction']),
        spans_with_reserved_improvement_seconds=0., status='invalid_map', identity_only=None,
        recommended_action='leave_uncorrected', map_construction_error=str(error),
        spans=spans, unaccounted_span_gaps=gaps,
        map_integrity=dict(finite=None, monotonic=None, duration_anchored=None),
        joined_checks=dict(status='invalid_map', reserved_events=[], no_motion=None,
            failed_choice_refitted=False, listening_approved=False),
        map_statistics={}, transitions=[], median_before_rms_hz=None, median_after_rms_hz=None,
        worst_after_rms_hz=None, worst_ratio=None, worsening_margins=0)


def assess_plan(folder, destination=None):
    folder = Path(folder).resolve()
    plan = load_saved_plan(folder, expand_regions=False)
    summary = plan['regions'][0]
    record = detail(folder, summary)
    rows = {r['channel']: r for r in plan['inventory']['channels']}
    channels = []
    for target in record['targets']:
        row = rows[target['channel']]
        start = max(summary['start_seconds'], row['start_seconds'])
        end = min(summary['end_seconds'], row['end_seconds'])
        frames = round((end-start)*row['sample_rate'])
        if frames < 2:
            channels.append(dict(channel=row['channel'], status='unavailable', available_seconds=0.))
            continue
        try:
            tt, nominal, speed, stats, markers, evidence = saved_continuous_map(
                folder, summary, row['channel'], start, frames, row['sample_rate'])
        except ValueError as error:
            if str(error) != 'Continuous duration anchoring exceeds speed bound':
                raise
            channels.append(rejected_map(row, target, start, end, error))
            continue
        checks = continuous_holdout_checks(folder, summary, row, row['channel'], start, tt, nominal, speed)
        observations = checks['reserved_events']
        final_events = record.get('final_holdouts', {}).get(str(row['channel']), [])
        spans = []
        for span in target['reference_spans']:
            a, b = max(start, span['start_seconds']), min(end, span['end_seconds'])
            if b <= a:
                continue
            events = []
            for observation in sorted(observations, key=lambda o: o['original_start_seconds']):
                stamp = observation['original_start_seconds']
                event = next((e for e in final_events if abs(e['start_seconds']-stamp)<1e-7), None)
                if (event is not None and a <= stamp and event['end_seconds'] <= b
                        and (not events or stamp-events[-1]['original_start_seconds'] >= LIMITS['independent_window_seconds'])):
                    events.append(observation)
            margins = [m for e in events for event in e['metrics'] for m in event['margins']]
            valid = bool(events) and all(len(e['metrics']) == 1 and len(e['metrics'][0]['margins']) == 3
                and all(np.isfinite([m['before_rms_hz'], m['after_rms_hz'], m['residual_ratio']]).all()
                    and m['before_rms_hz'] >= LIMITS['low_baseline_hz'] for m in e['metrics'][0]['margins']) for e in events)
            if not span.get('selected'):
                outcome = 'insufficient_evidence'
            elif not valid:
                outcome = 'insufficient_evidence'
            elif any(m['after_rms_hz'] > max(m['before_rms_hz']*1.05, m['before_rms_hz']+.1) for m in margins):
                outcome = 'worsening'
            elif len(events) < 2:
                outcome = 'insufficient_evidence'
            elif any(e['status'] != 'passed' for e in events):
                outcome = ('no_meaningful_benefit' if np.median([m['residual_ratio'] for m in margins]) >= .95
                    else 'insufficient_evidence')
            elif all(e['restoration_target_met'] for e in events):
                outcome = 'full_restoration_at_measured_events'
            else:
                outcome = 'partial_improvement_at_measured_events'
            spans.append(dict(start_seconds=a, end_seconds=b, seconds=b-a,
                proposed_correction=bool(span.get('selected')), outcome=outcome,
                planning_quality=span.get('quality_level'), reason=span['reason'],
                reserved_event_count=len(events), event_metrics=events,
                remaining_worst_rms_hz=max((m['after_rms_hz'] for m in margins), default=None),
                worst_ratio=max((m['residual_ratio'] for m in margins), default=None)))
        cursor = start
        gaps = []
        for span in sorted(spans, key=lambda s: s['start_seconds']):
            if span['start_seconds'] > cursor+1e-7:
                gaps.append(dict(start_seconds=cursor, end_seconds=span['start_seconds']))
            if span['start_seconds'] < cursor-1e-7:
                raise ValueError('Overlapping v5 correction spans')
            cursor = span['end_seconds']
        if cursor < end-1e-7:
            gaps.append(dict(start_seconds=cursor, end_seconds=end))
        measured = [m for e in observations for event in e['metrics'] for m in event['margins']]
        tested_seconds = sum(s['seconds'] for s in spans if s['outcome'] in {
            'partial_improvement_at_measured_events', 'full_restoration_at_measured_events'})
        channels.append(dict(channel=row['channel'], available_seconds=end-start,
            proposed_correction_seconds=sum(s['seconds'] for s in spans if s['proposed_correction']),
            spans_with_reserved_improvement_seconds=tested_seconds,
            status=('insufficient_evidence' if not measured else checks['status']),
            identity_only=checks['no_motion'], spans=spans,
            unaccounted_span_gaps=gaps,
            map_integrity=dict(finite=bool(np.isfinite(nominal).all()),
                monotonic=bool(np.all(np.diff(nominal)>0)), duration_anchored=bool(
                    abs(nominal[0]-tt[0])<1e-12 and abs(nominal[-1]-tt[-1])<1e-12)),
            joined_checks=checks, map_statistics=stats,
            transitions=[m for m in markers if m['kind'] in {'fade', 'blend', 'support_gap'}],
            median_before_rms_hz=float(np.median([m['before_rms_hz'] for m in measured])) if measured else None,
            median_after_rms_hz=float(np.median([m['after_rms_hz'] for m in measured])) if measured else None,
            worst_after_rms_hz=max((m['after_rms_hz'] for m in measured), default=None),
            worst_ratio=max((m['residual_ratio'] for m in measured), default=None),
            worsening_margins=sum(m['residual_ratio']>1.05 for m in measured)))
    result = dict(methodology=plan['methodology'], plan_sha256=digest(folder/'plan.json'),
        implementation_sha256=plan['implementation_sha256'], channels=channels,
        assessment_implementation_sha256=assessment_implementation_digest(),
        assessment_complete=True,
        assessment_status='completed_with_invalid_maps' if any(c['status']=='invalid_map' for c in channels) else 'completed',
        invalid_map_channels=[c['channel'] for c in channels if c['status']=='invalid_map'],
        proposed_correction_channel_seconds=sum(c.get('proposed_correction_seconds',0.) for c in channels),
        spans_with_reserved_improvement_channel_seconds=sum(c.get('spans_with_reserved_improvement_seconds',0.) for c in channels),
        interpretation='Time counts identify continuously supported proposed spans with at least two passing reserved events per span; they do not measure flutter at every instant. No-event identity is insufficient evidence, not no flutter. Different margins and duplicated feeds are not independent observations.',
        uncertainty='Event-level measurements only; oscillator provenance and speech accuracy are unresolved. Transitions have structural checks, not necessarily independent audio measurements.',
        maps_refitted_after_final=False, listening_performed=False, production_accepted=False)
    if destination is not None:
        write_json(Path(destination), result)
    return result
