"""Small deterministic fixtures for exercising the desk, never mission evidence."""
import hashlib
from pathlib import Path

import numpy as np
import soundfile as sf

from carrier_survey import SETTINGS, source_record
from channel_review import continuous_map, denoised_signature, verify_audio
from flutter_policy import digest, implementation_digest, policy_digest, METHODOLOGY
from review_sampling import PILOTS, bound, immutable_json, policy_hash


def make_demo(parent):
    # Changes to code produce a fresh fixture; existing demo judgments stay intact.
    root = Path(parent).resolve()/(implementation_digest()+policy_hash())[:20]
    config = dict(originals=str(root/'originals'), surveys=str(root/'surveys'), plans=str(root/'plans'),
                  denoised=str(root/'denoised'), state=str(root/'desk'), demo=True,
                  evidence_roots=[str(root/'evidence')], methodology_evidence={})
    if (root/'ready.json').exists():
        from review_sampling import read_json
        return read_json(root/'ready.json')
    sr, duration = 8000, 120
    tapes = list(PILOTS)+['A15_T599999']
    for index, tape in enumerate(tapes):
        original = root/'originals'/tape
        original.mkdir(parents=True, exist_ok=True)
        source = original/f'{tape}_HR1U_CH2_VOICE_207-00-00-00_207-00-02-00.wav'
        plan_dir = root/'plans'/tape
        plan_dir.mkdir(parents=True, exist_ok=True)
        source_times = np.arange(sr*duration)/sr
        # Establish a header before constructing the exact continuous map.
        sf.write(source, np.zeros(sr*duration), sr, subtype='FLOAT')
        regions = []
        group_tape = index == 4
        for i in range(3):
            a, b = i*40, (i+1)*40
            lo, hi = max(0,a-4), min(duration,b+4)
            tt = lo+np.arange(round((hi-lo)*200))/200
            speed = .009*np.sin(2*np.pi*6.5*tt)
            support = np.ones(len(tt))
            name = f'r{i:05d}'
            key = 'ch1_signed_2_15'
            if i == 1 and index == 2:
                key = 'ch3_signed_2_30'
            maps = plan_dir/(name+'_maps.npz')
            np.savez_compressed(maps, **{key:speed, key+'__support':support})
            target = dict(channel=2, selected=key, action='group_render_and_review' if group_tape else 'render_and_review',
                          reason='Synthetic direct/group evidence; demo only', numeric_transfer_passed=not group_tape,
                          reference_channel=1, evidence=dict(level='multiple_time_windows', transfer_group_supported=group_tape),
                          holdout_event_count=0 if group_tape else 2,
                          holdout=[dict(margins=[dict(before_rms_hz=15.9, after_rms_hz=.4, after_rms_cents=.3, residual_ratio=.025)])],
                          transfer_group=dict(passed=True, lag_fitted=False) if group_tape else {},
                          reserved_events=[dict(start=a+12-lo, end=a+12.3-lo),dict(start=a+27-lo,end=a+27.3-lo)],
                          reference_spans=[dict(start_seconds=a,end_seconds=b,action='use_reference')])
            region = dict(id=name,start_seconds=a,end_seconds=b,analysis_start=lo,analysis_end=hi,status='analyzed',
                          search_budget=dict(exhausted=False), targets=[target],
                          variants=[dict(key=key,reference_channel=3 if 'ch3' in key else 1,method='signed_2_30' if 'ch3' in key else 'signed_2_15')],
                          maps=dict(file=maps.name,sha256=digest(maps),rate=200))
            immutable_json(plan_dir/(name+'.json'),region)
            regions.append(dict(region,detail_file=name+'.json',detail_sha256=digest(plan_dir/(name+'.json'))))
        tt, nominal, *_ = continuous_map(plan_dir,regions,2,0,sr*duration,sr,progress=lambda _:None)
        # Synthetic harmonic voice-like signal plus reserved measurement tones.
        tau = np.interp(source_times, tt, nominal)
        envelope = .03*(.3+.7*np.sin(2*np.pi*.8*source_times)**2)
        voice_phase = (123+index*7)*tau+.071*tau*tau
        x = envelope*(np.sin(2*np.pi*voice_phase)+.5*np.sin(4*np.pi*voice_phase)+.2*np.sin(12*np.pi*voice_phase))
        for a in range(0,duration,40):
            for event in (a+12,a+27):
                mask = (source_times >= event-.2)&(source_times < event+.5)
                x[mask] += .2*np.sin(2*np.pi*2500*tau[mask])
        sf.write(source,x,sr,subtype='FLOAT')
        stat = source.stat()
        row = dict(channel=2,path=str(source),role='reference_and_target',head='HR1U',bytes=stat.st_size,
                   mtime_ns=stat.st_mtime_ns,sample_rate=sr,frames=len(x),start_seconds=0,end_seconds=duration)
        den = root/'denoised'/tape/'CH02'
        den.mkdir(parents=True,exist_ok=True)
        sf.write(den/'denoised.wav',x,sr,subtype='FLOAT')
        (den/'cleanup.jsonl').write_text('{"demo":true}\n',encoding='utf-8')
        immutable_json(den/'denoised.json',dict(status='complete',signature=denoised_signature(row,0,len(x)),
                                              audio=verify_audio(den/'denoised.wav',len(x),sr),
                                              cleanup_log_sha256=digest(den/'cleanup.jsonl')))
        survey_dir = root/'surveys'/tape
        survey_dir.mkdir(parents=True,exist_ok=True)
        immutable_json(survey_dir/'source.json',dict(source=source_record(source),settings=SETTINGS,
                       implementation_sha256=digest(Path(__file__).resolve().parents[1]/'scripts'/'carrier_survey.py'),
                       complete=True,next_sample=len(x),feature_blocks=[],islands=[]))
        immutable_json(survey_dir/'survey.json',dict(source=str(original),tape=tape,complete=True,settings=SETTINGS,
                       files=[dict(path=str(source),cache='source.json',sha256=digest(survey_dir/'source.json'),complete=True)],demo=True))
        plan = dict(schema=1,methodology=METHODOLOGY,policy_sha256=policy_digest(),implementation_sha256=implementation_digest(),
                    complete=True,source_validation='verified',full_tape=True, demo=True,
                    inventory=dict(tape=tape,channels=[row],carrier_survey=bound(survey_dir/'survey.json')),regions=regions)
        immutable_json(plan_dir/'plan.json',plan)
        annotations = {f'r{i:05d}':{'2':dict(conditions=['faint_speech','strong_correction','fade','dropout'] if i == 1 else [])} for i in range(3)}
        immutable_json(plan_dir/'review_conditions.json',annotations)
    for name in ('carrier_qualification','fresh_holdouts'):
        path = root/'evidence'/(name+'.json')
        immutable_json(path,dict(status='accepted',demo=True,evidence='Synthetic fixture, never qualified mission evidence'))
        config['methodology_evidence'][name] = bound(path)
    immutable_json(root/'ready.json',config)
    return config
