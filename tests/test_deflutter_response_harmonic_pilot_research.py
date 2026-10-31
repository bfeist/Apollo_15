import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_harmonic_pilot import harmonic_pilot_catalog
from deflutter_response_local_finite import finite_local_phase


def test_off_event_center_and_harmonic_phase_are_event_independent():
    sr=8000;t=np.arange(128*sr)/sr-40;events=[dict(start=float(v+.13),end=float(v+.38)) for v in (4,8,12,16,24,28,32,36,40,44)]
    truth=2*np.pi*60.04*t+.008/.027*np.sin(2*np.pi*.027*t+.3)
    x=.05*np.cos(3*truth+.7)
    pulse=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)*.2*np.cos(2*np.pi*2491*t+.73)
    ca=harmonic_pilot_catalog(x,sr,events);cb=harmonic_pilot_catalog(x+pulse,sr,events)
    assert ca==cb
    selected=ca[0];assert selected['harmonic']==3
    a,ma=finite_local_phase(x,sr,events,harmonic=3,demod_hz=selected['demod_hz'])
    b,mb=finite_local_phase(x+pulse,sr,events,harmonic=3,demod_hz=selected['demod_hz'])
    np.testing.assert_array_equal(a,b);assert ma==mb
    core=(t>0)&(t<48);error=a-truth;error-=np.median(error[core]);assert np.percentile(abs(error[core]),95)<.001
