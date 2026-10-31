import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_high_rejection import track_array_high_rejection


def fixture():
    sr=8000;t=np.arange(30*sr)/sr
    truth=.004*np.sin(2*np.pi*6.5*t+.31)
    delay=-.004/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t+.31)
    x=.0001*np.cos(2*np.pi*410.25*(t+delay))+.8*np.cos(2*np.pi*60*t)
    return x,sr,truth[::40]


def test_remote_loud_hum_cannot_capture_a_weak_carrier():
    x,sr,truth=fixture();speed,support=track_array_high_rejection(x,sr,400.5,36.045,.00005)
    keep=(np.arange(len(speed))/200>4)&(np.arange(len(speed))/200<26)
    assert np.all(support[keep]>=.95)
    assert np.corrcoef(speed[keep],truth[keep])[0,1]>.9
    assert .8<np.dot(speed[keep],truth[keep])/np.dot(truth[keep],truth[keep])<1.2


def test_high_rejection_latency_is_chunk_invariant():
    x,sr,_=fixture()
    a,ga=track_array_high_rejection(x,sr,400.5,36.045,.00005,chunk_frames=7*sr)
    b,gb=track_array_high_rejection(x,sr,400.5,36.045,.00005,chunk_frames=11*sr)
    assert np.max(abs(a-b))<2e-11 and np.array_equal(ga,gb)


def test_clipping_is_explicitly_rejected_not_repaired():
    x,sr,_=fixture();x[100000]=1.
    try:track_array_high_rejection(x,sr,400.5,36.045,.00005)
    except ValueError as e:assert 'clipped input' in str(e)
    else:raise AssertionError('Clipped research source accepted')
