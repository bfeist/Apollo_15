"""Calibration-only nuisance eligibility; all role coordinates mask fit samples."""
import numpy as np
from scipy import signal
from deflutter_response_line_audit import ResponseWithheld
from deflutter_response_harmonic_pilot import remove_harmonic_pilot_envelopes


def calibration_amplitude_reference(target,sr,calibration_events,context=40):
    if not calibration_events:raise ResponseWithheld('Calibration amplitude reference missing',{})
    fir=signal.firwin(129,280,fs=sr);amplitudes=[]
    for event in calibration_events:
        a=round((context+event['start'])*sr);b=round((context+event['end'])*sr)
        if a<0 or b>len(target) or b-a<.15*sr:raise ValueError('Calibration amplitude window outside input domain')
        # Separate short convolutions guarantee no other event can affect C.
        t=np.arange(a,b)/sr-context
        z=signal.convolve(target[a:b]*np.exp(-2j*np.pi*2500*t),fir,mode='same',method='direct')
        guard=round(.055*sr)
        amplitudes.append(float(2*np.median(abs(z[guard:-guard]))))
    reference=min(amplitudes)
    if not np.isfinite(reference) or reference<=1e-8:raise ResponseWithheld('Calibration response amplitude unavailable',dict(amplitudes=amplitudes))
    return reference,dict(amplitudes=amplitudes,minimum=reference,events=calibration_events,
        fir_taps=129,fir_cutoff_hz=280,fir_radius_seconds=64/sr,interior_guard_seconds=.055,
        access='Only explicitly passed calibration waveform intervals read; no V/F amplitude eligibility',
        limitation='Calibration amplitude cannot guarantee held-out amplitude or bound event-only contamination')


def remove_sealed_harmonic_envelopes(target,sr,events,context=40,*,calibration_events,fit_edge_guard_seconds=30,maximum_pilots=4):
    if any(e not in events for e in calibration_events):raise ValueError('Calibration events must be a subset of masked metadata')
    reference,calibration=calibration_amplitude_reference(target,sr,calibration_events,context)
    # Sealed-event samples are unavailable to the nuisance model, including
    # clipping/finite-input guards. Preserve them only in the returned waveform.
    held_out=[e for e in events if e not in calibration_events]
    masked=target.copy();selected=np.zeros(len(target),bool);t=np.arange(len(target))/sr-context
    for event in held_out:selected|=(t>=event['start']-.025)&(t<=event['end']+.025)
    masked[selected]=0.
    cleaned,metadata=remove_harmonic_pilot_envelopes(masked,sr,events,context,
        fit_edge_guard_seconds=fit_edge_guard_seconds,maximum_pilots=maximum_pilots,response_amplitude_reference=reference)
    # Prediction is independent of held-out content. Restore actual samples
    # after selection, subtracting the identical frozen nuisance prediction.
    prediction=masked-cleaned
    restored=target-prediction
    metadata.update(methodology='off-event-ground-response-development-v10-sealed-eligibility',calibration_amplitude=calibration,
        held_out_waveform_access='Event plus25ms guard zeroed before eligibility/fit; restored only after selection with same nuisance prediction',
        held_out_event_frequency_outcomes_used=False,
        limitation='Sealed eligibility repair only; empirical off-event guard cannot rule out event-only interference or authenticate transport')
    return restored,metadata
