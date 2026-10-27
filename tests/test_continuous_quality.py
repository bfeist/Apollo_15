"""Useful partial reduction is diagnosed without promoting it past numeric gates."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuous_quality import numeric_diagnostics, classify_numeric_quality
from flutter_policy import numeric_pass


def events(before, after, cents=None):
    return [dict(margins=[dict(before_rms_hz=before, after_rms_hz=after,
                              after_rms_cents=after*.7 if cents is None else cents,
                              residual_ratio=after/before) for _ in range(3)]) for _ in range(2)]


class ContinuousQualityTests(unittest.TestCase):
    def diagnose(self, main, **kwargs):
        controls = [events(50, 50), events(50, 95), events(50, 48)]
        shifted = [events(50, 50), events(50, 50), events(50, 5)]
        result = numeric_diagnostics(main, controls, shifted, **kwargs)
        self.assertEqual(result['numeric_pass'], numeric_pass(main, controls, shifted))
        return result

    def test_large_verified_reduction_can_still_fail_absolute_goal(self):
        result = self.diagnose(events(50, 10), supported=True, independent_windows=True)
        self.assertFalse(result['passed'])
        self.assertEqual(result['classification'], 'improving_above_absolute_goal')
        self.assertEqual(result['failed_predicates'], ['absolute_residual_hz', 'absolute_residual_cents'])
        self.assertFalse(result['production_accepted'])

    def test_low_residual_requires_support_and_independent_windows(self):
        result = self.diagnose(events(50, 2), supported=False, independent_windows=True)
        self.assertTrue(result['numeric_pass'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['classification'], 'unsupported_reference')
        self.assertEqual(self.diagnose(events(50, 2), supported=True, independent_windows=False)['classification'],
                         'insufficient_evidence')

    def test_negative_controls_and_missing_margins_remain_failures(self):
        main = events(50, 2)
        failed = numeric_diagnostics(main, [main], [events(50, 50)]*3)
        self.assertFalse(failed['passed'])
        self.assertEqual(failed['classification'], 'failed_negative_controls')
        main[0]['margins'].pop()
        self.assertEqual(self.diagnose(main)['classification'], 'insufficient_evidence')

    def test_low_baseline_is_not_promoted_by_a_good_ratio(self):
        result = self.diagnose(events(.5, .1))
        self.assertFalse(result['passed'])
        self.assertEqual(result['classification'], 'low_baseline_no_op')

    def test_existing_passing_measurements_still_pass(self):
        result = self.diagnose(events(50, 2), supported=True, independent_windows=True)
        self.assertTrue(result['passed'])
        self.assertEqual(result['classification'], 'qualified_low_residual')
        self.assertEqual(result['failed_predicates'], [])


class SupportedImprovementTests(unittest.TestCase):
    def quality(self, main, **kwargs):
        controls = kwargs.pop('controls', [events(50, 50), events(50, 95), events(50, 48)])
        shifted = kwargs.pop('time_controls', [events(50, 50), events(50, 50), events(50, 5)])
        kwargs.setdefault('supported', True)
        kwargs.setdefault('independent_windows', True)
        return classify_numeric_quality(main, controls, shifted, **kwargs)

    def test_verified_large_reduction_is_eligible_without_claiming_restoration(self):
        result = self.quality(events(50, 10))
        self.assertTrue(result['eligible_for_planning'])
        self.assertEqual(result['quality_level'], 'supported_improvement')
        self.assertFalse(result['restoration_target_met'])
        self.assertFalse(result['legacy_numeric_pass'])
        self.assertEqual(result['failed_predicates'], [])
        self.assertEqual(result['unmet_restoration_goals'], ['absolute_residual_hz', 'absolute_residual_cents'])
        self.assertFalse(result['listening_approved'])
        self.assertFalse(result['production_accepted'])

    def test_absolute_goal_is_an_additional_quality_label(self):
        result = self.quality(events(50, 2))
        self.assertTrue(result['eligible_for_planning'])
        self.assertTrue(result['restoration_target_met'])
        self.assertEqual(result['quality_level'], 'restoration_target_met')

    def test_small_improvement_is_not_promoted_merely_for_low_absolute_residual(self):
        result = self.quality(events(4, 2.5))
        self.assertTrue(result['legacy_numeric_pass'])
        self.assertFalse(result['eligible_for_planning'])
        self.assertFalse(result['restoration_target_met'])
        self.assertIn('median_event_ratio', result['failed_predicates'])

    def test_single_bad_margin_cannot_hide_behind_good_median(self):
        main = events(50, 2)
        main[0]['margins'][0].update(after_rms_hz=45., after_rms_cents=31.5, residual_ratio=.9)
        result = self.quality(main)
        self.assertFalse(result['eligible_for_planning'])
        self.assertIn('worst_event_ratio', result['failed_predicates'])

    def test_support_and_independence_remain_mandatory(self):
        for flag in ('supported', 'independent_windows'):
            for missing in (False, None):
                result = self.quality(events(50, 10), **{flag: missing})
                self.assertFalse(result['eligible_for_planning'])
                self.assertEqual(result['quality_level'], 'unqualified')

    def test_low_baseline_and_failed_controls_remain_unqualified(self):
        self.assertFalse(self.quality(events(.5, .1))['eligible_for_planning'])
        main = events(50, 10)
        self.assertFalse(self.quality(main, controls=[main])['eligible_for_planning'])
        self.assertFalse(self.quality(main, time_controls=[events(50, 50)])['eligible_for_planning'])


if __name__ == '__main__':
    unittest.main()
