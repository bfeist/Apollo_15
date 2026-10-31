import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_envelope import remove_off_event_envelopes


def test_excluded_coincident_burst_cannot_change_the_ground_prediction():
    sr=8000;t=np.arange(88*sr)/sr-40;e=dict(start=3.,end=3.25)
    a=.15*np.cos(2*np.pi*60*t)+.03*(1+.3*np.sin(2*np.pi*.037*t))*np.cos(2*np.pi*2520*t+.51)
    burst=((t>=3)&(t<3.25))*.2*np.cos(2*np.pi*2520*t+.73)
    clean_a,meta_a=remove_off_event_envelopes(a,a,sr,[e])
    clean_b,meta_b=remove_off_event_envelopes(a+burst,a+burst,sr,[e])
    assert meta_a==meta_b
    np.testing.assert_allclose(clean_b-clean_a,burst,atol=1e-15)


def test_missing_pilot_withholds_instead_of_learning_burst_energy():
    sr=8000;t=np.arange(88*sr)/sr-40;e=dict(start=3.,end=3.25)
    x=((t>=3)&(t<3.25))*.2*np.cos(2*np.pi*2491*t+.73)
    with pytest.raises(ValueError,match='pilot unavailable'):
        remove_off_event_envelopes(x,x,sr,[e])


def test_separate_pilot_and_envelope_do_not_learn_excluded_target_burst():
    sr=8000;t=np.arange(88*sr)/sr-40;e=dict(start=3.,end=3.25)
    phase=2*np.pi*60.031*t
    pilot=.15*np.cos(phase)
    a=.04*(1+.3*np.sin(2*np.pi*.037*t))*np.cos(42*phase+.51)
    burst=((t>=3)&(t<3.25))*.2*np.cos(2*np.pi*2491*t+.73)
    clean_a,meta_a=remove_off_event_envelopes(a,pilot,sr,[e],independent_pilot=True)
    clean_b,meta_b=remove_off_event_envelopes(a+burst,pilot,sr,[e],independent_pilot=True)
    assert meta_a==meta_b
    np.testing.assert_allclose(clean_b-clean_a,burst,atol=1e-15)
