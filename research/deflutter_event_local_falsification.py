"""Paired synthetic falsification of off-event nuisance measurement limits."""
import numpy as np
from deflutter_response_fresh_controls import cases,run
from deflutter_response_sealed_measurement import remove_sealed_harmonic_envelopes


def paired_cases(generation,pilot_mode,weak_pilot_stratum,count,source_pilot_offset_hz,embed_target_pilot,target_pilot_offset_hz):
    if count!=24 or weak_pilot_stratum:raise ValueError('Frozen12paired strong-pilot controls required')
    for case in cases(generation,pilot_mode,False,12,source_pilot_offset_hz,embed_target_pilot,target_pilot_offset_hz):
        sr=case['sr'];t=np.arange(len(case['target_extended']))/sr-40
        electrical=2*np.pi*((2500-case['hz'])/40)*t
        if case['shape']=='frequency_drift':electrical+=.008/.027*np.sin(2*np.pi*.027*t+.3)
        if case['shape']=='phase_jump':electrical+=(t>20)*.04
        pilot=case['target_pilot_amplitude']*np.cos(electrical+2*np.pi*target_pilot_offset_hz*t)
        gate=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in case['calibration']+case['validation']+case['reserved'])
        for variant in ('persistent','event_local'):
            x=case['target_extended'] if variant=='persistent' else pilot+(case['target_extended']-pilot)*gate
            yield dict(case,index=2*case['index']+int(variant=='event_local'),source_pair_index=case['index'],
                nuisance_scope=variant,target_extended=x,target=x[40*sr:88*sr],
                counterexample_truth='No historical warp in stationary class; event-local electrical waveform is absent from all off-event fitting samples',
                independence='Paired variants share source/truth, not independent replications')


if __name__=='__main__':
    run(iteration='I051_event_local_falsification',generation='sealed-event-local-null-v1',edge_guard=30,pilot_mode='target',
        count=24,envelope=True,finite_local_pilot=True,case_factory=paired_cases,measurement_function=remove_sealed_harmonic_envelopes,
        extra_protocol=dict(principal_hypothesis='Off-event residual guard cannot rule out event-local electrical contamination; test full evaluator for false complete acceptance',
            methodology='sealed-event-local-falsification-v1',paired_design='12base cases x persistent/event-local,3motion classes x4electrical regimes; allfloat/strongpilot',
            stop='Any stationary/wrong-motion complete acceptance rejects this response-measurement route as transport authentication; no additional pilot/cutoff rescue',
            limits='Synthetic development only; event-local disturbance deliberately not identifiable from off-event samples; no new real data or qualification'))
