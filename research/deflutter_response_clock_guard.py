"""Conservative clock-agreement guard for research response envelopes."""
import numpy as np
from scipy import signal
from deflutter_response_envelope import remove_off_event_envelopes


class ClockWithheld(ValueError):
    def __init__(self,reason,metadata):
        super().__init__('Electrical pilot unavailable/ambiguous: '+reason);self.metadata=metadata


def electrical_clock_agreement(target,pilot,sr,events,context=40,edge_guard=30):
    if target.shape!=pilot.shape or target.ndim!=1 or sr%1000 or len(target)<80*sr:
        raise ValueError('Matched physical source context required')
    t=np.arange(len(target))/sr-context;tb=np.arange(int(np.ceil(len(target)*1000/sr)))/1000-context
    keep=(tb>=-context+edge_guard)&(tb<len(target)/sr-context-edge_guard)
    for e in events:keep&=~((tb>=e['start']-.5)&(tb<=e['end']+.5))
    if np.sum(keep)<10_000:raise ClockWithheld('Insufficient off-event clock samples',dict(samples=int(np.sum(keep))))
    phases=[];levels=[]
    for x in (target,pilot):
        z=signal.resample_poly(x*np.exp(-2j*np.pi*60*t),1,sr//1000)
        z=signal.sosfiltfilt(signal.butter(4,.3,fs=1000,output='sos'),z)
        level=float(np.median(abs(z[keep])));theta=np.unwrap(np.angle(z));offset=np.gradient(theta)*1000/(2*np.pi)
        safe=(abs(z[keep])>=.1*level)&(abs(offset[keep])<.15)
        if level<1e-5 or np.mean(safe)<.95:raise ClockWithheld('Weak/unstable target or source clock',dict(level=level,safe_fraction=float(np.mean(safe))))
        phases.append(theta);levels.append(level)
    difference=phases[0]-phases[1];constant=float(np.median(difference[keep]));residual=difference[keep]-constant
    p95=float(np.percentile(abs(residual),95));maximum=float(np.max(abs(residual)))
    result=dict(methodology='separate-electrical-clock-agreement-research-v1',constant_phase_offset_radians=constant,
        absolute_phase_residual95_radians=p95,maximum_phase_residual_radians=maximum,target_source_amplitudes=levels,
        samples=int(np.sum(keep)),phase95_limit_radians=.001,phase_maximum_limit_radians=.005,
        passed=p95<=.001 and maximum<=.005,fit='Constant phase only; no frequency/gain/delay fitting or response-event outcome optimization',
        scope='Low-band fixed clock comparison outside all metadata events; it may withhold but never modifies a correction map',
        limitation='Clock agreement authenticates neither transport origin nor human perceptual benefit')
    if not result['passed']:raise ClockWithheld('Source/target electrical clock disagreement',result)
    return result


def remove_clock_guarded_envelopes(target,pilot,sr,events,context=40,*,fit_edge_guard_seconds=30):
    clock=electrical_clock_agreement(target,pilot,sr,events,context,fit_edge_guard_seconds)
    cleaned,metadata=remove_off_event_envelopes(target,pilot,sr,events,context,fit_edge_guard_seconds=fit_edge_guard_seconds,independent_pilot=True)
    metadata.update(methodology='off-event-ground-response-development-v6',clock_agreement=clock)
    return cleaned,metadata
