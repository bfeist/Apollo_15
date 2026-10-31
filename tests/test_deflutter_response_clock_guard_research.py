import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_clock_guard import ClockWithheld,electrical_clock_agreement,remove_clock_guarded_envelopes


def test_strong_wrong_clock_is_withheld_and_constant_phase_is_allowed():
    sr=8000;t=np.arange(88*sr)/sr-40;target=.15*np.cos(2*np.pi*60.031*t+.7)
    correct=.2*np.cos(2*np.pi*60.031*t-.2)
    result=electrical_clock_agreement(target,correct,sr,[])
    assert result['passed'] and result['absolute_phase_residual95_radians']<1e-6
    wrong=.2*np.cos(2*np.pi*60.051*t-.2)
    with pytest.raises(ClockWithheld,match='clock disagreement'):
        electrical_clock_agreement(target,wrong,sr,[])


def test_excluded_high_band_burst_does_not_change_the_rendered_prediction():
    sr=8000;t=np.arange(88*sr)/sr-40;e=dict(start=3.,end=3.25);phase=2*np.pi*60.031*t
    pilot=.15*np.cos(phase);target=.15*np.cos(phase+.5)+.04*np.cos(42*phase+.7)
    burst=((t>=3)&(t<3.25))*.2*np.cos(2*np.pi*2491*t+.73)
    a,ma=remove_clock_guarded_envelopes(target,pilot,sr,[e]);b,mb=remove_clock_guarded_envelopes(target+burst,pilot,sr,[e])
    assert ma['clock_agreement']['passed'] and mb['clock_agreement']['passed']
    # The fixed low-band quality check is allowed to observe input quality;
    # it only withholds. The fitted nuisance prediction must be invariant.
    assert ma['components']==mb['components']
    np.testing.assert_allclose(b-a,burst,atol=1e-15)
