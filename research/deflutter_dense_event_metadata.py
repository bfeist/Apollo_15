"""Frozen dense-passage selection from coordinate-only historical metadata.

No saved correction scores, reference successes or final response metrics are
returned. The entire historical archive is potentially previously exposed.
"""
import argparse
from pathlib import Path
from deflutter_campaign import ROOT,SAVED,SEED,read,write,sha,stamp,exposure


def run(generation,output_name,exclude_manifest,minimum_events=4):
    split=read(ROOT/'split_manifest.json');invs=[i for i in read(ROOT/'inventory.json')['tapes'] if 'channels' in i]
    roles={b['block']:b['role'] for b in split['blocks']}
    excluded={p['block'] for p in split['development_passages']}
    if exclude_manifest:excluded.update(p['block'] for p in read(Path(exclude_manifest))['passages'])
    available={};evidence=[];errors=[]
    for inv in invs:
        tape=inv['tape'];base=SAVED/tape/'plan'
        try:
            planpath=base/'plan.json';planraw=planpath.read_bytes()
            expected=next(f['sha256'] for f in inv['baseline_files'] if f['path'].replace('\\','/').endswith('/plan/plan.json'))
            if sha(planraw)!=expected:raise ValueError('Initial frozen plan identity changed')
            plan=read(planpath);detailpath=base/plan['regions'][0]['detail_file'];raw=detailpath.read_bytes()
            import json
            saved=json.loads(raw).get('final_holdouts',{})
            events={}
            for ch,rows in saved.items():
                independent=[]
                coords=sorted({(float(e['start_seconds']),float(e['end_seconds'])) for e in rows})
                for a,b in coords:
                    if not independent or a-independent[-1]['start_seconds']>=3:
                        independent.append(dict(start_seconds=a,end_seconds=b))
                events[int(ch)]=independent
            del saved,raw,plan
            proposals={};origin=inv['origin_filename_seconds']
            source_rows={r['channel']:r for r in inv['channels']}
            for ch,coords in events.items():
                if ch not in source_rows:continue
                source=source_rows[ch]
                for event in coords:
                    start=max(40,int(event['start_seconds']//60)*60-120);end=start+300
                    absolute=origin+start;block=int(absolute//1800)
                    if block in excluded or roles.get(block)!='promotion_validation':continue
                    if absolute-40<block*1800+360 or absolute+340>(block+1)*1800-360:continue
                    if start-40<source['start_seconds'] or end+40>source['end_seconds']:continue
                    count=sum(start+3<=e['start_seconds'] and e['end_seconds']<=end-3 for e in coords)
                    if count<minimum_events:continue
                    proposal=proposals.setdefault(start,dict(tape=tape,start_seconds=start,duration_seconds=300.,
                        absolute_start=absolute,block=block,role='promotion_validation',
                        prior_exposure='All v5 previously exposed; coordinate-only dense sampling; not untouched',
                        dependence_group=f'filename_clock_block_{block}',availability_channels=[]))
                    if not any(x['channel']==ch for x in proposal['availability_channels']):
                        proposal['availability_channels'].append(dict(channel=ch,independent_historical_coordinate_count=count))
            available[tape]=list(proposals.values())
            evidence.append(dict(tape=tape,plan_path=str(planpath),plan_sha256=sha(planraw),detail_path=str(detailpath),
                detail_sha256=sha(detailpath.read_bytes()),fields_used=['final_holdouts.channel','final_holdouts.start_seconds','final_holdouts.end_seconds'],
                eligible_window_count=len(proposals)))
        except Exception as exc:errors.append(dict(tape=tape,error=str(exc)));available[tape]=[]
    tapes=sorted(available);selected=[]
    for index in range(3):
        stratum=tapes[index*len(tapes)//3:(index+1)*len(tapes)//3]
        candidates=[p for tape in stratum for p in available[tape] if p['block'] not in {x['block'] for x in selected}]
        if not candidates:continue
        # Fixed hash of tape then window: never a correction score or success.
        candidate_tapes=sorted({p['tape'] for p in candidates})
        tape=min(candidate_tapes,key=lambda t:sha(f'{SEED}:{generation}:dense-tape:{t}'.encode()))
        p=min((p for p in candidates if p['tape']==tape),key=lambda p:sha(f'{SEED}:{generation}:dense-window:{tape}:{p["start_seconds"]}'.encode()))
        selected.append(dict(p,archive_stratum=index))
    destination=ROOT/output_name
    if destination.exists():raise ValueError('Dense selection already frozen')
    write(destination,dict(at=stamp(),generation=generation,passages=selected,minimum_historical_coordinate_count=minimum_events,
        role='promotion_validation',exclusion_blocks=sorted(excluded),source_context_each_side=40,
        selection='Fixed tape/window SHA256 minima in3archive-order strata among coordinate-dense300s windows; independent>=3s metadata counts; no correction outcomes',
        true_mission_time_authenticated=False,limitation='Availability-conditioned retrospective sample, not representative archive or untouched material; current lag-aware roles and2local final families must still qualify',
        candidate_windows_by_tape={t:len(v) for t,v in available.items()},errors=errors,evidence=evidence,
        saved_correction_outcomes_used=False))
    exposure('dense_coordinate_selection',evidence,'historical_response_coordinates_only',
        'Freeze unused promotion-block availability before combined-method outcomes; no saved residual/reference metrics')
    print(dict(path=str(destination),selected=selected,eligible_windows=sum(len(v) for v in available.values()),errors=errors),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--generation',default='adaptive-original-v1');p.add_argument('--output-name',default='dense_promotion_passages.json')
    p.add_argument('--exclude-manifest');p.add_argument('--minimum-events',type=int,default=4)
    a=p.parse_args();run(a.generation,a.output_name,a.exclude_manifest,a.minimum_events)
