"""Controlled replay of a new response measurement, not fresh promotion."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,sha,memory_peak
from deflutter_response_hum_null import cases
from deflutter_response_measurement import remove_off_event_ground
from deflutter_local_experiment import evaluate
from deflutter_local_acquisition import source_amplitude_reference,qualified_support_island
from deflutter_adaptive_source import derive_original
from deflutter_extractors import project_stationary_mains
from deflutter_real_experiment import make_map
from flutter_tracking import RATE,trajectory
from continuous_tracking import track_array


def extended_target(case):
    sr=case['sr'];t=np.arange(128*sr)/sr-40
    electrical=2*np.pi*(60*t+(.003/(2*np.pi*.02)*np.sin(2*np.pi*.02*t) if case['ground_drift'] else 0))
    if case['kind']=='one_interferer':target=.01875*np.cos(42*electrical)
    elif case['kind']=='symmetric_interferers':target=.15*.004*2490/150*(np.cos(42*electrical)-np.cos(41*electrical))
    else:target=.0001*np.cos(42*electrical)
    if case['pcm16']:target=np.rint(target*32768)/32768
    target[40*sr:88*sr]=case['target']
    return target


def run():
    folder=begin('I027_response_measurement','Off-event electrical projection distinguishes interference cancellation from true timing correction',
        'Replay exactly I026selected maps and synthetic arrays; estimate check-band harmonic coefficients only outside all metadata event windows',
        dict(null='No stationary null accepted after independent response contamination removal',positive='All four known-motion controls retain unchanged V/F gates'),
        synthetic=dict(parent='I026_response_hum_null',fresh_qualification=False,selection='No reselection; exact parent key and main map hash required',real_data_accessed=False))
    parents=read(ROOT/'iterations/I026_response_hum_null/results.json');rows=[];start=time.perf_counter()
    for case,parent in zip(cases(),parents):
        sr=case['sr'];target=extended_target(case);original=case['source']
        if sha(original.tobytes())!=parent['source_sha256'] or sha(case['target'].tobytes())!=parent['target_sha256']:raise ValueError('Parent sample regeneration mismatch')
        events=case['calibration']+case['validation']+case['reserved']
        cleaned,metadata=remove_off_event_ground(target,original,sr,events)
        selection=parent['selected'];version=selection['source'];capture=selection['capture_hz'];hz=case['hz'];core=original[40*sr:88*sr]
        extract=derive_original(original,sr,40,88)[0] if version=='adaptive_fullband' else project_stationary_mains(core,sr,hz,capture)[0] if version=='stationary' else core
        trace=trajectory(extract,sr,hz,capture);amplitude,_=source_amplitude_reference(trace[3],rate=RATE,available_end=48)
        curve,support=track_array(extract,sr,hz,capture,amplitude)
        support,island=qualified_support_island(support,case['calibration'],case['validation'],case['reserved'],minimum_final_events=2)
        pos,_,stats=make_map(curve,support,sr,len(case['target']))
        if stats['positions_sha256']!=selection['map_stats']['positions_sha256']:raise ValueError('Frozen selected main map not reproduced')
        clean_core=cleaned[40*sr:88*sr]
        row=dict(index=case['index'],kind=case['kind'],pcm16=case['pcm16'],ground_drift=case['ground_drift'],projection=metadata,
                 frozen_map_sha256=stats['positions_sha256'],parent_selected_key=selection['key'],source_sha256=parent['source_sha256'],target_sha256=parent['target_sha256'],
                 validation=evaluate(clean_core,sr,curve,support,trace[3],case['calibration'],case['validation']),
                 reserved=evaluate(clean_core,sr,curve,support,trace[3],case['calibration'],case['reserved']))
        rows.append(row);write(folder/'results.json',rows)
        print(case['index'],case['kind'],row['validation']['quality']['passed'],row['validation']['quality']['failed_predicates'],row['reserved']['quality']['passed'],flush=True)
    summary={kind:dict(cases=sum(r['kind']==kind for r in rows),validation_pass=sum(r['kind']==kind and r['validation']['quality']['passed'] for r in rows),
        final_pass=sum(r['kind']==kind and r['reserved']['quality']['passed'] for r in rows)) for kind in sorted({r['kind'] for r in rows})}
    finish(folder,dict(summary=summary,wall_seconds=time.perf_counter()-start,peak_working_set_bytes=memory_peak(),real_data_accessed=False),
        'development_replay_assessed','New measurement assessed only on exposed controlled cases. It does not repair prior real qualification or establish a validated alternative contract.',
        'Freeze fresh synthetic response-contamination stress draws before any new real gate; retain old consumed families as exposed diagnostics.')


if __name__=='__main__':run()
