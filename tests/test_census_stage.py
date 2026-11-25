"""Detection census storage, Proprius/Genus coordination and stage-05 integration."""
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
from _bootstrap import install
install()
import flutter_census as census
import flutter_detection as detection
import pipeline
from census_fixtures import SR, channels, write_tape
from denoise_archive import sha256
from flutter_plan_store import planning_lock
from flutter_planning import inventory
from shared_render import render_shared_channel
from shared_tape import census_candidates, load_tape_plan, read_curve, select_intervals

TAPE = 'A15_T900'
SETTINGS = census.make_settings(block_seconds=60.)
OFFSETS = {5: 2, 6: 3}


def make_tape(root, seconds=150):
    audio, motion = channels(seconds)
    audio[6] = (.004*np.random.default_rng(6).standard_normal(seconds*SR)).astype(np.float32)
    shifted = {ch: x[OFFSETS.get(ch, 0)*SR:] for ch, x in audio.items()}
    write_tape(root/'originals'/TAPE, TAPE, shifted, OFFSETS)
    return root/'originals'/TAPE, motion


def denoised_like(root, folder):
    """Completed v2 manifests over the originals, for matched comparison renders."""
    inv = inventory(folder)
    for row in inv['channels']:
        out = root/'denoised'/TAPE/f'CH{row["channel"]:02d}'
        out.mkdir(parents=True)
        name = f'CH{row["channel"]:02d}.wav'
        x, _ = sf.read(row['path'], dtype='float32')
        sf.write(out/name, x, SR, subtype='FLOAT')
        (out/'cleanup.jsonl').write_text('{}\n')
        info, stat = sf.info(out/name), (out/name).stat()
        (out/'denoised.json').write_text(json.dumps(dict(schema=2, cleanup_version='v2', status='complete',
            algorithm='synthetic-v2', action='denoise_v2', source=row,
            frozen=dict(channel_policy='all_numbered', audio_naming='channel_number'),
            signature=dict(source={k: row[k] for k in ('path', 'bytes', 'mtime_ns', 'frames', 'sample_rate', 'channel')},
                           offset_frames=0, frames=row['frames']), source_frame_interval=[0, row['frames']],
            canonical_start_offset_frames=round(row['start_seconds']*SR), gain=1.,
            audio=dict(filename=name, frames=info.frames, sample_rate=SR, format=info.format,
                       subtype=info.subtype, bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=sha256(out/name)),
            cleanup_log_sha256=sha256(out/'cleanup.jsonl'))), encoding='utf-8')
    return root/'denoised'/TAPE


def genus_factory(scratch, root):
    def command(session, host):
        argv = detection.local_command(session)
        if host == 'genus':
            argv = argv[:-1]+['genus', '--path-map', f'{root.as_posix()}={root.as_posix()}', '--scratch', str(scratch)]
        else:
            argv += ['--scratch', str(scratch)]
        return argv
    return command


def quiet(*_):
    pass


class CensusStageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.folder, self.motion = make_tape(self.root)
        self.run_dir, self.config = detection.open_run(root=self.root/'census', settings=SETTINGS,
                                                       originals=self.root/'originals')
        self.session = detection.Session(self.run_dir, self.config)

    def detect(self, **pool):
        options = dict(local_workers=1, genus_workers=0, worker_factory=genus_factory(self.root/'scratch', self.root),
                       output=quiet)
        options.update(pool)
        return detection.coordinate(self.session, [self.folder], **options)

    def test_block_reader_zero_fills_both_offset_edges_and_absent_channel_time(self):
        row = next(r for r in inventory(self.folder)['channels'] if r['channel'] == 5)
        audio, _ = sf.read(row['path'], dtype='float32')
        for start, end in [(0, 1), (0, 4), (148, 152), (151, 152)]:
            with self.subTest(start=start, end=end):
                read, stats = detection.read_block([row], start, end, SR)
                samples, coverage = read(5)
                expected = np.zeros((end-start)*SR, np.float32)
                lo, hi = max(start, OFFSETS[5]), min(end, 150)
                count = max(0, hi-lo)*SR
                if count:
                    expected[(lo-start)*SR:(hi-start)*SR] = audio[(lo-OFFSETS[5])*SR:(hi-OFFSETS[5])*SR]
                np.testing.assert_array_equal(samples, expected)
                self.assertEqual(coverage, count/len(expected))
                self.assertEqual(stats['bytes'], count*2)

    def test_genus_staging_publishes_once_and_census_only_plan_reaches_offset_channels(self):
        self.assertEqual(self.detect(local_workers=0, genus_workers=1), 0)
        status = detection.read_json(self.session.cluster/'status.json')
        self.assertEqual({v['host'] for v in status['jobs'].values()}, {'genus'})
        self.assertEqual(len(status['jobs']), 3)
        self.assertEqual(sorted(p.name for p in (self.session.cluster/'acks'/TAPE).iterdir()),
                         ['0000.json', '0001.json', '0002.json'])
        self.assertEqual(list((self.session.cluster/'inbox'/TAPE).iterdir()), [])
        self.assertEqual(list((self.root/'scratch').glob('*/*/*/*')), [])
        bundle = detection.load_census(self.run_dir, TAPE)
        coverage = bundle['evidence']['coverage']
        self.assertEqual(coverage['eligible_windows'], coverage['windows'])
        self.assertGreater(coverage['tape_time_fraction'], .9)
        # Census-only stage 05a: no v5 bundle exists for this tape.
        spec = importlib.util.spec_from_file_location(
            'plan_cli', ROOT/'scripts/30-track/05_deflutter/05a_plan_deflutter.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        plans = self.root/'deflutter_planning_census'/self.config['run_id']/TAPE
        with redirect_stdout(io.StringIO()) as text:
            self.assertEqual(cli.main(['--input', str(self.folder), '--evidence-root', str(self.root/'none'),
                                       '--raw-census-root', str(self.run_dir), '--output', str(plans)]), 0)
        self.assertEqual(json.loads(text.getvalue())['census_run_id'], self.config['run_id'])
        _, plan, data = load_tape_plan(plans)
        self.assertTrue(plan['full_tape'])
        self.assertEqual(plan['census']['run_id'], self.config['run_id'])
        self.assertEqual(plan['census']['evidence_sha256'], bundle['evidence_sha256'])
        self.assertEqual(plan['census']['kind'], 'raw_candidates')
        self.assertFalse(plan['qualified'])
        self.assertGreater(plan['map']['census_selected_seconds'], 130)
        # Coverage is the union of supported tape time, never channel-seconds.
        self.assertAlmostEqual(plan['map']['coverage'], plan['map']['supported_seconds']/inventory(self.folder)['duration_seconds'])
        self.assertLessEqual(plan['map']['supported_seconds'], 150)
        del data
        denoised = denoised_like(self.root, self.folder)
        renders = {}
        for ch in (5, 6):  # Offset channels; CH06 has no tones at all.
            renders[ch] = render_shared_channel(plans, ch, denoised/f'CH{ch:02d}', self.root/f'review{ch}',
                                                comparison=True, progress=quiet)
            self.assertEqual(renders[ch]['start_seconds'], OFFSETS[ch])
            self.assertFalse(renders[ch]['per_channel_anchoring'])
        self.assertEqual(renders[5]['map_sha256'], renders[6]['map_sha256'])
        a6, _ = sf.read(self.root/'review6/A.wav')
        b6, _ = sf.read(self.root/'review6/B.wav')
        self.assertGreater(float(np.max(abs(a6-b6))), 1e-4)
        settings = census.make_settings()
        fm = {}
        for name in ('A', 'B'):
            x, _ = sf.read(self.root/f'review5/{name}.wav')
            m = census.track(census.BlockSpectrum(x, SR), 987., settings['tracking'])
            fm[name] = float(m['e'][10*census.RATE:-10*census.RATE].std())
        self.assertLess(fm['B'], .25*fm['A'])

    def test_completed_uploads_and_blocks_are_reused_without_recomputation(self):
        record = detection.prepare_tape(self.run_dir, self.config, self.folder)
        job = detection.block_job(record, record['blocks'][1])
        detection.compute_block(self.run_dir, self.config, job, self.session.inbox(job), host='genus')
        self.assertEqual(self.detect(local_workers=2), 0)
        status = detection.read_json(self.session.cluster/'status.json')
        self.assertEqual(status['jobs'][f'{TAPE}/0001']['recovered'], 'inbox')
        evidence = self.run_dir/TAPE/'evidence.json'
        stamp = evidence.stat().st_mtime_ns
        with patch.object(detection.subprocess, 'Popen', side_effect=AssertionError('recomputed')):
            self.assertEqual(self.detect(), 0)
        self.assertEqual(evidence.stat().st_mtime_ns, stamp)

    def test_changed_sources_code_results_and_streams_are_refused(self):
        self.assertEqual(self.detect(local_workers=2), 0)
        with self.assertRaisesRegex(detection.IdentityError, 'different detector settings'):
            detection.open_run(self.run_dir, settings=census.make_settings(block_seconds=90.),
                               originals=self.root/'originals')
        changed = {k: 'x'*64 for k in self.config['identity']['implementation']}
        with patch.object(detection, 'implementation', return_value=changed), \
                self.assertRaisesRegex(detection.IdentityError, 'implementation changed'):
            self.detect()
        curves = self.run_dir/TAPE/'blocks/0001/curves.npy'
        saved = curves.read_bytes()
        curves.write_bytes(saved[:-4]+b'\0\0\0\0')
        with self.assertRaisesRegex(detection.IdentityError, 'changed'):
            detection.load_census(self.run_dir, TAPE)
        curves.write_bytes(saved)
        stream = self.run_dir/TAPE/'consensus.f64'
        with stream.open('r+b') as handle:
            handle.write(b'\1')
        with self.assertRaisesRegex(detection.IdentityError, 'stream'):
            detection.load_census(self.run_dir, TAPE)
        original = next(self.folder.glob('*CH2_*.wav'))
        with original.open('ab') as handle:
            handle.write(b'xx')
        with self.assertRaisesRegex(detection.IdentityError, 'changed since detection'):
            detection.prepare_tape(self.run_dir, self.config, self.folder)

    def test_incomplete_census_and_unverified_inbox_are_errors(self):
        record = detection.prepare_tape(self.run_dir, self.config, self.folder)
        job = detection.block_job(record, record['blocks'][0])
        inbox = self.session.inbox(job)
        detection.compute_block(self.run_dir, self.config, job, inbox, host='windows')
        result = detection.read_json(inbox/'result.json')
        result['tape'] = 'A15_T901'
        (inbox/'result.json').write_text(json.dumps(result))
        with self.assertRaisesRegex(detection.IdentityError, 'does not match its job'):
            self.detect()
        self.assertFalse(self.session.published(job).exists())
        with self.assertRaisesRegex(detection.IdentityError, 'missing or incomplete'):
            detection.load_census(self.run_dir, TAPE)
        spec = importlib.util.spec_from_file_location(
            'plan_cli2', ROOT/'scripts/30-track/05_deflutter/05a_plan_deflutter.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['--input', str(self.folder), '--evidence-root', str(self.root/'none'),
                      '--raw-census-root', str(self.run_dir), '--output', str(self.root/'plans'/TAPE)])
        with redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['--input', str(self.folder), '--evidence-root', str(self.root/'none'),
                      '--output', str(self.root/'plans'/TAPE)])
        self.assertFalse((self.root/'plans'/TAPE).exists())

    def test_saved_spans_keep_priority_and_stream_round_trips(self):
        self.assertEqual(self.detect(local_workers=2), 0)
        inv = inventory(self.folder)
        from deflutter_readiness import readiness
        row = readiness(self.root/'originals', self.root/'plans', self.root/'denoised', self.root/'none',
                        self.run_dir)['tapes'][0]
        self.assertTrue(row['census_complete'])
        self.assertGreater(row['census_coverage'], .9)
        candidate, bindings, provenance = census_candidates(self.run_dir, inv)
        self.assertEqual(candidate['priority'], 2)
        self.assertEqual(provenance['run_id'], self.config['run_id'])
        times = np.arange(0, 150*census.RATE)/census.RATE
        speed, support = read_curve(candidate, times)
        raw = np.fromfile(self.run_dir/TAPE/'consensus.f64', dtype='<f8').reshape(-1, 2)
        np.testing.assert_array_equal(speed, raw[:len(times), 0])
        np.testing.assert_array_equal(support, raw[:len(times), 1])
        expected = np.interp(times, np.arange(len(self.motion[::SR//census.RATE]))/census.RATE,
                             self.motion[::SR//census.RATE])
        covered = support == 1
        self.assertGreater(census.correlation(speed[covered], expected[covered]), .97)
        saved = self.root/'v5.f64'
        values = np.column_stack((.002*np.sin(2*np.pi*5*times), (times >= 20) & (times < 40)))
        values[:, 0] *= values[:, 1]
        values.astype('<f8').tofile(saved)
        v5 = dict(id='v5', source_key='v5', priority=1, rank=1., start_seconds=0, end_seconds=150,
                  curve=dict(format='stream', path=str(saved), rate=200, frames=len(times), start_seconds=0, dtype='<f8'))
        chosen = select_intervals([candidate, v5], 0, 150)
        inside = [s for s in chosen if s['start_seconds'] < 39 and s['end_seconds'] > 21]
        self.assertEqual([s['selected'] for s in inside], ['v5'])
        self.assertTrue(any(s['selected'] == candidate['id'] for s in chosen if s['end_seconds'] <= 20))

    def test_remote_worker_command_and_startup_identity_checks(self):
        spec = importlib.util.spec_from_file_location(
            'detect_cli', ROOT/'scripts/30-track/05_deflutter/05_detect_flutter.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, 'open_selected', return_value=(self.run_dir, self.config)), \
             patch.object(detection, 'coordinate', return_value=0) as coordinate, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['run', '--originals', str(self.folder.parent), '--tape', TAPE, '--plain']), 0)
        session = detection.Session(detection.DEFAULT_DETECTION_ROOT/'census-0123456789abcdef', self.config)
        connection = dict(detection.GENUS, **coordinate.call_args.kwargs['connection'])
        command = detection.remote_command(session, connection, '0,2,4,8,10,12')
        self.assertEqual(command[:2], ['ssh', '-o'])
        self.assertIn('bfeist@192.168.0.3', command)
        remote = shlex.split(command[-1])
        self.assertEqual(remote[0], 'exec')
        self.assertIn('OMP_NUM_THREADS=1', remote)
        self.assertEqual(remote[remote.index('taskset')+1:remote.index('taskset')+3], ['-c', '0,2,4,8,10,12'])
        self.assertEqual(remote[remote.index('--python')+1], detection.GENUS['python'])
        self.assertIn('--no-project', remote)
        self.assertEqual(remote[remote.index('--run-dir')+1],
                         '/mnt/f/Apollo_15_30-Track/deflutter_detection_census/census-0123456789abcdef')
        self.assertEqual(remote[remote.index('--path-map')+1], 'D:/Apollo_15_30-Track=/mnt/d/Apollo_15_30-Track')
        self.assertIn('F:/tempF=/mnt/f/tempF', remote)
        self.assertEqual(remote[remote.index('--scratch')+1], '/home/bfeist/.local/share/apollo15-census')
        uv = remote.index(detection.GENUS['uv'])
        self.assertEqual(remote[uv+1:uv+3], ['run', '--no-project'])
        self.assertLess(remote.index('taskset'), uv)
        hello = dict(implementation=self.config['identity']['implementation'], environment=detection.versions(),
                     threads={v: '1' for v in detection.THREAD_VARIABLES}, affinity=[0, 2, 4])
        worker = detection.Worker('genus-0', 'genus', None, None)
        detection.check_hello(worker, hello, self.config, '0,2,4')
        for change, message in [(dict(affinity=[0, 1, 2]), 'affinity'),
                                (dict(threads={'OMP_NUM_THREADS': '8'}), 'one thread'),
                                (dict(environment=dict(hello['environment'], numpy='0.0')), 'numpy')]:
            with self.subTest(change=change), self.assertRaisesRegex(detection.IdentityError, message):
                detection.check_hello(worker, dict(hello, **change), self.config, '0,2,4')
        for pool, message in [(dict(local_workers=17), 'developer headroom'),
                              (dict(local_workers=0, genus_workers=4, genus_cpus='0,2'), 'CPU mask'),
                              (dict(local_workers=0, genus_workers=0), 'at least one')]:
            with self.subTest(pool=pool), self.assertRaisesRegex(ValueError, message):
                self.detect(**pool)
        with planning_lock(self.session.cluster), self.assertRaisesRegex(ValueError, 'Another planner'):
            self.detect()

    def test_benchmark_trials_are_isolated_and_equivalent_across_hosts(self):
        keys = [f'{TAPE}/0000', f'{TAPE}/0002']
        factory = genus_factory(self.root/'scratch', self.root)
        first = detection.benchmark(self.run_dir, self.config, [self.folder], 'windows-1', keys,
                                    local_workers=1, genus_workers=0, worker_factory=factory, output=quiet)
        second = detection.benchmark(self.run_dir, self.config, [self.folder], 'genus-1', keys,
                                     local_workers=0, genus_workers=2, worker_factory=factory, stage_inputs=True,
                                     stage_root=self.root/'ssd', output=quiet)
        self.assertEqual(len(second['source_staging']), 1)
        self.assertEqual(second['source_staging'][0]['bytes'], sum(p.stat().st_size for p in self.folder.glob('*.wav')))
        self.assertTrue(all(b['execution']['input_mode'] == 'ssd' for b in second['per_block']))
        self.assertFalse(list((self.root/'ssd').rglob('*.wav')))
        self.assertEqual(list((self.root/'scratch').glob('*/*/*/*')), [])
        self.assertEqual((first['completed'], second['completed']), (2, 2))
        self.assertGreater(first['unique_tape_hours_per_wall_hour'], 0)
        self.assertFalse((self.run_dir/TAPE/'blocks').exists())
        self.assertFalse((self.run_dir/TAPE/'evidence.json').exists())
        result = detection.compare_trials(self.run_dir, self.config, 'windows-1', 'genus-1')
        self.assertTrue(result['equivalent'], result)
        self.assertEqual(result['compared'], 2)
        self.assertEqual({tuple(r['hosts']) for r in result['blocks']}, {('windows', 'genus')})
        with self.assertRaisesRegex(ValueError, 'already exists'):
            detection.benchmark(self.run_dir, self.config, [self.folder], 'genus-1', keys,
                                local_workers=1, genus_workers=0, output=quiet)

    def test_staging_rotates_tapes_and_reuses_completed_blocks_on_resume(self):
        from census_monitor import CensusMonitor
        from rich.console import Console
        audio, _ = channels(70, count=2)
        other = self.root/'originals'/'A15_T901'
        write_tape(other, other.name, audio)
        folders = [self.folder, other]
        stage_root = self.root/'ssd'
        monitor = CensusMonitor(self.run_dir)
        options = dict(local_workers=2, genus_workers=1, stage_inputs=True, stage_root=stage_root,
                       worker_factory=genus_factory(self.root/'scratch', self.root),
                       output=monitor.message, on_status=monitor.update)
        self.assertEqual(detection.coordinate(self.session, folders, **options), 0)
        state = detection.read_json(self.session.cluster/'status.json')
        self.assertEqual([s['tape'] for s in state['source_staging']], [f.name for f in folders])
        self.assertTrue(all(j['execution']['input_mode'] == 'ssd' for j in state['jobs'].values()))
        self.assertFalse(list(stage_root.rglob('*.wav')))
        display = io.StringIO()
        Console(file=display, width=110).print(monitor)
        self.assertIn('2/2 tapes', display.getvalue())
        self.assertIn('Failures 0', display.getvalue())
        self.assertIn('Proprius', display.getvalue())
        self.assertIn('Genus', display.getvalue())
        with patch.object(detection.SourceStage, 'load', side_effect=AssertionError('unnecessary copy')):
            self.assertEqual(detection.coordinate(self.session, folders, **options), 0)

    def test_staging_refuses_changed_source_and_releases_partial_tape(self):
        record = detection.prepare_tape(self.run_dir, self.config, self.folder)
        row = record['inventory']['channels'][1]
        row['mtime_ns'] -= 1
        with patch.object(detection, 'prepare_tape', return_value=record):
            self.assertEqual(self.detect(stage_inputs=True, stage_root=self.root/'ssd'), 2)
        state = detection.read_json(self.session.cluster/'status.json')
        self.assertIn('Original changed since inventory', state['failures'][0]['message'])
        self.assertFalse(list((self.root/'ssd').rglob('*.wav')))
        self.assertFalse((self.run_dir/TAPE/'blocks').exists())

    def test_tape_of_identical_channel_files_is_flagged_and_skipped(self):
        audio, _ = channels(70, count=1)
        copy = self.root/'originals'/'A15_T901'
        write_tape(copy, 'A15_T901', {ch: audio[1] for ch in (1, 2, 3)})
        messages = []
        code = detection.coordinate(self.session, [copy], local_workers=1, genus_workers=0, output=messages.append)
        self.assertEqual(code, 0)
        self.assertTrue(any('identical' in m for m in messages))
        audit = detection.read_json(self.run_dir/'A15_T901/inventory.json')['duplicate_audit']
        self.assertEqual(audit['duplicate_groups'], [[1, 2, 3]])
        self.assertFalse((self.run_dir/'A15_T901/evidence.json').exists())

    def test_block_plan_covers_tape_with_halos_and_whole_windows(self):
        settings = census.make_settings()
        blocks = detection.block_plan(1234.5, settings)
        self.assertEqual([b['core_start'] for b in blocks], [0., 600., 1200.])
        self.assertEqual(blocks[-1]['core_end'], 1234.5)
        self.assertEqual(blocks[1]['read_start'], 592.)
        self.assertEqual(blocks[1]['read_end'], 1208.)
        self.assertEqual(blocks[-1]['read_end'], 1234.5)
        self.assertGreaterEqual(blocks[-1]['read_end']-blocks[-1]['read_start'], 64)
        self.assertEqual(detection.block_plan(30., settings)[0]['read_start'], 0.)
        self.assertEqual(census.window_starts(0., 30., settings), [])


class CensusPipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for tape in ('A15_T001', 'A15_T002'):
            write_tape(self.root/'originals'/tape, tape, {1: np.zeros(20*SR, np.float32), 2: np.zeros(20*SR, np.float32)})
        self.census = self.root/'deflutter_detection_census/census-0123456789abcdef'
        self.analysis = self.root/'deflutter_analysis/census-0123456789abcdef/analysis-0123456789abcdef'
        self.options = ['--concatenated', str(self.root/'originals'), '--output', str(self.root/'runs'), '--plain',
                        '--flutter-detection-root', str(self.census), '--flutter-qualification-root', str(self.analysis)]

    def jobs(self, *selection):
        args, selected = pipeline.parse_args(['plan', *self.options, *selection])
        return [j for j in pipeline.make_jobs(args, selected, self.root/'runs/plan') if j['status'] == 'pending']

    def test_detection_and_planning_forwarding_single_tape_and_batch(self):
        single = self.jobs('--tape', 'A15_T001', '--stages', '05,05a', '--flutter-detect-genus-workers', '4',
                           '--flutter-detect-local-workers', '8', '--flutter-detect-input-mode', 'ssd')
        self.assertEqual([j['number'] for j in single], ['05', '05a'])
        detect, plan = single[0]['command'], single[1]['command']
        self.assertEqual(Path(detect[2]).name, '05_detect_flutter.py')
        self.assertEqual(detect[3], 'run')
        self.assertEqual(detect[detect.index('--run-dir')+1], str(self.census))
        self.assertEqual(detect[detect.index('--tape')+1], 'A15_T001')
        self.assertEqual(detect[detect.index('--genus-workers')+1], '4')
        self.assertEqual(detect[detect.index('--local-workers')+1], '8')
        self.assertEqual(detect[detect.index('--input-mode')+1], 'ssd')
        self.assertEqual(plan[plan.index('--qualification-root')+1], str(self.analysis))
        self.assertNotIn('--detection-root', plan)
        batch = self.jobs('--all-tapes', '--stages', '05a')
        self.assertEqual(len(batch), 2)
        self.assertTrue(all(j['command'][j['command'].index('--qualification-root')+1] == str(self.analysis)
                            for j in batch))
        once = self.jobs('--all-tapes', '--stages', '05', '--exclude-tape', 'A15_T002')
        self.assertEqual(len(once), 1)
        self.assertIn('--all-tapes', once[0]['command'])
        self.assertIn('--exclude-tape=A15_T002', once[0]['command'])
        self.assertNotIn('--tape', once[0]['command'])
        census_plans = self.root/'deflutter_planning_census'/self.census.name
        render = self.jobs('--tape', 'A15_T001', '--channel', '2', '--stages', '05b',
                           '--flutter-planning-root', str(census_plans),
                           '--defluttered-root', str(self.root/'defluttered_census'/self.census.name))[0]['command']
        self.assertEqual(render[render.index('--flutter-plan')+1], str(census_plans/'A15_T001/plan.json'))
        self.assertIn('--crosstalk-input', render)
        for extra in (('--tape', 'A15_T001', '--stages', '05', '--channel', '2'),
                      ('--tape', 'A15_T001', '--stages', '05', '--flutter-detect-local-workers', '17'),
                      ('--all-tapes', '--stages', '05,05a')):
            with self.subTest(extra=extra), redirect_stdout(io.StringIO()), \
                    patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                pipeline.parse_args(['plan', *self.options, *extra])


if __name__ == '__main__':
    unittest.main()
