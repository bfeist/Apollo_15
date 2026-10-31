"""Physical invariants for development-only off-event check measurement."""
import sys
from pathlib import Path
import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_measurement import remove_off_event_ground
from deflutter_response_measurement_masked import remove_masked_off_event_ground


def test_event_samples_do_not_fit_ground_and_coincident_burst_is_preserved():
    sr=8000;t=np.arange(88*sr)/sr-40
    pilot=.2*np.cos(2*np.pi*60*t)
    ground=.04*np.cos(2*np.pi*2520*t+.31)
    event=dict(start=3.,end=3.25)
    beep=.17*((t>=3)&(t<3.25))*np.cos(2*np.pi*2520*t-.8)
    clean_a,meta_a=remove_off_event_ground(ground,pilot,sr,[event],fit_edge_guard_seconds=30)
    clean_b,meta_b=remove_off_event_ground(ground+beep,pilot,sr,[event],fit_edge_guard_seconds=30)
    np.testing.assert_array_equal(meta_a['coefficients'],meta_b['coefficients'])
    np.testing.assert_allclose(clean_b-clean_a,beep,atol=1e-15)
    interior=(t>0)&(t<8)
    assert np.sqrt(np.mean(clean_a[interior]**2))<1e-6


def test_absent_pilot_withholds_instead_of_assuming_nominal_mains():
    x=np.zeros(80*8000)
    with pytest.raises(ValueError,match='pilot unavailable'):
        remove_off_event_ground(x,x,8000,[])


def test_unmatched_physical_context_is_rejected():
    with pytest.raises(ValueError,match='Matched full-context'):
        remove_off_event_ground(np.zeros(128),np.zeros(127),8000,[])


def test_target_local_pilot_cannot_learn_any_excluded_event_samples():
    sr=8000;t=np.arange(88*sr)/sr-40;event=dict(start=3.,end=3.25)
    ground=.15*np.cos(2*np.pi*60*t)+.03*np.cos(2*np.pi*2520*t+.51)
    burst=((t>=3)&(t<3.25))*.31*np.cos(2*np.pi*2491*t+.73)
    a,meta_a=remove_masked_off_event_ground(ground,ground,sr,[event])
    b,meta_b=remove_masked_off_event_ground(ground+burst,ground+burst,sr,[event])
    np.testing.assert_array_equal(meta_a['coefficients'],meta_b['coefficients'])
    np.testing.assert_allclose(b-a,burst,atol=1e-15)
    assert meta_a['pilot_event_samples_excluded']>sr
