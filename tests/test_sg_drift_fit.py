"""The SG drift fit is one slow curve per reel, with constants only at recorder gaps and known edits."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'sg-audio'))
import sg_drift_fit as fit  # noqa: E402

REEL = {'reel': 'T-AAA', 'file': 'T-AAA.wav', 'sha256': 'x', 'duration_s': '10000'}


def anchors(t, offset, status='ok'):
    return [{'anchor_id': f'a{i}', 'reel': 'T-AAA', 'source_s': str(s), 'offset_s': str(o), 'status': status,
             'get_seconds': str(s + o), 'event_id': f'e{i}', 'get_choice': 'journal'} for i, (s, o) in
            enumerate(zip(t, offset))]


class DriftFitTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(1)
        self.t = np.sort(self.rng.uniform(0, 10000, 200))

    def test_a_constant_rate_and_a_recorder_gap(self):
        offset = 1000 + 0.005 * self.t + np.where(self.t > 6000, 600, 0) + self.rng.normal(0, 0.4, len(self.t))
        res = fit.fit_reel(REEL, anchors(self.t, offset), [], None)
        self.assertEqual(res['degree'], 1)
        self.assertAlmostEqual(res['rate_start_ppm'], 5000, delta=60)
        self.assertEqual([s['boundary'] for s in res['segments']], ['file_start', 'recorder_gap'])
        self.assertAlmostEqual(res['segments'][1]['constant_s'] - res['segments'][0]['constant_s'], 600, delta=0.5)

    def test_a_slowly_changing_rate_takes_one_curve(self):
        x = self.t / 10000
        offset = 50 + 45 * x + 5 * x ** 2 + self.rng.normal(0, 0.3, len(self.t))
        res = fit.fit_reel(REEL, anchors(self.t, offset), [], None)
        self.assertEqual(res['degree'], 2)
        self.assertAlmostEqual(res['rate_start_ppm'], 4500, delta=250)
        self.assertAlmostEqual(res['rate_end_ppm'], 5500, delta=250)
        self.assertEqual(len(res['segments']), 1)

    def test_transcript_steps_do_not_bend_or_split_the_curve(self):
        steps = np.where((self.t > 3000) & (self.t < 5000), -3.5, 0.0)  # a run of printed pages 3.5 s early
        offset = 20 + 0.005 * self.t + steps + self.rng.normal(0, 0.4, len(self.t))
        res = fit.fit_reel(REEL, anchors(self.t, offset), [], None)
        self.assertEqual(len(res['segments']), 1)
        self.assertTrue(res['residual_steps_s'])
        self.assertAlmostEqual(res['rate_start_ppm'], 5000, delta=150)

    def test_a_known_edit_splits_and_a_weak_reel_takes_the_mission_rate(self):
        offset = 0.005 * self.t + np.where(self.t > 4000, 4.3, 0)
        res = fit.fit_reel(REEL, anchors(self.t, offset), [4000.0], None)
        self.assertEqual(res['segments'][1]['boundary'], 'known_edit')
        few = fit.fit_reel(REEL, anchors([100, 200, 300, 400], [10, 10.4, 10.1, 10.6]), [], 5200.0)
        self.assertEqual((few['rate_from'], few['rate_start_ppm']), ('mission_median', 5200.0))
        none = fit.fit_reel(REEL, [], [], 5200.0)
        self.assertEqual((none['status'], none['segments']), ('rate_only', []))


if __name__ == '__main__':
    unittest.main()
