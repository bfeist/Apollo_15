"""Consume reserved local events once on the frozen supported joined map."""
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
from flutter_tracking import trajectory,evaluate_events,RATE
from continuous_quality import classify_numeric_quality
from flutter_policy import LIMITS,shifted_curve,evidence_strength


def run(iteration,parent_iteration,lifetime_iteration):
    parent=ROOT/'iterations'/parent_iteration;life=ROOT/'iterations'/lifetime_iteration
    parents=[r for r in read(parent/'results.json') if r.get('quality',{}).get('passed')]
    lifetime={r['target_channel']:r for r in read(life/'results.json')}
    roles=read(parent/'roles_0.json')['roles']
    folder=begin(iteration,'A frozen physically supported joined map improves reserved local response events',
        'Identity and saved full-duration v5 baseline versus frozen I008 joined map, same original response samples',
        dict(minimum_reserved_events=2,relative='median<=.5 and every margin<=.8',
             controls='No-motion/wrong-sign/AM plus at least two losing fixed wrong-time controls',
             support='Same continuous physical island; no automatic reacquisition',
             restoration='Separate <=6Hz and <=5cents goals',
             failure='Keep any failed exact map; no clipping/refitting or alternate selection from these outcomes'),
        parent=parent_iteration,lifetime=lifetime_iteration,
        map_identity='Existing saved I008 600s maps; reproduce bit-for-bit before evaluation',
        exposure_limitation='Prospectively frozen retrospective local audit: real material previously evaluated by v5; not untouched archive evidence',
        reserved_role_policy='Existing immutable final roles from parent generation; consumed once here')
    invs={i['tape']:i for i in read(ROOT/'inventory.json')['tapes']};results=[];tick=time.perf_counter()
    for previous in parents:
        target=previous['target_channel'];frozen=lifetime[target];selected=previous['selected']
        tape=previous['tape'];start=frozen['start'];end=frozen['end'];inv=invs[tape]
        rows={r['channel']:r for r in inv['channels']};ref=selected['reference_channel']
        reference,sr,source=load_excerpt(inv,rows[ref],start,end,selected['extraction_version'])
        if source['decoded_sha256']!=frozen['diagnostics']['source']['decoded_sha256']:raise ValueError('Lifetime reference source changed')
        curve,gate=track_array(reference,sr,selected['carrier_hz'],selected['capture_hz'],frozen['diagnostics']['amplitude_reference'])
        low,high=frozen['diagnostics']['continuous_island_seconds']
        gate[:round(low*RATE)]=0;gate[round(high*RATE):]=0;curve*=gate
        x,sr,targetsource=load_excerpt(inv,rows[target],start,end,'original')
        pos,grid,stats=make_map(curve,gate,sr,len(x))
        targetpath=life/f'{tape}_{start}_CH{target:02d}'/'challenger_map.npz'
        saved=np.load(targetpath)
        if not all(np.array_equal(grid[k],saved[k]) for k in ('tt','nominal','speed','envelope')):
            raise ValueError('Frozen qualified map does not reproduce exactly')
        events=roles[str(target)]['final'];cal=roles[str(target)]['calibration']
        # This marker precedes reading any reserved response measurement.
        write(folder/f'CH{target:02d}_reserved_consumed.json',dict(status='consumed_before_measurement',
            events=events,map_sha256=sha(targetpath.read_bytes()),source=targetsource))
        exposure(iteration,[dict(tape=tape,channel=target,events=events)],'reserved_response_gate_consumed','Frozen exact local audit, no retry or refit')
        main=evaluate_events(x,sr,None,events,sample_positions=pos)
        trace=trajectory(reference,sr,selected['carrier_hz'],selected['capture_hz']);amp=trace[3]-np.median(trace[3])
        amp*=np.std(curve)/max(np.std(amp),1e-12)
        controls={}
        for name,e in dict(no_motion=np.zeros_like(curve),wrong_sign=-curve,amplitude=np.clip(amp,-.049,.049)).items():
            cp,_,_=make_map(e,gate,sr,len(x));controls[name]=evaluate_events(x,sr,None,events,sample_positions=cp)
        shifts={}
        for shift in LIMITS['wrong_time_seconds']:
            cp,_,_=make_map(shifted_curve(curve,shift),gate,sr,len(x));shifts[str(shift)]=evaluate_events(x,sr,None,events,sample_positions=cp)
        all_events=cal+events;lo=round(min(e['start'] for e in all_events)*RATE);hi=round(max(e['end'] for e in all_events)*RATE)
        quality=classify_numeric_quality(main,controls,shifts,supported=bool(np.all(gate[lo:hi]>=.95)),
            independent_windows=evidence_strength(cal,events)['level']=='multiple_time_windows')
        bp,baseline=baseline_exact(tape,rows[target],start,len(x),sr,events)
        result=dict(tape=tape,target_channel=target,selected=selected,start=start,end=end,
            physical_span=[start+low,start+high],physical_span_seconds=high-low,
            reserved_events=len(events),reserved_group_ids=[e['synchronized_event_index'] for e in events],
            quality=quality,validation=main,controls=controls,wrong_time=shifts,
            baseline=dict(metadata=baseline,validation=evaluate_events(x,sr,None,events,sample_positions=bp)),
            status='reserved_supported_partial' if quality['passed'] else 'insufficient_reserved_events' if len(events)<2 else 'reserved_failed',
            map_stats=stats,exact_saved_map_reproduced=True,exposure='Frozen retrospective audit',
            supported_seconds_are_event_supported_not_proof_at_every_instant=True,
            listening_approved=False,production_accepted=False)
        results.append(result);write(folder/'results.json',results)
        print('reserved_audit',target,result['status'],quality['measurements'],flush=True)
    finish(folder,dict(statuses=dict(Counter(r['status'] for r in results)),wall_seconds=time.perf_counter()-tick,
        peak_working_set_bytes=memory_peak(),independent_tape_passages=1,production_accepted=False,listening_approved=False),
        'local_audit_completed','Frozen exact-map reserved local audit completed. Partial improvement stays separate from restoration; insufficient events do not pass.',
        'Review successful local map; keep no correction outside supported island. Continue independent cross-tape and extractor research; no production rollout.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I009_local_audit');p.add_argument('--parent-iteration',default='I005c2');p.add_argument('--lifetime-iteration',default='I008_forward')
    a=p.parse_args();run(a.iteration,a.parent_iteration,a.lifetime_iteration)
