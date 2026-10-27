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


class CanonicalTapeTests(unittest.TestCase):
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


class BatchDenoiseTests(CanonicalTapeTests):
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


class BatchFlutterTests(CanonicalTapeTests):
    def setUp(self):
        super().setUp()
        self.plans = self.root/'plans'
        self.final = self.root/'final'
        self.options += ['--planning-root', str(self.plans), '--defluttered-root', str(self.final)]

    def test_plan_all_tapes_is_read_only_and_uses_per_tape_settings(self):
        jobs = self.jobs('--all-tapes', '--stages', '05a', '--flutter-monitor-seconds', '20', '--flutter-plan-start', '5',
                         '--flutter-plan-duration', '10')
        self.assertEqual([(j['tape'], j['channel']) for j in jobs],
                         [('A15_T001', None), ('A15_T002', None)])
        for job in jobs:
            command = job['command']
            self.assertEqual(command[command.index('--input')+1], str(self.originals/job['tape']))
            self.assertEqual(command[command.index('--output')+1],
                             str(self.plans/job['tape']/'audits'/'start_5_duration_10'))
            for flag, value in [('--monitor-seconds', '20.0'),
                                ('--start', '5.0'), ('--duration', '10.0')]:
                self.assertEqual(command[command.index(flag)+1], value)
        self.assertEqual(len({j['log'] for j in jobs}), 2)
        self.assertEqual(pipeline.main(['plan', *self.options, '--all-tapes', '--stages', '05a']), 0)
        self.assertFalse(self.plans.exists())
        self.assertFalse(self.runs.exists())
        self.assertFalse(self.cache.exists())

    def test_default_continuous_planning_forwards_monitor_and_io_sizes_without_survey(self):
        jobs = self.jobs('--all-tapes', '--stages', '05a',
                         '--flutter-monitor-seconds', '45', '--flutter-chunk-seconds', '3.7',
                         '--flutter-reference-limit', '6')
        for job in jobs:
            command = job['command']
            for flag, value in [('--monitor-seconds', '45.0'),
                                ('--chunk-seconds', '3.7'), ('--reference-limit', '6')]:
                self.assertEqual(command[command.index(flag)+1], value)
            self.assertNotIn('--carrier-survey-root', command)
            self.assertNotIn('--region-seconds', command)
            self.assertNotIn('--workers', command)

    def test_deflutter_all_tapes_selects_audio_channels_and_forwards_roots(self):
        jobs = self.jobs('--all-tapes', '--stages', '05b',
                         '--target-dbfs', '-6', '--max-boost-db', '9')
        self.assertEqual([(j['tape'], j['channel']) for j in jobs],
                         [('A15_T001', 2), ('A15_T001', 4), ('A15_T002', 2)])
        self.assertEqual(len({j['log'] for j in jobs}), 3)
        for job in jobs:
            command = job['command']
            channel = f'CH{job["channel"]:02d}'
            for flag, value in [('--flutter-plan', self.plans/job['tape']/'plan.json'),
                                ('--denoised-input', self.cache/job['tape']/channel),
                                ('--output', self.final/job['tape']/channel),
                                ('--target-dbfs', '-6.0'), ('--max-boost-db', '9.0')]:
                self.assertEqual(command[command.index(flag)+1], str(value))
            self.assertIn('--production-approval', command)
            self.assertNotIn('--experimental-transfer', command)
        with self.assertRaises(SystemExit):
            self.jobs('--all-tapes', '--stages', '05b', '--experimental-transfer')
        channel = self.jobs('--all-tapes', '--stages', '05b', '--channel', '4')
        self.assertEqual([(j['tape'], j['channel']) for j in channel], [('A15_T001', 4)])
        tape = self.jobs('--tape', 'A15_T002', '--all-channels', '--stages', '05b')
        self.assertEqual([(j['tape'], j['channel']) for j in tape], [('A15_T002', 2)])
        self.assertEqual(pipeline.main(['plan', *self.options, '--all-tapes', '--stages', '05b']), 0)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.runs.exists())
        with self.assertRaisesRegex(ValueError, 'No available non-IRIG'):
            self.jobs('--all-tapes', '--stages', '05b', '--channel', '1')

    def test_invalid_batch_overrides(self):
        for stage in ('05a', '05b'):
            for override in (['--input', str(self.originals)], ['--flutter-plan', 'one-plan.json'],
                             ['--flutter-config', 'legacy.json'], ['--flutter-targets', 'targets.json']):
                with self.subTest(stage=stage, override=override), self.assertRaises(SystemExit):
                    pipeline.parse_args(['run', '--all-tapes', '--stages', stage, *override])
        for extra in (['--flutter-plan-start', '5'], ['--flutter-plan-duration', '10']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                pipeline.parse_args(['run', '--all-tapes', '--stages', '05b', *extra])

    def test_batch_continues_after_stage_failure_or_exception(self):
        for stage, status, expected in [('05a', 'failed', 2), ('05b', 'blocked', 3)]:
            for exception in (False, True):
                with self.subTest(stage=stage, exception=exception):
                    calls = []
                    def execute(job, *unused):
                        calls.append(job['name'])
                        if len(calls) == 1 and exception:
                            raise OSError('Cannot launch stage')
                        code = 7 if len(calls) == 1 else 0
                        job.update(status=status if code else 'done', returncode=code)
                        return code
                    with patch.object(pipeline, 'execute', side_effect=execute):
                        result = pipeline.main(['run', *self.options, '--all-tapes', '--stages', stage])
                    self.assertEqual(result, 1 if exception else 7)
                    self.assertEqual(len(calls), expected)

    def test_invalid_tape_does_not_prevent_other_deflutter_jobs(self):
        sf.write(self.originals/'A15_T001'/'unidentified.wav', np.zeros(8000), 8000)
        jobs = self.jobs('--all-tapes', '--stages', '05b')
        self.assertEqual([j['status'] for j in jobs], ['failed', 'pending'])
        self.assertEqual(jobs[1]['tape'], 'A15_T002')
        self.assertEqual(jobs[1]['channel'], 2)

    def test_all_tapes_plan_and_render_end_to_end(self):
        # A later channel starts five seconds into the common acquisition clock.
        original = next((self.originals/'A15_T001').glob('*CH4*'))
        original.rename(original.with_name(original.name.replace('207-00-00-00_207-00-00-15',
                                                                 '207-00-00-05_207-00-00-20')))
        before = {p: p.read_bytes() for p in self.originals.rglob('*.wav')}
        from deflutter_readiness import rollout_template
        from flutter_policy import digest
        from channel_review import render_channel_review
        from flutter_plan_store import load_saved_plan
        # Plan the full tapes without a standalone survey or rollout acceptance.
        for stage in ('05a', '03'):
            command = [sys.executable, str(RUNNER), 'run', *self.options,
                       '--all-tapes', '--stages', stage]
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        rollout = self.root/'rollout.json'
        record = rollout_template()
        record.update(status='accepted', reviewer='synthetic integration fixture', reviewed_at='2026-10-24')
        for name in ('carrier_qualification', 'fresh_holdouts', 'speech_listening'):
            evidence = self.root/(name+'.json')
            evidence.write_text(json.dumps(dict(status='accepted', scope='Synthetic integration fixture only')))
            record[name] = dict(path=str(evidence), sha256=digest(evidence))
        rollout.write_text(json.dumps(record))
        for plan_path in self.plans.glob('*/plan.json'):
            plan = load_saved_plan(plan_path)
            approved = dict(status='accepted', rollout_approval=str(rollout), plan_sha256=digest(plan_path), channels=[])
            for row in plan['inventory']['channels']:
                if row['role'] == 'timing_only':
                    continue
                review_dir = self.root/'reviews'/plan['inventory']['tape']/f'CH{row["channel"]:02d}'
                render_channel_review(plan_path, row['channel'], review_dir, start=row['start_seconds'],
                    denoised_input=self.cache/plan['inventory']['tape']/f'CH{row["channel"]:02d}', require_prepared=True, progress=lambda _:None)
                approved['channels'].append(dict(channel=row['channel'], speech_preserved=True, transitions_accepted=True,
                    continuous_review=dict(path=str(review_dir/'review.json'), sha256=digest(review_dir/'review.json'))))
            (plan_path.parent/'production_acceptance.json').write_text(json.dumps(approved))
        command = [sys.executable, str(RUNNER), 'run', *self.options, '--all-tapes', '--stages', '05b']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(len(list(self.plans.glob('*/plan.json'))), 2)
        self.assertEqual(len(list(self.cache.glob('*/CH*/denoised.wav'))), 5)
        outputs = sorted(self.final.glob('*/CH*/deflutter.wav'))
        self.assertEqual(len(outputs), 3)
        self.assertFalse(list(self.final.glob('*/CH01')))
        for output in outputs:
            self.assertEqual(sf.info(output).frames, 15*8000)
            review = json.loads((output.parent/'review.json').read_text())
            self.assertEqual(review['status'], 'complete')
            self.assertEqual(review['start_seconds'], 5 if output.parent.name == 'CH04' else 0)
            self.assertTrue(review['denoised_intermediate']['reused'])
        manifest = next(json.loads(p.read_text()) for p in self.runs.glob('*/run.json')
                        if all(j['number'] == '05b' for j in json.loads(p.read_text())['stages']))
        self.assertEqual([j['status'] for j in manifest['stages']], ['done']*3)
        stamps = {p: p.stat().st_mtime_ns for p in outputs}
        again = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(again.returncode, 0)
        self.assertIn('blocked', again.stdout)
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in outputs})
        self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))


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
        self.assertIn('Stage 05b requires --channel', result.stderr)
        self.assertFalse(self.output.exists())


    def test_irig_fails_and_stops(self):
        self.wav.rename(self.source / 'CH1_IRIG.wav')
        result = self.run_pipeline('--stages', '03')
        self.assertNotEqual(result.returncode, 0)
        _, manifest = self.manifest()
        states = {s['number']: s['status'] for s in manifest['stages']}
        self.assertEqual(states['03'], 'failed')

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
        for stage in ('02', '02b', '04', '05', '06'):
            with self.subTest(stage=stage):
                result = self.run_pipeline('--stages', stage)
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_standalone_channel_denoise_without_plan(self):
        from test_continuous_planning import fixture
        original, channel = fixture(self.root, duration=60)
        command = [sys.executable, str(ROOT/'scripts/03_denoise.py'), '--input', str(original),
                   '--channel', str(channel), '--denoised-root', str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        intermediate = self.output/original.name/f'CH{channel:02d}'/'denoised.wav'
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


if __name__ == '__main__':
    unittest.main()
