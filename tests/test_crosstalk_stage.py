"""Stage-04 contracts: source identity, bounded subtraction and downstream timing."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.interpolate import CubicSpline

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install
install()
from crosstalk_stage import render_channel, evidence_for, prepared_crosstalk
from denoise_archive import sha256
from shared_render import render_shared_channel
from shared_tape import build_tape_plan
from tests.test_shared_tape import fixture, write
import pipeline


class CrosstalkStageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.plan, self.assessment, self.inv = fixture(self.root)
        self.evidence = self.root/'echo_evidence'
        self.evidence.mkdir()
        write(self.evidence/'directions.json', {})
        self.output = self.root/'crosstalk/CH20'
        self.row = next(r for r in self.inv['channels'] if r['channel'] == 20)

    def render(self, **kwargs):
        return render_channel(self.inv, 20, self.root/'denoised', self.output,
                              self.evidence, progress=lambda _: None, **kwargs)

    def replace_audio(self, ch, audio):
        folder = self.root/f'denoised/A15_T001/CH{ch:02d}'
        saved = json.loads((folder/'denoised.json').read_text())
        path = folder/saved['audio']['filename']
        sf.write(path, audio, 8000, subtype='FLOAT')
        stat = path.stat()
        saved['audio'].update(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=sha256(path))
        write(folder/'denoised.json', saved)
        return saved

    def echo(self, direction='earlier', echo=True, polarity=1):
        rate = 8000
        pos = np.arange(60*rate)
        times = pos/rate
        rng = np.random.default_rng(37)
        donor = signal.sosfilt(signal.butter(4, [400, 2600], fs=rate, btype='bandpass', output='sos'),
                               rng.normal(0, .12, len(pos)))
        donor *= (times >= 20) & (times < 20.65)
        spline = CubicSpline(pos, donor)
        separation = -1.37 if direction == 'earlier' else 1.37
        main = spline(np.clip(pos+.0173*rate, 0, len(pos)-1))
        repeat = spline(np.clip(pos+(.0173-separation)*rate, 0, len(pos)-1))
        target = .7*main + (polarity*.12*repeat if echo else 0.)
        donor_saved = self.replace_audio(2, donor)
        target_saved = self.replace_audio(20, target[rate:])
        start = 18.6 if direction == 'earlier' else 21.34
        report = dict(tape='T001', target=20, donor=2, direction=direction,
            direction_evidence=[dict(correlation=.9)], start=15., duration=12.,
            application_interval=[start, start+.8], calibration_interval=[start+.1, start+.55],
            main_tracking=dict(donor_minus_target_seconds=[.0173]*5),
            echo_tracking=dict(donor_minus_target_seconds=[.0173-separation]*5),
            sources={'20': dict(signature=target_saved['signature']), '2': dict(signature=donor_saved['signature'])})
        write(self.evidence/'review.json', report)
        write(self.evidence/'directions.json', {'T001/CH20': dict(remove=direction)})
        return target[rate:].astype('float32'), (.7*main)[rate:], (start, start+.8)

    def test_bypass_stream_is_identical_and_uses_same_shared_map(self):
        summary = self.render()
        source, _ = sf.read(self.root/'denoised/A15_T001/CH20/CH20.wav', dtype='float32')
        output, _ = sf.read(self.output/'crosstalk.wav', dtype='float32')
        np.testing.assert_array_equal(output, source)
        self.assertEqual(summary['applied_seconds'], 0)
        self.assertEqual(len(summary['stagger_assignments']), 30)
        build_tape_plan(self.plan, self.assessment, self.root/'maps')
        direct = render_shared_channel(self.root/'maps', 20, self.root/'denoised/A15_T001/CH20',
                                       self.root/'direct', progress=lambda _: None)
        after = render_shared_channel(self.root/'maps', 20, self.output, self.root/'after', progress=lambda _: None)
        self.assertEqual(direct['map_sha256'], after['map_sha256'])
        self.assertEqual(after['start_seconds'], 1.)
        self.assertTrue(after['crosstalk_removed'])
        a, _ = sf.read(self.root/'direct/deflutter.wav', dtype='float32')
        b, _ = sf.read(self.root/'after/deflutter.wav', dtype='float32')
        np.testing.assert_array_equal(a, b)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.render()

    def test_both_directions_reduce_repeat_and_preserve_every_other_sample(self):
        for direction in ('earlier', 'later'):
            with self.subTest(direction=direction):
                self.output = self.root/direction
                before, truth, interval = self.echo(direction)
                summary = self.render()
                after, _ = sf.read(self.output/'crosstalk.wav', dtype='float32')
                times = 1+np.arange(len(after))/8000
                selected = (times >= interval[0]+.1) & (times < interval[1]-.1)
                outside = (times < interval[0]) | (times >= interval[1])
                self.assertGreater(summary['applied_seconds'], 0, summary['events'])
                self.assertLess(np.linalg.norm((after-truth)[selected]), .4*np.linalg.norm((before-truth)[selected]))
                np.testing.assert_array_equal(after[outside], before[outside])
                self.assertEqual(summary['direction'], direction)

    def test_shared_main_only_is_not_an_echo(self):
        before, _, _ = self.echo(echo=False)
        summary = self.render()
        after, _ = sf.read(self.output/'crosstalk.wav', dtype='float32')
        np.testing.assert_array_equal(after, before)
        self.assertEqual(summary['applied_seconds'], 0)

    def test_inverted_echo_uses_supported_measurements_for_search_and_polarity(self):
        before, truth, interval = self.echo(polarity=-1)
        report = json.loads((self.evidence/'review.json').read_text())
        nominal = report['echo_tracking']['donor_minus_target_seconds'][0]
        report['echo_tracking'] = dict(
            time=[0, interval[0]-15+.1, interval[0]-15+.3, interval[0]-15+.6, 11],
            donor_minus_target_seconds=[nominal+.02, nominal, nominal, nominal, nominal-.02],
            correlation=[.9, -.8, -.9, -.7, .9])
        write(self.evidence/'review.json', report)
        _, events, _ = evidence_for(self.evidence, self.inv['tape'], 20)
        seed = events[0]['paths']['echo_tracking']
        self.assertEqual(seed['polarity'], -1)
        self.assertEqual(seed['radius'], .00035)
        summary = self.render()
        self.assertGreater(summary['applied_seconds'], 0)
        after, _ = sf.read(self.output/'crosstalk.wav', dtype='float32')
        self.assertLess(np.linalg.norm(after-truth), .4*np.linalg.norm(before-truth))

    def test_direction_conflict_and_overlapping_evidence_fail_before_writing(self):
        self.echo()
        write(self.evidence/'directions.json', {'T001/CH20': dict(remove='later')})
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            self.render()
        self.assertFalse(self.output.exists())
        self.echo()
        (self.evidence/'duplicate').mkdir()
        write(self.evidence/'duplicate/review.json', json.loads((self.evidence/'review.json').read_text()))
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            self.render()

    def test_missing_catalog_is_error_missing_donor_is_bypass(self):
        with self.assertRaises(FileNotFoundError):
            evidence_for(self.root/'absent', 'A15_T001', 20)
        before, _, _ = self.echo()
        (self.root/'denoised/A15_T001/CH02/denoised.json').unlink()
        summary = self.render()
        self.assertEqual(summary['missing_donors'], [2])
        after, _ = sf.read(self.output/'crosstalk.wav', dtype='float32')
        np.testing.assert_array_equal(before, after)

    def test_source_tampering_and_evidence_changes_are_rejected(self):
        self.echo()
        path = self.root/'denoised/A15_T001/CH20/CH20.wav'
        saved = json.loads((path.parent/'denoised.json').read_text())
        saved['audio']['sha256'] = '0'*64
        write(path.parent/'denoised.json', saved)
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            self.render()
        self.assertFalse(self.output.exists())
        self.echo()
        self.render()
        write(self.evidence/'directions.json', {})
        with self.assertRaises(ValueError):
            prepared_crosstalk(self.row, self.output)

    def test_dry_run_writes_nothing_and_failure_is_not_complete(self):
        self.echo()
        summary = self.render(dry_run=True)
        self.assertEqual(len(summary['planned_events']), 1)
        self.assertFalse(self.output.exists())
        with patch('crosstalk_stage.process_event', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.render()
        saved = json.loads((self.output/'crosstalk.json').read_text())
        self.assertEqual(saved['status'], 'failed')
        self.assertFalse((self.output/'crosstalk.wav').exists())

    def test_pipeline_routes_stage04_before_map_and_normalization(self):
        args, selected = pipeline.parse_args(['plan', '--tape', 'A15_T001', '--channel', '20',
            '--stages', '07,05b,04', '--crosstalk-root', str(self.root/'stage04')])
        jobs = [j for j in pipeline.make_jobs(args, selected, self.root/'runs') if j['status'] == 'pending']
        self.assertEqual([j['number'] for j in jobs], ['04', '05b', '07'])
        command = jobs[1]['command']
        self.assertEqual(command[command.index('--crosstalk-input')+1], str(self.root/'stage04/A15_T001/CH20'))
        for stages in ['04,07', '04,06']:
            with self.assertRaises(SystemExit):
                pipeline.parse_args(['--channel', '20', '--stages', stages])


if __name__ == '__main__':
    unittest.main()
