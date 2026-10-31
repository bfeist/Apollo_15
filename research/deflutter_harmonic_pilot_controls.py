"""Fresh harmonic-selection controls with an ambiguous fundamental."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import argparse
import numpy as np
from deflutter_campaign import SEED,sha
from deflutter_response_fresh_controls import cases,run
from deflutter_response_sealed_measurement import remove_sealed_harmonic_envelopes


AMPLITUDES={2:.02,3:.04,4:.015,5:.01,6:.008,7:.007,8:.005,9:.004,10:.003,11:.002,12:.002}


def execute(args):
    def harmonic_cases(*parameters):
        for case in cases(*parameters):
            sr=case['sr'];t=np.arange(len(case['target_extended']))/sr-40
            ground=(2500-case['hz'])/40;electrical=2*np.pi*ground*t
            if case['shape']=='frequency_drift':electrical+=.008/.027*np.sin(2*np.pi*.027*t+.3)
            if case['shape']=='phase_jump':electrical+=(t>20)*.04
            clock=electrical+2*np.pi*case['target_pilot_offset_hz']*t
            scale=case['target_pilot_amplitude']/.15
            rng=np.random.default_rng(int(sha((SEED+args.generation+str(case['index'])+'target-harmonics').encode())[:16],16))
            x=case['target_extended'].copy();components=[]
            for h,amplitude in AMPLITUDES.items():
                phase=float(rng.uniform(-np.pi,np.pi));x+=scale*amplitude*np.cos(h*clock+phase)
                components.append(dict(harmonic=h,amplitude=scale*amplitude,phase_radians=phase))
            x+=args.neighbor_amplitude*np.cos(electrical+2*np.pi*1.1*t+.4)
            if case['pcm16']:x=np.rint(x*32768)/32768
            case.update(target_extended=x,target=x[40*sr:88*sr],target_added_harmonics=components,
                target_fundamental_competitor=dict(offset_hz=1.1,amplitude=args.neighbor_amplitude),
                composition='Base float/PCM16stem plus nuisance harmonic components, finalPCM16rounding where declared')
            yield case
    def harmonic_measurement(target,sr,events,*,calibration_events,fit_edge_guard_seconds):
        return remove_sealed_harmonic_envelopes(target,sr,events,calibration_events=calibration_events,fit_edge_guard_seconds=fit_edge_guard_seconds,maximum_pilots=args.maximum_pilots)
    run(iteration=args.iteration,generation=args.generation,edge_guard=30,pilot_mode='target',
        weak_pilot_stratum=args.weak_pilot_stratum,envelope=True,count=args.count,
        source_pilot_offset_hz=args.source_pilot_offset_hz,target_pilot_offset_hz=args.target_pilot_offset_hz,
        finite_local_pilot=True,case_factory=harmonic_cases,measurement_function=harmonic_measurement,
        extra_protocol=dict(methodology='harmonic-pilot-controls-v2-sealed-amplitude',additional_target_harmonics=AMPLITUDES,
            competitor_hz_above_clock=1.1,competitor_amplitude=args.neighbor_amplitude,maximum_pilots=args.maximum_pilots,
            source_selection='No timing-error outcome in pilot selection; every trial retained, untested hypotheses budget-deferred',
            wrong_comb='All lowharmonics follow target_pilot_offset; response-band ground follows original electrical clock'))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',required=True);p.add_argument('--generation',required=True)
    p.add_argument('--count',type=int,default=48);p.add_argument('--neighbor-amplitude',type=float,default=.08)
    p.add_argument('--weak-pilot-stratum',action='store_true');p.add_argument('--maximum-pilots',type=int,default=4)
    p.add_argument('--source-pilot-offset-hz',type=float,default=0);p.add_argument('--target-pilot-offset-hz',type=float,default=0)
    execute(p.parse_args())
