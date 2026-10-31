import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_adaptive_mains import remove_adaptive_comb


def test_offset_comb_subtraction_preserves_adjacent_moving_tone():
    sr=8000;t=np.arange(100*sr)/sr
    phase=2*np.pi*60.037*t-.008/.03*np.cos(2*np.pi*.03*t)
    truth=.02*np.cos(2*np.pi*119.2*t+.12*np.sin(2*np.pi*6.5*t))
    x=truth+.2*np.cos(phase)+.5*np.cos(2*phase+.7)
    y,meta=remove_adaptive_comb(x,sr,119.2,35)
    core=slice(30*sr,70*sr)
    assert meta['status']=='measured_extraction'
    assert np.sqrt(np.mean((y[core]-truth[core])**2))<.001
    stages=[]
    instrumented,other=remove_adaptive_comb(x,sr,119.2,35,progress=stages.append)
    assert np.array_equal(y,instrumented) and meta==other
    assert stages[-1]['components_completed']==len(meta['components'])


def test_missing_pilot_does_not_track_a_legitimate_59hz_neighbor_as_mains():
    sr=8000;t=np.arange(100*sr)/sr
    x=.025*np.cos(2*np.pi*59.2*t+.05*np.sin(2*np.pi*6.5*t))
    y,meta=remove_adaptive_comb(x,sr,59.2,24)
    assert meta['status']=='pilot_unavailable_identity_extraction'
    assert np.array_equal(x,y)
