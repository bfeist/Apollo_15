"""Once-only local reserved audit of already-frozen exact main/control maps.

Global reserved mission blocks are not consumed. This is retrospective real
evidence, not untouched material, perceptual approval or production promotion.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from pathlib import Path
from deflutter_campaign import ROOT,begin,finish,read,write,sha,load_excerpt,exposure
from deflutter_map_preflight import load_required_positions
from flutter_tracking import evaluate_events
from continuous_quality import classify_numeric_quality
from flutter_policy import LIMITS,evidence_strength


def run(iteration,result_iterations):
    folder=begin(iteration,'Frozen exact-map local improvement persists on its unused reserved response families',
        'One chosen map/control set saved before validation; no refit, alternative selection, reanchor or cleanup change',
        dict(required='>=2unique final families in same continuous C/V-supported island; unchanged numeric/control/absolute goals',
             skip='Failed/incomplete validation never opens reserved outcomes',
             exposure='All v5material potentially historical exposure; no untouched or production claim'),
        parents=result_iterations,audit_kind='Per-event reserved families within promotion blocks; global reserved mission blocks untouched')
    invs={i['tape']:i for i in read(ROOT/'inventory.json')['tapes']};results=[]
    for parent_name in result_iterations:
        parent=ROOT/'iterations'/parent_name
        if not (parent/'results.json').exists():
            results.append(dict(parent=parent_name,status='parent_incomplete_no_reserved_read'));continue
        for result in read(parent/'results.json'):
            identity=f"{parent_name}_{result['tape']}_{result['start']}_CH{result['target_channel']:02d}"
            row=dict(parent=parent_name,identity=identity,tape=result['tape'],target_channel=result['target_channel'])
            if not result.get('quality',{}).get('passed',False):
                results.append(dict(row,status='validation_not_passed_no_reserved_read'));continue
            target=parent/f"{result['tape']}_{result['start']}_CH{result['target_channel']:02d}"
            events=result.get('reserved_for_audit',[])
            groups=[e['synchronized_group_id'] for e in events]
            if len(events)<2 or len(set(groups))!=len(events):
                results.append(dict(row,status='insufficient_unique_local_final_families'));continue
            marker=read(parent/f"{result['tape']}_{result['start']}_CH{result['target_channel']:02d}_validation_consumed.json")
            if marker.get('response_role_protocol')!='lag-aware-response-groups-research-v2' or not marker.get('exact_map_preflight_passed'):
                raise ValueError('Unsupported response role/preflight contract; do not inspect reserved outcomes')
            for name,key in [('frozen_before_validation.json','frozen_model_sha256'),('reserved_manifest.json','reserved_manifest_sha256'),('required_map_manifest.json','required_map_manifest_sha256')]:
                if sha((target/name).read_bytes())!=marker.get(key):raise ValueError('Before-validation frozen record identity changed')
            sealed=read(target/'reserved_manifest.json')
            if events!=sealed['events']:raise ValueError('Reserved selection changed after validation')
            common=result['common_acquisition'];development=common['roles']['calibration']+common['roles']['validation']
            if set(groups)&{e['synchronized_group_id'] for e in development}:raise ValueError('Reserved response family reused in development')
            island=result['selected']['support_island']['interval_seconds']
            if any(not island[0]<=e['start']<e['end']<=island[1] for e in events):raise ValueError('Final families cross unsupported islands')
            inv=invs[result['tape']];channel=next(r for r in inv['channels'] if r['channel']==result['target_channel'])
            x,sr,source=load_excerpt(inv,channel,result['start'],result['end'],'original')
            if source['decoded_sha256']!=result['target_source']['decoded_sha256']:raise ValueError('Frozen target audio identity changed')
            manifest=read(target/'required_map_manifest.json')
            if manifest['frames']!=len(x) or manifest['sample_rate']!=sr:raise ValueError('Frozen map frame/rate binding differs')
            consumed=folder/(identity+'_consumed.json')
            if consumed.exists():raise ValueError('Reserved attempt already consumed; no automatic retry')
            write(consumed,dict(status='consumed_before_measurement',events=events,source=source,
                frozen_map_manifest_sha256=sha((target/'required_map_manifest.json').read_bytes()),no_refit=True))
            exposure(iteration,[dict(identity=identity,events=events,source=source)],'reserved_gate_consumed_before_attempt',
                'Once-only exact-map local audit; failures cannot become new untouched evidence')
            try:
                measured={}
                for name,entry in manifest['maps'].items():
                    positions=load_required_positions(target,entry,sr,len(x))
                    measured[name]=evaluate_events(x,sr,None,events,sample_positions=positions)
                controls={k:measured[k] for k in ('no_motion','wrong_sign','amplitude')}
                shifts={str(s):measured[f'time_{s:g}'] for s in LIMITS['wrong_time_seconds']}
                cal=[dict(e,start=e['start_seconds'],end=e['end_seconds']) for e in common['roles']['calibration']]
                quality=classify_numeric_quality(measured['main'],controls,shifts,supported=True,
                    independent_windows=evidence_strength(cal,events)['level']=='multiple_time_windows')
                row.update(status='local_reserved_numeric_pass' if quality['passed'] else 'local_reserved_failed',
                    quality=quality,measurements=measured,source=source,reserved_families=len(groups),
                    original_exposure='Prospectively frozen retrospective only',listening_approved=False,production_accepted=False)
            except Exception as exc:
                row.update(status='reserved_attempt_failed_consumed',error=str(exc),production_accepted=False)
            results.append(row);write(folder/'results.json',results)
            print(iteration,identity,row['status'],row.get('quality',{}).get('measurements',{}).get('median_event_ratio'),flush=True)
    write(folder/'results.json',results)
    measured=[r for r in results if 'quality' in r]
    finish(folder,dict(targets=len(results),reserved_targets_evaluated=len(measured),
        numeric_pass=sum(r['quality']['passed'] for r in measured),restoration_goal_pass=sum(r['quality']['restoration_target_met'] for r in measured),
        global_reserved_mission_blocks_consumed=False,untouched_real_qualification=False,listening_approved=False,production_accepted=False),
        'local_retrospective_audit_recorded','All eligible fixed reserved outcomes retained; no source origin, listening, untouched-real or production upgrade.',
        'Assess actual matched outputs and source identity, then lifetime/replication with unchanged gates; preserve every consumed outcome.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I024_reserved_audit');p.add_argument('--parents',required=True)
    a=p.parse_args();run(a.iteration,a.parents.split(','))
