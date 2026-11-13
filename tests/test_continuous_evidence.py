"""Readiness requires completed v2 sources and one shared tape map."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
from deflutter_readiness import readiness


class ContinuousEvidenceTests(unittest.TestCase):
    def tape(self, root):
        source = root/'originals'/'A15_T510083'
        source.mkdir(parents=True)
        sf.write(source/'A15_T510083_HR1U_CH2_VOICE_207-00-00-00_207-00-00-20.wav',
                 np.zeros(160000), 8000, subtype='FLOAT')
        return source


    def status(self, root):
        return readiness(root/'originals', root/'plans', root/'denoised', root/'evidence')

    def test_missing_new_inputs_do_not_become_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.tape(root)
            status = self.status(root)
            self.assertFalse(status['ready_for_render'])
            self.assertEqual(status['cleanup_version'], 'v2')
            self.assertFalse(status['tapes'][0]['shared_map_ready'])
            self.assertFalse(status['tapes'][0]['completed_tone_evidence'])

    def test_shared_readiness_requires_v2_and_common_map(self):
        from test_shared_tape import fixture
        from shared_tape import build_tape_plan
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan, assessment, inv = fixture(root)
            build_tape_plan(plan, assessment, root/'plans'/inv['tape'])
            status = self.status(root)
            self.assertTrue(status['ready_for_render'])
            self.assertEqual(status['tapes'][0]['denoised_channels'], 3)

    def test_invalid_originals_do_not_become_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tape = self.tape(root)
            (tape/'incomplete.partial').touch()
            self.assertFalse(self.status(root)['ready_for_render'])




if __name__ == '__main__':
    unittest.main()
