"""Bounded retrospective qualification of a frozen dual-source tone catalog.

Same-channel references are allowed, response-band predictors remain forbidden.
Every target is chosen from event availability by a fixed hash before any loss.
All variants see calibration only; exactly one challenger sees validation.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import time

import numpy as np
from scipy import ndimage
from scipy.integrate import cumulative_trapezoid
import soundfile as sf

from deflutter_campaign import ROOT, SAVED, SEED, begin, finish, load_excerpt, read, write, append, sha, exposure, memory_peak, stamp
from flutter_tracking import trajectory, evaluate_events, beeps, RATE
from continuous_tracking import track_array
from continuous_acquisition import SynchronizedEvidence
from continuous_quality import classify_numeric_quality
from flutter_policy import LIMITS, shifted_curve, evidence_strength
from flutter_planning import loss
from carrier_capture import capture_candidates
from continuous_maps import saved_continuous_map
from flutter import resample_at
from deflutter_extractors import project_stationary_mains


def make_map(curve,support,sr,frames):
    """One full-passage anchoring; never restore unsupported local speed."""
    tt=np.arange(len(curve))/RATE
    last=(frames-1)/sr
    if tt[-1]<last:
        tt=np.r_[tt,last];curve=np.r_[curve,curve[-1]];support=np.r_[support,support[-1]]
    distance=ndimage.distance_transform_edt(np.r_[False,support>=.95,False])[1:-1]
    fade=.5-.5*np.cos(np.pi*np.clip(distance/(.5*RATE),0,1))
    edge=.5-.5*np.cos(np.pi*np.clip(np.minimum(tt,last-tt)/.5,0,1))
    envelope=support*fade*edge
    speed=curve*envelope
    area=float(np.trapezoid(envelope,tt))
    dc=float(np.trapezoid(speed,tt)/area) if area else 0.
    speed-=dc*envelope
    if not np.isfinite(speed).all() or np.max(abs(speed))>.05:
        raise ValueError('Exact anchored map exceeds 5% speed bound')
    delay=cumulative_trapezoid(speed,tt,initial=0.)
    if abs(delay[-1])>1e-9:raise ValueError('Map endpoint integration failed')
    delay[-1]=0.;nominal=tt+delay
    if np.any(np.diff(nominal)<=0):raise ValueError('Nonmonotonic exact map')
    pos=np.interp(np.arange(frames)/sr,nominal,tt)*sr
    return pos,dict(tt=tt,nominal=nominal,speed=speed,envelope=envelope),dict(
        speed_bound=float(np.max(abs(speed))),dc_speed_removed=dc,
        maximum_displacement_seconds=float(np.max(abs(delay))),
        unsupported_speed_max=float(np.max(abs(speed[envelope==0]))) if np.any(envelope==0) else 0.,
        positions_sha256=sha(pos.tobytes()),strictly_increasing=bool(np.all(np.diff(pos)>0)))


def merge_catalog(records):
    groups=[]
    for r in sorted(records,key=lambda r:(r['resolution_seconds'],r['carrier_hz']),reverse=True):
        if r['resolution_seconds']<2:continue
        existing=next((g for g in groups if abs(g['carrier_hz']-r['carrier_hz'])<=max(.5,r['bin_width_hz'],g['bin_width_hz'])),None)
        if existing is None:
            existing=dict(carrier_hz=r['carrier_hz'],bin_width_hz=r['bin_width_hz'],members=[])
            groups.append(existing)
        existing['members'].append(r)
    for g in groups:
        members=g['members'];g['group_id']=sha(json.dumps(sorted(r['candidate_id'] for r in members)).encode())[:24]
        g['provenance']=sorted({r['version'] for r in members})
        # Compare measurements across all intervals, not a stale opening score.
        g['rank']=float(np.median([r['spectral_score_db'] for r in members]))
        g['covered_windows']=len({int(r['start_seconds']//60) for r in members})
        g['observed_seconds']=sum(max(r['end_seconds'] for r in members if int(r['start_seconds']//60)==w)-
                                  min(r['start_seconds'] for r in members if int(r['start_seconds']//60)==w)
                                  for w in {int(r['start_seconds']//60) for r in members})
    return sorted(groups,key=lambda g:(-g['covered_windows'],-g['observed_seconds'],-g['rank'],g['group_id']))


def baseline_exact(tape,row,passage_start,frames,sr,events):
    base=SAVED/tape/'plan';raw=(base/'plan.json').read_bytes()
    frozen=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==tape)
    expected=next(f['sha256'] for f in frozen['baseline_files'] if f['path'].replace('\\','/').endswith('/plan/plan.json'))
    if sha(raw)!=expected:raise ValueError('Frozen baseline plan identity changed')
    plan=json.loads(raw);summary=plan['regions'][0]
    # Reconstruct the FULL saved duration-anchored map before cropping. This
    # intentionally costs memory; a locally reanchored excerpt is not baseline.
    fullstart=max(float(summary['start_seconds']),row['start_seconds'])
    fullend=min(float(summary['end_seconds']),row['end_seconds'])
    tt,nominal,speed,stats,markers,evidence=saved_continuous_map(base,summary,row['channel'],
        fullstart,round((fullend-fullstart)*sr),sr)
    output_times=passage_start-fullstart+np.arange(frames)/sr
    positions=np.interp(output_times,nominal,tt)*sr+(fullstart-passage_start)*sr
    del tt,nominal,speed
    return positions,dict(stats=stats,evidence=evidence,map_scope=[fullstart,fullend],
        excerpt_reanchored=False,plan_sha256=sha((base/'plan.json').read_bytes()))


def run(iteration,catalog_iteration,max_passages,targets_per_passage,per_channel,passage_offset=0,passages_file=None,defer_without_response=False,provenance_balanced=False,lag_aware_roles=False,bounded_acquisition=False,preflight_maps=False,required_final_events=0,adaptive_extraction=False):
    if bounded_acquisition and (targets_per_passage!=1 or not lag_aware_roles):
        raise ValueError('Bounded acquisition requires exactly one target and lag-aware response roles')
    if required_final_events and not bounded_acquisition:raise ValueError('Reserved availability requires bounded common acquisition')
    if required_final_events<0:raise ValueError('Reserved availability must be nonnegative')
    if adaptive_extraction and not (bounded_acquisition and preflight_maps and lag_aware_roles):
        raise ValueError('Adaptive-original research gates require bounded acquisition, exact-map preflight and lag-aware roles')
    catalog_folder=ROOT/'iterations'/catalog_iteration
    if not (catalog_folder/'metrics.json').exists():raise ValueError('Catalog is not complete')
    catalog=[json.loads(line) for line in (catalog_folder/'catalog.jsonl').read_text().splitlines()]
    records=read(catalog_folder/'source_records.json')
    allpassages=read(Path(passages_file))['passages'] if passages_file else read(ROOT/'split_manifest.json')['development_passages']
    passages=allpassages[passage_offset:passage_offset+max_passages]
    folder=begin(iteration,'Dual-source carrier extraction with same-channel independent response improves exact mapped events',
        'Frozen legacy saved full map versus one calibration-selected challenger; identity included',
        dict(eligibility='Unchanged classify_numeric_quality, continuous >=.95 support, independent event roles',
             target_selection='Outcome-independent hash among channels with >=2 calibration and >=2 validation events',
             validation='One frozen choice per target, no retry on these outcomes',
             production=False),
        catalog_sha256=sha((catalog_folder/'catalog.jsonl').read_bytes()),
        methodology='adaptive-original-bounded-island-research-v1' if adaptive_extraction else 'bounded-map-preflight-research-v2' if preflight_maps else 'bounded-single-island-research-v2' if bounded_acquisition else 'dual-source-acquisition-research-v1',
        passages=passages,targets_per_passage=targets_per_passage,groups_per_channel=per_channel,
        provenance_balanced_pool=provenance_balanced,
        lag_aware_response_roles=lag_aware_roles,
        bounded_acquisition=bounded_acquisition,
        exact_required_map_preflight=preflight_maps,required_final_event_availability=required_final_events,
        adaptive_original_extraction=adaptive_extraction,
        adaptive_protocol='Full-band source-only comb,40s real context each side; no canonical/cache write; identity if pilot unavailable; withhold transformed paths with original clipping;6GiB worker peak/120s-per-source limit' if adaptive_extraction else None,
        acquisition_protocol='Earliest event-ready60s monitor; shortest existing60/120/180/300 horizon; common event set frozen before source scoring; source-only max8s median within acquisition context; map speed zero outside attempt' if bounded_acquisition else 'Whole-passage event set',
        calibration_budgets=[8,16,32,'full_declared_pool'],maximum_declared_carrier_groups=30*per_channel,
        variant_accounting='All declared source/capture variants within the carrier pool; actual attempted/withheld counts retained in tracking.json',
        pool_ranking='At most declared groups/channel, three-window support then interval median excess, no channel labels',
        provenance='original-only, cleaned-only, shared retained; extraction original and cached denoised independently',
        real_evidence='Prospectively frozen retrospective; all v5 real material potentially exposed')
    tick=time.perf_counter();invs={r['tape']:r for r in read(ROOT/'inventory.json')['tapes']}
    results=[];tracking=[];catalog_summary=Counter();funnel=[]
    write(folder/'results.json',results)
    for pi,passage in enumerate(passages):
        tape=passage['tape'];start=passage['start_seconds'];end=start+passage['duration_seconds'];inv=invs[tape]
        data={};sources={};event_batches=[]
        for row in inv['channels']:
            if start<row['start_seconds'] or end>row['end_seconds']:continue
            x,sr,source=load_excerpt(inv,row,start,end,'original')
            data[(row['channel'],'original')]=(x,sr);sources[(row['channel'],'original')]=source
            event_batches.append((row['channel'],beeps(x,sr),start))
        if lag_aware_roles:
            from deflutter_event_groups import assign_roles
            assigned,ledger_state=assign_roles(event_batches)
        else:
            ledger=SynchronizedEvidence();assigned=ledger.ingest_batch(event_batches);ledger_state=ledger.state()
        # No reserved response metric is computed. Role metadata is retained.
        role_rows={}
        for ch,events in assigned.items():
            role_rows[ch]={role:[dict(e,start=e['start_seconds']-start,end=e['end_seconds']-start)
                for e in events if e['role']==role and e['start_seconds']>=start+3 and e['end_seconds']<=end-3]
                for role in ('calibration','validation','final')}
        write(folder/f'roles_{pi}.json',dict(ledger=ledger_state,roles=role_rows))
        eligible=[ch for ch,r in role_rows.items() if min(len(r['calibration']),len(r['validation']))>=2 and len(r['final'])>=required_final_events]
        targets=sorted(eligible,key=lambda ch:sha(f'{SEED}:target:{tape}:{start}:{ch}'.encode()))[:targets_per_passage]
        funnel.append(dict(tape=tape,start=start,channels=len(data),event_eligible_channels=len(eligible),
                           required_final_event_availability=required_final_events,
                           selected_targets=targets,nonselected_eligible=sorted(set(eligible)-set(targets))))
        print('passage',pi,tape,start,'eligible',len(eligible),'targets',targets,flush=True)
        if not targets and defer_without_response:
            funnel[-1].update(tracking_status='budget_deferred',reason='No independent response gate; extensive physical tracking already recorded on initial four passages')
            write(folder/'funnel.json',funnel)
            exposure(iteration,[dict(tape=tape,start=start,targets=[])],'event_coordinates_only','No independent response gate; no target outcomes evaluated')
            continue
        common_proposal=None
        if bounded_acquisition and targets:
            from deflutter_local_acquisition import common_event_acquisition, source_amplitude_reference
            target=targets[0]
            common_proposal=common_event_acquisition([e for es in role_rows[target].values() for e in es],end-start,minimum_final_events=required_final_events)
            write(folder/f'acquisition_{pi}.json',dict(proposal=common_proposal,
                frozen_before_source_scoring=True,response_outcomes_accessed=False,
                online_map_prefix_qualified=False,interval_selection='Event coordinates and frozen roles only'))
            if common_proposal is None:
                funnel[-1]['acquisition_status']='no_common_event_ready_interval'
                write(folder/'funnel.json',funnel);continue
            for role in ('calibration','validation','final'):
                role_rows[target][role]=[dict(e,start=e['start_seconds'],end=e['end_seconds']) for e in common_proposal['roles'][role]]
        grouped={}
        for row in inv['channels']:
            ch=row['channel'];subset=[r for r in catalog if r['tape']==tape and r['channel']==ch and r['passage_start']==start]
            groups=merge_catalog(subset)
            if provenance_balanced:
                selected_groups=[]
                for provenance in (['original'],['denoised'],['denoised','original']):
                    selected=next((g for g in groups if g['provenance']==provenance),None)
                    if selected is not None:selected_groups.append(selected)
                chosen_ids={g['group_id'] for g in selected_groups}
                selected_groups += [g for g in groups if g['group_id'] not in chosen_ids]
                grouped[ch]=selected_groups[:per_channel]
            else:grouped[ch]=groups[:per_channel]
            for g in groups:
                catalog_summary['union_groups']+=1;catalog_summary['provenance_'+'_'.join(g['provenance'])]+=1
                append(ROOT/'candidate_catalog.jsonl',dict(candidate_id=g['group_id'],status='spectral_hypothesis',
                    member_ids=[r['candidate_id'] for r in g['members']],tape=tape,channel=ch,passage_start=start,
                    carrier_hz=g['carrier_hz'],provenance=g['provenance'],intervals=[
                        dict(version=r['version'],start=r['start_seconds'],end=r['end_seconds'],score=r['spectral_score_db']) for r in g['members']]))
            catalog_summary['pool_deferred']+=max(0,len(groups)-per_channel)
        # Calibrate proxy across frozen per-channel pool; no response validation.
        candidates=[];curves={};withheld_adaptive={}
        original_clipped={ch:bool(np.any(abs(values[0])>=.999)) for (ch,version),values in data.items() if version=='original'} if adaptive_extraction else {}
        for ch,groups in grouped.items():
            source_row=next(r for r in inv['channels'] if r['channel']==ch)
            for g in groups:
                hz=g['carrier_hz'];distance=abs(hz-round(hz/60)*60)
                for version in ('original','denoised','original_stationary_projection')+(('adaptive_original',) if adaptive_extraction else ()):
                    if version=='denoised' and not next(r for r in inv['channels'] if r['channel']==ch).get('denoised_available'):continue
                    if adaptive_extraction and version!='original' and original_clipped.get(ch,False):
                        tracking.append(dict(group_id=g['group_id'],reference_channel=ch,extraction_version=version,
                            status='transformed_source_withheld_original_clipping',source=sources[(ch,'original')]))
                        continue
                    source_version='original' if version=='original_stationary_projection' else version
                    if (ch,source_version) not in data:
                        if source_version=='adaptive_original':
                            from deflutter_adaptive_source import load_adaptive_original,SourceWithheld
                            if ch in withheld_adaptive:
                                tracking.append(dict(group_id=g['group_id'],reference_channel=ch,extraction_version=version,
                                    status='adaptive_source_withheld',**withheld_adaptive[ch]));continue
                            extraction_started=time.monotonic()
                            def source_progress(stage):
                                peak=memory_peak()
                                write(folder/'adaptive_source_progress.json',dict(at=stamp(),reference_channel=ch,
                                    peak_working_set_bytes=peak,elapsed_source_seconds=time.monotonic()-extraction_started,**stage))
                                if peak>6*2**30:raise SourceWithheld('Declared6GiB worker peak-memory budget reached')
                                if time.monotonic()-extraction_started>120:raise SourceWithheld('Declared120s adaptive-source budget reached')
                            try:x,sr,source=load_adaptive_original(inv,source_row,start,end,progress=source_progress)
                            except SourceWithheld as exc:
                                withheld_adaptive[ch]=dict(reason=str(exc),metadata=exc.metadata)
                                tracking.append(dict(group_id=g['group_id'],reference_channel=ch,extraction_version=version,
                                    status='adaptive_source_withheld',**withheld_adaptive[ch]));continue
                        else:x,sr,source=load_excerpt(inv,source_row,start,end,source_version)
                        data[(ch,source_version)]=(x,sr);sources[(ch,source_version)]=source
                    x,sr=data[(ch,source_version)]
                    captures=list(capture_candidates(hz,sr,(2.,30.)))
                    if distance>1:captures.append(min(35.,distance*.65))
                    for capture in sorted(set(round(c,6) for c in captures)):
                        key=f'{g["group_id"]}:{version}:{capture:g}'
                        if hz-capture<=2850 and hz+capture>=2150:
                            tracking.append(dict(key=key,status='response_band_withheld',
                                reason='Capture overlaps independent response band'))
                            continue
                        try:
                            extract,projection_meta=project_stationary_mains(x,sr,hz,capture) if version=='original_stationary_projection' else (x,None)
                            trace=trajectory(extract,sr,hz,capture)
                            amplitude_meta=None
                            if common_proposal:
                                a=round(common_proposal['analysis_start']*RATE);b=round(common_proposal['analysis_end']*RATE)
                                acquisition_trace=trajectory(extract[:round(common_proposal['analysis_end']*sr)],sr,hz,capture)
                                amp,amplitude_meta=source_amplitude_reference(acquisition_trace[3][a:b],rate=RATE,available_end=(b-a)/RATE)
                                amplitude_meta['source_origin_seconds']=a/RATE
                            else:amp=max(float(np.median(trace[3][400:-400])),1e-12)
                            curve,support=track_array(extract,sr,hz,capture,amp)
                            island_info=None
                            if common_proposal:
                                times=np.arange(len(support))/RATE
                                support=support*((times>=common_proposal['start_seconds'])&(times<common_proposal['end_seconds']))
                                from deflutter_local_acquisition import qualified_support_island
                                kept,island_info=qualified_support_island(support,role_rows[targets[0]]['calibration'],
                                    role_rows[targets[0]]['validation'],role_rows[targets[0]]['final'],rate=RATE,
                                    minimum_final_events=required_final_events)
                                support=kept if kept is not None else np.zeros_like(support)
                            support_fraction=float(np.mean(support[600:-600]>=.95))
                            required=role_rows[targets[0]]['calibration']+role_rows[targets[0]]['validation'] if common_proposal else None
                            required_support=bool(np.all(support[round(min(e['start'] for e in required)*RATE):round(max(e['end'] for e in required)*RATE)]>=.95)) if required else support_fraction>=.5
                            item=dict(key=key,group_id=g['group_id'],reference_channel=ch,carrier_hz=hz,
                                extraction_version=version,capture_hz=capture,source=sources[(ch,source_version)],projection=projection_meta,
                                amplitude_reference=amp,amplitude_metadata=amplitude_meta,
                                support_island=island_info,
                                support_fraction=support_fraction,motion_rms=float(np.std(curve[600:-600])),
                                provenance=g['provenance'],status='tracked_unvalidated' if required_support else 'insufficient_local_support' if common_proposal else 'tracking_failed')
                            tracking.append(item)
                            if not required_support:continue
                            candidates.append(item);curves[key]=(curve,support,trace[3])
                        except (ValueError,FloatingPointError) as exc:
                            tracking.append(dict(key=key,status='tracking_failed',error=str(exc)))
                print('tracking_progress',pi,ch,g['group_id'],len(tracking),flush=True)
        print('tracked',pi,len(candidates),'of',len(tracking),flush=True)
        write(folder/'tracking.json',tracking)
        for target in targets:
            x,sr=data[(target,'original')];roles=role_rows[target];cal=roles['calibration'];val=roles['validation']
            first=round(min(e['start'] for e in cal)*RATE);last=round(max(e['end'] for e in cal)*RATE)
            scores=[]
            for item in candidates:
                curve,support,amp=curves[item['key']]
                if not np.all(support[first:last]>=.95):continue
                try:pos,grid,stats=make_map(curve,support,sr,len(x))
                except ValueError as exc:
                    tracking.append(dict(key=item['key'],status='invalid_exact_map',error=str(exc),target_channel=target))
                    write(folder/'tracking.json',tracking)
                    continue
                metric=evaluate_events(x,sr,None,cal,sample_positions=pos)
                score=loss(metric)
                if score is not None:scores.append(dict(item,calibration_loss=score,map_stats=stats))
            # Budget comparisons are calibration-only. Count references, keeping
            # source/capture variants of one group together as one reference.
            group_best={}
            for item in scores:
                gid=item['group_id']
                if gid not in group_best or item['calibration_loss']<group_best[gid]['calibration_loss']:group_best[gid]=item
            ranked=sorted(group_best.values(),key=lambda r:(r['calibration_loss'],r['key']))
            # Proxy ranking has already calibrated the pool; these budgets
            # explicitly measure full-map variant qualification, not unseen
            # candidates. Do not claim cheap screening was no computation.
            rank_order=sorted(group_best,key=lambda gid:sha((SEED+gid).encode()))
            budget_rows=[]
            for budget in sorted(set((8,16,32,max(32,len(group_best))))):
                subset=[group_best[gid] for gid in rank_order[:budget]]
                best=min(subset,key=lambda r:(r['calibration_loss'],r['key'])) if subset else None
                budget_rows.append(dict(budget=budget,completed_references=len(subset),deferred=max(0,len(group_best)-budget),
                    best_calibration_loss=best['calibration_loss'] if best else None,selected_key=best['key'] if best else None))
            selected_key=budget_rows[-1]['selected_key']
            selected=next((r for r in scores if r['key']==selected_key),None)
            preflight_records=[]
            if preflight_maps:
                from deflutter_map_preflight import select_preflight
                def progress(records):
                    write(folder/f'{tape}_{start}_CH{target:02d}_map_preflight.json',dict(records=records,
                        validation_outcomes_accessed=False,selection='Calibration rank after exact source/control map validity; no validation retries'))
                selected,preflight_records=select_preflight(scores,curves,sr,len(x),progress=progress)
                for record in preflight_records:
                    if not record['passed']:tracking.append(dict(key=record['key'],status='invalid_required_map',
                        errors=record['errors'],target_channel=target,response_outcomes_accessed=False))
                write(folder/'tracking.json',tracking)
            target_result=dict(tape=tape,target_channel=target,start=start,end=end,
                target_source=sources[(target,'original')],
                common_acquisition=common_proposal,
                exact_map_preflight=preflight_records,main_calibration_choice_before_preflight=selected_key,
                budget_scope='Main-map calibration only; exact required-map preflight precedes the sole selected validation attempt',
                roles_counts={k:len(v) for k,v in roles.items()},calibration_variants=scores,
                budgets=budget_rows,selected=selected,status='insufficient_evidence' if selected is None else 'frozen_before_validation')
            if selected is None and preflight_records:target_result['status']='all_required_maps_invalid'
            if selected:
                curve,support,amp=curves[selected['key']]
                pos,grid,stats=make_map(curve,support,sr,len(x))
                if preflight_records and stats['positions_sha256']!=preflight_records[-1]['maps']['main']['positions_sha256']:
                    raise ValueError('Main map changed after exact preflight; refuse target access')
                targetfolder=folder/f'{tape}_{start}_CH{target:02d}';targetfolder.mkdir()
                if preflight_records:
                    from deflutter_map_preflight import freeze_required_maps
                    manifest=freeze_required_maps(targetfolder,curve,support,amp,sr,len(x),preflight_records[-1]['maps'])
                    write(targetfolder/'required_map_manifest.json',manifest)
                    target_result['required_map_manifest']=manifest
                else:np.savez(targetfolder/'challenger_map.npz',**grid)
                final_ids=set(selected.get('support_island',{}).get('retained_final_family_ids',[])) if selected.get('support_island') else set()
                reserved=[e for e in roles['final'] if e['synchronized_group_id'] in final_ids]
                target_result['reserved_for_audit']=reserved
                write(targetfolder/'reserved_manifest.json',dict(events=reserved,source_island=selected.get('support_island'),
                    frozen_before_validation=True,outcomes_evaluated=False,minimum_final_events=required_final_events))
                write(targetfolder/'frozen_before_validation.json',target_result)
                write(folder/f'{tape}_{start}_CH{target:02d}_validation_consumed.json',dict(
                    status='consumed_before_measurement',selected=selected,validation=val,
                    exact_map_preflight_passed=preflight_records[-1]['passed'] if preflight_records else None,
                    frozen_model_sha256=sha((targetfolder/'frozen_before_validation.json').read_bytes()),
                    reserved_manifest_sha256=sha((targetfolder/'reserved_manifest.json').read_bytes()),
                    required_map_manifest_sha256=sha((targetfolder/'required_map_manifest.json').read_bytes()) if preflight_records else None,
                    response_role_protocol=ledger_state['version']))
                exposure(iteration,[dict(tape=tape,target=target,start=start,validation=val)],
                    'validation_gate_consumed_before_attempt','No retry or reselection after interrupted/failed validation')
                main=evaluate_events(x,sr,None,val,sample_positions=pos)
                control_curves=dict(no_motion=np.zeros_like(curve),wrong_sign=-curve)
                am=amp-np.median(amp);am*=np.std(curve)/max(np.std(am),1e-12)
                control_curves['amplitude']=np.clip(am,-.049,.049)
                controls={}
                for name,e in control_curves.items():
                    cp,_,cs=make_map(e,support,sr,len(x))
                    if preflight_records and cs['positions_sha256']!=target_result['required_map_manifest']['maps'][name]['positions_sha256']:
                        raise ValueError('Control map changed after freeze')
                    controls[name]=evaluate_events(x,sr,None,val,sample_positions=cp)
                shifted={}
                for seconds in LIMITS['wrong_time_seconds']:
                    cp,_,cs=make_map(shifted_curve(curve,seconds),support,sr,len(x))
                    if preflight_records and cs['positions_sha256']!=target_result['required_map_manifest']['maps'][f'time_{seconds:g}']['positions_sha256']:
                        raise ValueError('Wrong-time map changed after freeze')
                    shifted[str(seconds)]=evaluate_events(x,sr,None,val,sample_positions=cp)
                firstall=round(min(e['start'] for e in cal+val)*RATE);lastall=round(max(e['end'] for e in cal+val)*RATE)
                quality=classify_numeric_quality(main,controls,shifted,
                    supported=bool(np.all(support[firstall:lastall]>=.95)),
                    independent_windows=evidence_strength(cal,val)['level']=='multiple_time_windows')
                target_result.update(quality=quality,validation=main,controls=controls,wrong_time=shifted,
                    map_stats=stats,status='supported_partial' if quality['passed'] else 'validation_failed',
                    same_channel=selected['reference_channel']==target)
                # Baseline read occurs only after the challenger is frozen and
                # assessed. Full saved map is consumed as retrospective evidence.
                row=next(r for r in inv['channels'] if r['channel']==target)
                try:
                    bp,baseinfo=baseline_exact(tape,row,start,len(x),sr,val)
                    target_result['baseline']=dict(identity=False,metadata=baseinfo,
                        validation=evaluate_events(x,sr,None,val,sample_positions=bp))
                    np.savez(targetfolder/'baseline_positions.npz',positions=bp)
                except ValueError as exc:
                    target_result['baseline']=dict(status='invalid_map',error=str(exc))
                write(targetfolder/'result.json',target_result)
            results.append(target_result)
            write(folder/'results.json',results)
            print('target',tape,start,target,target_result['status'],
                target_result.get('quality',{}).get('measurements',{}).get('median_event_ratio'),flush=True)
        exposure(iteration,[dict(tape=tape,start=start,targets=targets)],'calibration_and_once_validation_response',
            'retrospective qualification; final-role outcomes not evaluated')
        write(folder/'funnel.json',funnel)
        del data,curves
    summary=dict(targets=len(results),statuses=dict(Counter(r['status'] for r in results)),
        supported_same_channel=sum(r.get('same_channel',False) and r.get('quality',{}).get('passed',False) for r in results),
        supported_transferred=sum(not r.get('same_channel',True) and r.get('quality',{}).get('passed',False) for r in results),
        catalog=dict(catalog_summary),wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak(),
        listening_status='pending',production_accepted=False,final_reserved_outcomes_evaluated=False)
    finish(folder,summary,'assessed_retrospective',
        f"{summary['statuses']}; same-channel passes {summary['supported_same_channel']}, transferred passes {summary['supported_transferred']}. All exact-map failures and controls retained.",
        'Inspect worst cases and baseline paired residuals; render matched cached-denoised blinded excerpts from saved full maps; use fresh declared blocks for revised challengers.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I005');p.add_argument('--catalog-iteration',default='I003');p.add_argument('--max-passages',type=int,default=6);p.add_argument('--passage-offset',type=int,default=0);p.add_argument('--targets-per-passage',type=int,default=2);p.add_argument('--groups-per-channel',type=int,default=2);p.add_argument('--passages-file');p.add_argument('--defer-tracking-without-response',action='store_true');p.add_argument('--provenance-balanced',action='store_true');p.add_argument('--lag-aware-roles',action='store_true');p.add_argument('--bounded-acquisition',action='store_true');p.add_argument('--preflight-maps',action='store_true');p.add_argument('--require-final-events',type=int,default=0);p.add_argument('--adaptive-extraction',action='store_true')
    a=p.parse_args();run(a.iteration,a.catalog_iteration,a.max_passages,a.targets_per_passage,a.groups_per_channel,a.passage_offset,a.passages_file,a.defer_tracking_without_response,a.provenance_balanced,a.lag_aware_roles,a.bounded_acquisition,a.preflight_maps,a.require_final_events,a.adaptive_extraction)
