"""Event-independent target-local pilot with finite influence and gap limits."""
import numpy as np
from scipy import signal,interpolate
from deflutter_response_line_audit import ResponseWithheld,persistent_response_line_audit


def finite_local_phase(target,sr,events,context=40,*,harmonic=1,demod_hz=None):
    if target.ndim!=1 or sr%1000 or len(target)<80*sr or not np.isfinite(target).all():raise ValueError('Finite physical mono context required')
    if np.any(abs(target)>=.999):raise ResponseWithheld('Original target clipping',dict(clipped_samples=int(np.sum(abs(target)>=.999))))
    if not isinstance(harmonic,int) or not 1<=harmonic<=12:raise ValueError('Electrical harmonic must be an integer1..12')
    demod_hz=60.*harmonic if demod_hz is None else float(demod_hz)
    if abs(demod_hz/harmonic-60)>.150001:raise ValueError('Electrical pilot outside declared source search')
    t=np.arange(len(target))/sr-context;tb=np.arange(int(np.ceil(len(target)*1000/sr)))/1000-context
    z=signal.resample_poly(target*np.exp(-2j*np.pi*demod_hz*t),1,sr//1000)
    fir=signal.firwin(1001,1.,fs=1000,window=('kaiser',8.6))
    z=signal.convolve(z,fir,mode='same',method='direct')
    # 0.5s FIR radius + <=0.011s resample radius, with a0.039s margin.
    observed=(tb>=-context+.55)&(tb<len(target)/sr-context-.55)
    for e in events:observed&=~((tb>=e['start']-.55)&(tb<=e['end']+.55))
    core=(tb>=0)&(tb<len(target)/sr-2*context)
    usable=observed&(tb>=-context+30)&(tb<len(target)/sr-context-30)
    if np.sum(usable)<10_000:raise ResponseWithheld('Insufficient uncontaminated electrical observations',dict(observed_samples=int(np.sum(usable))))
    level=float(np.median(abs(z[usable])))
    if level<1e-5:raise ResponseWithheld('Target-local electrical pilot unavailable',dict(amplitude=level))
    indices=np.flatnonzero(observed)
    # A0.15Hz offset cannot wrap by pi across a2.5second observation gap.
    internal_gaps=np.diff(tb[indices]);relevant=(tb[indices[:-1]]<tb[core][-1])&(tb[indices[1:]]>0)
    gap=float(np.max(internal_gaps[relevant])) if np.any(relevant) else 0.
    if gap>2.5:raise ResponseWithheld('Electrical phase gap exceeds frozen2.5s interpolation domain',dict(maximum_gap_seconds=gap))
    theta=interpolate.CubicSpline(tb[indices],np.unwrap(np.angle(z[indices])),extrapolate=False)(tb)
    # Physical edges remain outside the30s qualification context. Their fill
    # supports filtering only and is never a qualified extrapolated interval.
    valid=np.isfinite(theta);theta=np.interp(tb,tb[valid],theta[valid])
    offset=np.gradient(theta)*1000/(2*np.pi)
    safe=(abs(z[usable])>=.1*level)&(abs(offset[usable])<.15)
    base_offset=(demod_hz+offset)/harmonic-60
    if np.mean(safe)<.95 or np.any(abs(offset[core])>.15) or np.any(abs(base_offset[core])>.15):raise ResponseWithheld('Target-local electrical phase ambiguous',dict(safe_fraction=float(np.mean(safe)),maximum_offset_hz=float(np.max(abs(offset[core]))),harmonic=harmonic,demod_hz=demod_hz))
    phase=(2*np.pi*demod_hz*t+np.interp(t,tb,theta))/harmonic
    return phase,dict(methodology='finite-event-independent-local-pilot-research-v1',pilot_amplitude=level,
        fir_taps=1001,fir_cutoff_hz=1.,fir_window='kaiser8.6',event_exclusion_seconds=.55,harmonic=harmonic,demod_hz=demod_hz,
        phase_interpolation='Cubic across observed phase only; no excluded samples enter observations',
        maximum_observation_gap_seconds=gap,maximum_gap_limit_seconds=2.5,
        observed_samples=int(np.sum(observed)),safe_fraction=float(np.mean(safe)),
        physical_edge_fill='Outside30squalified dependency context only; no streaming claim')


def remove_finite_local_envelopes(target,sr,events,context=40,*,fit_edge_guard_seconds=30):
    phase,pilot=finite_local_phase(target,sr,events,context)
    t=np.arange(len(target))/sr-context;tb=np.arange(int(np.ceil(len(target)*1000/sr)))/1000
    allowed=np.ones(len(target),bool)
    for e in events:allowed&=~((t>=e['start']-.5)&(t<=e['end']+.5))
    down=lambda x:signal.resample_poly(x,1,sr//1000)
    lp=lambda x:signal.sosfiltfilt(signal.butter(4,.2,fs=1000,output='sos'),x)
    weights=lp(down(allowed.astype(float)));guard=(tb>=fit_edge_guard_seconds)&(tb<len(target)/sr-fit_edge_guard_seconds)
    if np.min(weights[guard])<.25:raise ResponseWithheld('Insufficient off-event envelope support',dict(minimum_weight=float(np.min(weights[guard]))))
    interior=(t>=-context+fit_edge_guard_seconds)&(t<len(target)/sr-context-fit_edge_guard_seconds)
    cleaned=target.copy();components=[]
    for harmonic in range(36,48):
        oscillator=np.exp(1j*harmonic*phase)
        envelope=lp(down(target*allowed*np.conj(oscillator)))/np.maximum(weights,.05)
        fitted=np.interp(t+context,tb,envelope.real)+1j*np.interp(t+context,tb,envelope.imag)
        cleaned[interior]-=2*np.real(fitted[interior]*oscillator[interior])
        components.append(dict(harmonic=harmonic,amplitude_percentiles=np.percentile(2*abs(envelope[guard]),[5,50,95]).tolist()))
    audit=persistent_response_line_audit(target,cleaned,sr,events,context)
    return cleaned,dict(methodology='off-event-ground-response-development-v8',pilot=pilot,components=components,
        response_line_audit=audit,envelope_cutoff_hz=.2,fit_edge_guard_seconds=fit_edge_guard_seconds,
        correction_map_fitted=False,event_phase_outcomes_used_for_model=False,
        limitation='Research measurement only: no transport authentication or perceptual approval; interpolation/noise/nearby pilot competitors need independent controls')
