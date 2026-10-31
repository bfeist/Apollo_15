"""Posthoc response-measurement diagnostic of immutable I024maps on consumed V.

No candidate selection, new gate, reserved outcome, production or independent
qualification. Changed response contract cannot promote exposed real families.
"""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import argparse
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak,sha
from deflutter_response_measurement import remove_off_event_ground
from deflutter_map_preflight import load_required_positions
from deflutter_output_beep_check import summarize
from flutter_tracking import evaluate_events


def run(iteration='I029_response_real_diagnostic'):
    control=read(ROOT/'iterations/I030_response_guarded_controls/metrics.json')
    if control['false_complete_accepts'] or not control['easy_positive_pass']:
        raise ValueError('Fresh control domain not supported; no real diagnostic launched')
    parent=ROOT/'iterations/I024_gate2';result=read(parent/'results.json')[0]
    folder=begin(iteration,'I024consumed validation-tone motion reduction survives separate off-event electrical measurement',
        'One immutable selected map and saved controls; original target with40sphysical context and reference pilot; only already-consumed Vevents',
        dict(scope='Posthoc diagnostic, not an alternative qualification attempt; preserve all original outcomes',budget='Single380second original target/pilot pair,6GiB/5minute ceiling; no new target/candidate/Fscore'),
        parent='I024_gate2',response_contract='off-event-ground-response-development-v2',fresh_evidence=False)
    start=time.perf_counter()
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==result['tape'])
    ch=next(c for c in inv['channels'] if c['channel']==result['target_channel'])
    ref=next(c for c in inv['channels'] if c['channel']==result['selected']['reference_channel'])
    lo=result['start'];hi=result['end']
    target,sr,target_source=load_excerpt(inv,ch,lo-40,hi+40,'original')
    pilot,refsr,pilot_source=load_excerpt(inv,ref,lo-40,hi+40,'original')
    if sr!=refsr:raise ValueError('Physical source timebases differ')
    activity={name:dict(peak=float(np.max(abs(x))),clip_samples=int(np.sum(abs(x)>=.999)))
              for name,x in [('target',target),('pilot',pilot)]}
    write(folder/'source_activity.json',activity)
    if any(r['clip_samples'] for r in activity.values()):
        raise ValueError('Clipped physical context cannot support this linear ground diagnostic')
    roles=read(parent/'roles_0.json')['roles'][str(result['target_channel'])]
    exclude=[dict(e,start=e['start'],end=e['end']) for values in roles.values() for e in values]
    # Exclusion reads event coordinates only; never compute reserved metrics.
    n=round((hi-lo)*sr);raw=target[40*sr:40*sr+n]
    if sha(raw.tobytes())!=result['target_source']['decoded_sha256']:
        raise ValueError('Original target no longer matches frozen consumed passage')
    expected_ref=result['selected']['source'].get('parent_source',result['selected']['source'])['decoded_sha256']
    if result['selected']['source']['parent_extent_seconds']!=[lo-40,hi+40]:
        raise ValueError('Physical pilot extent differs from frozen parent extent')
    if sha(pilot.tobytes())!=expected_ref:
        raise ValueError('Physical pilot passage differs from frozen original reference')
    cleaned,metadata=remove_off_event_ground(target,pilot,sr,exclude,fit_edge_guard_seconds=30)
    clean=cleaned[40*sr:40*sr+n]
    events=[{k:v for k,v in e.items() if k!='margins'} for e in result['validation']]
    if any(e['role']!='validation' for e in events):raise ValueError('Only consumed Vcoordinates allowed')
    directory=parent/f"{result['tape']}_{lo}_CH{result['target_channel']:02d}"
    manifest=read(directory/'required_map_manifest.json');outputs={}
    for version,x in [('original',raw),('off_event_projection',clean)]:
        measurements={}
        for name,entry in manifest['maps'].items():
            positions=load_required_positions(directory,entry,sr,n)
            values=evaluate_events(x,sr,None,events,sample_positions=positions)
            measurements[name]=dict(summary=summarize(values),measurements=values)
        outputs[version]=measurements
        if memory_peak()>6*2**30 or time.perf_counter()-start>300:raise RuntimeError('Declared diagnostic budget exceeded; preserve partial state')
        write(folder/'results.json',outputs)
    write(folder/'source_manifest.json',dict(target=target_source,pilot=pilot_source,projection=metadata,excluded_metadata_events=exclude,
          reserved_outcomes_accessed=False,unmasked_context_events='Only known frozen passage metadata available; events outside passage may be unmasked'))
    exposure(folder.name,[dict(source=target_source,events=events)],'consumed_validation_diagnostic_changed_response_measurement',
        'No reselection or new qualification; reserved outcomes not evaluated; original event scores remain authoritative for their frozen contract')
    summary={v:{k:r['summary'] for k,r in m.items()} for v,m in outputs.items()}
    finish(folder,dict(summary=summary,reserved_outcomes_accessed=False,qualification_upgraded=False,
        peak_working_set_bytes=memory_peak(),wall_seconds=time.perf_counter()-start),
        'posthoc_response_diagnostic_recorded','Exact frozen maps assessed on consumed Vonly under a new development measurement; no fresh real qualification.',
        'Assess output quality and measurement/source-transfer assumptions; freeze independent controls and unused blocks for any new gate.')
    print(summary,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I029_response_real_diagnostic')
    run(p.parse_args().iteration)
