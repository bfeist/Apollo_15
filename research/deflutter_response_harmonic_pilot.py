"""Source-feature harmonic pilot selection; never timing-error selection."""
import numpy as np
from scipy import signal
from deflutter_response_local_finite import finite_local_phase
from deflutter_response_line_audit import ResponseWithheld,persistent_response_line_audit


def harmonic_pilot_catalog(target,sr,events,context=40):
    core=target[context*sr:-context*sr];t=np.arange(len(core))/sr;allowed=np.ones(len(core),bool)
    for e in events:allowed&=~((t>=e['start']-.55)&(t<=e['end']+.55))
    weights=signal.windows.hann(len(core),sym=False)*allowed
    if np.mean(allowed)<.3:raise ResponseWithheld('Insufficient off-event pilot-discovery support',dict(observed_fraction=float(np.mean(allowed))))
    spectrum=2*abs(np.fft.rfft(core*weights))/np.sum(weights);f=np.fft.rfftfreq(len(core),1/sr);rows=[]
    for h in range(1,13):
        k=np.flatnonzero(abs(f-60*h)<=.15*h);i=int(k[np.argmax(spectrum[k])]);amplitude=float(spectrum[i])
        rows.append(dict(harmonic=h,demod_hz=float(f[i]),coherent_amplitude=amplitude,
            clock_information_proxy=amplitude*h,status='eligible_source_feature' if amplitude>=2e-5 else 'coherent_amplitude_below_threshold'))
    return sorted(rows,key=lambda r:(-r['clock_information_proxy'],r['harmonic']))


def envelopes_from_phase(target,sr,events,phase,context=40,edge_guard=30):
    t=np.arange(len(target))/sr-context;tb=np.arange(int(np.ceil(len(target)*1000/sr)))/1000
    allowed=np.ones(len(target),bool)
    for e in events:allowed&=~((t>=e['start']-.5)&(t<=e['end']+.5))
    down=lambda x:signal.resample_poly(x,1,sr//1000)
    lp=lambda x:signal.sosfiltfilt(signal.butter(4,.2,fs=1000,output='sos'),x)
    weights=lp(down(allowed.astype(float)));guard=(tb>=edge_guard)&(tb<len(target)/sr-edge_guard)
    if np.min(weights[guard])<.25:raise ResponseWithheld('Insufficient off-event envelope support',dict(minimum_weight=float(np.min(weights[guard]))))
    interior=(t>=-context+edge_guard)&(t<len(target)/sr-context-edge_guard)
    cleaned=target.copy();components=[]
    for h in range(36,48):
        oscillator=np.exp(1j*h*phase)
        envelope=lp(down(target*allowed*np.conj(oscillator)))/np.maximum(weights,.05)
        fitted=np.interp(t+context,tb,envelope.real)+1j*np.interp(t+context,tb,envelope.imag)
        cleaned[interior]-=2*np.real(fitted[interior]*oscillator[interior])
        components.append(dict(harmonic=h,amplitude_percentiles=np.percentile(2*abs(envelope[guard]),[5,50,95]).tolist()))
    return cleaned,components


def remove_harmonic_pilot_envelopes(target,sr,events,context=40,*,fit_edge_guard_seconds=30,maximum_pilots=4,response_amplitude_reference=None):
    catalog=harmonic_pilot_catalog(target,sr,events,context);trials=[]
    for item in catalog:
        if item['status']!='eligible_source_feature':continue
        if len(trials)>=maximum_pilots:break
        record=dict(item)
        try:
            phase,pilot=finite_local_phase(target,sr,events,context,harmonic=item['harmonic'],demod_hz=item['demod_hz'])
            cleaned,components=envelopes_from_phase(target,sr,events,phase,context,fit_edge_guard_seconds)
            audit=persistent_response_line_audit(target,cleaned,sr,events,context,response_amplitude_reference=response_amplitude_reference)
            record.update(status='measurement_source_supported',pilot=pilot,audit=audit);trials.append(record)
            return cleaned,dict(methodology='off-event-ground-response-development-v9',catalog=catalog,trials=trials,
                selected=record,components=components,maximum_pilots=maximum_pilots,
                selection='Coherent amplitude xharmonic source-feature priority; first finite-phase/residual-audit pass before timing calibration; no frequency-error loss',
                correction_map_fitted=False,event_phase_outcomes_used_for_model=False,
                limitation='Bounded nuisance-measurement prototype, not authenticated transport or perceptual improvement; untested pilot hypotheses remain deferred')
        except ValueError as exc:
            record.update(status='measurement_source_withheld',reason=str(exc),metadata=getattr(exc,'metadata',None));trials.append(record)
    attempted={r['harmonic'] for r in trials}
    for r in catalog:
        if r['status']=='eligible_source_feature' and r['harmonic'] not in attempted:r['status']='budget_deferred'
    raise ResponseWithheld('No qualified pilot within declared source-feature budget',dict(catalog=catalog,trials=trials,maximum_pilots=maximum_pilots))
