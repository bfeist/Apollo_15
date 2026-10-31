"""Development-only check-band ground projection using off-event samples.

Never a correction predictor or canonical cleanup. Requires physical pilot
context and reports identifiability limits instead of asserting tape origin.
"""
import numpy as np
from scipy import signal


def remove_off_event_ground(target,pilot,sr,events,context=40,*,fit_edge_guard_seconds=0):
    if len(target)!=len(pilot) or sr%1000 or len(target)<80*sr:
        raise ValueError('Matched full-context mono target/pilot required')
    t=np.arange(len(target))/sr-context
    demod=signal.resample_poly(pilot*np.exp(-2j*np.pi*60*t),1,sr//1000)
    demod=signal.sosfiltfilt(signal.butter(4,.3,fs=1000,output='sos'),demod)
    interior=slice(30_000,-30_000)
    level=float(np.median(abs(demod[interior])))
    offset=np.gradient(np.unwrap(np.angle(demod)))*1000/(2*np.pi)
    safe=(abs(demod[interior])>=.1*level)&(abs(offset[interior])<.15)
    if level<1e-5 or np.mean(safe)<.95:raise ValueError('Electrical pilot unavailable/ambiguous')
    theta=np.interp(np.arange(len(target))/sr,np.arange(len(demod))/1000,np.unwrap(np.angle(demod)))
    phase=2*np.pi*60*t+theta
    if fit_edge_guard_seconds<0 or 2*fit_edge_guard_seconds>=len(target)/sr:
        raise ValueError('Invalid physical edge guard')
    mask=(t>=-context+fit_edge_guard_seconds)&(t<len(target)/sr-context-fit_edge_guard_seconds)
    for event in events:mask&=~((t>=event['start']-.5)&(t<=event['end']+.5))
    # Sample at1kHz for regression after mixing: at most700Hz pair differences,
    # so retain original-rate off-event regressors here (small bounded arrays).
    fit=np.flatnonzero(mask)[::2]
    harmonics=list(range(36,48))
    design=np.stack([f(h*phase[fit]) for h in harmonics for f in (np.cos,np.sin)],axis=1)
    coefficients,_,rank,_=np.linalg.lstsq(design,target[fit],rcond=None)
    if rank!=len(coefficients):raise ValueError('Off-event harmonic regression not identifiable')
    cleaned=target.copy()
    for i,h in enumerate(harmonics):cleaned-=coefficients[2*i]*np.cos(h*phase)+coefficients[2*i+1]*np.sin(h*phase)
    return cleaned,dict(methodology='off-event-ground-response-development-v2' if fit_edge_guard_seconds else 'off-event-ground-response-development-v1',harmonics=harmonics,
        pilot_amplitude=level,pilot_safe_fraction=float(np.mean(safe)),fit_samples=len(fit),rank=int(rank),
        coefficients=coefficients.tolist(),event_exclusion_seconds=.5,fit_edge_guard_seconds=fit_edge_guard_seconds,response_outcomes_used=False,
        limitation='Shared electrical phase must be valid; coincident historical components remain unidentifiable; this is not independent qualification')
