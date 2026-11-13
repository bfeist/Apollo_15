"""Shared-map contract checks on synthetic files; no archive analysis or listening trials."""
import json
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install
install()
from denoise_archive import sha256
from flutter_planning import inventory
from shared_tape import build_tape_plan, compile_map, load_tape_plan, select_intervals
from shared_render import render_shared_channel
from inspect_tone import inspect_tone


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def fixture(root, tape='A15_T001'):
    original = root/'originals'/tape
    original.mkdir(parents=True)
    audio = (.1*np.sin(2*np.pi*431*np.arange(60*8000)/8000)).astype('float32')
    for ch, offset in [(1, 0), (2, 0), (20, 1)]:
        role = 'IRIG' if ch == 1 else 'VOICE'
        sf.write(original/f'{tape}_HR1U_CH{ch}_{role}_207-00-00-0{offset}_207-00-01-00.wav',
                 audio[offset*8000:], 8000)
    inv = inventory(original)
    for row in inv['channels']:
        folder = root/'denoised'/tape/f'CH{row["channel"]:02d}'
        folder.mkdir(parents=True)
        name = f'CH{row["channel"]:02d}.wav'
        sf.write(folder/name, audio[round(row['start_seconds']*8000):], 8000, subtype='FLOAT')
        (folder/'cleanup.jsonl').write_text('{}\n')
        info, stat = sf.info(folder/name), (folder/name).stat()
        write(folder/'denoised.json', dict(schema=2, cleanup_version='v2', status='complete',
            algorithm='synthetic-v2', action='denoise_v2', source=row,
            frozen=dict(channel_policy='all_numbered', audio_naming='channel_number'),
            signature=dict(source={k: row[k] for k in ('path','bytes','mtime_ns','frames','sample_rate','channel')},
                offset_frames=0, frames=row['frames']), source_frame_interval=[0, row['frames']],
            canonical_start_offset_frames=round(row['start_seconds']*8000), gain=1.,
            audio=dict(filename=name, frames=info.frames, sample_rate=8000, format=info.format,
                subtype=info.subtype, bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=sha256(folder/name)),
            cleanup_log_sha256=sha256(folder/'cleanup.jsonl')))
    bundle = root/'evidence'/tape
    folder = bundle/'plan'
    folder.mkdir(parents=True)
    times = np.arange(60*200)/200
    values = np.column_stack((.003*np.sin(2*np.pi*3*times), np.ones(len(times))))
    values.astype('<f8').tofile(folder/'s0.f64')
    use = [dict(start_seconds=a, end_seconds=b, action='use_reference', selected='s0')
           for a, b in [(10, 20), (30, 40), (40, 50)]]
    detail = dict(id='continuous', kind='continuous_reference', status='analyzed',
        start_seconds=0, end_seconds=60, analysis_start=0, analysis_end=60,
        targets=[dict(channel=2, reference_spans=use), dict(channel=20, reference_spans=[])],
        variants=[dict(key='s0', rank=.9, reference_channel=1, carrier_hz=1000., method='saved-tone')],
        maps=dict(rate=200, streams=dict(s0=dict(file='s0.f64', sha256=sha256(folder/'s0.f64'),
            frames=len(times), start_seconds=0, dtype='<f8'))))
    write(folder/'continuous.json', detail)
    plan = dict(schema=2, kind='continuous_reference', complete=True, full_tape=True,
        source_validation='verified', inventory=inv, parameters=dict(start=0),
        regions=[dict(detail, detail_file='continuous.json', detail_sha256=sha256(folder/'continuous.json'))])
    write(folder/'plan.json', plan)
    checks = [dict(start_seconds=s['start_seconds'], end_seconds=s['end_seconds'],
        outcome=outcome, event_metrics=[]) for s, outcome in zip(use,
        ['full_restoration_at_measured_events', 'insufficient_evidence', 'worsening'])]
    write(bundle/'joined_assessment.json', dict(plan_sha256=sha256(folder/'plan.json'),
        channels=[dict(channel=2, spans=checks), dict(channel=20, spans=[])]))
    write(bundle/'survey_manifest.json', dict(inventory=inv, windows=[dict(start_seconds=0,
        end_seconds=60, channels={'1': dict(status='analyzed', candidates=[dict(carrier_hz=1000.,
        tracking_status='measured', support_seconds=list(range(60)))])})]))
    return folder/'plan.json', bundle/'joined_assessment.json', inv


class SharedTapeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.evidence, self.assessment, self.inv = fixture(self.root)
        self.destination = self.root/'shared'

    def build(self):
        return build_tape_plan(self.evidence, self.assessment, self.destination)

    def test_all_channel_union_full_restoration_and_rejected_evidence(self):
        plan = self.build()
        self.assertEqual(plan['map']['eligible_seconds'], 20)
        self.assertTrue(plan['full_tape'])
        _, _, data = load_tape_plan(self.destination)
        self.assertTrue(data[:, 1].flags.c_contiguous)
        self.assertTrue(np.any(abs(data[(data[:, 0] > 12) & (data[:, 0] < 18), 2]) > .001))
        self.assertTrue(np.all(data[(data[:, 0] > 41) & (data[:, 0] < 49), 2] == 0))
        self.assertEqual(data[0, 0], data[0, 1])
        self.assertEqual(data[-1, 0], data[-1, 1])
        self.assertTrue(np.all(np.diff(data[:, 1]) > 0))
        del data
        evidence = json.loads((self.destination/'evidence.json').read_text())
        self.assertEqual(evidence['rejected'][0]['reason'], 'worsening')

    def test_one_map_for_offset_track_without_tones_and_exact_excerpt(self):
        self.build()
        reviews = []
        for ch in [2, 20]:
            review = render_shared_channel(self.destination, ch,
                self.root/'denoised/A15_T001'/f'CH{ch:02d}', self.root/f'review{ch}',
                comparison=True, progress=lambda _: None)
            reviews.append(review)
        self.assertEqual(reviews[0]['map_sha256'], reviews[1]['map_sha256'])
        self.assertEqual(reviews[1]['start_seconds'], 1)
        self.assertFalse(reviews[1]['per_channel_anchoring'])
        full, sr = sf.read(self.root/'review20/B.wav', dtype='float32')
        other, _ = sf.read(self.root/'review2/B.wav', dtype='float32')
        np.testing.assert_allclose(full, other[sr:], atol=2e-6, rtol=0)
        base, _ = sf.read(self.root/'review20/A.wav', dtype='float32')
        self.assertGreater(float(np.max(abs(full-base))), .001)
        render_shared_channel(self.destination, 20, self.root/'denoised/A15_T001/CH20',
            self.root/'excerpt', start=12, duration=5, comparison=True, progress=lambda _: None)
        crop, _ = sf.read(self.root/'excerpt/B.wav', dtype='float32')
        np.testing.assert_array_equal(crop, full[11*sr:16*sr])
        self.assertFalse((self.root/'excerpt/index.html').exists())
        from quindar_tape import differential_displacement
        downstream = differential_displacement([dict(channel=ch,
            path=str(self.root/f'review{ch}/B.wav')) for ch in (2, 20)])
        self.assertTrue(downstream['synchronized_channels'])
        self.assertEqual(downstream['maximum_displacement_difference_seconds'], 0)

    def test_saved_witnesses_are_union_coverage_and_never_votes(self):
        source = json.loads(self.evidence.read_text())
        detail = json.loads((self.evidence.parent/'continuous.json').read_text())
        detail['targets'][1]['reference_spans'] = detail['targets'][0]['reference_spans'][:2]
        write(self.evidence.parent/'continuous.json', detail)
        source['regions'] = [dict(detail, detail_file='continuous.json',
                                 detail_sha256=sha256(self.evidence.parent/'continuous.json'))]
        write(self.evidence, source)
        assessment = json.loads(self.assessment.read_text())
        assessment['plan_sha256'] = sha256(self.evidence)
        assessment['channels'][1]['spans'] = assessment['channels'][0]['spans'][:2]
        write(self.assessment, assessment)
        self.assertEqual(self.build()['map']['eligible_seconds'], 20)
        choices = [dict(id='one', source_key='one', start_seconds=0, end_seconds=10,
                        priority=1, rank=.9, witnesses=[1]),
                   dict(id='many', source_key='many', start_seconds=0, end_seconds=10,
                        priority=1, rank=.8, witnesses=list(range(2, 30)))]
        for choice in choices:
            choice['curve'] = dict(format='stream', path=str(self.evidence.parent/'s0.f64'),
                rate=200, frames=12000, start_seconds=0, dtype='<f8')
        self.assertEqual(select_intervals(choices, 0, 10)[0]['selected'], 'one')

    def test_support_gaps_use_fallback_then_bypass_and_recover(self):
        times = np.arange(20*200)/200
        candidates = []
        for key, priority in [('preferred', 0), ('fallback', 1)]:
            support = np.ones(len(times))
            support[(times >= 8) & (times < 12)] = 0 if priority == 0 else 1
            support[(times >= 15) & (times < 17)] = .9  # Below the existing usable-support threshold.
            speed = .003*np.sin(2*np.pi*3*times)*support
            path = self.root/(key+('.npz' if priority == 0 else '.f64'))
            if priority == 0:
                np.savez(path, curve=speed, curve__support=support)
            else:
                np.column_stack((speed, support)).astype('<f8').tofile(path)
            candidates.append(dict(id=key, source_key=key, priority=priority, rank=1,
                start_seconds=0, end_seconds=20,
                curve=dict(format='npz' if priority == 0 else 'stream', path=str(path),
                    key='curve', rate=200, frames=len(times), start_seconds=0, dtype='<f8')))
        expected = [(0, 8, 'preferred'), (8, 12, 'fallback'), (12, 15, 'preferred'),
                    (15, 17, None), (17, 20, 'preferred')]
        selections = select_intervals(candidates, 0, 20)
        self.assertEqual([(s['start_seconds'], s['end_seconds'], s['selected']) for s in selections], expected)
        self.assertEqual(select_intervals(candidates[::-1], 0, 20), selections)
        # The same fallback applies between references of equal source priority.
        candidates[0].update(priority=1, rank=2)
        self.assertEqual(select_intervals(candidates, 0, 20), selections)
        stats = compile_map(self.root/'map.npy', candidates, selections, 0, 20)
        data = np.load(self.root/'map.npy')
        self.assertEqual(stats['eligible_seconds'], 18)
        self.assertGreater(np.max(abs(data[1800:2200, 2])), .002)
        self.assertTrue(np.all(data[3000:3400, 2:] == 0))
        self.assertTrue(np.all(np.diff(data[:, 1]) > 0))
        self.assertEqual(data[0, 0], data[0, 1])
        self.assertEqual(data[-1, 0], data[-1, 1])
        # Support/selection edges fade instead of introducing a speed jump.
        self.assertLess(np.max(abs(np.diff(data[:, 2]))), .0004)

    def test_cli_discovers_completed_audits_and_imports_them_once(self):
        folder = self.root/'deflutter_planning/A15_T001'
        audit = folder/'audits/start_20_duration_10'
        audit.mkdir(parents=True)
        times = np.arange(10*200)/200
        np.savez(audit/'map.npz', ref=.003*np.sin(2*np.pi*3*times), ref__support=np.ones(len(times)))
        detail = dict(id='regional', status='analyzed', start_seconds=20, end_seconds=30,
            analysis_start=20, analysis_end=30,
            targets=[dict(channel=2, action='render_and_review', selected='ref')],
            variants=[dict(key='ref', reference_channel=1, reference_rank=1, tracking=dict(support=1))],
            maps=dict(file='map.npz', sha256=sha256(audit/'map.npz'), rate=200))
        write(audit/'detail.json', detail)
        write(audit/'plan.json', dict(schema=1, complete=True, full_tape=False,
            source_validation='verified', inventory=self.inv,
            regions=[dict(detail, detail_file='detail.json', detail_sha256=sha256(audit/'detail.json'))]))
        write(folder/'plan.json', dict(schema=1, complete=False))
        unfinished = folder/'audits/unfinished'
        unfinished.mkdir()
        write(unfinished/'plan.json', dict(schema=1, complete=False))
        spec = importlib.util.spec_from_file_location('shared_planner_cli',
            Path(__file__).resolve().parents[1]/'scripts/05_deflutter/05a_plan_deflutter.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with redirect_stdout(io.StringIO()):
            result = cli.main(['--input', str(self.root/'originals/A15_T001'),
                '--evidence-root', str(self.root/'evidence'), '--output', str(self.destination),
                '--regional-plan', str(audit)])
        self.assertEqual(result, 0)
        _, plan, data = load_tape_plan(self.destination)
        self.assertEqual(plan['map']['eligible_seconds'], 30)
        self.assertGreater(np.max(abs(data[4200:5800, 2])), .002)
        self.assertEqual(sum(s['path'] == str((audit/'plan.json').resolve()) for s in plan['sources']), 1)
        del data

    def test_old_plan_and_changed_original_cannot_render(self):
        with self.assertRaisesRegex(ValueError, 'completed shared tape map'):
            render_shared_channel(self.evidence, 2, self.root/'denoised/A15_T001/CH02', self.root/'old')
        self.build()
        original = Path(self.inv['channels'][0]['path'])
        with original.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'Original source identity changed'):
            load_tape_plan(self.destination)

    def test_identity_bypass_and_existing_audio_remain_immutable(self):
        plan = self.build()
        render_shared_channel(self.destination, 2, self.root/'denoised/A15_T001/CH02',
            self.root/'bypass', start=22, duration=5, comparison=True, progress=lambda _: None)
        a, _ = sf.read(self.root/'bypass/A.wav', dtype='float32')
        b, _ = sf.read(self.root/'bypass/B.wav', dtype='float32')
        # Bypass keeps any constant displacement from earlier correction;
        # here the synthetic zero-mean correction returns to the same coordinate.
        np.testing.assert_array_equal(a, b)
        before = (self.root/'bypass/B.wav').read_bytes()
        with self.assertRaisesRegex(ValueError, 'destination must be new'):
            render_shared_channel(self.destination, 2, self.root/'denoised/A15_T001/CH02', self.root/'bypass')
        self.assertEqual(before, (self.root/'bypass/B.wav').read_bytes())
        self.assertEqual(self.build(), plan)

    def test_tampered_map_and_old_cleanup_are_rejected(self):
        self.build()
        manifest = self.root/'denoised/A15_T001/CH20/denoised.json'
        saved = json.loads(manifest.read_text())
        saved['cleanup_version'] = 'v1'
        write(manifest, saved)
        with self.assertRaises(ValueError):
            render_shared_channel(self.destination, 20, manifest.parent, self.root/'rejected')
        with (self.destination/'tape_map.npy').open('ab') as handle:
            handle.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            load_tape_plan(self.destination)
        self.assertFalse((self.root/'rejected').exists())

    def test_sighting_uses_channel_local_offset_and_saved_discovery(self):
        self.build()
        result = inspect_tone(self.evidence.parent.parent, 1, 15, 1000,
            plan_path=self.destination)
        self.assertEqual(result['discovery_status'], 'found')
        self.assertEqual(result['eligible_evidence'][0]['reference_channel'], 1)
        self.assertIsNotNone(result['tape_selection']['selected'])
        self.assertFalse(result['new_audio_analysis'])
        local = inspect_tone(self.evidence.parent.parent, 20, 14, 2000, local_time=True)
        self.assertEqual(local['tape_seconds'], 15)


if __name__ == '__main__':
    unittest.main()
