import contextlib
import io
from pathlib import Path
import runpy
import tempfile
import unittest
import wave


concat = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/01_concatenate.py'))


class ConcatenateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.output = self.root / 'output'

    def fragment(self, number, start, end, channel='CH10_NTWK_LT', value=1, indexed=True):
        folder = self.source / channel
        folder.mkdir(parents=True, exist_ok=True)
        suffix = f'-{number}' if indexed else ''
        path = folder / f'A15_T510073_HR1U_{channel}_{start}_{end}{suffix}.wav'
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            wav.writeframes(value.to_bytes(2, 'little') * 8)
        return path

    def process(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return concat['process_tape_folder'](self.source, self.output, min_age=0, **kwargs)

    def test_different_fragment_ranges_join_in_index_order_and_skip_on_rerun(self):
        for channel in ('CH10_NTWK_LT', 'CH9_FLIGHTPLAN'):
            self.fragment(1, '207-02-00-00', '207-02-30-00', channel, 2)
            self.fragment(0, '207-01-30-00', '207-02-00-00', channel, 1)
        self.assertEqual(self.process(strict=True), 2)
        outputs = list(self.output.glob('*.wav'))
        self.assertEqual(len(outputs), 2)
        for output in outputs:
            self.assertTrue(output.name.endswith('_207-01-30-00_207-02-30-00.wav'))
            with wave.open(str(output), 'rb') as wav:
                self.assertEqual(wav.readframes(16), b'\x01\x00' * 8 + b'\x02\x00' * 8)
        marker = self.output / '.timerange-207-01-30-00_207-02-30-00'
        self.assertTrue(marker.exists())
        marker.unlink()
        self.assertEqual(self.process(strict=True), 0)
        self.assertTrue(marker.exists())

    def test_missing_and_duplicate_indices_deferred(self):
        first = self.fragment(0, '207-01-30-00', '207-02-00-00')
        last = self.fragment(2, '207-02-00-00', '207-02-30-00')
        with self.assertRaises(RuntimeError):
            self.process(strict=True)
        last.unlink()
        duplicate = self.source / 'duplicate'
        duplicate.mkdir()
        (duplicate / first.name).write_bytes(first.read_bytes())
        with self.assertRaises(RuntimeError):
            self.process(strict=True)
        self.assertFalse(self.output.exists())

    def test_gap_deferred(self):
        self.fragment(0, '207-01-30-00', '207-02-00-00')
        self.fragment(1, '207-02-30-00', '207-03-00-00')
        with self.assertRaises(RuntimeError):
            self.process(strict=True)

    def test_midnight_overflow_in_delivered_timestamp(self):
        self.fragment(0, '207-23-35-00', '207-24-05-00')
        self.fragment(1, '208-00-05-00', '208-00-35-00')
        self.assertEqual(self.process(strict=True), 1)
        self.assertTrue((self.output / '.timerange-207-23-35-00_208-00-35-00').exists())

    def test_timestamp_without_index_is_not_a_fragment_number(self):
        self.fragment(0, '207-01-30-00', '207-02-00-37', indexed=False)
        self.assertEqual(self.process(strict=True), 1)

    def test_dry_run_writes_nothing(self):
        self.fragment(0, '207-01-30-00', '207-02-00-00')
        self.assertEqual(self.process(dry_run=True), 0)
        self.assertFalse(self.output.exists())

    def test_no_shared_marker_when_channel_ranges_differ(self):
        self.fragment(0, '207-01-30-00', '207-02-00-00')
        self.fragment(0, '207-01-30-00', '207-02-30-00', channel='CH9_FLIGHTPLAN')
        self.assertEqual(self.process(strict=True), 2)
        self.assertEqual(list(self.output.glob('.timerange-*')), [])

    def test_small_channel_differences_use_outer_range_and_preserve_wav_names(self):
        self.fragment(0, '207-01-30-00', '207-02-00-00')
        self.fragment(0, '207-01-30-02', '207-02-00-02', channel='CH9_FLIGHTPLAN')
        self.assertEqual(self.process(strict=True), 2)
        marker = self.output / '.timerange-207-01-30-00_207-02-00-02'
        self.assertTrue(marker.exists())
        self.assertEqual(len(list(self.output.glob('*_207-01-30-02_207-02-00-02.wav'))), 1)
        self.assertEqual(self.process(range_tolerance=0), 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.process(range_tolerance=2), 0)
        self.assertTrue(marker.exists())

    def test_tolerance_checks_full_spread_and_midnight(self):
        get_range = concat['tape_time_range']
        self.assertIsNone(get_range([
            '207-01-30-00_207-02-00-00',
            '207-01-30-02_207-02-00-02',
            '207-01-30-04_207-02-00-04',
        ], 2))
        self.assertEqual(get_range([
            '207-23-30-00_207-24-00-00',
            '207-23-30-00_208-00-00-01',
        ], 2), '207-23-30-00_208-00-00-01')

    def test_invalid_tolerance(self):
        for tolerance in (-1, float('nan'), float('inf')):
            with self.subTest(tolerance=tolerance), self.assertRaises(ValueError):
                self.process(range_tolerance=tolerance)


if __name__ == '__main__':
    unittest.main()
