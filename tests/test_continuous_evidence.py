"""Readiness and sampling follow continuous lifetimes without an IRIG survey gate."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from deflutter_readiness import readiness
from flutter_policy import METHODOLOGY, digest, implementation_digest, policy_digest
from review_evidence import Desk, review_intervals
from review_fixtures import make_demo


class ContinuousEvidenceTests(unittest.TestCase):
    def tape(self, root):
        source = root/'originals'/'A15_T510083'
        source.mkdir(parents=True)
        sf.write(source/'A15_T510083_HR1U_CH2_VOICE_207-00-00-00_207-00-00-20.wav',
                 np.zeros(160000), 8000, subtype='FLOAT')
        return source

    def status(self, root, **kwargs):
        return readiness(root/'originals', root/'plans', root/'denoised',
                         root/'approvals', root/'pending_rollout.json', **kwargs)

    def test_default_planning_needs_originals_but_neither_survey_nor_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.tape(root)
            status = self.status(root)
            self.assertTrue(status['ready_for_batch_planning'])
            self.assertFalse(status['ready_for_production'])
            self.assertTrue(status['tapes'][0]['originals_valid'])
            self.assertFalse(any(e.startswith('survey:') for e in status['tapes'][0]['errors']))

    def test_production_readiness_still_needs_current_plan_policy_and_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.tape(root)
            plan = dict(schema=2, kind='continuous_reference', full_tape=True,
                        methodology=METHODOLOGY, implementation_sha256=implementation_digest(),
                        policy_sha256=policy_digest())
            with patch('deflutter_readiness.completed_denoised', return_value=True), \
                 patch('deflutter_readiness.load_saved_plan', return_value=plan), \
                 patch('deflutter_readiness.require_rollout', return_value={}), \
                 patch('deflutter_readiness.require_production', return_value={}):
                self.assertTrue(self.status(root)['ready_for_production'])
                plan['policy_sha256'] = 'old_policy'
                self.assertFalse(self.status(root)['ready_for_production'])

    def test_invalid_originals_do_not_become_ready_without_a_survey(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tape = self.tape(root)
            (tape/'incomplete.partial').touch()
            self.assertFalse(self.status(root)['ready_for_batch_planning'])

    def test_sampling_exposes_reference_changes_and_bypasses_as_actual_spans(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spans = [dict(start_seconds=0, end_seconds=80, action='use_reference',
                          selected='ref_a', reference_channel=1, reason='Validated reference',
                          evidence_kind='direct_numeric', numeric_transfer_passed=True,
                          evidence=dict(level='multiple_time_windows')),
                     dict(start_seconds=80, end_seconds=90, action='leave_uncorrected',
                          reason='Carrier lost'),
                     dict(start_seconds=90, end_seconds=160, action='use_reference',
                          selected='ref_b', reference_channel=3, reason='Validated replacement',
                          evidence_kind='transfer_group', numeric_transfer_passed=False,
                          transfer_group=dict(passed=True), evidence=dict(level='multiple_time_windows'))]
            detail = dict(id='continuous', status='analyzed', start_seconds=0, end_seconds=160,
                          targets=[dict(channel=2, action='group_render_and_review', selected='ref_a',
                                        reason='Continuous tracking', reference_spans=spans,
                                        numeric_transfer_passed=True, evidence=dict(level='multiple_time_windows'))])
            path = root/'continuous.json'
            path.write_text(json.dumps(detail), encoding='utf-8')
            plan = dict(schema=2, kind='continuous_reference', regions=[dict(
                detail, detail_file=path.name, detail_sha256=digest(path))])
            intervals = list(review_intervals(root, plan))
            self.assertEqual([(a['start_seconds'], a['end_seconds']) for a, _ in intervals],
                             [(0, 80), (80, 90), (90, 160)])
            targets = [b['targets'][0] for _, b in intervals]
            self.assertEqual([t['selected'] for t in targets], ['ref_a', None, 'ref_b'])
            self.assertEqual(targets[1]['action'], 'leave_uncorrected')
            self.assertEqual(targets[0]['action'], 'render_and_review')
            self.assertTrue(targets[0]['numeric_transfer_passed'])
            self.assertEqual(targets[2]['action'], 'group_render_and_review')
            self.assertTrue(targets[2]['transfer_group']['passed'])
            self.assertEqual(targets[2]['reference_channel'], 3)
            self.assertEqual(len(plan['regions']), 1)
            path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed'):
                list(review_intervals(root, plan))

    def test_headless_full_channel_identity_review_needs_no_carrier_survey(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = make_demo(root/'fixture')
            tape = 'A15_T510083'
            path = Path(config['plans'])/tape/'plan.json'
            plan = json.loads(path.read_text())
            target = dict(channel=2, action='leave_uncorrected', selected=None,
                          reason='No independently supported reference', numeric_transfer_passed=False,
                          evidence=dict(level='sparse'), reference_spans=[dict(
                              start_seconds=0., end_seconds=120., action='leave_uncorrected',
                              reason='No independently supported reference')])
            detail = dict(id='continuous', kind='continuous_reference', status='analyzed',
                          start_seconds=0., end_seconds=120., analysis_start=0., analysis_end=120.,
                          targets=[target], variants=[], maps=dict(rate=200, streams={}), final_holdouts={})
            artifact = path.parent/'continuous.json'
            artifact.write_text(json.dumps(detail), encoding='utf-8')
            plan.update(schema=2, kind='continuous_reference', regions=[dict(
                detail, detail_file=artifact.name, detail_sha256=digest(artifact))])
            plan['inventory'].pop('carrier_survey')
            path.write_text(json.dumps(plan), encoding='utf-8')
            config.update(surveys=str(root/'absent_surveys'), state=str(root/'state'))
            desk = Desk(config)
            self.addCleanup(desk.close)
            desk.scan(lambda _: None)
            item = desk.item(tape, 2)
            self.assertEqual(item['blockers'], [])
            review_path = desk.build_map(item, lambda _: None)
            review = json.loads(review_path.read_text())
            self.assertEqual(review['joined_map_checks']['status'], 'passed')
            self.assertTrue(review['joined_map_checks']['no_motion'])
            self.assertTrue(np.all(np.load(review_path.parent/'speed_error.npy') == 0))
            self.assertFalse(any('survey.json' in r['path'] for r in review['bindings']))


if __name__ == '__main__':
    unittest.main()
