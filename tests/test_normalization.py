import tempfile
from pathlib import Path
import sys
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from normalization import gain_for_peak, normalize_pending_file, verify_peak


class NormalizationTests(unittest.TestCase):
    def test_streaming_uses_global_peak_and_preserves_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'render.partial'
            # Different levels across the ten-second block boundary and short tail.
            x = np.r_[np.full(80001, .1), np.full(80000, -.9), np.full(19, .2)]
            sf.write(path, x, 8000, format='WAV', subtype='FLOAT')
            before, _ = sf.read(path)
            record = normalize_pending_file(path, -6, 3, lambda _: None)
            after, rate = sf.read(path)
            self.assertEqual(rate, 8000)
            self.assertEqual(len(after), len(before))
            np.testing.assert_allclose(after, before*record['gain'], atol=3e-8, rtol=0)
            self.assertAlmostEqual(abs(after).max(), 10**(-6/20), places=6)
            verify_peak(float(abs(after).max()), record)

    def test_silence_boost_limit_and_invalid_settings(self):
        self.assertEqual(gain_for_peak(0)['gain'], 1)
        self.assertAlmostEqual(gain_for_peak(.0001, -3, 6)['gain_db'], 6)
        for peak, target, boost in [(float('nan'), -3, 12), (.1, 0, 12),
                                    (.1, -3, -1), (.1, -3, float('inf'))]:
            with self.subTest(peak=peak, target=target, boost=boost), self.assertRaises(ValueError):
                gain_for_peak(peak, target, boost)
