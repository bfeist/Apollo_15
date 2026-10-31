"""Research-only off-event residual-line guard for check-band measurements."""
import numpy as np
from scipy import signal,ndimage
from deflutter_response_clock_guard import remove_clock_guarded_envelopes


class ResponseWithheld(ValueError):
    def __init__(self,reason,metadata):
        super().__init__('Response measurement unavailable/ambiguous: '+reason);self.metadata=metadata


def persistent_response_line_audit(target,cleaned,sr,events,context=40,*,response_amplitude_reference=None):
    if target.shape!=cleaned.shape or sr%1000 or len(target)<(2*context+16)*sr:
        raise ValueError('Matched physical context and>=16scoreseconds required')
    if response_amplitude_reference is None:
        # Historical replay only: callers used all role amplitudes before freeze.
        # This branch is feature-exposed and cannot support sealed qualification.
        t=np.arange(len(target))/sr-context
        z=signal.resample_poly(target*np.exp(-2j*np.pi*2500*t),1,sr//1000)
        z=signal.sosfiltfilt(signal.butter(5,280,fs=1000,output='sos'),z)
        tb=np.arange(len(z))/1000-context;amplitudes=[]
        for e in events:
            k=(tb>e['start']+.055)&(tb<e['end']-.055)
            if np.sum(k)<40:raise ResponseWithheld('Insufficient metadata event amplitude samples',dict(event=e))
            amplitudes.append(float(2*np.median(abs(z[k]))))
        response=min(amplitudes) if amplitudes else 0.
        amplitude_scope='legacy all-event amplitude features; exposed before selection, not sealed evidence'
    else:
        response=float(response_amplitude_reference)
        amplitude_scope='caller-frozen calibration-only amplitude reference; no other event waveform read here'
    if not np.isfinite(response) or response<=1e-8:raise ResponseWithheld('No measurable response amplitude',dict(amplitude_reference=response))
    core=cleaned[context*sr:len(cleaned)-context*sr];tc=np.arange(len(core))/sr
    allowed=np.ones(len(core),bool)
    for e in events:allowed&=~((tc>=e['start']-.5)&(tc<=e['end']+.5))
    n=8*sr;window=signal.windows.hann(n,sym=False);f=np.fft.rfftfreq(n,1/sr);band=(f>=2150)&(f<=2850)
    spectra=[];observed=[]
    for start in range(0,len(core)-n+1,4*sr):
        weight=window*allowed[start:start+n];fraction=float(np.sum(weight)/np.sum(window))
        if fraction<.5:continue
        a=2*abs(np.fft.rfft(core[start:start+n]*weight))/np.sum(weight)
        spectra.append(ndimage.maximum_filter1d(a[band],size=13,mode='nearest'));observed.append(fraction)
    if len(spectra)<3:raise ResponseWithheld('Insufficient off-event spectral windows',dict(windows=len(spectra)))
    median=np.median(spectra,axis=0);i=int(np.argmax(median));strength=float(1.5*median[i])
    result=dict(methodology='off-event-residual-response-line-audit-research-v1',windows=len(spectra),window_seconds=8,hop_seconds=4,
        minimum_observed_fraction=min(observed),neighborhood_hz=.75,amplitude_proxy_inflation=1.5,
        strongest_residual_frequency_hz=float(f[band][i]),residual_line_amplitude_proxy=strength,
        minimum_response_amplitude=response,residual_to_response_ratio=strength/response,ratio_limit=.005,
        passed=strength/response<=.005,event_waveforms_used=amplitude_scope,
        limitation='Conservative empirical narrow-line proxy, not a mathematical contamination bound; drifting/broad interference and speech-noise false withholds need independent stress tests')
    if not result['passed']:raise ResponseWithheld('Persistent check-band line remains after nuisance prediction',result)
    return result


def remove_audited_clock_envelopes(target,pilot,sr,events,context=40,*,fit_edge_guard_seconds=30):
    cleaned,metadata=remove_clock_guarded_envelopes(target,pilot,sr,events,context,fit_edge_guard_seconds=fit_edge_guard_seconds)
    audit=persistent_response_line_audit(target,cleaned,sr,events,context)
    metadata.update(methodology='off-event-ground-response-development-v7',residual_line_audit=audit)
    return cleaned,metadata
