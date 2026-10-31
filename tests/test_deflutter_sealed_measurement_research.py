"""Intervene on held-out content without changing nuisance-model selection."""
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_response_sealed_measurement import calibration_amplitude_reference,remove_sealed_harmonic_envelopes


def test_held_out_waveform_cannot_change_amplitude_pilot_or_prediction():
    sr=8000;t=np.arange(128*sr)/sr-40
    events=[dict(start=float(v+.13),end=float(v+.38)) for v in (4,8,12,16,24,28,32,36,40,44)]
    calibration=events[:4]
    gate=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)
    x=.15*np.cos(2*np.pi*60*t)+.0004*np.cos(2*np.pi*2520*t+.7)+.15*gate*np.cos(2*np.pi*2500*t)
    changed=x.copy();rng=np.random.default_rng(489823)
    for event in events[4:]:
        k=(t>=event['start']-.02)&(t<=event['end']+.02)
        # Includes clipped held-out content and guard-adjacent impulses. It is
        # unavailable until after selection and must not veto pilot eligibility.
        changed[k]=rng.uniform(-1.2,1.2,np.sum(k))
    a,meta=remove_sealed_harmonic_envelopes(x,sr,events,calibration_events=calibration)
    b,other=remove_sealed_harmonic_envelopes(changed,sr,events,calibration_events=calibration)
    assert meta==other
    assert meta['calibration_amplitude']==calibration_amplitude_reference(changed,sr,calibration)[1]
    assert np.max(abs((x-a)-(changed-b)))<5e-16
    assert np.max(abs((b-a)-(changed-x)))<5e-16
