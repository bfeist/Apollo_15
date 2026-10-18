"""Accepted continuous-tone flutter correction for aligned review excerpts."""
import numpy as np
import soundfile as sf
from scipy import signal
from scipy.integrate import cumulative_trapezoid
from scipy.interpolate import CubicSpline
from audio_processing import remove

RATE = 200

def read_excerpt(path, start, duration):
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        if f.channels != 1 or start < 0 or (start+duration)*sr > len(f):
            raise ValueError(f'Invalid mono excerpt: {path}')
        f.seek(round(start*sr))
        return f.read(round(duration*sr), dtype='float64'), sr

def estimate(x, sr, carrier_hz=631.5, hum_hz=(540,600,660,720)):
    x = np.asarray(x, dtype=float)
    if x.ndim != 1 or not np.isfinite(x).all() or len(x) < 10*sr or sr % RATE:
        raise ValueError("Reference requires finite mono audio, at least 10 seconds, and a rate divisible by 200")
    if not np.isfinite(carrier_hz) or not 80 < carrier_hz < sr/2-80:
        raise ValueError("Reference carrier outside supported band")
    if np.max(abs(x)) < 1e-12:
        raise ValueError("Silent reference")
    if any(not np.isfinite(h) or not 3 < h < sr/2-3 or abs(h-carrier_hz)<23 for h in hum_hz):
        raise ValueError("Interference frequencies must be valid and outside the reference core")
    # Measure and remove only nearby stationary interferers, never the reference.
    f,p=signal.welch(x,sr,nperseg=sr*2)
    lines=[]
    for target in hum_hz:
        ids=np.flatnonzero(abs(f-target)<3); hz=float(f[ids[np.argmax(p[ids])]])
        lines.append(dict(hz=hz,width_hz=2))
    y=remove(x,sr,lines)
    t=np.arange(len(y))/sr
    z=signal.resample_poly(y*np.exp(-2j*np.pi*carrier_hz*t),1,sr//RATE)
    z=signal.sosfiltfilt(signal.butter(6,40,fs=RATE,output='sos'),z)
    amp=abs(z)
    if np.median(amp) < 1e-12: raise ValueError("No measurable reference")
    tt=np.arange(len(z))/RATE
    freq=carrier_hz+np.gradient(np.unwrap(np.angle(z)))*RATE/(2*np.pi)
    centre=float(np.median(freq[400:-400]))
    raw=(freq-centre)/centre
    weight=np.minimum(amp/np.median(amp),1)**2
    weight[(abs(freq-centre)>45)|(amp<np.median(amp)*.3)]=0
    # Seed rate comes only from this reference, not Quindar or target speech.
    clipped=np.clip(raw,-.06,.06)
    ff,pp=signal.welch(clipped[400:-400],RATE,nperseg=min(2000,len(clipped)-800))
    band=(ff>=4)&(ff<=10);rate=float(ff[band][np.argmax(pp[band])])
    # Robust local harmonic regression: update phase/amplitude every 20 ms,
    # using 0.8 s of reference evidence; no long-gap extrapolation.
    grid=np.arange(0,len(tt),4);values=[];quality=[]
    for i in grid:
        lo=max(0,i-80);hi=min(len(tt),i+81);u=tt[lo:hi]-tt[i]
        a=np.column_stack([np.ones(len(u)),u,np.sin(2*np.pi*rate*u),np.cos(2*np.pi*rate*u)])
        base=weight[lo:hi]*np.exp(-.5*(u/.20)**2);w=base.copy()
        for _ in range(3):
            c=np.linalg.lstsq(a*np.sqrt(w[:,None]),raw[lo:hi]*np.sqrt(w),rcond=None)[0]
            residual=raw[lo:hi]-a@c;scale=max(.002,1.4826*np.median(abs(residual-np.median(residual))))
            w=base*np.minimum(1,1.5*scale/(abs(residual)+1e-12))
        support=float(np.mean(weight[lo:hi]>.1))
        gate=np.clip((support-.35)/.4,0,1)
        values.append(c[3]*gate);quality.append(gate)
    error=np.interp(tt,tt[grid],values)
    error=np.clip(error,-.04,.04)
    error*=np.clip(np.minimum(tt,tt[-1]-tt)/2,0,1)
    return tt,error,dict(centre_hz=centre,modulation_hz=rate,removed_lines=lines,
        rms_percent=float(np.std(error[400:-400])*100),support_gate_mean=float(np.mean(quality))),freq


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

