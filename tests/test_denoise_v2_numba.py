"""Strict equivalence and isolation checks for the optional accelerated backend."""
from pathlib import Path
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2 as reference
try:
    import denoise_v2_numba as accelerated
except ImportError:
    accelerated = None


@unittest.skipIf(accelerated is None, 'optional Numba backend is not installed')
class NumbaBackendTests(unittest.TestCase):
    def test_private_reference_namespace_preserves_canonical_functions(self):
        self.assertIs(reference.model_window.__globals__['observations'], reference.observations)
        self.assertIs(accelerated.model_window.__globals__['observations'], accelerated.fused_observations)
        self.assertIs(accelerated.corrected_chunks.__globals__['model_window'], accelerated.model_window)
        self.assertEqual(reference.configuration(), accelerated.configuration())
        self.assertEqual(accelerated.fused_observations.targetoptions['fastmath'], False)
        self.assertEqual(accelerated.fused_observations.targetoptions['parallel'], False)
        self.assertIs(reference.model_window.__globals__['np'], np)
        features = accelerated.backend_configuration()['active_features']
        self.assertEqual('complex_exp' in features, os.name == 'nt')

    def test_private_exponential_matches_complex_arrays_and_preserves_real_fallback(self):
        proxy = accelerated._NumpyWithExp()
        t = np.arange(1024)/8000
        for value in (1j*53*2*np.pi*60*t,
                      -2j*np.pi*np.linspace(-.9,.9,181)[:,None]*t[:30],
                      (np.linspace(-.3,.3,60)+1j*t[:60])[::2]):
            np.testing.assert_array_equal(accelerated._reference.np.exp(value), np.exp(value))
            if 'complex_exp' in accelerated.ACTIVE_FEATURES:
                np.testing.assert_array_equal(proxy.exp(value), np.exp(value))
        for value in (np.linspace(-3,3,100), np.array([1,2,3]), 1., 1+2j,
                      np.array([1j,2j], dtype=np.complex64)):
            np.testing.assert_array_equal(proxy.exp(value), np.exp(value))
        self.assertIs(proxy.sin, np.sin)
        self.assertEqual(accelerated.complex_exp.targetoptions['fastmath'], False)
        self.assertEqual(accelerated.complex_exp.targetoptions['parallel'], False)

    def test_projection_matches_strided_odd_length_and_cancellation_inputs(self):
        rng = np.random.default_rng(2048)
        for count, width, hop in [(8000, 4000, 2000), (113, 17, 7), (79, 1, 3)]:
            for strided in (False, True):
                x = rng.normal(size=count*(2 if strided else 1))
                osc = np.exp(1j*rng.uniform(-np.pi, np.pi, len(x)))
                if strided:
                    x, osc = x[::2], osc[::2]
                window = np.hanning(width) if width > 1 else np.ones(1)
                window /= window.sum()
                starts = np.arange(0, len(x)-width+1, hop)
                with self.subTest(count=count, width=width, strided=strided):
                    np.testing.assert_array_equal(reference.observations(x, osc, starts, window),
                                                  accelerated.fused_observations(x, osc, starts, window))
        x = np.tile([1e12, -1e12, 1e-12, -1e-12], 2000)
        osc = np.ones(len(x), dtype=np.complex128)*(1+1j)
        window = np.ones(4000)/4000
        starts = np.array([0, 2000, 4000])
        np.testing.assert_array_equal(reference.observations(x, osc, starts, window),
                                      accelerated.fused_observations(x, osc, starts, window))

    def test_short_context_and_quiet_bypass_keep_records_and_samples(self):
        for seconds in (1, 16):
            x = np.zeros(seconds*8000)
            a, ra = reference.model_window(x)
            b, rb = accelerated.model_window(x)
            np.testing.assert_array_equal(a, b)
            self.assertEqual(ra, rb)


if __name__ == '__main__':
    unittest.main()
