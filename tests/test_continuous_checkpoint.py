"""Interrupted stream writes preserve the last committed phase/filter state."""
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
from continuous_planning import Session, allocate_session_key
import continuous_planning
from flutter_policy import digest


class ContinuousCheckpointTests(unittest.TestCase):
    def source(self, root):
        sr = 8000
        t = np.arange(32*sr)/sr
        phase = t-.012/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
        path = root/'reference.wav'
        sf.write(path, .3*np.sin(2*np.pi*777*phase), sr, subtype='FLOAT')
        return dict(path=str(path), start_seconds=0., sample_rate=sr, frames=len(t))

    def session(self, root, key, row):
        return Session(root, key, row, 0., 777., 65., .15, 'stream_signed_2_30', 7.)

    def test_resume_discards_uncommitted_suffix_and_restores_original_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = self.source(root)
            original = self.session(root, 's000000', row)
            original.advance(10.)
            saved = original.checkpoint('first')
            state_hash = digest(root/saved['state_file'])
            committed_bytes = saved['frames']*16
            original.advance(20.)
            original.checkpoint('uncommitted')
            expected = np.fromfile(root/original.file, dtype='<f8').copy()
            self.assertGreater((root/original.file).stat().st_size, committed_bytes)
            self.assertEqual(state_hash, digest(root/saved['state_file']))
            restored = Session.restore(root, saved, 1.371)
            self.assertEqual((root/original.file).stat().st_size, committed_bytes)
            restored.advance(20.)
            actual = np.fromfile(root/restored.file, dtype='<f8')
            np.testing.assert_allclose(actual, expected, atol=1e-14, rtol=1e-12)
            self.assertEqual(restored.read_frames, original.read_frames)

    def test_corrupt_committed_prefix_is_rejected_without_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = self.source(root)
            session = self.session(root, 's000000', row)
            session.advance(10.)
            saved = session.checkpoint()
            session.advance(20.)
            path = root/session.file
            with path.open('r+b') as stream:
                stream.seek(100)
                byte = stream.read(1)
                stream.seek(100)
                stream.write(bytes([byte[0] ^ 1]))
            before, size = digest(path), path.stat().st_size
            with self.assertRaisesRegex(ValueError, 'Checkpoint stream changed'):
                Session.restore(root, saved, 7.)
            self.assertEqual(digest(path), before)
            self.assertEqual(path.stat().st_size, size)

    def test_new_sessions_do_not_append_to_orphan_streams(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = self.source(root)
            orphan = root/'s000000.f64'
            orphan.write_bytes(b'orphaned acquisition')
            with self.assertRaises(FileExistsError):
                self.session(root, 's000000', row)
            self.assertEqual(orphan.read_bytes(), b'orphaned acquisition')
            key, sequence = allocate_session_key(root, 0)
            self.assertEqual((key, sequence), ('s000001', 2))
            self.session(root, key, row)
            self.assertEqual(orphan.read_bytes(), b'orphaned acquisition')

    def test_interruption_before_plan_publication_keeps_previous_generation_resumable(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            source = root/'A15_T510083'
            source.mkdir()
            sf.write(source/'A15_T510083_HR1U_CH2_VOICE_207-00-00-00_207-00-02-00.wav',
                     np.zeros(120*8000), 8000, subtype='FLOAT')
            output = root/'plan'
            original_write = continuous_planning.write_json
            def interrupted(path, value):
                if path.name == 'plan.json' and value['summary']['monitor_windows'] == 2:
                    raise RuntimeError('Interrupted before committing new plan')
                original_write(path, value)
            with patch('continuous_planning.write_json', side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, 'Interrupted'):
                    continuous_planning.plan_tape(source, output)
            committed = json.loads((output/'plan.json').read_text())
            self.assertFalse(committed['complete'])
            self.assertEqual(committed['summary']['monitor_windows'], 1)
            detail = output/committed['regions'][0]['detail_file']
            checkpoint = output/committed['checkpoint_file']
            self.assertEqual(digest(detail), committed['regions'][0]['detail_sha256'])
            self.assertEqual(digest(checkpoint), committed['checkpoint_sha256'])
            result = continuous_planning.plan_tape(source, output, resume=True)
            self.assertTrue(result['complete'])
            self.assertEqual(result['summary']['monitor_windows'], 2)
            self.assertEqual(digest(detail), committed['regions'][0]['detail_sha256'])
            self.assertEqual(digest(checkpoint), committed['checkpoint_sha256'])


if __name__ == '__main__':
    unittest.main()
