"""Fresh combined full-band extraction, single-island and exact-map controls."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
from collections import Counter
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,write,sha,memory_peak
from deflutter_near_mains_controls import cases
from deflutter_adaptive_source import derive_original
from deflutter_extractors import project_stationary_mains
from deflutter_local_acquisition import source_amplitude_reference,qualified_support_island
from deflutter_map_preflight import select_preflight
from deflutter_real_experiment import make_map
from deflutter_motion_experiment import fidelity
from deflutter_local_experiment import evaluate
from continuous_tracking import track_array
from flutter_tracking import RATE,trajectory,evaluate_events
from flutter_planning import loss


def run():
    folder=begin('I023_fullband_controls',
        'Full-band original-source separation with exact single-island maps preserves faithful timing and rejects false-motion controls',
        'Raw, exact stationary projection and adaptive full-band source paths; one calibration-selected exact-map-valid choice before validation/final',
        dict(easy='Unquantized40dBnear/far pass signed fidelity and unchanged validation plus2reserved-event gate',
             controls='Zero accepted AM/zero/wrong-motion/hum-only/dropout; ambiguous0.1Hz kept separate and must not advance',
             hard='60/80dB and PCM16 reported per regime; no lowered fidelity, support, map or response limits'),
        synthetic=dict(generation='fullband-island-controls-v1',count=48,sr=8000,core_seconds=48,real_context_each_side=40,
            calibration=[4,8,12,16],validation=[24,28,32,36],reserved=[40,44],
            source='Separate known-motion reference with same hum, no response bursts; target independent2500Hz oscillator',
            tracking_context='Same bounded-core stream as real runner; full-band extraction alone uses40s real context each side',
            captures_hz=[26,35],amplitude='Source-only strongest complete8s median',
            map='Only contiguous source island containing allC/V and2reserved families; all exact controls preflight before target validation',
            budget=dict(peak_bytes=int(1.5*2**30),wall_seconds=900),real_data_accessed=False))
    rows=[];tick=time.perf_counter()
    for case in cases('fullband-island-controls-v1',include_reserved=True):
        original=case['reference_extended'];sr=case['sr'];hz=case['hz'];target=case['x'];n=len(target)
        raw_core=original[40*sr:88*sr]
        def progress(stage):
            peak=memory_peak()
            write(folder/'phase.json',dict(case_index=case['index'],phase='adaptive_fullband',
                elapsed_seconds=time.perf_counter()-tick,peak_working_set_bytes=peak,**stage))
            if peak>int(1.5*2**30):raise RuntimeError('Declared1.5GiB controlled-run memory ceiling')
            if time.perf_counter()-tick>900:raise RuntimeError('Declared15minute controlled-run time ceiling')
        adaptive,metadata=derive_original(original,sr,40,88,progress=progress)
        trials=[];scores=[];arrays={}
        for source in ('raw','stationary','adaptive_fullband'):
            for capture in (26.,35.):
                projection=None
                if source=='adaptive_fullband':
                    extract=adaptive
                elif source=='stationary':extract,projection=project_stationary_mains(raw_core,sr,hz,capture)
                else:extract=raw_core
                trace=trajectory(extract,sr,hz,capture)
                amplitude,amplitude_meta=source_amplitude_reference(trace[3],rate=RATE,available_end=48)
                curve,support=track_array(extract,sr,hz,capture,amplitude);amp=trace[3]
                key=f'{source}:{capture:g}'
                support,island=qualified_support_island(support,case['calibration'],case['validation'],case['reserved'],minimum_final_events=2)
                trial=dict(key=key,source=source,capture_hz=capture,amplitude_reference=amplitude,
                    amplitude_metadata=amplitude_meta,support_island=island,projection=projection)
                if support is not None:
                    try:
                        pos,_,stats=make_map(curve,support,sr,n)
                        metric=evaluate_events(target,sr,curve,case['calibration'],sample_positions=pos)
                        trial.update(calibration_loss=loss(metric),fidelity=fidelity(curve,case['truth'],support),map_stats=stats)
                        if trial['calibration_loss'] is not None:scores.append(trial);arrays[key]=(curve,support,amp)
                    except ValueError as exc:trial['invalid_map']=str(exc)
                trials.append(trial)
        selected,preflight=select_preflight(scores,arrays,sr,n)
        row={k:v for k,v in case.items() if k not in ('x','extended','reference_extended','truth')}
        row.update(source_sha256=sha(original.tobytes()),target_sha256=sha(target.tobytes()),adaptive=metadata,trials=trials,
            preflight=preflight,selected=selected,reserved_consumed=False)
        if selected:
            curve,support,amp=arrays[selected['key']]
            write(folder/f"case_{case['index']:03d}_frozen.json",dict(selected=selected,preflight_passed=True,validation_outcomes_accessed=False))
            row['validation_assessment']=evaluate(target,sr,curve,support,amp,case['calibration'],case['validation'])
            if row['validation_assessment']['quality']['passed']:
                write(folder/f"case_{case['index']:03d}_final_consumed.json",dict(selected_key=selected['key'],reserved=case['reserved'],consumed_before_measurement=True))
                row['reserved_consumed']=True
                row['final_assessment']=evaluate(target,sr,curve,support,amp,case['calibration'],case['reserved'])
        rows.append(row);write(folder/'results.json',rows)
        write(folder/'phase.json',dict(case_index=case['index'],phase='case_committed',completed_cases=len(rows),peak_working_set_bytes=memory_peak()))
        print(folder.name,case['index'],case['kind'],case['hum_to_carrier_db'],case['pcm16'],
            selected['key'] if selected else None,row.get('validation_assessment',{}).get('quality',{}).get('passed'),
            row.get('final_assessment',{}).get('quality',{}).get('passed'),flush=True)
    summary={}
    for kind in sorted({r['kind'] for r in rows}):
        group=[r for r in rows if r['kind']==kind]
        summary[kind]=dict(cases=len(group),fidelity_pass=sum(r['selected'].get('fidelity',{}).get('pass_fidelity',False) for r in group if r['selected']),
            validation_pass=sum(r.get('validation_assessment',{}).get('quality',{}).get('passed',False) for r in group),
            final_pass=sum(r.get('final_assessment',{}).get('quality',{}).get('passed',False) for r in group))
    negative=sum(summary[k]['validation_pass'] for k in ('am_only','zero','wrong_motion','hum_only','dropout'))
    easy=[r for r in rows if r['kind'] in ('near','far') and r['hum_to_carrier_db']==40 and not r['pcm16']]
    passed=negative==0 and summary['coincident']['validation_pass']==0 and all(r.get('final_assessment',{}).get('quality',{}).get('passed',False) and r['selected']['fidelity']['pass_fidelity'] for r in easy)
    finish(folder,dict(summary=summary,negative_accepted=negative,advance_to_unused_gate=passed,
        wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak(),real_data_accessed=False),
        'freeze_unused_real_gate' if passed else 'reject_combined_method',
        'Fresh combined-method controls completed. Every hard miss and false/ambiguous acceptance is retained; no real qualification from synthetic data.',
        'If controlled domain passes: freeze new dense unused-block protocol with original/denoised catalogs preserved; otherwise diagnose failed stage without relaxing gates.')


if __name__=='__main__':run()
