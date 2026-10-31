"""Fresh stationary electrical-sideband nulls; never real qualification."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import numpy as np
from deflutter_campaign import begin,finish,write,sha,memory_peak
from deflutter_adaptive_source import derive_original
from deflutter_extractors import project_stationary_mains
from deflutter_local_acquisition import source_amplitude_reference,qualified_support_island
from deflutter_map_preflight import select_preflight
from deflutter_real_experiment import make_map
from deflutter_local_experiment import evaluate
from continuous_tracking import track_array
from flutter_tracking import RATE,trajectory,evaluate_events
from flutter_planning import loss


def cases():
    sr=8000;t=np.arange(128*sr)/sr-40
    times=[4.113,8.371,12.719,16.157,24.283,28.691,32.419,36.827,40.193,44.563]
    events=[dict(start=s,end=s+.25,synchronized_group_id=f'fresh_hum_null_{i}') for i,s in enumerate(times)]
    burst=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)
    for index in range(12):
        kind=('one_interferer','symmetric_interferers','true_motion')[index%3]
        pcm=index>=6;drift=(index//3)%2==1
        electrical=2*np.pi*(60*t+(.003/(2*np.pi*.02)*np.sin(2*np.pi*.02*t) if drift else 0))
        if kind=='symmetric_interferers':
            hz=150.;beep=2490.;r=.004
            source=.3*np.cos(2*np.pi*hz*t)+.3*r*(np.cos(3*electrical)-np.cos(2*electrical))
            target=.15*burst*np.cos(2*np.pi*beep*t)+.15*r*beep/hz*(np.cos(42*electrical)-np.cos(41*electrical))
        elif kind=='one_interferer':
            hz=100.;beep=2500.;r=.005
            source=.3*np.cos(2*np.pi*hz*t)+.3*r*np.cos(2*electrical)
            target=.15*burst*np.cos(2*np.pi*beep*t)+.15*r*beep/hz*np.cos(42*electrical)
        else:
            hz=151.7;beep=2500.
            delay=-.003/(2*np.pi*7.13)*np.cos(2*np.pi*7.13*t+.731)
            source=.3*np.cos(2*np.pi*hz*(t+delay))
            target=.15*burst*np.cos(2*np.pi*beep*(t+delay))+.0001*np.cos(42*electrical)
        source+=.15*np.cos(electrical)
        if pcm:
            source=np.rint(source*32768)/32768;target=np.rint(target*32768)/32768
        yield dict(index=index,kind=kind,pcm16=pcm,ground_drift=drift,hz=hz,sr=sr,source=source,target=target[40*sr:88*sr],
                   calibration=events[:4],validation=events[4:8],reserved=events[8:])


def run():
    folder=begin('I026_response_hum_null',
        'Stationary ground harmonics inside both predictor and check-tone captures must not establish transport correction',
        'Twelve fresh stationary one-sided/symmetric-sideband nulls and known-motion positives; same full-band source choices, exact map preflight and frozen gates',
        dict(null='Zero complete validation and reserved-event accepts for stationary oscillators',positive='Known motion must retain frozen gates; no relaxed limits'),
        synthetic=dict(generation='response-ground-harmonics-v1',cases=12,source_context_seconds=40,core_seconds=48,
            response='Unfluttered oscillator bursts plus continuous electrical harmonic(s), or labeled true-motion positive',
            selection='Calibration-only raw/stationary/adaptive source x26/35Hz, then unchanged V and conditional F',
            caution='Development counterexample search; not an estimate of real false-positive rate',real_data_accessed=False))
    rows=[];start=time.perf_counter()
    for case in cases():
        sr=case['sr'];hz=case['hz'];original=case['source'];target=case['target'];core=original[40*sr:88*sr]
        adaptive,meta=derive_original(original,sr,40,88)
        trials=[];arrays={}
        for version in ('raw','stationary','adaptive_fullband'):
            for capture in (26.,35.):
                x=adaptive if version=='adaptive_fullband' else project_stationary_mains(core,sr,hz,capture)[0] if version=='stationary' else core
                trace=trajectory(x,sr,hz,capture);amplitude,_=source_amplitude_reference(trace[3],rate=RATE,available_end=48)
                curve,support=track_array(x,sr,hz,capture,amplitude)
                support,island=qualified_support_island(support,case['calibration'],case['validation'],case['reserved'],minimum_final_events=2)
                trial=dict(key=f'{version}:{capture:g}',source=version,capture_hz=capture,support_island=island)
                if support is not None:
                    try:
                        pos,_,stats=make_map(curve,support,sr,len(target))
                        trial.update(calibration_loss=loss(evaluate_events(target,sr,curve,case['calibration'],sample_positions=pos)),map_stats=stats)
                        arrays[trial['key']]=(curve,support,trace[3])
                    except ValueError as exc:trial['invalid_map']=str(exc)
                trials.append(trial)
        selected,preflight=select_preflight([r for r in trials if r.get('calibration_loss') is not None],arrays,sr,len(target))
        row={k:v for k,v in case.items() if k not in ('source','target')}
        row.update(source_sha256=sha(original.tobytes()),target_sha256=sha(target.tobytes()),adaptive=meta,trials=trials,selected=selected,preflight=preflight)
        if selected:
            curve,support,amp=arrays[selected['key']]
            write(folder/f"case_{case['index']:03d}_frozen.json",dict(selected=selected,validation_accessed=False))
            row['validation_assessment']=evaluate(target,sr,curve,support,amp,case['calibration'],case['validation'])
            if row['validation_assessment']['quality']['passed']:
                write(folder/f"case_{case['index']:03d}_final_consumed.json",dict(reserved=case['reserved'],before_measurement=True))
                row['final_assessment']=evaluate(target,sr,curve,support,amp,case['calibration'],case['reserved'])
        rows.append(row);write(folder/'results.json',rows)
        write(folder/'phase.json',dict(completed_cases=len(rows),elapsed_seconds=time.perf_counter()-start,peak_bytes=memory_peak()))
        print(case['index'],case['kind'],case['ground_drift'],case['pcm16'],selected['key'] if selected else None,
              row.get('validation_assessment',{}).get('quality',{}).get('passed'),row.get('final_assessment',{}).get('quality',{}).get('passed'),flush=True)
    summary={kind:dict(cases=sum(r['kind']==kind for r in rows),validation_pass=sum(r['kind']==kind and r.get('validation_assessment',{}).get('quality',{}).get('passed',False) for r in rows),
                      final_pass=sum(r['kind']==kind and r.get('final_assessment',{}).get('quality',{}).get('passed',False) for r in rows)) for kind in sorted({r['kind'] for r in rows})}
    false=sum(v['final_pass'] for k,v in summary.items() if k!='true_motion')
    finish(folder,dict(summary=summary,false_complete_accepts=false,real_data_accessed=False,wall_seconds=time.perf_counter()-start,peak_working_set_bytes=memory_peak()),
           'validator_counterexample' if false else 'nulls_rejected',
           'Fresh response-band hum nulls assessed with frozen controls; all outcomes retained, no real source-origin claim.',
           'If a null passes, withhold physical timing interpretation and design independent response measurement; otherwise inspect rejection predicates and continue real output assessment.')


if __name__=='__main__':run()
