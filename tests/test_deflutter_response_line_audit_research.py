import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_line_audit import ResponseWithheld,persistent_response_line_audit


def test_unmodeled_persistent_line_is_withheld_and_removed_line_passes():
    sr=8000;t=np.arange(128*sr)/sr-40
    events=[dict(start=float(v+.13),end=float(v+.38)) for v in (4,8,12,16,24,28,32,36,40,44)]
    gate=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)
    beep=.15*gate*np.cos(2*np.pi*2500*t)
    ground=.15*np.cos(2*np.pi*60*t)+.03*np.cos(2*np.pi*2520*t+.7)
    target=beep+ground
    with pytest.raises(ResponseWithheld,match='Persistent check-band line'):
        persistent_response_line_audit(target,target,sr,events)
    result=persistent_response_line_audit(target,beep+.15*np.cos(2*np.pi*60*t),sr,events)
    assert result['passed']


def test_a_small_broadband_noise_floor_does_not_look_like_a_strong_line():
    sr=8000;t=np.arange(128*sr)/sr-40;rng=np.random.default_rng(732191)
    events=[dict(start=float(v+.13),end=float(v+.38)) for v in (4,8,12,16,24,28,32,36,40,44)]
    gate=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)
    x=.15*gate*np.cos(2*np.pi*2500*t)+rng.normal(0,.005,len(t))
    assert persistent_response_line_audit(x,x,sr,events)['passed']
