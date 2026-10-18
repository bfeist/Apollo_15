"""Regional coverage, true correction controls, reference changes and resumability."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf
from scipy.integrate import cumulative_trapezoid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from flutter_planning import inventory, make_regions, plan_tape, select_trial
from flutter_tracking import _harmonic_values, bound, harmonic_map, signed_map, trajectory
from pipeline import make_jobs, parse_args


class RegionalPlanningTests(unittest.TestCase):
    def test_signed_tracking_harmonics_and_dropout(self):
        sr = 8000
        t = np.arange(20*sr)/sr
        truth = .015*np.sin(2*np.pi*6.5*t)+.003*np.sin(2*np.pi*13*t+.3)
        tau = t+cumulative_trapezoid(truth, dx=1/sr, initial=0)
        x = np.sin(2*np.pi*777*tau)
        trace = trajectory(x, sr, 777, 65)
        h = harmonic_map(trace)
        expected = truth[::40]
        self.assertGreater(np.corrcoef(h[600:-600], expected[600:-600])[0, 1], .98)
        self.assertLess(np.std(h[600:-600]-expected[600:-600]), .002)
        am = (1+.6*np.sin(2*np.pi*6.5*t))*np.sin(2*np.pi*777*t)
        self.assertLess(np.std(harmonic_map(trajectory(am, sr, 777, 65))[600:-600]), 1e-5)
        x[(t > 9) & (t < 11)] = 0
        gap = trajectory(x, sr, 777, 65)
        self.assertLess(gap[5][1900:2100].max(), .01)
        self.assertLess(abs(signed_map(gap)[1900:2100]).max(), 1e-8)

    def test_batched_fit_preserves_scalar_svd_including_gaps(self):
        sr = 8000
        t = np.arange(20*sr)/sr
        tau = t-.02/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
        x = np.sin(2*np.pi*777*tau)
        x[(t > 8) & (t < 12)] = 0
        trace = trajectory(x, sr, 777, 65)
        grid = np.arange(0, len(trace[0]), 5)
        expected = bound(np.interp(trace[0], trace[0][grid], _harmonic_values(trace, grid, 3)), trace[5])
        np.testing.assert_allclose(harmonic_map(trace), expected, atol=1e-9, rtol=1e-7)

    def test_harmonic_preference_and_material_wide_improvement(self):
        a = dict(method='harmonic_3', calibration_loss=.21)
        b = dict(method='signed_2_30', calibration_loss=.20)
        self.assertIs(select_trial([a, b]), a)
        b['calibration_loss'] = .10
        self.assertIs(select_trial([a, b]), b)
        self.assertIsNone(select_trial([dict(method='harmonic_3', calibration_loss=None)]))

    def test_17_hour_coverage_preserves_missing_channel_boundaries(self):
        inv = dict(duration_seconds=17*3600+7.25, channels=[
            dict(start_seconds=0, end_seconds=17*3600+7.25, role='reference_and_target'),
            dict(start_seconds=123.25, end_seconds=59000, role='reference_and_target')])
        regions = make_regions(inv)
        self.assertEqual(regions[0]['start_seconds'], 0)
        self.assertEqual(regions[-1]['end_seconds'], inv['duration_seconds'])
        self.assertAlmostEqual(sum(r['end_seconds']-r['start_seconds'] for r in regions), inv['duration_seconds'])
        self.assertTrue(all(a['end_seconds'] == b['start_seconds'] for a, b in zip(regions, regions[1:])))
        self.assertIn(123.25, [r['start_seconds'] for r in regions])
        self.assertIn(59000, [r['end_seconds'] for r in regions])

    def fixture(self, root):
        source = root/'synthetic_tape'
        source.mkdir()
        sr = 8000
        t = np.arange(40*sr)/sr
        truth = .012*np.sin(2*np.pi*np.where(t < 20, 6.5*t, 11*t))
        tau = t+cumulative_trapezoid(truth, dx=1/sr, initial=0)
        # First reference has unrelated phase noise near the flutter band;
        # harmonic fitting can reject it. Later, true 11 Hz motion needs wide tracking.
        noisy_tau = tau+.004/(2*np.pi*8)*np.sin(2*np.pi*8*t)
        ref1 = .4*np.sin(2*np.pi*1500*noisy_tau)*(t < 20)
        ref2 = .4*np.sin(2*np.pi*1033*tau)*(t >= 20)
        envelope = sum(((t > at) & (t < at+.25)).astype(float) for at in (5, 12, 25, 32))
        target = .3*envelope*np.sin(2*np.pi*2500*tau)
        target += np.random.default_rng(1).normal(0, .001, len(t))
        # Independent pitch motion must not inherit the references' correction.
        other_tau = t-.012/(2*np.pi*8)*np.cos(2*np.pi*8*t)
        unrelated = .3*envelope*np.sin(2*np.pi*2500*other_tau)
        unrelated += np.random.default_rng(2).normal(0, .001, len(t))
        for ch, x in ((2, ref1), (3, ref2), (4, target), (5, unrelated)):
            sf.write(source/f'A15_SYNTH_HR2U_CH{ch}_VOICE_001-00-00-00_001-00-00-40.wav', x, sr, subtype='FLOAT')
        return source

    def test_complete_tape_reference_switch_transfer_and_resume(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            source = self.fixture(root)
            output = root/'plan'
            before = {p.name: p.read_bytes() for p in source.iterdir()}
            partial = plan_tape(source, output, region_seconds=20, max_regions=1)
            self.assertFalse(partial['complete'])
            self.assertEqual(partial['regions'][1]['status'], 'pending')
            finished = plan_tape(source, output, region_seconds=20, resume=True)
            self.assertTrue(finished['complete'])
            self.assertTrue(finished['full_tape'])
            self.assertEqual(len(finished['transitions']), 1)
            refs, methods = [], []
            for r in finished['regions']:
                target = next(t for t in r['targets'] if t['channel'] == 4)
                refs.append(target['reference_channel'])
                methods.append(next(v['method'] for v in r['variants'] if v['key'] == target['selected']))
                self.assertEqual(target['action'], 'render_and_review', target)
                other = next(t for t in r['targets'] if t['channel'] == 5)
                self.assertNotEqual(other['action'], 'render_and_review')
                self.assertTrue((output/r['detail_file']).exists())
                self.assertEqual(len(r['coverage']), 4)
            self.assertEqual(refs, [2, 3])
            self.assertEqual(methods, ['harmonic_3', 'signed_2_30'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in source.iterdir()})
            index = json.loads((output/'plan.json').read_text())
            self.assertEqual(index['summary']['analyzed'], 2)
            with self.assertRaisesRegex(ValueError, 'sources, parameters or implementation'):
                plan_tape(source, output, region_seconds=30, resume=True)
            mapfile = output/finished['regions'][0]['maps']['file']
            mapfile.write_bytes(mapfile.read_bytes()+b'changed')
            with self.assertRaisesRegex(ValueError, 'timing map changed'):
                plan_tape(source, output, region_seconds=20, resume=True)

    def test_silent_tape_has_explicit_uncorrected_decisions(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            source = root/'silent'
            source.mkdir()
            path = source/'A15_SYNTH_HR2U_CH2_VOICE_001-00-00-00_001-00-00-20.wav'
            sf.write(path, np.zeros(160000), 8000)
            plan = plan_tape(source, root/'plan', region_seconds=15)
            self.assertTrue(plan['complete'])
            for region in plan['regions']:
                self.assertEqual(region['targets'][0]['action'], 'leave_uncorrected')
                self.assertFalse(region['maps'])
            with self.assertRaisesRegex(ValueError, 'non-overlapping'):
                plan_tape(source, source/'bad')
            sf.write(source/'duplicate_CH2_VOICE_001-00-00-00_001-00-00-20.wav', np.zeros(160000), 8000)
            with self.assertRaisesRegex(ValueError, 'one concatenated'):
                inventory(source)

    def test_parallel_preserves_decisions_maps_and_boundary_order(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            source = self.fixture(root)
            serial = plan_tape(source, root/'serial', region_seconds=15, workers=1)
            parallel = plan_tape(source, root/'parallel', region_seconds=15, workers=2)
            self.assertTrue(parallel['complete'])
            self.assertEqual(parallel['performance']['workers'], 2)
            self.assertEqual(serial['transitions'], parallel['transitions'])
            for left, right in zip(serial['regions'], parallel['regions']):
                self.assertEqual(left['id'], right['id'])
                self.assertEqual(left['targets'], right['targets'])
                if left['maps']:
                    with np.load(root/'serial'/left['maps']['file']) as a, np.load(root/'parallel'/right['maps']['file']) as b:
                        self.assertEqual(a.files, b.files)
                        for key in a.files:
                            np.testing.assert_array_equal(a[key], b[key])
            # Worker count is a performance setting, not a change in analysis.
            resumed = plan_tape(source, root/'parallel', region_seconds=15, workers=1, resume=True)
            self.assertEqual(parallel['transitions'], resumed['transitions'])

    def test_pipeline_step_does_not_replace_speech_input(self):
        args, selected = parse_args(['plan', '--stages', '05a,03', '--input', 'speech', '--tape', 'A15_T510083'])
        jobs = make_jobs(args, selected, Path('runs/test'))
        active = [j for j in jobs if j['status'] == 'pending']
        self.assertEqual([j['number'] for j in active], ['03', '05a'])
        self.assertIn('D:\\Apollo_15_30-Track\\orig_concatenated\\A15_T510083', active[1]['command'])
        self.assertIn('speech', active[0]['command'])


if __name__ == '__main__':
    unittest.main()
