import contextlib
import io
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock
import wave
import zipfile


archive = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/01a_zip_originals.py'))
concat = archive['CONCAT']


class ZipOriginalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.delivery = self.root / 'delivery' / 'A15_T510073'
        self.tape = self.root / 'concatenated' / self.delivery.name
        self.output = self.root / 'originals'
        self.zip_path = self.output / f'{self.tape.name}.zip'
        for channel in (1, 2):
            self.fragment(channel)
        self.concatenate()

    def fragment(self, channel, number=0, start='207-01-00-00', end='207-01-30-00'):
        path = self.delivery / f'CH{channel}' / (
            f'A15_T510073_HR1U_CH{channel}_TEST_{start}_{end}-{number}.wav')
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x01\x00' * 8)
        return path

    def concatenate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            concat['process_tape_folder'](self.delivery, self.tape, min_age=0, strict=True)

    def run_archive(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return archive['archive_tape'](self.tape, self.delivery, self.output,
                                           min_age=0, **kwargs)

    def test_one_zip_contains_all_originals_and_rerun_preserves_it(self):
        self.assertEqual(self.run_archive(), 'created')
        with zipfile.ZipFile(self.zip_path) as zipped:
            self.assertIsNone(zipped.testzip())
            expected = {f'{self.tape.name}/{p.name}': p.read_bytes() for p in self.tape.iterdir()}
            self.assertEqual(set(zipped.namelist()), set(expected))
            for name, content in expected.items():
                self.assertEqual(zipped.read(name), content)
        original = self.zip_path.read_bytes()
        timestamp = self.zip_path.stat().st_mtime_ns
        self.fragment(3)
        self.assertEqual(self.run_archive(), 'skipped')
        self.assertEqual(self.zip_path.read_bytes(), original)
        self.assertEqual(self.zip_path.stat().st_mtime_ns, timestamp)
        self.assertEqual(list(self.output.iterdir()), [self.zip_path])

    def test_dry_run_does_not_create_destination(self):
        self.assertEqual(self.run_archive(dry_run=True), 'ready')
        self.assertFalse(self.output.exists())

    def test_missing_marker_is_deferred(self):
        next(self.tape.glob('.timerange-*')).unlink()
        self.assertEqual(self.run_archive(), 'deferred')
        self.assertFalse(self.output.exists())

    def test_missing_channel_despite_stale_marker_is_deferred(self):
        next(self.tape.glob('*.wav')).unlink()
        self.assertEqual(self.run_archive(), 'deferred')

    def test_new_delivery_channel_despite_stale_marker_is_deferred(self):
        self.fragment(3)
        self.assertEqual(self.run_archive(), 'deferred')

    def test_new_fragment_despite_stale_marker_is_deferred(self):
        self.fragment(1, 1, '207-01-30-00', '207-02-00-00')
        self.assertEqual(self.run_archive(), 'deferred')

    def test_partial_obsolete_and_truncated_files_are_deferred(self):
        for name in ('channel.wav.partial', 'obsolete.wav'):
            with self.subTest(name=name):
                extra = self.tape / name
                extra.write_bytes(b'partial')
                self.assertEqual(self.run_archive(), 'deferred')
                extra.unlink()
        path = next(self.tape.glob('*.wav'))
        path.write_bytes(path.read_bytes()[:-2])
        self.assertEqual(self.run_archive(), 'deferred')

    def test_recent_files_are_deferred(self):
        with self.assertRaises(archive['DeferredTape']):
            archive['complete_tape'](self.tape, self.delivery, min_age=60)

    def test_interruption_removes_temporary_zip_and_allows_retry(self):
        with mock.patch.object(zipfile.ZipFile, 'write', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_archive()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.run_archive(), 'created')

    def test_source_arriving_during_zip_is_not_published(self):
        original_write = zipfile.ZipFile.write

        def write_and_change(zipped, *args, **kwargs):
            result = original_write(zipped, *args, **kwargs)
            self.fragment(3)
            return result

        with mock.patch.object(zipfile.ZipFile, 'write', new=write_and_change):
            self.assertEqual(self.run_archive(), 'deferred')
        self.assertEqual(list(self.output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
