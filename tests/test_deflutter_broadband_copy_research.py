"""Known copy and no-copy controls for fixed-lag availability, no map tuning."""
import sys
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_broadband_copy_probe import fractional_shift,measure_copy


def test_copy_delay_sign_and_held_out_predictor_separate_real_copy_from_no_copy():
    sr=8000;rng=np.random.default_rng(93861)
    kernel=signal.firwin(257,[350,1950],pass_zero=False,fs=sr)
    a=signal.fftconvolve(rng.normal(0,.1,84*sr),kernel,mode='same')
    scope=dict(H_seconds=-1.359)
    b=fractional_shift(a,1.359*sr)
    copied=measure_copy([a,b],sr,scope)
    assert abs(copied['lag_seconds']+1.359)<.25/sr
    assert copied['available']
    assert copied['Bcorrelation']>.99
    unrelated=signal.fftconvolve(rng.normal(0,.1,84*sr),kernel,mode='same')
    independent=measure_copy([a,unrelated],sr,scope)
    assert not independent['available']
    assert independent['Bcorrelation']<.1
