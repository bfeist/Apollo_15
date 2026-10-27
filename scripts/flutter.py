"""Bounded inverse timing maps and audio resampling shared by stages 05b/05c."""
import numpy as np
from scipy import signal
from scipy.interpolate import CubicSpline
from scipy.integrate import cumulative_trapezoid

RATE = 200

def positions(error,sr,n):
    error=np.asarray(error)
    if error.ndim != 1 or len(error)<2 or sr<=0 or n<2 or abs(len(error)/RATE-n/sr)>1/RATE:
        raise ValueError("Timing map duration does not match audio")
    if not np.isfinite(error).all() or np.max(abs(error))>.05:raise ValueError('Invalid correction')
    t=np.arange(n)/sr;tt=np.arange(len(error))/RATE
    delay=cumulative_trapezoid(error,dx=1/RATE,initial=0)
    delay-=np.linspace(delay[0],delay[-1],len(delay))
    nominal=t+np.interp(t,tt,delay)
    if np.any(np.diff(nominal)<=0):raise ValueError('Nonmonotonic map')
    return np.interp(t,nominal,t)*sr


def resample_at(x, positions):
    """8x FIR oversampling and cubic interpolation for near-unity varispeed."""
    up=8
    oversampled=signal.resample_poly(x,up,1,window=('kaiser',8.6))
    spline=CubicSpline(np.arange(len(oversampled))/up,oversampled)
    return spline(positions)
