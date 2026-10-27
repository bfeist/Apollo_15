"""Versioned, explicit deflutter evidence and production acceptance contracts.

These initial absolute thresholds are development limits, not qualified claims.
A signed-off rollout record must bind the exact implementation/policy and
evidence before production rendering. Planning produces evidence for review.
"""
import hashlib
import json
from pathlib import Path

import numpy as np

METHODOLOGY = 'continuous-carrier-v4'
LIMITS = dict(max_residual_hz=6., max_residual_cents=5., max_event_ratio=.95,
              median_event_ratio=.8, control_margin=.10, low_baseline_hz=1.,
              minimum_events_per_split=2, independent_window_seconds=3.,
              minimum_support=.95, wrong_time_seconds=[-.137, .073, .311],
              minimum_wrong_time_losses=2)
DEFAULT_SURVEY_ROOT = Path('D:/Apollo_15_30-Track/carrier_survey_v2')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def implementation_digest():
    names = ('flutter_policy.py', 'flutter_planning.py', 'flutter_tracking.py',
             'flutter.py', 'carrier_survey.py', 'channel_review.py', 'irig_clock.py', 'flutter_groups.py',
             'continuous_tracking.py', 'continuous_planning.py', 'continuous_maps.py',
             'continuous_acquisition.py', 'continuous_quality.py', 'carrier_capture.py')
    return hashlib.sha256(b''.join(Path(__file__).with_name(n).read_bytes() for n in names)).hexdigest()


def policy_digest():
    from flutter_groups import SETTINGS
    from continuous_planning import SETTINGS as CONTINUOUS_SETTINGS
    from continuous_quality import IMPROVEMENT_LIMITS
    from continuous_acquisition import DEFAULT_HORIZONS, ROLE_CYCLE
    return hashlib.sha256(json.dumps(dict(direct=LIMITS, group=SETTINGS, continuous=CONTINUOUS_SETTINGS,
        improvement=IMPROVEMENT_LIMITS, acquisition_horizons=DEFAULT_HORIZONS, evidence_roles=ROLE_CYCLE), sort_keys=True).encode()).hexdigest()


def shifted_curve(curve, seconds, rate=200):
    """Wrong-time controls have missing support at edges, never wrapped samples."""
    t = np.arange(len(curve))/rate
    return np.interp(t-seconds, t, curve, left=0., right=0.)


def evidence_strength(calibration, holdout):
    def windows(events):
        # Greedy separation is invariant to arbitrary bin origins.
        selected = []
        for e in sorted(events, key=lambda e: e['start']):
            if not selected or e['start']-selected[-1] >= LIMITS['independent_window_seconds']:
                selected.append(e['start'])
        return len(selected)
    a, b = windows(calibration), windows(holdout)
    return dict(calibration_windows=a, holdout_windows=b,
                level='multiple_time_windows' if min(a, b) >= LIMITS['minimum_events_per_split'] else 'sparse',
                oscillator_feed_group='unknown', independent_oscillators=False,
                margins_are_independent=False, listening_approved=False,
                transfer_group_supported=False, production_accepted=False)


def numeric_pass(main, controls, time_controls=()):
    margins = [m for e in main for m in e['margins']]
    if not margins or not controls or any(len(e['margins']) != 3 for e in main):
        return False
    if any(len(c) != len(main) or any(len(e['margins']) != 3 for e in c) for c in list(controls)+list(time_controls)):
        return False
    ratios = [m['residual_ratio'] for m in margins]
    if min(m['before_rms_hz'] for m in margins) < LIMITS['low_baseline_hz']:
        return False  # No meaningful flutter: identity is the no-harm choice.
    return bool(max(ratios) <= LIMITS['max_event_ratio']
        and np.median(ratios) <= LIMITS['median_event_ratio']
        and max(m['after_rms_hz'] for m in margins) <= LIMITS['max_residual_hz']
        and max(m['after_rms_cents'] for m in margins) <= LIMITS['max_residual_cents']
        and all(np.median(ratios)+LIMITS['control_margin'] <
                np.median([m['residual_ratio'] for e in c for m in e['margins']]) for c in controls)
        and sum(np.median(ratios)+LIMITS['control_margin'] <
                np.median([m['residual_ratio'] for e in c for m in e['margins']]) for c in time_controls)
                >= LIMITS['minimum_wrong_time_losses'])


