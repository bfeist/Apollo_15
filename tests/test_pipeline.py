import json
import os
from collections import deque
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts' / 'pipeline.py'
sys.path.insert(0, str(ROOT/'scripts'))
from audio_processing import clean_tones, denoise
from flutter_planning import inventory
import pipeline


class BatchDenoiseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.originals = self.root/'originals'
        self.cache = self.root/'denoised'
        self.runs = self.root/'runs'
        audio = .1*np.sin(2*np.pi*440*np.arange(15*8000)/8000)
        for tape, channels in [('A15_T001', (1, 2, 4)), ('A15_T002', (1, 2))]:
            folder = self.originals/tape
            folder.mkdir(parents=True)
            for ch in channels:
                role = 'IRIG' if ch == 1 else 'VOICE'
                sf.write(folder/f'{tape}_HR1U_CH{ch}_{role}_207-00-00-00_207-00-00-15.wav',
                         audio, 8000, subtype='FLOAT')
        self.options = ['--concatenated', str(self.originals), '--denoised-root', str(self.cache),
                        '--output', str(self.runs), '--denoise-workers', '1', '--plain']

    def jobs(self, *selection):
        args, selected = pipeline.parse_args(['plan', *self.options, *selection])
        return pipeline.make_jobs(args, selected, self.runs/'plan')

    def test_scopes_and_read_only_plan(self):
        jobs = self.jobs('--all-tapes')
        self.assertEqual([(j['tape'], j['channel']) for j in jobs if j['status'] == 'pending'],
                         [('A15_T001', 1), ('A15_T001', 2), ('A15_T001', 4),
                          ('A15_T002', 1), ('A15_T002', 2)])
        self.assertTrue(all(j['status'] == 'pending' for j in jobs))
        self.assertEqual(len({j['log'] for j in jobs}), len(jobs))
        tape = self.jobs('--tape', 'A15_T001', '--all-channels')
        self.assertEqual(len([j for j in tape if j['status'] == 'pending']), 3)
        self.assertEqual(self.jobs('--tape', 'A15_T001', '--stages', '03'), tape)
        channel = self.jobs('--all-tapes', '--channel', '2')
        self.assertEqual(len(channel), 2)
        self.assertTrue(all(j['channel'] == 2 for j in channel))
        timing = self.jobs('--all-tapes', '--channel', '1')
        self.assertEqual(len(timing), 2)
        self.assertTrue(all(j['channel'] == 1 and j['status'] == 'pending' for j in timing))
        self.assertEqual(pipeline.main(['plan', *self.options, '--all-tapes']), 0)
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.runs.exists())

    def test_invalid_batch_arguments_and_empty_inventory(self):
        for selection in [('--all-tapes', '--tape', 'A15_T001'),
                          ('--all-channels', '--channel', '2'),
                          ('--all-tapes', '--stages', '03,05b'),
                          ('--all-tapes', '--input', str(self.originals))]:
            with self.subTest(selection=selection), self.assertRaises(SystemExit):
                pipeline.parse_args(['run', *selection])
        with self.assertRaisesRegex(ValueError, 'No available'):
            self.jobs('--all-tapes', '--channel', '30')
        empty = self.root/'empty'
        empty.mkdir()
        args, selected = pipeline.parse_args(['plan', '--all-tapes', '--concatenated', str(empty)])
        with self.assertRaisesRegex(ValueError, 'No canonical tape'):
            pipeline.make_jobs(args, selected, self.runs)

    def test_batch_outputs_logs_and_reuse(self):
        command = [sys.executable, str(RUNNER), 'run', *self.options, '--all-tapes']
        originals = {p: p.read_bytes() for p in self.originals.rglob('*.wav')}
        first = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(first.returncode, 0, first.stdout+first.stderr)
        outputs = sorted(self.cache.glob('*/CH*/denoised.wav'))
        self.assertEqual(len(outputs), 5)
        self.assertEqual(sum(p.parent.name == 'CH01' for p in outputs), 2)
        for path in outputs:
            self.assertEqual(sf.info(path).frames, 15*8000)
        manifest_path = next(self.runs.glob('*/run.json'))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(sum(j['status'] == 'done' for j in manifest['stages']), 5)
        for job in manifest['stages']:
            if job['status'] == 'done':
                self.assertTrue((manifest_path.parent/job['log']).is_file())
        stamps = {p: p.stat().st_mtime_ns for p in outputs}
        again = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(again.returncode, 0, again.stdout+again.stderr)
        self.assertIn('5 skipped existing', again.stdout)
        with patch.object(pipeline, 'execute', side_effect=AssertionError('Completed channel was launched')), \
             patch('channel_review.verify_audio', side_effect=AssertionError('Completed audio was decoded')):
            self.assertEqual(pipeline.main(['run', *self.options, '--all-tapes']), 0)
        manifests = [json.loads(p.read_text()) for p in self.runs.glob('*/run.json')]
        self.assertEqual(sum(all(j['status'] == 'skipped existing' and j['returncode'] == 0
                                 for j in m['stages']) for m in manifests), 2)
        jobs = self.jobs('--all-tapes')
        self.assertTrue(all(j['status'] == 'skipped existing' for j in jobs))
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in outputs})
        self.assertTrue(all(p.read_bytes() == content for p, content in originals.items()))
        # Existing files alone cannot hide incomplete or incompatible caches.
        cache_manifest = outputs[0].with_suffix('.json')
        saved = cache_manifest.read_text()
        for change in ('running', 'failed', 'source', 'implementation', 'evidence', 'invalid json', 'invalid shape'):
            with self.subTest(change=change):
                record = json.loads(saved)
                if change in {'running', 'failed'}:
                    record['status'] = change
                elif change == 'source':
                    record['signature']['source']['mtime_ns'] += 1
                elif change == 'implementation':
                    record['signature']['cleanup_sha256'] = 'old implementation'
                elif change == 'evidence':
                    record['cleanup_log_sha256'] = 'changed evidence'
                cache_manifest.write_text('{' if change == 'invalid json' else
                                          '[]' if change == 'invalid shape' else json.dumps(record))
                statuses = [j['status'] for j in self.jobs('--all-tapes')]
                self.assertEqual(statuses.count('pending'), 1)
                self.assertEqual(statuses.count('skipped existing'), 4)
        cache_manifest.write_text(saved)
        # A partial batch still schedules just the unfinished channel.
        missing = outputs[0]
        missing.unlink()
        calls = []
        def execute(job, *unused):
            calls.append((job['tape'], job['channel']))
            job.update(status='done', returncode=0)
            return 0
        with patch.object(pipeline, 'execute', side_effect=execute):
            self.assertEqual(pipeline.main(['run', *self.options, '--all-tapes']), 0)
        self.assertEqual(calls, [(missing.parent.parent.name, int(missing.parent.name[2:]))])

    def test_failed_channel_does_not_stop_batch_or_hide_failure(self):
        calls = []
        def execute(job, *unused):
            calls.append(job['channel'])
            code = 7 if len(calls) == 1 else 0
            job.update(status='failed' if code else 'done', returncode=code)
            return code
        with patch.object(pipeline, 'execute', side_effect=execute):
            result = pipeline.main(['run', *self.options, '--all-tapes'])
        self.assertEqual(result, 7)
        self.assertEqual(len(calls), 5)
        manifest = json.loads(next(self.runs.glob('*/run.json')).read_text())
        self.assertEqual([j['status'] for j in manifest['stages']],
                         ['failed', 'done', 'done', 'done', 'done'])

    def test_invalid_tape_is_reported_and_other_tapes_continue(self):
        bad = self.originals/'A15_T001'/'unidentified.wav'
        sf.write(bad, np.zeros(8000), 8000)
        jobs = self.jobs('--all-tapes')
        self.assertEqual(jobs[0]['status'], 'failed')
        self.assertIn('unidentified.wav', jobs[0]['error'])
        calls = []
        def execute(job, *unused):
            calls.append(job['tape'])
            job.update(status='done', returncode=0)
            return 0
        with patch.object(pipeline, 'execute', side_effect=execute):
            result = pipeline.main(['run', *self.options, '--all-tapes'])
        self.assertEqual(result, 2)
        self.assertEqual(calls, ['A15_T002', 'A15_T002'])
        manifest = json.loads(next(self.runs.glob('*/run.json')).read_text())
        self.assertEqual(manifest['stages'][0]['status'], 'failed')

    def test_unlabelled_delivery_does_not_block_numbered_channels(self):
        folder = self.originals/'A15_T001'
        extra = folder/'A15_T001_HR1U__206-23-59-59_207-00-00-15.wav'
        sf.write(extra, np.zeros(16*8000), 8000)
        before = extra.read_bytes()
        with self.assertWarnsRegex(UserWarning, 'Unlabelled recording'):
            inv = inventory(folder)
        self.assertEqual([r['channel'] for r in inv['channels']], [1, 2, 4])
        self.assertEqual(inv['duration_seconds'], 15)
        self.assertTrue(all(r['start_seconds'] == 0 for r in inv['channels']))
        self.assertEqual(inv['excluded_files'][0]['path'], str(extra.resolve()))
        self.assertEqual(inv['missing_channels'], sorted(set(range(1, 31))-{1, 2, 4}))
        with self.assertWarnsRegex(UserWarning, 'Unlabelled recording'):
            jobs = self.jobs('--all-tapes')
        self.assertEqual(len(jobs), 5)
        self.assertTrue(all(j['status'] == 'pending' for j in jobs))
        # Exercise the same inventory path used by the stage-03 subprocess.
        command = [sys.executable, str(ROOT/'scripts'/'03_denoise.py'),
                   '--input', str(folder), '--output', str(self.cache/'CH02'),
                   '--channel', '2', '--workers', '1']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('Unlabelled recording', result.stderr)
        self.assertEqual(sf.info(self.cache/'CH02'/'denoised.wav').frames, 15*8000)
        self.assertEqual(extra.read_bytes(), before)
        # Arbitrary malformed WAV names still fail rather than being ignored.
        extra.rename(folder/'unexpected_206-23-59-59_207-00-00-15.wav')
        with self.assertRaisesRegex(ValueError, 'Expected a timestamped canonical'):
            inventory(folder)


