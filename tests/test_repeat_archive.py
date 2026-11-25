"""Archive discovery must establish two target occurrences and an independent donor path."""
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/30-track/04_crosstalk'))
from repeat_scan import RATE, scan
from repeat_validate import matched_level_db, validate
from repeat_catalog import direction_for, intervals
import repeat_archive
import repeat_cluster
import repeat_render
from tests.test_shared_tape import fixture


class RepeatDiscoveryTests(unittest.TestCase):
    def trial(self, direction, *, add_echo=True, corrupt_donor=False, capture_delay=0):
        rng = np.random.default_rng(45)
        size = 14*RATE
        donor = signal.sosfilt(signal.butter(3, [390, 2600], btype='band', fs=RATE, output='sos'),
                               rng.normal(0, .12, size))
        donor[:5*RATE] = donor[6*RATE:] = 0
        main = np.roll(donor, 231)
        shift = 10829 if direction == 'later' else -10829
        echo = -.30*np.roll(main, shift)
        target = main + rng.normal(0, .001, size) + (echo if add_echo else 0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Donor WAV begins a second later on the same tape clock.
            rows = []
            for ch, offset, audio in [(1, 0, target), (2, 1, donor[RATE:])]:
                path = root/f'CH{ch:02d}.wav'
                if corrupt_donor and ch == 2:
                    audio = rng.normal(0, .1, len(audio))
                if ch == 2 and capture_delay:
                    audio = np.r_[np.zeros(capture_delay), audio]
                sf.write(path, audio, RATE, subtype='FLOAT')
                rows.append(dict(row=dict(channel=ch, start_seconds=offset), name=path.name,
                                 audio=dict(sha256=str(ch))))
            spec = dict(tape='test', channels=rows)
            if capture_delay:
                spec['clock_shift_frames'] = {'1': 0, '2': -capture_delay}
            (root/'sources.json').write_text(json.dumps(spec))
            hits = scan(root/'CH01.wav')
            np.save(root/'hits.npy', hits, allow_pickle=False)
            return validate(root/'CH01.wav', root/'hits.npy')

    def test_earlier_and_later_inverted_repeats_and_channel_offset(self):
        for direction in ('earlier', 'later'):
            with self.subTest(direction=direction):
                result = self.trial(direction)
                self.assertTrue(result['events'])
                for event in result['events']:
                    self.assertAlmostEqual(event['later']-event['early'], 10829/RATE, places=3)
                    self.assertEqual(event['donor'], 2)
                    self.assertEqual(event['later_db'] > 0, direction == 'earlier')
                    main = event['late_path' if direction == 'earlier' else 'early_path']
                    self.assertAlmostEqual(main, -231/RATE, places=3)

    def test_shared_main_alone_and_wrong_donor_bypass(self):
        self.assertFalse(self.trial('earlier', add_echo=False)['events'])
        self.assertFalse(self.trial('later', corrupt_donor=True)['events'])

    def test_capture_delay_recenters_donor_but_preserves_both_target_occurrences(self):
        baseline = self.trial('later')['events']
        shifted = self.trial('later', capture_delay=7*RATE)['events']
        self.assertTrue(baseline)
        self.assertEqual(len(baseline), len(shifted))
        for before, after in zip(baseline, shifted):
            self.assertEqual(after.pop('donor_center_shift_frames'), 7*RATE)
            self.assertEqual(before, after)

    def test_direction_requires_separate_minutes_and_preserves_prior(self):
        events = [dict(early=i, later_db=10) for i in [1, 2, 3, 4]]
        self.assertIsNone(direction_for(events, None))
        events += [dict(early=65, later_db=12), dict(early=125, later_db=9)]
        self.assertEqual(direction_for(events, None)['remove'], 'earlier')
        events.append(dict(early=185, later_db=-10))
        self.assertIsNone(direction_for(events, None))
        prior = dict(remove='later', evidence='known phrase')
        self.assertEqual(direction_for(events, prior), prior)

    def test_overlapping_voice_does_not_reverse_matched_relative_level(self):
        rng = np.random.default_rng(360)
        reference, overlap = rng.normal(size=(2,2000))
        overlap -= reference*np.dot(overlap, reference)/np.dot(reference, reference)
        early, late = -.2*reference+2*overlap, reference
        self.assertGreater(np.linalg.norm(early), np.linalg.norm(late))
        self.assertAlmostEqual(matched_level_db(early,late,reference,reference), 20*np.log10(5))

    def test_never_apply_over_another_measured_main_or_reserved_phrase(self):
        events = [dict(early=t, later=t+1.375, donor=2, early_path=.01, late_path=-1.365)
                  for t in [10., 10.25, 11.375, 14.]]
        selected = intervals(events, 'earlier', [(14*RATE, 15*RATE)])
        self.assertEqual([(g['a']/RATE,g['b']/RATE) for g in selected], [(10.,10.5)])

    @unittest.skipUnless(os.name == 'nt', 'Windows owns archive publication leases')
    def test_archive_publication_and_resume(self):
        from argparse import Namespace
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            _, _, inv = fixture(root)
            stack.enter_context(patch.object(repeat_archive, 'capture_coordinates', return_value={}))
            for p in (root/'denoised').rglob('denoised.json'):
                saved = json.loads(p.read_bytes())
                wav = sf.read(p.parent/saved['audio']['filename'], dtype='float32')[0]
                saved['audio']['decoded_float32_sha256'] = hashlib.sha256(wav.tobytes()).hexdigest()
                saved['audio']['peak'] = float(np.max(abs(wav)))
                p.write_text(json.dumps(saved))
            for module in (repeat_archive, repeat_render):
                for key, folder in [('CANONICAL','denoised'), ('ORIGINALS','originals'), ('SSD','ssd')]:
                    stack.enter_context(patch.object(module, key, root/folder))
            stack.enter_context(patch.object(repeat_archive, 'DATA', root/'detect'))
            opts = Namespace(run='fixture', local=2, genus=0, retain=False)
            repeat_archive.archive(opts, [inv['tape']])
            summary = json.loads((root/'detect/fixture/complete.json').read_bytes())
            self.assertEqual((summary['channels'], summary['applied_seconds']), (3, 0))
            wav = Path(summary['output_root'])/inv['tape']/'CH20/crosstalk.wav'
            source = root/'denoised'/inv['tape']/'CH20/CH20.wav'
            self.assertEqual(wav.read_bytes(), source.read_bytes())
            self.assertFalse(wav.samefile(source))
            stamp = wav.stat().st_mtime_ns
            repeat_archive.archive(opts, [inv['tape']])
            self.assertEqual(wav.stat().st_mtime_ns, stamp)

    def test_cluster_runs_multiple_staged_tapes_and_reuses_sealed_results(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            tapes = ['A15_T001','A15_T002']
            stack.enter_context(patch.object(repeat_archive, 'capture_coordinates', return_value={}))
            for name in tapes:
                fixture(root, tape=name)
            for key, folder in [('CANONICAL','denoised'), ('ORIGINALS','originals')]:
                stack.enter_context(patch.object(repeat_archive, key, root/folder))
            stack.enter_context(patch.object(repeat_cluster, 'SSD', root/'ssd'))
            base = root/'detect'/'fixture'
            render = stack.enter_context(patch.object(repeat_cluster, 'render_archive'))
            repeat_cluster.run(base, tapes, local=2, genus=0, tape_limit=2)
            render.assert_called_once_with(base, tapes, 2)
            for tape in tapes:
                self.assertTrue(repeat_cluster.complete(base/tape))
                self.assertFalse((root/'ssd'/'fixture'/tape).exists())
            with patch.object(repeat_cluster, 'stage_tape', side_effect=AssertionError('rewrote sealed tape')):
                repeat_cluster.run(base, tapes, local=2, genus=0, tape_limit=2)


if __name__ == '__main__':
    unittest.main()
