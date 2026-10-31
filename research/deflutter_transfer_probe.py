"""Calibration-only transfer diagnostic; never reads validation/final outcomes."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure
from deflutter_real_experiment import make_map
from deflutter_extractors import project_stationary_mains
from deflutter_map_preflight import preflight
from continuous_tracking import track_array
from flutter_tracking import RATE,trajectory,evaluate_events
from flutter_policy import shifted_curve
from flutter_planning import loss


def run():
    parent=ROOT/'iterations/I016_gate1R'
    marker=read(next(parent.glob('*validation_consumed.json')));s=marker['selected']
    proposal=read(parent/'acquisition_0.json')['proposal']
    cal=[dict(e,start=e['start_seconds'],end=e['end_seconds']) for e in proposal['roles']['calibration']]
    lags=(-.02,-.01,-.005,-.0025,0.,.0025,.005,.01,.02);gains=(.6,.7,.8,.9,1.,1.1,1.2)
    folder=begin('I022_transfer_probe',
        'Remaining calibration error is consistent with a small stable target-specific delay or gain mismatch',
        'Frozen chosen source/extractor/capture versus declared lag/gain grid, calibration events only',
        dict(advance='Best calibration loss<.15 and improvement>=.05; individual event winners agree within5ms lag and.2gain',
             reject='No validation/final inspection, no re-labeling I016; invalid exact maps rejected without clipping/refit'),
        parent_iteration='I016_gate1R',lag_seconds=list(lags),gains=list(gains),calibration=cal,
        validation_outcomes_accessed=False,reserved_outcomes_accessed=False,
        followup='Only if calibration evidence supports hypothesis: fresh synthetic known-transfer controls, then unused real families')
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']=='A15_T510108')
    source_row=next(r for r in inv['channels'] if r['channel']==s['reference_channel'])
    target_row=next(r for r in inv['channels'] if r['channel']==10)
    original,sr,source=load_excerpt(inv,source_row,12060,12360,'original')
    target,tsr,target_source=load_excerpt(inv,target_row,12060,12360,'original')
    if source['decoded_sha256']!=s['source']['decoded_sha256'] or sr!=tsr:raise ValueError('Frozen source binding differs')
    extracted,projection=project_stationary_mains(original,sr,s['carrier_hz'],s['capture_hz'])
    trace=trajectory(extracted,sr,s['carrier_hz'],s['capture_hz'])
    curve,support=track_array(extracted,sr,s['carrier_hz'],s['capture_hz'],s['amplitude_reference'])
    t=np.arange(len(curve))/RATE;allowed=(t>=proposal['start_seconds'])&(t<proposal['end_seconds'])
    support*=allowed
    _,_,base_stats=make_map(curve,support,sr,len(target))
    if base_stats['positions_sha256']!=s['map_stats']['positions_sha256']:raise ValueError('Frozen main map not exactly reproduced')
    write(folder/'source_manifest.json',dict(reference=source,target=target_source,projection=projection,
        original_reference_peak=float(abs(original).max()),original_reference_clip_samples=int(np.sum(abs(original)>=.999)),
        frozen_main_hash_matches=True))
    rows=[];lo=round(min(e['start'] for e in cal)*RATE);hi=round(max(e['end'] for e in cal)*RATE)
    for lag in lags:
        shifted=shifted_curve(curve,lag)
        ss=np.interp(t-lag,t,support,left=0,right=0)*allowed
        amp=np.interp(t-lag,t,trace[3],left=trace[3][0],right=trace[3][-1])
        for gain in gains:
            transformed=shifted*gain
            p=preflight(transformed,ss,amp,sr,len(target))
            row=dict(lag_seconds=lag,gain=gain,physical_supported=bool(np.all(ss[lo:hi]>=.95)),preflight=p)
            if p['passed'] and row['physical_supported']:
                pos,grid,stats=make_map(transformed,ss,sr,len(target))
                metrics=evaluate_events(target,sr,transformed,cal,sample_positions=pos)
                row.update(calibration_loss=loss(metrics),per_event_loss=[loss([e]) for e in metrics],calibration=metrics)
            rows.append(row);write(folder/'results.json',rows)
        print(folder.name,'lag',lag,'committed',len(rows),flush=True)
    scored=[r for r in rows if r.get('calibration_loss') is not None]
    best=min(scored,key=lambda r:(r['calibration_loss'],abs(r['lag_seconds']),abs(r['gain']-1)))
    baseline=next(r for r in scored if r['lag_seconds']==0 and r['gain']==1)
    per_event=[min(scored,key=lambda r:(r['per_event_loss'][i],abs(r['lag_seconds']),abs(r['gain']-1))) for i in range(len(cal))]
    lag_spread=max(r['lag_seconds'] for r in per_event)-min(r['lag_seconds'] for r in per_event)
    gain_spread=max(r['gain'] for r in per_event)-min(r['gain'] for r in per_event)
    advance=best['calibration_loss']<.15 and baseline['calibration_loss']-best['calibration_loss']>=.05 and lag_spread<=.005 and gain_spread<=.2
    metrics=dict(trials=len(rows),valid_trials=len(scored),baseline_calibration_loss=baseline['calibration_loss'],
        best={k:best[k] for k in ('lag_seconds','gain','calibration_loss','per_event_loss')},
        per_event_winners=[{k:r[k] for k in ('lag_seconds','gain','calibration_loss','per_event_loss')} for r in per_event],
        lag_spread_seconds=lag_spread,gain_spread=gain_spread,advance_to_fresh_controls=advance,
        validation_outcomes_accessed=False,reserved_outcomes_accessed=False,real_qualification_upgraded=False)
    exposure(folder.name,[dict(source=target_source,calibration=cal)],'calibration_only_transfer_diagnostic',
        'Declared grid; no validation or final evaluation; I016 outcome remains consumed and unchanged')
    finish(folder,metrics,'test_fresh_controls' if advance else 'reject_transfer_as_primary_explanation',
        'Calibration-only transfer diagnostic completed. No fitted model is evaluated on consumed I016 validation or reserved events.',
        'If supported: fresh known-transfer controlled gate before a frozen unused real block. Otherwise pursue the measured extraction/support limitation without more fitting here.')
    print(metrics,flush=True)


if __name__=='__main__':run()
