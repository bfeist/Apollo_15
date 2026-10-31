"""Qualification of measurement inputs on exposed real audio, no beep scoring."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure,memory_peak
from deflutter_response_line_audit import remove_audited_clock_envelopes


def run():
    normal=read(ROOT/'iterations/I039_line_audit_controls/metrics.json')
    wrong=read(ROOT/'iterations/I040_coherent_wrong_audit/metrics.json')
    wrong_withheld=sum(v['withheld'] for v in wrong['summary'].values())
    if normal['negative_validation_accepts'] or normal['false_complete_accepts'] or not normal['easy_positive_pass'] or wrong['false_complete_accepts'] or wrong_withheld!=12:
        raise ValueError('Frozen controlled prerequisites not satisfied; no real probe')
    parent=ROOT/'iterations/I024_gate2';r=read(parent/'results.json')[0]
    folder=begin('I041_real_measurement_inputs','A recorded real numerical gain has a source/target electrical model that passes independent input guards',
        'One already-selected original source/target pair; clock agreement and off-event residual-line audit only, no phase-error or candidate loss',
        dict(scope='Exposed retrospective measurement-input diagnostic; no validation/final gate or qualification upgrade',budget='One380second pair,3GiB/5minutes, originals/caches read-only'),
        parent='I024_gate2',reference_channel=r['selected']['reference_channel'],target_channel=r['target_channel'],
        response_event_amplitudes='Existing event metadata windows, including reserved-membership coordinates; no frequency-error outcome measurement')
    start=time.perf_counter();invs=read(ROOT/'inventory.json')['tapes'];inv=next(i for i in invs if i['tape']==r['tape'])
    target_row=next(c for c in inv['channels'] if c['channel']==r['target_channel'])
    pilot_row=next(c for c in inv['channels'] if c['channel']==r['selected']['reference_channel'])
    all_roles=read(parent/'roles_0.json')['roles']
    if any(all_roles[str(pilot_row['channel'])].values()):raise ValueError('Pilot has known response events; distinct source is not sufficient independence')
    target,sr,ts=load_excerpt(inv,target_row,r['start']-40,r['end']+40,'original')
    pilot,psr,ps=load_excerpt(inv,pilot_row,r['start']-40,r['end']+40,'original')
    if psr!=sr or sha(target[40*sr:-40*sr].tobytes())!=r['target_source']['decoded_sha256']:raise ValueError('Frozen target source binding changed')
    if ps['decoded_sha256']!=r['selected']['source']['parent_source']['decoded_sha256']:raise ValueError('Frozen physical-parent pilot binding changed')
    events=[e for role in all_roles[str(target_row['channel'])].values() for e in role]
    write(folder/'source_manifest.json',dict(target=ts,pilot=ps,events=events,pilot_known_response_events=0,
        context_limits='Unknown response events outside the300second metadata passage may remain in physical40second context'))
    clips=dict(target=int(np.sum(abs(target)>=.999)),pilot=int(np.sum(abs(pilot)>=.999)))
    row=dict(parent='I024_gate2',clips=clips,beep_frequency_error_outcomes_accessed=False)
    try:
        if any(clips.values()):raise ValueError('Clipped physical context; linear measurement withheld')
        measured,meta=remove_audited_clock_envelopes(target,pilot,sr,events)
        core=measured[40*sr:-40*sr]
        np.savez_compressed(folder/'response_measurement.npz',samples=core,sample_rate=sr)
        row.update(status='measurement_inputs_supported_development_only',measurement=meta,decoded_measurement_sha256=sha(core.tobytes()),
            stored_file_sha256=sha((folder/'response_measurement.npz').read_bytes()))
    except ValueError as exc:
        row.update(status='measurement_withheld',reason=str(exc),metadata=getattr(exc,'metadata',None))
    write(folder/'results.json',row)
    if memory_peak()>3*2**30 or time.perf_counter()-start>300:raise RuntimeError('Declared probe budget exceeded')
    exposure(folder.name,[dict(source=ts,roles='Known metadata coordinates/amplitude features only; no phase-error outcome')],
        'measurement_input_features_only_on_exposed_material','No model/map reselection, correction rendering or reserved frequency-error measurement')
    finish(folder,dict(status=row['status'],reason=row.get('reason'),wall_seconds=time.perf_counter()-start,
        peak_working_set_bytes=memory_peak(),new_real_frequency_outcomes_accessed=False,qualification_upgraded=False),
        'freeze_unused_measurement_gate' if row['status']=='measurement_inputs_supported_development_only' else 'diagnose_measurement_inputs',
        'Exposed original source/target measurement inputs inspected; no new validation/final outcome or physical-origin upgrade.',
        'If supported: freeze source-only pilot selection and derived-target binding for an unused gate. If withheld: retain explicit cause and continue independent diagnosis.')
    print(row['status'],row.get('reason'),row.get('metadata'),flush=True)


if __name__=='__main__':run()
