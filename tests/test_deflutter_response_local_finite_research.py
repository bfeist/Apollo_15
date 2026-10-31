import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_local_finite import finite_local_phase
from deflutter_response_line_audit import ResponseWithheld


def fixtures():
    sr=8000;t=np.arange(128*sr)/sr-40
    events=[dict(start=float(v+.13),end=float(v+.38)) for v in (4,8,12,16,24,28,32,36,40,44)]
    electrical=2*np.pi*60.04*t+.008/.027*np.sin(2*np.pi*.027*t+.3)
    x=.15*np.cos(electrical)+.03*np.cos(42*electrical+.7)
    burst=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)*.2*np.cos(2*np.pi*2491*t+.73)
    return sr,t,events,electrical,x,burst


def test_excluded_events_cannot_affect_phase_and_known_clock_is_accurate():
    sr,t,events,truth,x,burst=fixtures()
    a,ma=finite_local_phase(x,sr,events);b,mb=finite_local_phase(x+burst,sr,events)
    np.testing.assert_array_equal(a,b);assert ma==mb
    core=(t>0)&(t<48);error=a-truth;error-=np.median(error[core])
    assert np.percentile(abs(error[core]),95)<.001


def test_burst_only_input_does_not_invent_a_clock():
    sr,t,events,truth,x,burst=fixtures()
    with pytest.raises(ResponseWithheld,match='pilot unavailable'):
        finite_local_phase(burst,sr,events)
