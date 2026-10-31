"""Frozen-reference forward lifetime and exact joined-map research test."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure,memory_peak
from deflutter_real_experiment import make_map,baseline_exact
from continuous_tracking import track_array
from flutter_tracking import trajectory,beeps,evaluate_events,RATE
from continuous_acquisition import SynchronizedEvidence
from continuous_quality import classify_numeric_quality
from flutter_policy import LIMITS,shifted_curve,evidence_strength


def run(iteration,parent_iteration,duration):
    parent=ROOT/'iterations'/parent_iteration
    winners=[r for r in read(parent/'results.json') if r.get('quality',{}).get('passed')]
    if not winners:raise ValueError('No numerically supported frozen reference')
    tape=winners[0]['tape'];start=winners[0]['start'];old_duration=winners[0]['end']-start
    if any(r['tape']!=tape or r['start']!=start for r in winners):raise ValueError('Use one frozen parent passage')
    inv=next(r for r in read(ROOT/'inventory.json')['tapes'] if r['tape']==tape)
    split=read(ROOT/'split_manifest.json');absolute=inv['origin_filename_seconds']+start
    block=int(absolute//1800);role=next(r for r in split['blocks'] if r['block']==block)
    if role['role']!='development' or absolute<block*1800+360 or absolute+duration>(block+1)*1800-360:
        raise ValueError('Lifetime context would cross the development block core')
    folder=begin(iteration,'A fixed validated carrier remains predictive across new time and I/O boundaries',
        'Frozen parent reference extended to 600s; same source/capture/amplitude; no new candidate selection',
        dict(parent_reproduction='Exact original 300s map before extension',prefix='raw tracker prefix agrees until old physical-end context',
             chunks='7s vs 11s maximum speed/support error <=1e-12',
             map='Finite, monotonic, <=5% speed, zero unsupported local correction, no automatic reacquisition',
             response='Unchanged numeric/control gates on NEW validation events; old calibration retained; final-role metrics withheld'),
        parent_iteration=parent_iteration,parent_results_sha256=sha((parent/'results.json').read_bytes()),
        tape=tape,start_seconds=start,duration_seconds=duration,old_duration_seconds=old_duration,
        selection='All passing targets from one parent passage; no success-only forward excerpt selection',
        frozen_references=[r['selected'] for r in winners],
        forward_response_only_after=start+old_duration+3,
        no_human_listening=True)
    tick=time.perf_counter();rows={r['channel']:r for r in inv['channels']};sources=[];target_data={};batches=[]
    # Continue immutable synchronized roles, rather than resetting C/C/V/V/F.
    parent_roles=read(parent/'roles_0.json');ledger=SynchronizedEvidence.restore(parent_roles['ledger'])
    targets={r['target_channel'] for r in winners}
    for ch,row in rows.items():
        if start<row['start_seconds'] or start+duration>row['end_seconds']:continue
        # New detection has real four-second left context. Only future events
        # enter the ledger; previously consumed outcomes are never retested.
        lo=start+old_duration-4
        x,sr,source=load_excerpt(inv,row,lo,start+duration,'original');sources.append(dict(channel=ch,role='future_event_detection',**source))
        events=[e for e in beeps(x,sr) if lo+e['start']>=start+old_duration+3 and lo+e['end']<=start+duration-3]
        batches.append((ch,events,lo))
        if ch in targets:
            target_data[ch]=load_excerpt(inv,row,start,start+duration,'original')
    added=ledger.ingest_batch(batches)
    write(folder/'future_roles.json',dict(ledger=ledger.state(),events=added))
    results=[];cache={}
    for previous in winners:
        selected=previous['selected'];target=previous['target_channel'];ref=selected['reference_channel']
        if selected['extraction_version'] not in ('original','denoised'):
            raise ValueError('This bounded lifetime runner requires a frozen original or cached-denoised extractor; never refit projection coefficients silently')
        key=selected['key']
        if key not in cache:
            old,sr,oldsource=load_excerpt(inv,rows[ref],start,start+old_duration,selected['extraction_version'])
            if oldsource['decoded_sha256']!=selected['source']['decoded_sha256']:raise ValueError('Frozen reference source changed')
            hz=selected['carrier_hz'];capture=selected['capture_hz']
            oldtrace=trajectory(old,sr,hz,capture);amplitude=max(float(np.median(oldtrace[3][400:-400])),1e-12)
            oldcurve,oldgate=track_array(old,sr,hz,capture,amplitude)
            _,oldgrid,oldstats=make_map(oldcurve,oldgate,sr,len(old))
            saved=np.load(parent/f'{tape}_{start}_CH{target:02d}'/'challenger_map.npz')
            reproduction=all(np.array_equal(oldgrid[name],saved[name]) for name in ('tt','nominal','speed','envelope'))
            if not reproduction:raise ValueError('Frozen 300s map does not reproduce exactly')
            reference,sr,source=load_excerpt(inv,rows[ref],start,start+duration,selected['extraction_version']);sources.append(dict(channel=ref,role='frozen_reference',**source))
            curve,gate=track_array(reference,sr,hz,capture,amplitude,chunk_frames=7*sr)
            alternate,alternate_gate=track_array(reference,sr,hz,capture,amplitude,chunk_frames=11*sr)
            chunk_error=float(np.max(abs(curve-alternate)));gate_error=float(np.max(abs(gate-alternate_gate)))
            if max(chunk_error,gate_error)>1e-12:raise ValueError('Real-data chunk invariance failed')
            prefix_end=round((old_duration-3)*RATE)
            prefix_error=float(np.max(abs(curve[:prefix_end]-oldcurve[:prefix_end])))
            if prefix_error>1e-12:raise ValueError('Future reference input changed past raw tracker estimates')
            oldcal=parent_roles['roles'][str(target)]['calibration'];oldval=parent_roles['roles'][str(target)]['validation']
            anchor=round(min(e['start'] for e in oldcal+oldval)*RATE)
            low=anchor
            while low>0 and gate[low-1]>=.95:low-=1
            high=anchor
            while high<len(gate) and gate[high]>=.95:high+=1
            lifetime_gate=gate.copy();lifetime_gate[:low]=0;lifetime_gate[high:]=0
            curve*=lifetime_gate
            trace=trajectory(reference,sr,hz,capture)
            cache[key]=(curve,lifetime_gate,trace[3],dict(amplitude_reference=amplitude,
                exact_parent_map_reproduced=reproduction,chunk_error=chunk_error,support_chunk_error=gate_error,
                raw_prefix_error=prefix_error,continuous_island_seconds=[low/RATE,high/RATE],
                reacquisition_after_loss=False,source=source))
        curve,gate,amp,diagnostics=cache[key]
        x,sr,target_source=target_data[target];sources.append(dict(channel=target,role='target_full_passage',**target_source))
        future=[dict(e,start=e['start_seconds']-start,end=e['end_seconds']-start) for e in added.get(target,[])]
        validation=[e for e in future if e['role']=='validation'];calibration=parent_roles['roles'][str(target)]['calibration']
        result=dict(tape=tape,target_channel=target,start=start,end=start+duration,
            frozen_selection=selected,diagnostics=diagnostics,new_validation_count=len(validation),
            new_reserved_count=sum(e['role']=='final' for e in future),reserved_outcomes_evaluated=False)
        pos,grid,stats=make_map(curve,gate,sr,len(x));result['map_stats']=stats
        targetfolder=folder/f'{tape}_{start}_CH{target:02d}';targetfolder.mkdir()
        np.savez(targetfolder/'challenger_map.npz',**grid)
        # Mark gate consumed before any measurement or possible interruption.
        write(targetfolder/'gate_consumed.json',dict(status='consumed_before_measurement',validation=validation,
            frozen_selection=selected,source_sha256=target_source['decoded_sha256']))
        if len(validation)<2:
            result['status']='insufficient_fresh_validation'
        else:
            main=evaluate_events(x,sr,None,validation,sample_positions=pos)
            am=amp-np.median(amp);am*=np.std(curve)/max(np.std(am),1e-12)
            control_curves=dict(no_motion=np.zeros_like(curve),wrong_sign=-curve,amplitude=np.clip(am,-.049,.049))
            controls={}
            for name,e in control_curves.items():
                cp,_,_=make_map(e,gate,sr,len(x));controls[name]=evaluate_events(x,sr,None,validation,sample_positions=cp)
            shifts={}
            for shift in LIMITS['wrong_time_seconds']:
                cp,_,_=make_map(shifted_curve(curve,shift),gate,sr,len(x))
                shifts[str(shift)]=evaluate_events(x,sr,None,validation,sample_positions=cp)
            first=round(min(e['start'] for e in calibration+validation)*RATE);last=round(max(e['end'] for e in calibration+validation)*RATE)
            quality=classify_numeric_quality(main,controls,shifts,supported=bool(np.all(gate[first:last]>=.95)),
                independent_windows=evidence_strength(calibration,validation)['level']=='multiple_time_windows')
            result.update(status='supported_partial' if quality['passed'] else 'forward_validation_failed',
                quality=quality,validation=main,controls=controls,wrong_time=shifts)
            bp,baseline=baseline_exact(tape,rows[target],start,len(x),sr,validation)
            result['baseline']=dict(metadata=baseline,validation=evaluate_events(x,sr,None,validation,sample_positions=bp))
            np.savez(targetfolder/'baseline_positions.npz',positions=bp)
        write(targetfolder/'result.json',result);results.append(result);write(folder/'results.json',results)
        print('lifetime',target,result['status'],result.get('quality',{}).get('measurements'),diagnostics,flush=True)
    write(folder/'source_manifest.json',sources)
    exposure(iteration,sources,'forward_validation_response_and_frozen_predictor','New time only; old map reproduced structurally, reserved response metrics withheld')
    finish(folder,dict(statuses=dict(Counter(r['status'] for r in results)),wall_seconds=time.perf_counter()-tick,
        peak_working_set_bytes=memory_peak(),parent_reference_changed=False,reserved_outcomes_evaluated=False),
        'forward_assessed','Frozen-reference lifetime test completed with exact-map, support and chunk checks. No automatic reacquisition or production approval.',
        'Review forward outcomes and matched full-map crops; diagnose support loss if present. Use a new unused gate for changed methods.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I008_forward');p.add_argument('--parent-iteration',default='I005c2');p.add_argument('--duration',type=int,default=600)
    a=p.parse_args();run(a.iteration,a.parent_iteration,a.duration)
