"""Single-thread fused execution backend for the reviewed v2.16 algorithm.

The accelerated modules retain the reference decisions and chunk contract.
Their arithmetic differs within the separately qualified numerical tolerance.
"""
import numba

import denoise_v2_fast as _implementation
import fast_kernels


BACKEND_ID = 'apollo15-v2.16-numba-fused-v3'
ACTIVE_FEATURES = ('fused_observations', 'phasor_recurrence', 'harmonic_banks',
                   'uniform_interp', 'fused_accumulation', 'compact_union')
FEATURE_POLICY = {host: list(ACTIVE_FEATURES) for host in ('windows', 'genus')}

ALGORITHM = _implementation.ALGORITHM
SETTINGS = _implementation.SETTINGS
Settings = _implementation.Settings
configuration = _implementation.configuration
model_window = _implementation.model_window
corrected_chunks = _implementation.corrected_chunks


def warmup():
    """Compile the single-thread kernels before qualification or rendering."""
    fast_kernels.warmup()


def backend_configuration():
    return dict(backend_id=BACKEND_ID, numba=numba.__version__,
                fastmath=False, parallel=False, cache=True,
                feature_policy=FEATURE_POLICY, active_features=list(ACTIVE_FEATURES))
