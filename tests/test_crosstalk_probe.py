import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

from research.crosstalk_probe import RATE, analysis_filter, lag_peaks, read_window, trial


class CrosstalkProbeTests(unittest.TestCase):
    def test_delayed_copy_and_coincident_shared_feed(self):
        rng = np.random.default_rng(1508)
        donor, clean = rng.normal(0, .06, size=(2, 24 * RATE))
        lag = -round(1.377 * RATE)
        delayed = np.zeros_like(donor)
        delayed[-lag:] = .26 * donor[:lag]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {20: root / "target.wav", 21: root / "donor.wav"}
            sf.write(paths[21], donor, RATE, subtype="PCM_24")
            for label, copy in (("delayed", delayed), ("shared", .26 * donor)):
                with self.subTest(label=label):
                    sf.write(paths[20], clean + copy, RATE, subtype="PCM_24")
                    inputs = [signal.fftconvolve(read_window(p, 3, 3), analysis_filter(), mode="same")
                              for p in paths.values()]
                    peaks = lag_peaks(*inputs)
                    output = root / label
                    trial(paths, 20, 21, 3, [15], 3, lag, output)
                    evaluation = json.loads((output / "trial.json").read_text())["evaluations"][0]
                    if label == "delayed":
                        self.assertEqual(peaks["early"]["lag_samples"], lag)
                        self.assertTrue(all(evaluation["fir_gates"].values()))
                        original, _ = sf.read(output / "01_baseline.wav")
                        corrected, _ = sf.read(output / "01_fir.wav")
                        removed, _ = sf.read(output / "01_removed_fir.wav")
                        self.assertEqual(len(original), 3 * RATE)
                        np.testing.assert_allclose(original - corrected, removed, atol=2 ** -22)
                        truth = clean[15 * RATE:18 * RATE]
                        self.assertLess(np.linalg.norm(corrected - truth), .85 * np.linalg.norm(original - truth))
                    else:
                        self.assertEqual(peaks["coincident"]["lag_samples"], 0)
                        self.assertFalse(evaluation["fir_gates"]["beats_controls"])
                        self.assertFalse(list(output.glob("*.wav")))
