"""Numerical equivalence and isolation of the fused execution backend."""
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install
install()
import denoise_v2 as reference
import denoise_v2_numba as accelerated
import fast_kernels as kernels

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/03_denoise/runtime/research'))
from qualify_denoise_numba import synthetic_cases, decision_structure, difference, within_tolerance


def test_denoise_fused_namespace_preserves_reference_and_contract():
    assert reference.model_window.__globals__['observations'] is reference.observations
    assert reference.model_window.__globals__['np'] is np
    assert accelerated.model_window.__globals__['fk'] is kernels
    assert accelerated.corrected_chunks.__globals__['model_window'] is accelerated.model_window
    assert accelerated.configuration() == reference.configuration()
    assert accelerated.ALGORITHM == 'apollo15-hum-subtraction-v2.16'
    assert accelerated.backend_configuration()['feature_policy']['windows'] == list(accelerated.ACTIVE_FEATURES)
    assert accelerated.FEATURE_POLICY['windows'] == accelerated.FEATURE_POLICY['genus']


@pytest.mark.parametrize('name,samples,seeds', list(synthetic_cases()), ids=lambda v: v if isinstance(v,str) else None)
def test_denoise_synthetic_equivalence(name, samples, seeds):
    a, ra = reference.model_window(samples, seeds, defer_compact=True)
    b, rb = accelerated.model_window(samples, seeds, defer_compact=True)
    assert within_tolerance(difference(a,b), difference((samples-a).astype('<f4'), (samples-b).astype('<f4')),
                            decision_structure(ra) == decision_structure(rb)), name


def test_denoise_qualification_rejects_decision_changes_and_tolerance_excess():
    from copy import deepcopy
    row = dict(components=[dict(kind='compact_shifted_line', hz=60.02, harmonic=1,
                               true_absence_intervals_seconds=[])],
               refinement=[dict(hz=837.23, origin='saved_noncomb', stop='pass_limit', passes=[{}])])
    for mutate in (lambda r:r['components'].clear(),
                   lambda r:r['refinement'][0]['passes'].append({}),
                   lambda r:r['refinement'][0].update(stop='unreliable_track'),
                   lambda r:r.update(upper_bypass='no_coherent_reference'),
                   lambda r:r['components'][0].update(harmonic=2)):
        changed = deepcopy(row); mutate(changed)
        assert decision_structure(row) != decision_structure(changed)
    assert within_tolerance(dict(peak=1e-9), dict(peak=2**-22,rms=2**-25), True)
    for model,peak,rms,equal in [(1.01e-9,0,0,True),(0,2**-21,0,True),
                                 (0,0,2**-24,True),(0,0,0,False)]:
        assert not within_tolerance(dict(peak=model),dict(peak=peak,rms=rms),equal)
