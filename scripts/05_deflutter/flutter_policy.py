"""Historical numerical thresholds and fingerprints used by saved tone evidence.

Legacy per-channel approval and rollout machinery has been retired.
"""
import hashlib
import json
from pathlib import Path

import numpy as np

METHODOLOGY = 'continuous-carrier-v5'
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
    from v5_compatibility import PLANNING_DIGESTS

    names = ('flutter_policy.py', 'flutter_planning.py', 'flutter_tracking.py',
             'flutter.py', 'carrier_survey.py', 'channel_review.py', 'irig_clock.py', 'flutter_groups.py',
             'continuous_tracking.py', 'continuous_planning.py', 'continuous_maps.py',
             'continuous_acquisition.py', 'continuous_quality.py', 'carrier_capture.py',
             'v5_allchannel.py', 'v5_assessment.py')
    content = b''.join(Path(__file__).with_name(n).read_bytes() for n in names)
    normalized = hashlib.sha256(content.replace(b'\r\n', b'\n')).hexdigest()
    return PLANNING_DIGESTS.get(normalized) or hashlib.sha256(content).hexdigest()


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