@unittest.skipUnless(os.name == 'nt', 'Windows process-tree cancellation')
class CancellationTests(unittest.TestCase):
    def test_interrupt_stops_stage_and_child(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handles = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            script = root/'stage.py'
            script.write_text(
                'import json, os, subprocess, sys, time\n'
                'from pathlib import Path\n'
                'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                'Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))\n'
                'print("ready", flush=True)\n'
                'time.sleep(60)\n')
            ids = root/'pids.json'
            job = dict(command=[sys.executable, str(script), str(ids)], number='03')

            def interrupt(*args):
                for pid in json.loads(ids.read_text()):
                    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                    self.assertTrue(handle)
                    handles.append(handle)
                raise KeyboardInterrupt

            try:
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.execute(job, [job], deque(), root, None, Mock(print=interrupt))
                self.assertEqual(len(handles), 2)
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                # Also clean up if the assertion catches a cancellation regression.
                if ids.exists():
                    for pid in json.loads(ids.read_text()):
                        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for handle in handles:
                    kernel.CloseHandle(handle)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.sr = 8000
        t = np.arange(20 * self.sr) / self.sr
        rng = np.random.default_rng(7)
        # Varying speech-like carrier plus persistent hum and noise.
        x = .1 * np.sin(2 * np.pi * 60 * t) + .12 * np.sin(2 * np.pi * 440 * t) * (1 + .8 * np.sin(2 * np.pi * 3 * t))
        x += rng.normal(0, .005, len(t))
        self.wav = self.source / 'speech.wav'
        sf.write(self.wav, x, self.sr, subtype='FLOAT')

    def run_pipeline(self, *args):
        return subprocess.run([sys.executable, str(RUNNER), 'run', '--plain', '--input', str(self.source),
                               '--output', str(self.output), *args], capture_output=True, text=True, encoding='utf-8')

    def manifest(self):
        path = max(self.output.glob('*/run.json'), key=lambda p: p.stat().st_mtime_ns)
        return path.parent, json.loads(path.read_text())

    def test_order_residuals_and_explicit_skip(self):
        before = self.wav.read_bytes()
        result = self.run_pipeline('--stages', '03,05b', '--skip-deflutter',
                                   '--target-dbfs', '-3', '--max-boost-db', '12')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run, manifest = self.manifest()
        self.assertEqual([s['status'] for s in manifest['stages'] if s['number'] in {'03','05b'}], ['done', 'skipped explicitly'])
        raw, _ = sf.read(self.wav)
        clean, _ = sf.read(run / '03_denoise/speech.wav')
        tonal_metadata = json.loads((run / '03_denoise/speech.json').read_text())
        self.assertEqual(tonal_metadata['tonal_cleanup_method'], 'iterative_tones_plus_residual_hum_v1')
        self.assertIn('hum_passes', tonal_metadata)
        self.assertIn('residual_hum_lines', tonal_metadata)
        removed, _ = sf.read(run / '03_denoise_removed/speech.wav')
        np.testing.assert_allclose(clean + removed, raw, atol=3e-8)
        t = np.arange(len(raw)) / self.sr
        amplitude = lambda x: abs(np.mean(x * np.exp(-2j * np.pi * 60 * t)))
        self.assertLess(amplitude(clean), amplitude(raw) * .1)
        broadband_metadata = tonal_metadata
        self.assertEqual(broadband_metadata['reduction_cap_db'], 3)
        self.assertFalse(broadband_metadata['noise_origin_classified'])
        tonal, _ = clean_tones(raw, self.sr)
        expected, _ = denoise(tonal, self.sr, reduction_cap_db=3)
        np.testing.assert_allclose(clean, expected, atol=3e-8)
        self.assertEqual(before, self.wav.read_bytes())

    def test_deflutter_requires_plan_before_export(self):
        result = self.run_pipeline('--stages', '05b', '--target-dbfs', '-3', '--max-boost-db', '6')
        self.assertEqual(result.returncode, 2)
        run, manifest = self.manifest()
        states = {s['number']: s['status'] for s in manifest['stages']}
        self.assertEqual(states['05b'], 'blocked')
        self.assertFalse((run / '06_normalize').exists())

    def test_deflutter_includes_normalization(self):
        t = np.arange(20*self.sr)/self.sr
        tau = t-.025/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
        ref = self.root/'reference.wav'
        sf.write(ref, np.sin(2*np.pi*631.5*tau), self.sr, subtype='FLOAT')
        sf.write(self.wav, .2*np.sin(2*np.pi*1000*tau), self.sr, subtype='FLOAT')
        config = self.root/'flutter.json'
        config.write_text(json.dumps(dict(tape='synthetic', start_seconds=0, duration_seconds=20,
            reference=dict(path=str(ref)), targets=[dict(file='speech.wav', start_seconds=0)])))
        result = self.run_pipeline('--stages', '05b', '--flutter-config', str(config),
            '--target-dbfs', '-3', '--max-boost-db', '12')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        run, manifest = self.manifest()
        self.assertEqual([s['status'] for s in manifest['stages'] if s['number'] == '05b'], ['done'])
        final, _ = sf.read(run/'05b_deflutter/speech.wav')
        m = json.loads((run/'05b_deflutter/speech.json').read_text())
        from flutter import estimate, positions, resample_at
        reference, _ = sf.read(ref)
        _, error, _, _ = estimate(reference, self.sr)
        original, _ = sf.read(self.wav)
        corrected = resample_at(original, positions(error, self.sr, len(original)))
        np.testing.assert_allclose(final, corrected*m['gain'], atol=6e-8)
        self.assertLessEqual(abs(final).max(), 10**(-3/20)+1e-7)
        self.assertGreater(np.std(corrected-original), .01)
        self.assertEqual(m['normalization']['target_dbfs'], -3)

    def test_irig_fails_and_stops(self):
        self.wav.rename(self.source / 'CH1_IRIG.wav')
        result = self.run_pipeline('--stages', '03,05b')
        self.assertNotEqual(result.returncode, 0)
        _, manifest = self.manifest()
        states = {s['number']: s['status'] for s in manifest['stages']}
        self.assertEqual(states['03'], 'failed')
        self.assertEqual(states['05b'], 'not run')

    def test_standalone_denoise_excerpts(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/03_denoise.py'),
                                 '--input', str(self.source), '--output', str(self.output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        raw, _ = sf.read(self.wav)
        tonal, _ = clean_tones(raw, self.sr)
        expected, _ = denoise(tonal, self.sr)
        actual, rate = sf.read(self.output/'speech.wav')
        self.assertEqual(rate, self.sr)
        np.testing.assert_allclose(actual, expected, atol=3e-8)
        self.assertEqual(json.loads((self.output/'speech.json').read_text())['stage'], 'denoise')

    def test_retired_stages_are_rejected(self):
        for stage in ('02', '04', '02b', '05', '06'):
            with self.subTest(stage=stage):
                result = self.run_pipeline('--stages', stage)
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_standalone_channel_denoise_without_plan(self):
        from test_flutter_planning import RegionalPlanningTests
        original = RegionalPlanningTests().fixture(self.root)
        command = [sys.executable, str(ROOT/'scripts/03_denoise.py'), '--input', str(original),
                   '--channel', '4', '--denoised-root', str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        intermediate = self.output/original.name/'CH04'/'denoised.wav'
        self.assertTrue(intermediate.is_file())
        before = intermediate.stat().st_mtime_ns
        again = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn('Reusing verified denoised intermediate', again.stdout)
        self.assertEqual(intermediate.stat().st_mtime_ns, before)

    def test_plan_writes_nothing(self):
        result = subprocess.run([sys.executable, str(RUNNER), 'plan', '--output', str(self.output)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_whole_channel_pipeline_uses_original_plan_and_reusable_denoised_intermediate(self):
        from test_flutter_planning import RegionalPlanningTests
        original=RegionalPlanningTests().fixture(self.root)
        command=[sys.executable,str(RUNNER),'run','--plain','--tape',original.name,'--channel','4',
                 '--stages','03,05a,05b',
                 '--concatenated',str(self.root),'--output',str(self.output),
                 '--denoised-root',str(self.root/'temporary'),'--defluttered-root',str(self.root/'final'),
                 '--planning-root',str(self.root/'plans'),'--flutter-plan-workers','1','--experimental-transfer']
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        _,manifest=self.manifest()
        self.assertEqual([s['number'] for s in manifest['stages'] if s['status']=='done'],['03','05a','05b'])
        final=self.root/'final'/original.name/'CH04'
        review=json.loads((final/'review.json').read_text())
        intermediate=self.root/'temporary'/original.name/'CH04'/'denoised.wav'
        self.assertEqual(Path(review['denoised_intermediate']['file']),intermediate)
        self.assertTrue(review['denoised_intermediate']['reused'])
        self.assertFalse((final/'cleaned.wav').exists())
        self.assertIn('normalization',review)
        self.assertLessEqual(review['audio']['deflutter']['peak'],10**(-3/20)+1e-7)
        self.assertEqual(sf.info(final/'deflutter.wav').frames,sf.info(intermediate).frames)
        plan=json.loads((self.root/'plans'/original.name/'plan.json').read_text())
        self.assertEqual(Path(plan['inventory']['input']),original)
        before=intermediate.stat().st_mtime_ns
        again=subprocess.run(command+['--stages','03'],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(again.returncode,0,again.stdout+again.stderr)
        self.assertIn('skipped existing',again.stdout)
        _, manifest = self.manifest()
        self.assertEqual(next(j['status'] for j in manifest['stages'] if j['number'] == '03'),
                         'skipped existing')
        self.assertEqual(intermediate.stat().st_mtime_ns,before)


if __name__ == '__main__':
    unittest.main()
