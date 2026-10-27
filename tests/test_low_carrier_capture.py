"""Physical low-carrier cases absent from the original 777Hz tracker tests."""
from pathlib import Path
import sys
import unittest

import numpy as np
from scipy.integrate import cumulative_trapezoid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from carrier_capture import capture_candidates
from continuous_planning import competing_carriers
from continuous_tracking import track_array
from flutter_tracking import evaluate_events


class LowCarrierCaptureTests(unittest.TestCase):
    def test_modulation_sidebands_need_a_band_aware_capture_candidate(self):
        sr = 8000
        t = np.arange(24*sr)/sr
        motion = (.022*np.sin(2*np.pi*6.5*t)+.004*np.sin(2*np.pi*13*t+.3)
                  +.002*np.sin(2*np.pi*19.5*t-.2))
        tau = t+cumulative_trapezoid(motion, dx=1/sr, initial=0)
        reference = .3*np.sin(2*np.pi*117.5*tau)
        independent = .3*np.sin(2*np.pi*2500*tau)
        events = [dict(start=v, end=v+.25) for v in (5., 9., 13., 17.)]
        narrow, wider = capture_candidates(117.5, sr, (2., 30.))
        self.assertEqual((narrow, wider), (12., 35.))
        a, _ = track_array(reference, sr, 117.5, narrow, .15)
        b, support = track_array(reference, sr, 117.5, wider, .15)
        old = [m['after_rms_hz'] for e in evaluate_events(independent, sr, a, events) for m in e['margins']]
        corrected = [m['after_rms_hz'] for e in evaluate_events(independent, sr, b, events) for m in e['margins']]
        wrong = [m['residual_ratio'] for e in evaluate_events(independent, sr, -b, events) for m in e['margins']]
        self.assertGreater(max(old), 8.)
        self.assertLess(max(corrected), 1.5)
        self.assertGreater(min(wrong), 1.8)
        split, split_support = track_array(reference, sr, 117.5, wider, .15, chunk_frames=12347)
        np.testing.assert_allclose(split, b, atol=2e-14, rtol=0)
        np.testing.assert_array_equal(split_support, support)

    def test_widening_remains_bounded_and_competing_tones_still_reject(self):
        self.assertEqual(capture_candidates(777., 8000, (2., 30.)), (69.93,))
        self.assertLess(max(capture_candidates(3980., 8000, (2., 30.))), 20.)
        sr = 8000
        t = np.arange(12*sr)/sr
        crowded = .3*np.sin(2*np.pi*117.5*t)+.3*np.sin(2*np.pi*161.5*t)
        self.assertTrue(competing_carriers(crowded, sr, 117.5, 50.))
        self.assertFalse(competing_carriers(crowded, sr, 117.5, 35.))
        with self.assertRaises(ValueError):
            capture_candidates(4001., sr, (2., 30.))


if __name__ == '__main__':
    unittest.main()
