"""Persistent tape plans are reusable inputs, not disposable run outputs."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from flutter_plan_store import load_saved_plan, planning_directory, planning_lock
from flutter_planning import plan_tape
from pipeline import make_jobs, parse_args
import test_flutter_planning as fixtures

spec = importlib.util.spec_from_file_location('saved_plan_deflutter', ROOT/'scripts/05b_deflutter.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = fixtures.RegionalPlanningTests().fixture(self.root)
        self.output = self.root/'plans'/'synthetic_tape'

    def create(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return plan_tape(self.source, self.output, region_seconds=20, **kwargs)

    def test_pipeline_shares_persistent_tape_path_and_isolates_audits(self):
        args, selected = parse_args(['plan', '--stages', '05a,05b', '--tape', 'synthetic_tape',
                                     '--flutter-planning-root', str(self.root/'plans')])
        jobs = {j['number']: j for j in make_jobs(args, selected, self.root/'run')}
        self.assertIn(str(self.output), jobs['05a']['command'])
        self.assertIn(str(self.output/'plan.json'), jobs['05b']['command'])
        audit = planning_directory('synthetic_tape', self.root/'plans', 12, 60)
        self.assertEqual(audit.parent, self.output/'audits')
        self.assertNotEqual(audit, self.output)
        with self.assertRaises(ValueError):
            planning_directory('../escape', self.root)

    def test_completed_plan_reused_without_analysis_or_rewrite(self):
        self.create()
        index = self.output/'plan.json'
        prior = json.loads(index.read_text())
        prior['implementation_sha256'] = 'older-implementation-preserved'
        index.write_text(json.dumps(prior), encoding='utf-8')
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with (patch('flutter_planning.analyze_region', side_effect=AssertionError('Analysis reran')),
              patch('flutter_planning.save_plan', side_effect=AssertionError('Plan rewritten'))):
            reused = self.create(reuse=True)
        self.assertTrue(reused['complete'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        mapfile = self.output/reused['regions'][0]['maps']['file']
        mapfile.write_bytes(mapfile.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.create(reuse=True)

    def test_full_plan_can_start_after_audit_without_changing_audit(self):
        audit = planning_directory('synthetic_tape', self.root/'plans', 0, 20)
        with contextlib.redirect_stdout(io.StringIO()), planning_lock(audit):
            plan_tape(self.source, audit, duration=20, region_seconds=20, reuse=True)
        before = {p.relative_to(self.output): p.read_bytes()
                  for p in self.output.rglob('*') if p.is_file()}
        with planning_lock(self.output):
            full = self.create(reuse=True)
        self.assertTrue(full['full_tape'])
        self.assertTrue(full['complete'])
        self.assertTrue(load_saved_plan(audit)['complete'])
        for relative, contents in before.items():
            self.assertEqual((self.output/relative).read_bytes(), contents)

    def test_new_plan_still_rejects_unrecognized_existing_contents(self):
        self.output.mkdir(parents=True)
        for name in ('audits', 'r00000.json', 'index.html'):
            with self.subTest(name=name):
                existing = self.output/name
                existing.write_bytes(b'preserve me')
                with self.assertRaisesRegex(ValueError, 'fresh output folder'):
                    self.create(reuse=True)
                self.assertEqual(existing.read_bytes(), b'preserve me')
                existing.unlink()
        (self.output/'unrelated').mkdir()
        with self.assertRaisesRegex(ValueError, 'fresh output folder'):
            self.create(reuse=True)

    def test_partial_plan_automatically_resumes_and_lock_is_released(self):
        first = self.create(max_regions=1)
        self.assertFalse(first['complete'])
        with planning_lock(self.output):
            with self.assertRaisesRegex(ValueError, 'already using'):
                with planning_lock(self.output):
                    pass
        with planning_lock(self.output):
            final = self.create(reuse=True)
        self.assertTrue(final['complete'])
        self.assertEqual(final['summary']['analyzed'], 2)
        original = next(self.source.glob('*.wav'))
        original.touch()
        with self.assertRaisesRegex(ValueError, 'Original source changed'):
            load_saved_plan(self.output)

    def test_stage05_consumes_saved_map_and_preserves_plan(self):
        plan = self.create()
        cleaned = self.root/'cleaned'
        cleaned.mkdir()
        original = next(self.source.glob('*_CH4_*'))
        x, sr = sf.read(original, frames=20*8000)
        sf.write(cleaned/'speech.wav', .9*x, sr, subtype='FLOAT')
        manifest = self.root/'targets.json'
        config = dict(tape='synthetic_tape', region_id='r00000', start_seconds=0, duration_seconds=20,
                      targets=[dict(file='speech.wav', channel=4, start_seconds=0)])
        manifest.write_text(json.dumps(config))
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with (patch.object(stage, 'estimate', side_effect=AssertionError('Reference tracking reran')),
              contextlib.redirect_stdout(io.StringIO())):
            result = stage.process_saved_region(cleaned, self.root/'rendered', self.output/'plan.json', manifest)
        self.assertFalse(result['planning_recomputed'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})
        y, rate = sf.read(self.root/'rendered/speech.wav')
        self.assertEqual(rate, sr)
        self.assertEqual(len(y), len(x))
        self.assertGreater(np.std(y-.9*x), .001)
        selected = next(t['selected'] for t in plan['regions'][0]['targets'] if t['channel'] == 4)
        with np.load(self.output/plan['regions'][0]['maps']['file']) as saved, np.load(self.root/'rendered/speech.timing_map.npz') as rendered:
            tt = np.arange(4000)/200
            expected = saved[selected][:4000]*np.clip(np.minimum(tt, tt[-1]-tt)/.1, 0, 1)
            np.testing.assert_array_equal(rendered['speed_error'], expected)
        config['targets'][0]['channel'] = 2  # Reference-only channel has no validated transfer.
        manifest.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'requires transfer validation'):
            stage.process_saved_region(cleaned, self.root/'blocked', self.output/'plan.json', manifest)
        self.assertFalse((self.root/'blocked').exists())


if __name__ == '__main__':
    unittest.main()
