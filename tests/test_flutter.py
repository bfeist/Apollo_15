"""Reference tracking and inverse-resampling regressions."""
from pathlib import Path
import sys
import unittest
import numpy as np
from scipy.integrate import cumulative_trapezoid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from flutter import positions, resample_at
from flutter_tracking import signed_map, trajectory

class FlutterTests(unittest.TestCase):
    def test_identity_and_invalid_maps(self):
        sr = 8000
        n = sr*15
        pos = positions(np.zeros(3000), sr, n)
        np.testing.assert_allclose(pos, np.arange(n), atol=1e-8)
        t = np.arange(n)/sr
        x = np.sin(2*np.pi*700*t)
        self.assertLess(max(abs(resample_at(x, pos)[100:-100]-x[100:-100])), .001)
        for error in [np.full(3000, np.nan), np.ones(3000), np.zeros(20)]:
            with self.assertRaises(ValueError):
                positions(error, sr, n)

    def test_signed_tracking_and_dropout(self):
        sr = 8000
        t = np.arange(20*sr)/sr
        truth = .015*np.sin(2*np.pi*6.5*t)+.003*np.sin(2*np.pi*13*t+.3)
        tau = t+cumulative_trapezoid(truth, dx=1/sr, initial=0)
        x = np.sin(2*np.pi*777*tau)
        trace = trajectory(x, sr, 777, 65)
        h = signed_map(trace)
        expected = truth[::40]
        self.assertGreater(np.corrcoef(h[600:-600], expected[600:-600])[0, 1], .98)
        self.assertLess(np.std(h[600:-600]-expected[600:-600]), .002)
        am = (1+.6*np.sin(2*np.pi*6.5*t))*np.sin(2*np.pi*777*t)
        self.assertLess(np.std(signed_map(trajectory(am, sr, 777, 65))[600:-600]), 1e-5)
        x[(t > 9) & (t < 11)] = 0
        gap = trajectory(x, sr, 777, 65)
        self.assertLess(gap[5][1900:2100].max(), .01)
        self.assertLess(abs(signed_map(gap)[1900:2100]).max(), 1e-8)


if __name__ == "__main__":
    unittest.main()
