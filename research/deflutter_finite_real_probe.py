"""Exposed real measurement-input scopes; never a new timing-quality gate."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure,memory_peak
from deflutter_response_local_finite import remove_finite_local_envelopes


def run(*,measurement_function=remove_finite_local_envelopes,iteration='I045_finite_real_inputs',
        hypothesis='Target-local finite pilot avoids the demonstrated cross-channel electrical-clock mismatch on exposed real audio'):
    normal=read(ROOT/'iterations/I043_finite_local_controls/metrics.json');wrong=read(ROOT/'iterations/I044_finite_wrong_clock/metrics.json')
    if normal['negative_validation_accepts'] or normal['false_complete_accepts'] or not normal['easy_positive_pass'] or sum(v['withheld'] for v in wrong['summary'].values())!=12:
        raise ValueError('Controlled prerequisites not met; no real probe')
    parent=ROOT/'iterations/I024_gate2';r=read(parent/'results.json')[0];common=r['common_acquisition']
    scopes=[dict(name='full_passage',start=0.,end=r['end']-r['start']),
            dict(name='source_frozen_acquisition',start=common['start_seconds'],end=common['end_seconds'])]
    folder=begin(iteration,hypothesis,
        'Two predeclared scopes:full original300seconds and the earlier source-frozen60..180acquisition interval;40sreal context each side; no map change or phase-error scoring',
        dict(scope='Measurement-input/source-feature diagnostic only, not an alternative validation attempt',budget='At most380soriginal target plus nested200sview,3GiB/5minutes',
             restrictions='Keep full-scope failure and bounded-scope result separate; no map clipping/reanchoring, no real qualification from source-model guards'),
        scopes=scopes,parent='I024_gate2',new_real_frequency_error_outcomes_accessed=False,
        reserved_access='Existing role coordinates and amplitude features; no reserved timing-error outcome',
        measurement_function=measurement_function.__module__+'.'+measurement_function.__name__)
    start=time.perf_counter();inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==r['tape'])
    channel=next(c for c in inv['channels'] if c['channel']==r['target_channel'])
    original,sr,source=load_excerpt(inv,channel,r['start']-40,r['end']+40,'original')
    if sha(original[40*sr:-40*sr].tobytes())!=r['target_source']['decoded_sha256']:raise ValueError('Frozen original target binding differs')
    roles=read(parent/'roles_0.json')['roles'][str(r['target_channel'])];known=[e for values in roles.values() for e in values]
    write(folder/'source_manifest.json',dict(source=source,known_event_metadata=known,
        context_caveat='Only existing metadata events masked; unknown context events outside the300second passage can remain'))
    rows=[]
    for scope in scopes:
        a,b=scope['start'],scope['end'];x=original[round(a*sr):round((b+80)*sr)]
        events=[dict(e,start=e['start']-a,end=e['end']-a) for e in known if a-40+.15<=e['start'] and e['end']<=b+40-.15]
        row=dict(scope=scope,physical_source_extent=[r['start']+a-40,r['start']+b+40],raw_input_sha256=sha(x.tobytes()),metadata_events=events)
        try:
            measured,meta=measurement_function(x,sr,events)
            core=measured[40*sr:-40*sr];path=folder/(scope['name']+'_measurement.npz')
            np.savez_compressed(path,samples=core,sample_rate=sr)
            row.update(status='measurement_inputs_supported_development_only',model=meta,measurement_file=path.name,
                stored_file_sha256=sha(path.read_bytes()),decoded_measurement_sha256=sha(core.tobytes()))
        except ValueError as exc:row.update(status='measurement_withheld',reason=str(exc),metadata=getattr(exc,'metadata',None))
        rows.append(row);write(folder/'results.json',rows)
        print(scope['name'],row['status'],row.get('reason'),row.get('metadata'),flush=True)
        if memory_peak()>3*2**30 or time.perf_counter()-start>300:raise RuntimeError('Declared3GiB/5minute probe budget exceeded')
    supported=sum(r['status']=='measurement_inputs_supported_development_only' for r in rows)
    exposure(folder.name,[dict(source=source,scopes=scopes,features='Existing event metadata/amplitudes and electrical/nuisance-model source features only')],
        'exposed_measurement_input_probe','No calibration-loss/validation/final timing-error measurement, no source/model/map reselection or promotion')
    finish(folder,dict(scopes=len(rows),supported_input_scopes=supported,scope_statuses=[dict(scope=x['scope'],status=x['status'],reason=x.get('reason')) for x in rows],
        wall_seconds=time.perf_counter()-start,peak_working_set_bytes=memory_peak(),new_real_frequency_outcomes_accessed=False,qualification_upgraded=False),
        'freeze_unused_measurement_gate' if supported else 'diagnose_input_withholding',
        'Both predeclared real input scopes retained; full-scope and bounded-scope behavior distinct. No actual correction-map outcome was evaluated.',
        'If supported: integrate explicitly frozen derived measurement and pilot confidence into an unused gate; otherwise diagnose source-model failure without relaxing guards.')


if __name__=='__main__':run()
