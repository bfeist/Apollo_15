import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from audio_processing import clean_tones, denoise, detect_residual_hum, frequency_median, remove


class DehumTests(unittest.TestCase):
    def test_frequency_median_exactly_matches_2d_filter(self):
        rng = np.random.default_rng(20261018)
        for shape in [(1, 1), (17, 3), (257, 19), (8001, 7)]:
            for width in (81, 161):
                # Ties, reflected edges and strided input all exercise rank selection.
                values = rng.integers(-100, 100, (shape[0]*2, shape[1])).astype(float)[::2]
                before = values.copy()
                with self.subTest(shape=shape, width=width):
                    expected = ndimage.median_filter(values, size=(width, 1))
                    np.testing.assert_array_equal(frequency_median(values, width), expected)
                    np.testing.assert_array_equal(values, before)

    def test_complete_cleanup_exactly_matches_legacy_median(self):
        sr = 8000
        t = np.arange(20*sr)/sr
        noise = np.random.default_rng(72).normal(0, .002, len(t))
        signals = [np.zeros_like(t), noise,
                   noise + .02*np.sin(2*np.pi*779.75*t)*(t < 12)
                   + .03*np.sin(2*np.pi*120*t) + .01*np.sin(2*np.pi*743*t)]
        for index, x in enumerate(signals):
            with self.subTest(signal=index):
                with patch('audio_processing.frequency_median',
                           side_effect=lambda db, width: ndimage.median_filter(db, size=(width, 1))):
                    old, old_metadata = clean_tones(x, sr)
                    old, old_window = denoise(old, sr)
                new, new_metadata = clean_tones(x, sr)
                new, new_window = denoise(new, sr)
                np.testing.assert_array_equal(new, old)
                self.assertEqual(new_metadata, old_metadata)
                self.assertEqual(new_window, old_window)

    def test_residual_pass_removes_hum_missed_by_general_detector(self):
        sr = 8000
        t = np.arange(30*sr)/sr
        noise = np.random.default_rng(72).normal(0, .002, len(t))
        x = noise + .02*np.sin(2*np.pi*779.75*t)*(t < 16.5)
        before = x.copy()
        y, metadata = clean_tones(x, sr)
        self.assertFalse(metadata['passes'])
        self.assertTrue(metadata['hum_passes'])
        self.assertTrue(any(abs(l['hz']-779.75) < .5
                            for p in metadata['hum_passes'] for l in p['lines']))
        keep = (t > 1) & (t < 15)
        basis = np.exp(-2j*np.pi*779.75*t[keep])
        amplitude = lambda a: abs(np.mean(a[keep]*basis))
        self.assertLess(amplitude(y), amplitude(x)*.1)
        np.testing.assert_array_equal(x, before)
        self.assertEqual(len(y), len(x))
        self.assertTrue(np.isfinite(y).all())

    def test_residual_pass_preserves_moving_voice_and_unrelated_frequency(self):
        sr = 8000
        t = np.arange(20*sr)/sr
        # Changing mean pitch avoids a perfectly repeated FM signal, whose
        # stationary sidebands can themselves satisfy a persistent-tone detector.
        f0 = 137 + 11*np.sin(2*np.pi*.7*t) + 1.9*t
        phase = 2*np.pi*np.cumsum(f0)/sr
        envelope = ((t % 2 > .35) & (t % 2 < 1.8)).astype(float)
        voice = envelope*sum(.015/k*np.sin(k*phase) for k in range(1, 9))
        self.assertEqual(detect_residual_hum(voice, sr), [])
        unrelated = .03*np.sin(2*np.pi*743*t)
        hum = .04*np.sin(2*np.pi*779.75*t)
        x = unrelated + voice + hum
        lines = detect_residual_hum(x, sr)
        self.assertTrue(any(abs(l['hz']-779.75)<.5 for l in lines))
        y = remove(x, sr, lines)
        keep = (t > 1) & (t < 19)
        basis = np.exp(-2j*np.pi*743*t[keep])
        self.assertGreater(abs(np.mean(y[keep]*basis)), .99*abs(np.mean(x[keep]*basis)))

    def test_silence_and_low_rate_harmonic_bounds(self):
        for sr in [1000, 8000, 44100, 48000]:
            with self.subTest(sr=sr):
                x = np.zeros(15*sr)
                y, metadata = clean_tones(x, sr)
                np.testing.assert_array_equal(x, y)
                self.assertEqual(metadata['hum_passes'], [])
        sr = 1000
        t = np.arange(15*sr)/sr
        x = .05*np.sin(2*np.pi*480*t)+.02*np.sin(2*np.pi*120*t)
        lines = detect_residual_hum(x, sr)
        self.assertTrue(lines)
        self.assertTrue(all(80<l['hz']<sr/2-5 for l in lines))
        self.assertTrue(np.isfinite(remove(x, sr, lines)).all())


if __name__ == '__main__':
    unittest.main()
