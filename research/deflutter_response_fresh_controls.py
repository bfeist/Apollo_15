"""Fresh stress draws for off-event check-band measurement; no real data."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import argparse
import numpy as np
from deflutter_campaign import SEED,begin,finish,write,sha,memory_peak
from deflutter_response_measurement import remove_off_event_ground
from deflutter_response_measurement_masked import remove_masked_off_event_ground
from deflutter_response_envelope import remove_off_event_envelopes
from deflutter_response_clock_guard import remove_clock_guarded_envelopes
from deflutter_response_line_audit import remove_audited_clock_envelopes
from deflutter_response_local_finite import remove_finite_local_envelopes
from deflutter_adaptive_source import derive_original
from deflutter_extractors import project_stationary_mains
from deflutter_local_acquisition import source_amplitude_reference,qualified_support_island
from deflutter_map_preflight import select_preflight
from deflutter_real_experiment import make_map
from deflutter_local_experiment import evaluate
from continuous_tracking import track_array
from flutter_tracking import RATE,trajectory,evaluate_events
from flutter_planning import loss


def cases(generation='fresh-response-measurement-v1',pilot_mode='source',weak_pilot_stratum=False,count=48,source_pilot_offset_hz=0,embed_target_pilot=False,target_pilot_offset_hz=0):
    rng=np.random.default_rng(int(sha((SEED+generation).encode())[:16],16))
    sr=8000;t=np.arange(128*sr)/sr-40;core=slice(40*sr,88*sr)
    for index in range(count):
        kind=('stationary','true_motion','wrong_motion')[index%3];tier=(index//3)%4;pcm=index>=24
        shape=('constant','frequency_drift','amplitude_drift','phase_jump')[tier]
        ground=60+rng.uniform(-.04,.04);hz=2500-40*ground
        starts=np.array([4,8,12,16,24,28,32,36,40,44])+rng.uniform(.1,.8,10)
        events=[dict(start=float(s),end=float(s+.25),synchronized_group_id=f'fresh_response_{index}_{j}') for j,s in enumerate(starts)]
        burst=sum(((t>=e['start'])&(t<e['end'])).astype(float) for e in events)
        phase=rng.uniform(-np.pi,np.pi)
        electrical=2*np.pi*ground*t
        if shape=='frequency_drift':electrical+=.008/.027*np.sin(2*np.pi*.027*t+.3)
        amplitude=1+.35*np.sin(2*np.pi*.037*t+.8) if shape=='amplitude_drift' else np.ones_like(t)
        target_electrical=electrical+((t>20)*.04 if shape=='phase_jump' else 0)
        rate=rng.uniform(5,17);speed=.0035
        delay=-speed/(2*np.pi*rate)*np.cos(2*np.pi*rate*t+phase)
        source_delay=delay if kind=='true_motion' else -speed/(2*np.pi*11.13)*np.cos(2*np.pi*11.13*t-.37) if kind=='wrong_motion' else np.zeros_like(t)
        target_delay=delay if kind in ('true_motion','wrong_motion') else np.zeros_like(t)
        r=rng.uniform(.006,.01)
        source_pilot_amplitude=1e-7 if pilot_mode=='source' and weak_pilot_stratum and (index//12)%2 else .15
        reference=source_pilot_amplitude*np.cos(electrical+2*np.pi*source_pilot_offset_hz*t)+.3*np.cos(2*np.pi*hz*(t+source_delay)+phase)+.3*r*amplitude*np.cos(2*electrical)
        target=.15*burst*np.cos(2*np.pi*2500*(t+target_delay)+phase)+.15*r*2500/hz*amplitude*np.cos(42*target_electrical)
        target_pilot_amplitude=None
        if pilot_mode=='target' or embed_target_pilot:
            target_pilot_amplitude=1e-7 if weak_pilot_stratum and (index//12)%2 else .15
            target+=target_pilot_amplitude*np.cos(target_electrical+2*np.pi*target_pilot_offset_hz*t)
        if pcm:reference=np.rint(reference*32768)/32768;target=np.rint(target*32768)/32768
        yield dict(index=index,kind=kind,shape=shape,pcm16=pcm,sr=sr,hz=hz,source=reference,target_extended=target,target=target[core],target_pilot_amplitude=target_pilot_amplitude,source_pilot_amplitude=source_pilot_amplitude,source_pilot_offset_hz=source_pilot_offset_hz,target_pilot_offset_hz=target_pilot_offset_hz,
            calibration=events[:4],validation=events[4:8],reserved=events[8:])


def passed(row,key):
    value=row.get(key)
    return isinstance(value,dict) and value.get('quality',{}).get('passed',False)


def summarize(rows):
    summary={}
    for kind in ('stationary','true_motion','wrong_motion'):
        for shape in ('constant','frequency_drift','amplitude_drift','phase_jump'):
            subset=[r for r in rows if r['kind']==kind and r['shape']==shape]
            summary[f'{kind}:{shape}']=dict(cases=len(subset),withheld=sum(r.get('response_withheld',False) for r in subset),
                validation_pass=sum(passed(r,'validation') for r in subset),final_pass=sum(passed(r,'reserved_assessment') for r in subset))
    return summary


def run(iteration='I028_response_fresh_controls',generation='fresh-response-measurement-v1',edge_guard=0,pilot_mode='source',weak_pilot_stratum=False,masked_pilot_events=False,envelope=False,independent_pilot=False,count=48,source_pilot_offset_hz=0,embed_target_pilot=False,clock_guard=False,target_pilot_offset_hz=0,residual_line_audit=False,finite_local_pilot=False,case_factory=None,measurement_function=None,extra_protocol=None):
    if clock_guard and (not independent_pilot or not envelope or pilot_mode!='source'):raise ValueError('Clock guard requires separate-pilot envelope model')
    if residual_line_audit and not clock_guard:raise ValueError('Residual audit requires clock guard')
    if finite_local_pilot and (pilot_mode!='target' or not envelope or independent_pilot or clock_guard):raise ValueError('Finite local pilot requires target-local envelope model')
    folder=begin(iteration,'Off-event electrical projection generalizes beyond exposed stationary harmonic counterexamples',
        f'{count}fresh source/target draws, 3motion classes x4electrical regimes; float/PCM16 only where index>=24; calibration-only six source choices; unchanged exact-map and response limits',
        dict(negative='Zero complete V/F acceptance for stationary and wrong-motion controls',positive='All available-pilot constant/frequency-drift known-motion controls pass; unavailable pilot must withhold, not invent correction; report harder misses'),
        synthetic=dict(generation=generation,count=count,core_seconds=48,context_seconds=40,fit_edge_guard_seconds=edge_guard,source_pilot_offset_hz=source_pilot_offset_hz,
            budget=dict(peak_bytes=2*2**30,wall_seconds=900),
            pilot_mode=pilot_mode,weak_pilot_stratum=weak_pilot_stratum,
            masked_pilot_events=masked_pilot_events,
            response_envelope=envelope,
            independent_pilot=independent_pilot,
            embed_target_pilot=embed_target_pilot,clock_agreement_guard=clock_guard,target_pilot_offset_hz=target_pilot_offset_hz,
            residual_line_audit=residual_line_audit,
            finite_local_pilot=finite_local_pilot,
            case_factory=case_factory.__module__+'.'+case_factory.__name__ if case_factory else None,
            measurement_function=measurement_function.__module__+'.'+measurement_function.__name__ if measurement_function else None,
            extra_protocol=extra_protocol,
            response_projection='All12check-band harmonic coefficients fitted off all event metadata windows; no event-outcome fit',
            phase_jump='Target electrical phase changes.04radian at20s; reference pilot does not, tests transfer mismatch',real_data_accessed=False))
    rows=[];start=time.perf_counter()
    provider=case_factory or cases
    for case in provider(generation,pilot_mode,weak_pilot_stratum,count,source_pilot_offset_hz,embed_target_pilot,target_pilot_offset_hz):
        sr=case['sr'];hz=case['hz'];original=case['source'];core=original[40*sr:88*sr]
        try:
            measurement=remove_audited_clock_envelopes if residual_line_audit else remove_clock_guarded_envelopes if clock_guard else remove_off_event_envelopes if envelope else remove_masked_off_event_ground if masked_pilot_events else remove_off_event_ground
            if independent_pilot and (not envelope or pilot_mode!='source'):raise ValueError('Independent-pilot option requires separate source and envelope measurement')
            extra=dict(independent_pilot=True) if independent_pilot and not clock_guard else {}
            if measurement_function:
                target,meta=measurement_function(case['target_extended'],sr,case['calibration']+case['validation']+case['reserved'],calibration_events=case['calibration'],fit_edge_guard_seconds=edge_guard)
            elif finite_local_pilot:
                target,meta=remove_finite_local_envelopes(case['target_extended'],sr,case['calibration']+case['validation']+case['reserved'],fit_edge_guard_seconds=edge_guard)
            else:
                target,meta=measurement(case['target_extended'],case['target_extended'] if pilot_mode=='target' else original,sr,case['calibration']+case['validation']+case['reserved'],fit_edge_guard_seconds=edge_guard,**extra)
        except ValueError as exc:
            if not any(prefix in str(exc) for prefix in ('Electrical pilot unavailable/ambiguous','Response measurement unavailable/ambiguous')):raise
            row={k:v for k,v in case.items() if k not in ('source','target','target_extended')}
            row.update(response_withheld=True,reason=str(exc),source_sha256=sha(original.tobytes()),target_sha256=sha(case['target_extended'].tobytes()))
            if hasattr(exc,'metadata'):row['response_withhold_metadata']=exc.metadata
            rows.append(row);write(folder/'results.json',rows)
            write(folder/'phase.json',dict(completed_cases=len(rows),elapsed_seconds=time.perf_counter()-start,peak_bytes=memory_peak()))
            print(case['index'],case['kind'],case['shape'],case['pcm16'],'response_pilot_withheld',flush=True)
            continue
        target=target[40*sr:88*sr]
        adaptive,source_meta=derive_original(original,sr,40,88)
        trials=[];arrays={}
        for version in ('raw','stationary','adaptive_fullband'):
            for capture in (26.,35.):
                x=adaptive if version=='adaptive_fullband' else project_stationary_mains(core,sr,hz,capture)[0] if version=='stationary' else core
                trace=trajectory(x,sr,hz,capture);amp,_=source_amplitude_reference(trace[3],rate=RATE,available_end=48)
                curve,support=track_array(x,sr,hz,capture,amp)
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
        row={k:v for k,v in case.items() if k not in ('source','target','target_extended')}
        row.update(source_sha256=sha(original.tobytes()),target_sha256=sha(case['target_extended'].tobytes()),response_projection=meta,
                   adaptive_source=source_meta,trials=trials,selected=selected,preflight=preflight)
        if selected:
            curve,support,amp=arrays[selected['key']]
            write(folder/f"case_{case['index']:03d}_frozen.json",dict(selected=selected,validation_accessed=False))
            row['validation']=evaluate(target,sr,curve,support,amp,case['calibration'],case['validation'])
            if row['validation']['quality']['passed']:
                write(folder/f"case_{case['index']:03d}_final_consumed.json",dict(reserved=case['reserved'],before_measurement=True))
                row['reserved_assessment']=evaluate(target,sr,curve,support,amp,case['calibration'],case['reserved'])
        rows.append(row);write(folder/'results.json',rows)
        if memory_peak()>2*2**30 or time.perf_counter()-start>900:raise RuntimeError('Declared2GiB/15minute controlled budget exceeded')
        write(folder/'phase.json',dict(completed_cases=len(rows),elapsed_seconds=time.perf_counter()-start,peak_bytes=memory_peak()))
        print(case['index'],case['kind'],case['shape'],case['pcm16'],selected['key'] if selected else None,
              row.get('validation',{}).get('quality',{}).get('passed'),row.get('reserved_assessment',{}).get('quality',{}).get('passed'),flush=True)
    summary=summarize(rows)
    false=sum(v['final_pass'] for k,v in summary.items() if not k.startswith('true_motion:'))
    positive=all(summary[f'true_motion:{s}']['final_pass']==summary[f'true_motion:{s}']['cases']-summary[f'true_motion:{s}']['withheld'] and summary[f'true_motion:{s}']['final_pass']>0 for s in ('constant','frequency_drift'))
    validation_false=sum(v['validation_pass'] for k,v in summary.items() if not k.startswith('true_motion:'))
    finish(folder,dict(summary=summary,false_complete_accepts=false,negative_validation_accepts=validation_false,easy_positive_pass=positive,wall_seconds=time.perf_counter()-start,
           peak_working_set_bytes=memory_peak(),real_data_accessed=False),
           'development_domain_supported' if false==0 and positive else 'measurement_contract_not_ready',
           'Fresh response projection stress controls complete; hard regimes and all failures retained. No real gate or qualification upgrade.',
           'Diagnose residual null acceptances or positive misses before any real use; separate exposed development replay from independent qualification.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I028_response_fresh_controls')
    p.add_argument('--generation',default='fresh-response-measurement-v1');p.add_argument('--fit-edge-guard',type=float,default=0)
    p.add_argument('--pilot-mode',choices=['source','target'],default='source');p.add_argument('--weak-pilot-stratum',action='store_true')
    p.add_argument('--masked-pilot-events',action='store_true')
    p.add_argument('--response-envelope',action='store_true')
    p.add_argument('--independent-pilot',action='store_true')
    p.add_argument('--count',type=int,default=48);p.add_argument('--source-pilot-offset-hz',type=float,default=0)
    p.add_argument('--embed-target-pilot',action='store_true');p.add_argument('--clock-guard',action='store_true')
    p.add_argument('--target-pilot-offset-hz',type=float,default=0)
    p.add_argument('--residual-line-audit',action='store_true')
    p.add_argument('--finite-local-pilot',action='store_true')
    a=p.parse_args();run(a.iteration,a.generation,a.fit_edge_guard,a.pilot_mode,a.weak_pilot_stratum,a.masked_pilot_events,a.response_envelope,a.independent_pilot,a.count,a.source_pilot_offset_hz,a.embed_target_pilot,a.clock_guard,a.target_pilot_offset_hz,a.residual_line_audit,a.finite_local_pilot)
