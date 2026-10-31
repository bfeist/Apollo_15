import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_extractors import project_stationary_mains


def test_projection_removes_hum_and_preserves_close_moving_carrier():
    sr=8000;t=np.arange(24*sr)/sr
    truth=.02*np.cos(2*np.pi*129.1*t+.3*np.sin(2*np.pi*6.5*t))
    mixed=truth+.5*np.cos(2*np.pi*120*t+.8)
    out,meta=project_stationary_mains(mixed,sr,129.1,35)
    assert np.sqrt(np.mean((out-truth)**2))<.0001
    fitted=next(r for r in meta['components'] if r['hz']==120.)
    assert abs(fitted['amplitude']-.5)<.0001


def test_projection_has_no_notch_band_and_handles_fractional_tail():
    sr=8000;t=np.arange(20*sr+317)/sr
    truth=.02*np.cos(2*np.pi*121.7*t)
    out,_=project_stationary_mains(truth+.5*np.cos(2*np.pi*120*t),sr,121.7,35)
    # A nearby unrelated coherent historical line loses only its finite-window
    # projection, rather than the attenuation of a broad cleanup notch.
    assert np.dot(out,truth)/np.dot(truth,truth)>.999
