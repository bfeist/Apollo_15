"""Exclude every event sample from pilot learning as well as harmonic fitting."""
import numpy as np
from deflutter_response_measurement import remove_off_event_ground


def remove_masked_off_event_ground(target,pilot,sr,events,context=40,*,fit_edge_guard_seconds=30):
    if target.shape!=pilot.shape:raise ValueError('Matched full-context target/pilot required')
    t=np.arange(len(pilot))/sr-context
    allowed=np.ones(len(pilot),dtype=bool)
    for event in events:allowed&=~((t>=event['start']-.5)&(t<=event['end']+.5))
    masked=np.where(allowed,pilot,0.)
    result,metadata=remove_off_event_ground(target,masked,sr,events,context,fit_edge_guard_seconds=fit_edge_guard_seconds)
    metadata.update(methodology='off-event-ground-response-development-v3',pilot_event_samples_excluded=int(np.sum(~allowed)),
        pilot_event_mask_before_demodulation=True,pilot_missing_samples='Zero-weight missing input before phase lowpass; no event interpolation or outcome learning',
        limitation='Pilot must remain measurable across masked gaps; phase discontinuities/absent pilots may withhold. Historical/electrical coincident components remain unidentifiable.')
    return result,metadata
