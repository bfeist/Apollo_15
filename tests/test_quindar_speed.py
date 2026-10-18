"""Known-speed controls for post-deflutter Quindar assessment."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from quindar_speed import detect, fit_events, corrected_time, render_excerpt


class QuindarSpeedTests(unittest.TestCase):
    def test_waveform_detection_with_speed_error_flutter_and_noise(self):
        sr = 8000
        rng = np.random.default_rng(55)
        t = np.arange(20*sr)/sr
        x = rng.normal(0, .003, len(t))
        truth = []
        for j, start in enumerate(np.arange(1., 19., 2.)):
            hz = (2525 if j % 2 == 0 else 2475)*.987
            ids = (t >= start)&(t < start+.25/.987)
            phase = 2*np.pi*hz*t[ids]+.4*np.sin(2*np.pi*6.3*t[ids]+j)
            x[ids] += .2*np.sin(phase)
            truth.append(hz)
        e = detect(x, sr)
        self.assertEqual(len(e), len(truth))
        self.assertLess(np.max(abs(np.array([v['frequency_hz'] for v in e])-truth)), 1.5)
        self.assertFalse(detect(np.sin(2*np.pi*2475*t), sr))
        self.assertFalse(detect(rng.normal(0, .03, len(t)), sr))

    def test_joint_assignment_drift_and_single_family_rejection(self):
        rng = np.random.default_rng(7)
        e = []
        for j, t in enumerate(np.linspace(300, 61000, 240)):
            hz = (2475 if j % 2 else 2525)*(.99-.0003*t/3600)+rng.normal(0, 1.5)
            e.append(dict(start_seconds=t, end_seconds=t+.25, frequency_hz=hz,
                          band_power_fraction=.9, amplitude_ratio=1.2, trim_range_hz=1.,
                          residual_fm_sd_hz=5., duration_seconds=.25))
        r = fit_events(e)
        self.assertLess(abs(r['linear_coefficients'][0]-.99), .0002)  # <0.5 Hz at 2.5 kHz
        self.assertLess(abs(r['linear_coefficients'][1]+.0003), .000015)
        self.assertTrue(all(v['linear_rmse_hz'] < v['constant_rmse_hz'] for v in r['validation']))
        for j, row in enumerate(r['events']):
            self.assertEqual(row['kind'], 'end' if j % 2 else 'start')
        with self.assertRaises(ValueError):
            fit_events([dict(row, frequency_hz=2450+rng.normal()) for row in e])

    def test_render_direction_duration_frequency_and_input_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.wav'
            sr = 8000
            t = np.arange(sr*8)/sr
            coefficients = [.987, -.0003]
            x = .4*np.sin(2*np.pi*2525*corrected_time(t, coefficients))
            sf.write(path, x, sr, subtype='FLOAT')
            original = path.read_bytes()
            y, rate = render_excerpt(path, 1., 6., coefficients)
            self.assertEqual(rate, sr)
            self.assertEqual(len(y), round((corrected_time(7., coefficients)-corrected_time(1., coefficients))*sr))
            z = signal.hilbert(y)[500:-500]
            freq = np.polyfit(np.arange(len(z))/sr, np.unwrap(np.angle(z)), 1)[0]/(2*np.pi)
            self.assertAlmostEqual(freq, 2525., places=2)
            self.assertEqual(original, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
