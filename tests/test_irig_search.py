"""Independent first-four-channel clock discovery and original-coordinate evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from flutter_policy import digest
from irig_search import first_four, search_tape


class IrigSearchTests(unittest.TestCase):
    def make_tape(self, root, channels=(1, 2, 3, 4, 5), tone=True):
        tape = root/'originals'/'A15_T510083'
        tape.mkdir(parents=True)
        sr = 8000
        t = np.arange(16*sr)/sr
        for channel in channels:
            x = np.zeros(len(t))
            if tone and channel in (3, 5):
                # Late, off-grid island: early-only sampling would miss it.
                x = .1*np.sin(2*np.pi*961*t)*((t >= 9.37) & (t < 13.77))
            label = 'IRIG' if channel == 5 else 'VOICE'
            sf.write(tape/f'A15_T510083_HR1U_CH{channel:02d}_{label}_207-00-00-00_207-00-00-16.wav',
                     x, sr, subtype='FLOAT')
        return tape

    def attempts(self, x, sr, start, decoder):
        frames = [dict(day_of_year=207, hours=13, minutes=34, seconds=i,
                       historical_year_bits=0, control=0, sbs=48840+i,
                       valid_fields=True, original_sample=start+i*sr) for i in range(4)]
        return [dict(filter='raw', polarity=1, frames=frames, discontinuities=[],
                     sequential_runs=[frames], longest_run=4, status='decoded_provisional')]

    def decoder_patches(self):
        return (patch('irig_clock.legacy_decoder', return_value=object()),
                patch('irig_clock.legacy_hashes', return_value={}))

    def test_late_island_on_voice_labelled_ch03_retains_clock_evidence_and_resumes(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            tape = self.make_tape(root)
            before = {p: digest(p) for p in tape.glob('*.wav')}
            output = root/'search'
            # Other tracks' preparation does not block this independent search.
            (tape/'A15_T510083_HR1U_CH05_VOICE.wav.partial').touch()
            partial = search_tape(tape, output, max_windows=3, screen_only=True)
            self.assertIsNone(partial['timecode_detected'])
            self.assertFalse(partial['coverage_complete'])
            self.assertFalse((output/'decoding').exists())
            decoder, hashes = self.decoder_patches()
            with decoder, hashes, patch('irig_clock.decode_excerpt', side_effect=self.attempts):
                report = search_tape(tape, output, root/'legacy')
                stamps = {p: p.stat().st_mtime_ns for p in (output/'decoding').glob('decode_*.json')}
                repeated = search_tape(tape, output, root/'legacy')
            self.assertEqual(report, repeated)
            self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in stamps})
            self.assertTrue(report['coverage_complete'])
            self.assertTrue(report['timecode_detected'])
            self.assertEqual(report['status'], 'decoded_provisional')
            self.assertEqual(report['missing_channels'], [])
            self.assertEqual(len(report['snapshots']), 1)
            snapshot = report['snapshots'][0]
            self.assertIn('CH03_VOICE', snapshot['source']['path'])
            self.assertEqual(snapshot['clock_basis'], 'unverified')
            self.assertGreater(snapshot['first_frame']['original_sample'], 0)
            self.assertEqual(snapshot['last_frame']['original_sample']-
                             snapshot['first_frame']['original_sample'], 3*8000)
            cache = json.loads(Path(snapshot['decoder_cache']['path']).read_text())
            run = cache['excerpts'][snapshot['excerpt_index']]['attempts'][snapshot['attempt_index']]['sequential_runs'][snapshot['run_index']]
            self.assertEqual(run[0], snapshot['first_frame'])
            self.assertEqual(digest(snapshot['decoder_cache']['path']), snapshot['decoder_cache']['sha256'])
            index = json.loads((output/'screening/survey.json').read_text())
            self.assertEqual([r['channel'] for r in index['files']], [1, 2, 3, 4])
            self.assertEqual(before, {p: digest(p) for p in before})

    def test_missing_channel_never_produces_a_negative_coverage_claim(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            tape = self.make_tape(root, channels=(1, 2, 3, 5), tone=False)
            decoder, hashes = self.decoder_patches()
            with decoder, hashes:
                report = search_tape(tape, root/'search', root/'legacy')
            self.assertEqual(report['missing_channels'], [4])
            self.assertFalse(report['coverage_complete'])
            self.assertIsNone(report['timecode_detected'])
            self.assertTrue(report['decoding_complete'])

    def test_pure_tone_is_not_timecode_and_screening_is_not_decoding(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            tape = self.make_tape(root)
            output = root/'search'
            report = search_tape(tape, output, screen_only=True)
            self.assertTrue(report['coverage_complete'])
            self.assertIsNone(report['timecode_detected'])
            decoder, hashes = self.decoder_patches()
            empty = [dict(filter='raw', polarity=1, frames=[], sequential_runs=[], longest_run=0,
                          discontinuities=[], status='no_decode')]
            with decoder, hashes, patch('irig_clock.decode_excerpt', return_value=empty):
                report = search_tape(tape, output, root/'legacy')
            self.assertFalse(report['timecode_detected'])
            self.assertEqual(report['status'], 'no_timecode_detected')
            self.assertEqual(report['snapshots'], [])

    def test_duplicate_channel_and_overlapping_output_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tape = self.make_tape(root, channels=(1,))
            with self.assertRaisesRegex(ValueError, 'separate'):
                search_tape(tape, tape/'search', screen_only=True)
            original = next(tape.glob('*.wav'))
            (tape/original.name.replace('VOICE', 'OTHER')).write_bytes(original.read_bytes())
            with self.assertRaisesRegex(ValueError, 'one concatenated'):
                first_four(tape)


if __name__ == '__main__':
    unittest.main()
