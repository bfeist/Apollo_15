"""The SG respeed places every source instant at output time t + D(t), with a clean kernel."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401  (every registered script folder)
import sg_respeed as respeed  # noqa: E402

SR = 48000


class RespeedTests(unittest.TestCase):
    def test_clicks_land_at_the_curve_and_tones_stay_clean(self):
        rng = np.random.default_rng(0)
        n = 40 * SR
        clicks = np.arange(1, 39) * SR + rng.integers(0, SR // 2, 38)
        x = np.zeros((n, 2), np.float32)
        x[clicks, 1] = 0.5
        x[:, 0] = 0.3 * np.sin(2 * np.pi * 1000 * np.arange(n) / SR)
        reel = {'reel': 'X', 'drift_coefficients_s': [0.2, 0.04], 'duration_s': 40.0, 'drift_scale_s': 40.0}
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / 's.wav', Path(d) / 'o.wav'
            sf.write(src, x, SR, subtype='PCM_24')
            t, u = respeed.inverse_map(reel, 40.0)
            stats = respeed.render(src, out, t, u, lambda m: None)
            y, _ = sf.read(out)
        self.assertEqual(stats['frames'], len(y))
        self.assertAlmostEqual(len(y) / SR, 40.0 + 0.24, delta=1 / SR)
        for c in clicks:
            want = (c / SR + respeed.drift(reel, np.array(c / SR))) * SR
            i = int(round(want))
            self.assertLessEqual(abs(i - 300 + int(np.argmax(np.abs(y[i - 300:i + 300, 1]))) - want), 1)
        # The tone is resampled: its frequency drops by the local rate; the waveform stays clean.
        uu = np.arange(SR, len(y) - SR) / SR
        tt = np.interp(uu, u, t)
        err = y[SR:len(y) - SR, 0] - 0.3 * np.sin(2 * np.pi * 1000 * tt)
        self.assertLess(10 * np.log10(np.mean(err ** 2) / 0.045), -90)
        self.assertEqual(stats['clipped_samples'], 0)


if __name__ == '__main__':
    unittest.main()
