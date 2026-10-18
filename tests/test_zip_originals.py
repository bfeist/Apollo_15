import contextlib
import io
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock
import zipfile


archive = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/01a_zip_originals.py'))


class ZipOriginalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tape = self.root / 'concatenated' / 'A15_T510073'
        self.output = self.root / 'originals'
        self.zip_path = self.output / f'{self.tape.name}.zip'
        self.tape.mkdir(parents=True)
        (self.tape / 'channel.wav').write_bytes(b'original audio')

    def run_archive(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return archive['archive_tape'](self.tape, self.output, **kwargs)

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
        (self.tape / 'another.wav').write_bytes(b'new audio')
        self.assertEqual(self.run_archive(), 'skipped')
        self.assertEqual(self.zip_path.read_bytes(), original)
        self.assertEqual(self.zip_path.stat().st_mtime_ns, timestamp)
        self.assertEqual(list(self.output.iterdir()), [self.zip_path])

    def test_dry_run_does_not_create_destination(self):
        self.assertEqual(self.run_archive(dry_run=True), 'ready')
        self.assertFalse(self.output.exists())

    def test_all_files_included_without_validation_or_delivery(self):
        for name in ('channel.wav.partial', 'obsolete.wav', '.timerange-one',
                     '.timerange-two', 'nested/notes.txt'):
            path = self.tape / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'arbitrary recent contents')
        self.assertEqual(self.run_archive(), 'created')
        with zipfile.ZipFile(self.zip_path) as zipped:
            expected = {
                f'{self.tape.name}/{p.relative_to(self.tape).as_posix()}': p.read_bytes()
                for p in self.tape.rglob('*') if p.is_file()
            }
            self.assertEqual(set(zipped.namelist()), set(expected))
            for name, content in expected.items():
                self.assertEqual(zipped.read(name), content)

    def test_cli_accepts_any_folder_name_without_delivery(self):
        self.tape.rename(self.tape.with_name('other-tape'))
        with mock.patch('sys.argv', ['01a_zip_originals.py', '--input',
                                    str(self.tape.parent), '--output', str(self.output)]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(archive['main'](), 0)
        self.assertTrue((self.output / 'other-tape.zip').is_file())

    def test_interruption_removes_temporary_zip_and_allows_retry(self):
        with mock.patch.object(zipfile.ZipFile, 'write', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_archive()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.run_archive(), 'created')


if __name__ == '__main__':
    unittest.main()
