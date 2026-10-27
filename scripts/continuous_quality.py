"""Explain existing numeric deflutter gates without weakening their decisions.

An improving candidate above the restoration goal is still unqualified. This
module records that distinction so a bypass is not mistaken for absent signal.
"""
import math

import numpy as np

from flutter_policy import LIMITS

IMPROVEMENT_LIMITS = dict(median_event_ratio=.5, max_event_ratio=.8)


def _rows(value):
    return list(value.items()) if isinstance(value, dict) else [(str(i), row) for i, row in enumerate(value)]


def numeric_diagnostics(main, controls, time_controls=(), limits=None, *, supported=None,
                        independent_windows=None):
    """Return predicate measurements; optional support/window flags stay explicit.

    ``numeric_pass`` matches flutter_policy.numeric_pass for ordinary measurement
    records. ``passed`` additionally includes externally supplied support and
    independent-window checks. Neither classification nor a partial improvement
    supplies listening or production acceptance.
    """
    limits = LIMITS if limits is None else limits
    controls, time_controls = _rows(controls), _rows(time_controls)
    expected = len(main)
    layouts = [main]+[rows for _, rows in controls+time_controls]
    complete = bool(main and controls and all(
        len(rows) == expected and all(len(event.get('margins', [])) == 3 for event in rows)
        for rows in layouts))
    margins = [margin for event in main for margin in event.get('margins', [])]
    fields = ('residual_ratio', 'before_rms_hz', 'after_rms_hz', 'after_rms_cents')
    complete = complete and all(all(key in margin and math.isfinite(margin[key]) for key in fields)
                                for margin in margins)
    ratio = float(np.median([m['residual_ratio'] for m in margins])) if complete else None
    def median_control(rows):
        values = [m.get('residual_ratio') for event in rows for m in event.get('margins', [])]
        return float(np.median(values)) if values and all(v is not None and math.isfinite(v) for v in values) else None
    regular = {str(key): median_control(rows) for key, rows in controls}
    shifted = {str(key): median_control(rows) for key, rows in time_controls}
    wrong_time_losses = sum(value is not None and ratio is not None and
                            ratio+limits['control_margin'] < value for value in shifted.values())
    measurements = dict(
        events=expected, margins=len(margins), median_event_ratio=ratio,
        maximum_event_ratio=max((m['residual_ratio'] for m in margins), default=None) if complete else None,
        minimum_baseline_hz=min((m['before_rms_hz'] for m in margins), default=None) if complete else None,
        maximum_residual_hz=max((m['after_rms_hz'] for m in margins), default=None) if complete else None,
        maximum_residual_cents=max((m['after_rms_cents'] for m in margins), default=None) if complete else None,
        control_median_ratios=regular, wrong_time_median_ratios=shifted,
        wrong_time_losses=wrong_time_losses)
    checks = dict(
        complete_measurements=complete,
        meaningful_baseline=bool(complete and measurements['minimum_baseline_hz'] >= limits['low_baseline_hz']),
        worst_event_ratio=bool(complete and measurements['maximum_event_ratio'] <= limits['max_event_ratio']),
        median_event_ratio=bool(complete and ratio <= limits['median_event_ratio']),
        absolute_residual_hz=bool(complete and measurements['maximum_residual_hz'] <= limits['max_residual_hz']),
        absolute_residual_cents=bool(complete and measurements['maximum_residual_cents'] <= limits['max_residual_cents']),
        polarity_no_motion_am_controls=bool(complete and all(
            value is not None and ratio+limits['control_margin'] < value for value in regular.values())),
        wrong_time_controls=bool(complete and wrong_time_losses >= limits['minimum_wrong_time_losses']))
    numeric_passed = all(checks.values())
    if supported is not None:
        checks['reference_support'] = bool(supported)
    if independent_windows is not None:
        checks['independent_windows'] = bool(independent_windows)
    passed = all(checks.values())
    non_absolute = [value for key, value in checks.items()
                    if key not in ('absolute_residual_hz', 'absolute_residual_cents')]
    if passed:
        classification = 'qualified_low_residual'
    elif all(non_absolute):
        classification = 'improving_above_absolute_goal'
    elif not complete or independent_windows is False:
        classification = 'insufficient_evidence'
    elif supported is False:
        classification = 'unsupported_reference'
    elif not checks['meaningful_baseline']:
        classification = 'low_baseline_no_op'
    elif not checks['polarity_no_motion_am_controls'] or not checks['wrong_time_controls']:
        classification = 'failed_negative_controls'
    else:
        classification = 'insufficient_improvement'
    return dict(numeric_pass=numeric_passed, passed=passed, classification=classification,
                checks=checks, failed_predicates=[key for key, value in checks.items() if not value],
                measurements=measurements, listening_approved=False, production_accepted=False)


def classify_numeric_quality(main, controls, time_controls=(), limits=None, *, supported,
                             independent_windows, improvement_limits=None):
    """Classify strong supported reduction separately from the restoration goal.

    This is planning eligibility, never speech/listening or production approval.
    Every margin must improve by at least 20%, the median by at least 50%, and
    existing baseline, complete-evidence, support and negative-control checks
    must pass. The original absolute Hz/cents goals determine the quality label.
    ``flutter_policy.numeric_pass`` itself remains unchanged.
    """
    improvement_limits = IMPROVEMENT_LIMITS if improvement_limits is None else improvement_limits
    diagnostic = numeric_diagnostics(main, controls, time_controls, limits,
                                     supported=bool(supported), independent_windows=bool(independent_windows))
    measured = diagnostic['measurements']
    goals = {key: diagnostic['checks'][key] for key in ('absolute_residual_hz', 'absolute_residual_cents')}
    checks = {key: value for key, value in diagnostic['checks'].items() if key not in goals}
    # Replace only the two relative-improvement criteria with the stricter tier
    # requirements. Support and all controls retain the established contract.
    checks['worst_event_ratio'] = bool(measured['maximum_event_ratio'] is not None and
        measured['maximum_event_ratio'] <= improvement_limits['max_event_ratio'])
    checks['median_event_ratio'] = bool(measured['median_event_ratio'] is not None and
        measured['median_event_ratio'] <= improvement_limits['median_event_ratio'])
    eligible = all(checks.values())
    restored = eligible and all(goals.values())
    quality = 'restoration_target_met' if restored else 'supported_improvement' if eligible else 'unqualified'
    return dict(passed=eligible, eligible_for_planning=eligible, quality_level=quality,
                restoration_target_met=restored, restoration_goal_checks=goals,
                legacy_numeric_pass=diagnostic['numeric_pass'],
                checks=checks, failed_predicates=[key for key, value in checks.items() if not value],
                unmet_restoration_goals=[key for key, value in goals.items() if not value],
                measurements=measured, improvement_limits=dict(improvement_limits),
                listening_approved=False, production_accepted=False)
