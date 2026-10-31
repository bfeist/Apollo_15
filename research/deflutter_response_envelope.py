"""Development response-only electrical envelopes, learned off every event."""
import numpy as np
from scipy import signal


def remove_off_event_envelopes(target,pilot,sr,events,context=40,*,fit_edge_guard_seconds=30,independent_pilot=False):
    if target.ndim!=1 or target.shape!=pilot.shape or sr%1000 or len(target)<80*sr or not np.isfinite(target).all() or not np.isfinite(pilot).all():
        raise ValueError('Matched finite physical target/pilot context required')
    t=np.arange(len(target))/sr-context;tb=np.arange(int(np.ceil(len(target)*1000/sr)))/1000
    allowed=np.ones(len(target),dtype=bool)
    for e in events:allowed&=~((t>=e['start']-.5)&(t<=e['end']+.5))
    def lp(x,cut):return signal.sosfiltfilt(signal.butter(4,cut,fs=1000,output='sos'),x)
    def down(x):return signal.resample_poly(x,1,sr//1000)
    weights=down(allowed.astype(float));pw=lp(np.ones_like(weights),.3) if independent_pilot else lp(weights,.3);ew=lp(weights,.2)
    guard=(tb>=fit_edge_guard_seconds)&(tb<len(target)/sr-fit_edge_guard_seconds)
    if not np.any(guard) or np.min(pw[guard])<.25 or np.min(ew[guard])<.25:
        raise ValueError('Electrical pilot unavailable/ambiguous: insufficient off-event support')
    pilot_samples=pilot if independent_pilot else pilot*allowed
    demod=lp(down(pilot_samples*np.exp(-2j*np.pi*60*t)),.3)/np.maximum(pw,.05)
    level=float(np.median(abs(demod[guard])));theta=np.unwrap(np.angle(demod))
    offset=np.gradient(theta)*1000/(2*np.pi)
    safe=(abs(demod[guard])>=.1*level)&(abs(offset[guard])<.15)
    if level<1e-5 or np.mean(safe)<.95:raise ValueError('Electrical pilot unavailable/ambiguous')
    phase=2*np.pi*60*t+np.interp(np.arange(len(target))/sr,tb,theta)
    interior=(t>=-context+fit_edge_guard_seconds)&(t<len(target)/sr-context-fit_edge_guard_seconds)
    cleaned=target.copy();components=[]
    for h in range(36,48):
        oscillator=np.exp(1j*h*phase)
        envelope=lp(down(target*allowed*np.conj(oscillator)),.2)/np.maximum(ew,.05)
        fitted=np.interp(np.arange(len(target))/sr,tb,envelope.real)+1j*np.interp(np.arange(len(target))/sr,tb,envelope.imag)
        cleaned[interior]-=2*np.real(fitted[interior]*oscillator[interior])
        components.append(dict(harmonic=h,amplitude_percentiles=np.percentile(2*abs(envelope[guard]),[5,50,95]).tolist()))
    return cleaned,dict(methodology='off-event-ground-response-development-v5' if independent_pilot else 'off-event-ground-response-development-v4',pilot_cutoff_hz=.3,envelope_cutoff_hz=.2,
        pilot_amplitude=level,pilot_safe_fraction=float(np.mean(safe)),minimum_off_event_weight=float(np.min(ew[guard])),
        components=components,pilot_and_envelope_event_samples_excluded=int(np.sum(~allowed)),event_exclusion_seconds=.5,
        fit_edge_guard_seconds=fit_edge_guard_seconds,measurement_support_seconds=[fit_edge_guard_seconds-context,len(target)/sr-fit_edge_guard_seconds-context],
        event_outcomes_used_for_model=False,original_unchanged=True,
        independent_pilot=independent_pilot,pilot_requirement='Distinct independently bound input without target response samples; target envelope alone uses event mask' if independent_pilot else 'Target-local event-masked pilot',
        limitation='Offline prediction across excluded windows; no transport authentication, source identity or perceptual quality claim. Abrupt/ambiguous ground dynamics remain a stress domain.')
