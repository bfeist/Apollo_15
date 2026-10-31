"""Research-only removal of a slowly moving digitization mains comb.

Estimate electrical phase from the strong fundamental, then remove narrow
complex envelopes around its harmonics. This is not a timing predictor. Its
purpose is to stop known digitization contamination capturing another tone.
Use real source context (>=30s each side) and independently validate correction.
"""
import numpy as np
from scipy import signal

VERSION='adaptive-mains-comb-research-v1'


def remove_adaptive_comb(x,sr,carrier_hz,capture_hz,*,pilot_hz=60.,pilot_cutoff_hz=.3,
                         envelope_cutoff_hz=.2,pilot_minimum_amplitude=1e-5,progress=None):
    if x.ndim!=1 or not np.isfinite(x).all() or sr%1000 or len(x)<60*sr:
        raise ValueError('Adaptive comb requires finite mono samples, >=60s and rate divisible by 1000')
    t=np.arange(len(x))/sr;tb=np.arange(int(np.ceil(len(x)*1000/sr)))/1000
    decimation=sr//1000
    def lowpass(z,cutoff):
        return signal.sosfiltfilt(signal.butter(4,cutoff,fs=1000,output='sos'),z)
    pilot=signal.resample_poly(x*np.exp(-2j*np.pi*pilot_hz*t),1,decimation)
    pilot=lowpass(pilot,pilot_cutoff_hz)
    middle=slice(30000,-30000)
    amplitude=float(np.median(abs(pilot[middle])))
    theta=np.unwrap(np.angle(pilot));offset=np.gradient(theta)*1000/(2*np.pi)
    safe=(abs(pilot[middle])>=.1*amplitude)&(abs(offset[middle])<.15)
    metadata=dict(version=VERSION,pilot_hz=pilot_hz,pilot_cutoff_hz=pilot_cutoff_hz,
        envelope_cutoff_hz=envelope_cutoff_hz,pilot_amplitude=amplitude,
        pilot_safe_fraction=float(np.mean(safe)),required_real_context_seconds=30.,components=[])
    if amplitude<pilot_minimum_amplitude or np.mean(safe)<.95:
        return x.copy(),dict(metadata,status='pilot_unavailable_identity_extraction')
    # Use slow residual phase; each harmonic receives its own complex envelope
    # so electrical waveform shape need not share an arbitrary phase offset.
    residual=np.interp(t,tb[:len(theta)],theta)
    base_phase=2*np.pi*pilot_hz*t+residual
    low=max(pilot_hz,carrier_hz-max(pilot_hz,2*capture_hz))
    high=min(sr/2-1,carrier_hz+max(pilot_hz,2*capture_hz))
    y=x.copy()
    for harmonic in range(int(np.ceil(low/pilot_hz)),int(np.floor(high/pilot_hz))+1):
        oscillator=np.exp(1j*harmonic*base_phase)
        envelope=signal.resample_poly(x*np.conj(oscillator),1,decimation)
        envelope=lowpass(envelope,envelope_cutoff_hz)
        interpolated=np.interp(t,tb[:len(envelope)],envelope.real)+1j*np.interp(t,tb[:len(envelope)],envelope.imag)
        y-=2*np.real(interpolated*oscillator)
        metadata['components'].append(dict(harmonic=harmonic,
            median_amplitude=float(2*np.median(abs(envelope[middle])))))
        if progress is not None:progress(dict(harmonic=harmonic,components_completed=len(metadata['components'])))
    metadata.update(status='measured_extraction',pilot_offset_percentiles_hz=np.percentile(offset[middle],[5,50,95]).tolist(),
        limitation='Coincident historical energy is not identifiable; missing/ambiguous pilot bypasses extraction; no correction qualification implied')
    return y,metadata
