"""Strict, single-thread observation acceleration for the frozen v2.16 model.

The reference source is executed in a private module so its model and chunk
functions share accelerated kernels without monkeypatching the canonical
``denoise_v2`` module. Windows also accelerates complex-array exponentials;
Linux retains NumPy's faster exponential implementation. All fitting, gating,
refinement, compact union application and diagnostics use the reference code.
"""
import importlib.util
import os
from pathlib import Path
import sys

import numba
import numpy as np


BACKEND_ID = 'apollo15-v2.16-numba-platform-kernels-v2'
FEATURE_POLICY = dict(windows=['observations', 'complex_exp'], genus=['observations'])
ACTIVE_FEATURES = tuple(FEATURE_POLICY['windows' if os.name == 'nt' else 'genus'])


@numba.njit(cache=True, fastmath=False, parallel=False)
def fused_observations(samples, oscillator, starts, window):
    """Project production's regular, zero-origin observation frames in order."""
    result = np.empty(len(starts), dtype=np.complex128)
    for index in range(len(starts)):
        real, imag = 0., 0.
        start = starts[index]
        for offset in range(len(window)):
            sample = 2.*samples[start+offset]
            real += (sample*oscillator[start+offset].real)*window[offset]
            imag += (-sample*oscillator[start+offset].imag)*window[offset]
        result[index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False, parallel=False)
def complex_exp(value):
    """Strict complex128 exponential, preserving input shape and element order."""
    result = np.empty(value.shape, dtype=np.complex128)
    for index in range(value.size):
        result.flat[index] = np.exp(value.flat[index])
    return result


class _NumpyWithExp:
    """Override complex128 array exp only inside this private reference module."""
    def __getattr__(self, name):
        return getattr(np, name)

    def exp(self, value):
        if isinstance(value, np.ndarray) and value.dtype == np.complex128:
            return complex_exp(value)
        return np.exp(value)


def _load_reference():
    name = '_apollo15_denoise_v2_numba_reference'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name('denoise_v2.py'))
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules during execution.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    module.observations = fused_observations
    if 'complex_exp' in ACTIVE_FEATURES:
        module.np = _NumpyWithExp()
    return module


_reference = _load_reference()
ALGORITHM = _reference.ALGORITHM
SETTINGS = _reference.SETTINGS
Settings = _reference.Settings
configuration = _reference.configuration
model_window = _reference.model_window
corrected_chunks = _reference.corrected_chunks


def warmup():
    """Compile the production float64/complex128 signature before timing/work."""
    fused_observations(np.zeros(8000), np.ones(8000, dtype=np.complex128),
                       np.array([0, 2000, 4000]), np.ones(4000)/4000)
    if 'complex_exp' in ACTIVE_FEATURES:
        complex_exp(np.zeros(8, dtype=np.complex128))
        complex_exp(np.zeros((2, 4), dtype=np.complex128))


def backend_configuration():
    return dict(backend_id=BACKEND_ID, numba=numba.__version__,
                fastmath=False, parallel=False, cache=True,
                feature_policy=FEATURE_POLICY, active_features=list(ACTIVE_FEATURES))