def checked_evidence(record, base):
    path = (Path(base)/record['path']).resolve()
    if digest(path) != record['sha256']:
        raise ValueError(f'Acceptance evidence changed: {path}')
    return json.loads(path.read_text(encoding='utf-8'))


def require_rollout(path):
    if path is None:
        raise ValueError('Production requires --rollout-approval after carrier qualification and fresh holdout/listening acceptance')
    path = Path(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('demo'):
        raise ValueError('Synthetic demo evidence cannot authorize production')
    if (value.get('methodology') != METHODOLOGY or value.get('implementation_sha256') != implementation_digest()
            or value.get('policy_sha256') != policy_digest() or value.get('status') != 'accepted'
            or not value.get('reviewer') or not value.get('reviewed_at')):
        raise ValueError('Rollout approval must accept the current implementation and frozen policy')
    for name in ('carrier_qualification', 'fresh_holdouts', 'speech_listening'):
        evidence = checked_evidence(value[name], path.parent)
        if evidence.get('status') != 'accepted':
            raise ValueError(f'{name} evidence is not accepted')
        if name == 'speech_listening' and evidence.get('sampling_certificate'):
            from review_sampling import require_methodology
            require_methodology(evidence['sampling_certificate'], path.parent)
    return value


def require_production(path, plan_path, channel, experimental=False):
    if experimental:
        raise ValueError('Experimental transfer is review-only; it cannot fill production coverage')
    if path is None:
        raise ValueError('Production rendering requires --production-approval bound to this plan and channel; use render_channel_review.py for pilots')
    path, plan_path = Path(path), Path(plan_path)
    if plan_path.is_dir():
        plan_path /= 'plan.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    require_rollout(path.parent/value['rollout_approval'])
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if plan.get('demo') or plan.get('review_only'):
        raise ValueError('Synthetic or pilot-only plans cannot authorize production')
    if plan.get('methodology') != METHODOLOGY or plan.get('implementation_sha256') != implementation_digest():
        raise ValueError('Production plan uses an incompatible methodology or implementation')
    if value.get('plan_sha256') != digest(plan_path) or value.get('status') != 'accepted':
        raise ValueError('Production acceptance does not match the saved plan')
    if value.get('acceptance_policy'):
        from review_sampling import POLICY, require_sampled
        if value['acceptance_policy'] != POLICY:
            raise ValueError('Unknown production acceptance policy')
        require_sampled(value, path, plan_path, channel)
        return value
    row = next((r for r in value['channels'] if r['channel'] == channel), None)
    if row is None or row.get('speech_preserved') is not True or row.get('transitions_accepted') is not True:
        raise ValueError('Channel speech and transition listening acceptance is missing')
    review = checked_evidence(row['continuous_review'], path.parent)
    original = next(r for r in plan['inventory']['channels'] if r['channel'] == channel)
    if (review.get('status') != 'complete' or review.get('experimental_transfer')
            or review.get('plan_sha256') != digest(plan_path) or review.get('channel') != channel
            or review.get('frames') != original['frames'] or review.get('start_seconds') != original['start_seconds']
            or review.get('joined_map_checks', {}).get('status') != 'passed'):
        raise ValueError('Acceptance requires a matching conservative continuous review with passed joined-map checks')
    for name, expected in review.get('timing_artifacts', {}).items():
        review_path = (path.parent/row['continuous_review']['path']).resolve()
        if digest(review_path.parent/name) != expected:
            raise ValueError('Accepted continuous map artifact changed')
    return value
