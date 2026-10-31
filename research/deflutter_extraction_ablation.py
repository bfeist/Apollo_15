"""Calibration-only source/extractor diagnostic on the exposed 400Hz passage.

This is explicitly development. No validation or reserved outcome is evaluated,
and no result can repair or promote the contaminated old response families.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import time
import argparse
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure,memory_peak
from deflutter_extractors import project_stationary_mains
from deflutter_adaptive_mains import remove_adaptive_comb
from flutter_tracking import trajectory,evaluate_events,RATE
from continuous_tracking import track_array
from flutter_planning import loss
from deflutter_high_rejection import track_array_high_rejection


def run(iteration='I012',high_rejection=False):
    parent=ROOT/'iterations/I005c2';previous=next(r for r in read(parent/'results.json') if r['target_channel']==14)
    selected=previous['selected'];tape=previous['tape'];start=previous['start'];duration=previous['end']-start
    folder=begin(iteration,'Contamination extraction and amplitude calibration explain the exposed carrier result',
        'Raw, cached denoised, exact stationary projection, adaptive comb; global versus calibration-span amplitude normalization',
        'Mechanistic calibration/support measurements only; no validation, reserved, promotion or independent-real claim',
        target=14,reference=selected,capture_hz=selected['capture_hz'],context_seconds=40,
        data_status='All old response families exposed/quarantined; development only',
        high_rejection_comparison=high_rejection,
        principal_question='Can original-sample extraction recover the phase information that cached cleanup exposed?')
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==tape);rows={r['channel']:r for r in inv['channels']}
    original,sr,rawsource=load_excerpt(inv,rows[selected['reference_channel']],start-40,start+duration+40,'original')
    clean,_,cleansource=load_excerpt(inv,rows[selected['reference_channel']],start-40,start+duration+40,'denoised')
    target,_,targetsource=load_excerpt(inv,rows[14],start,start+duration,'original')
    cal=read(parent/'roles_0.json')['roles']['14']['calibration']
    offset=40*RATE;end=offset+round(duration*RATE);first=round(min(e['start'] for e in cal)*RATE);last=round(max(e['end'] for e in cal)*RATE)
    hz=selected['carrier_hz'];capture=selected['capture_hz'];results=[];curves={};tick=time.perf_counter()
    for mode in ('raw','denoised','stationary','adaptive'):
        if mode=='raw':extract=original;meta=None
        elif mode=='denoised':extract=clean;meta=None
        elif mode=='stationary':extract,meta=project_stationary_mains(original,sr,hz,capture)
        else:extract,meta=remove_adaptive_comb(original,sr,hz,capture)
        trace=trajectory(extract,sr,hz,capture);amp=trace[3][offset:end]
        for policy in ('global_core','calibration_span'):
            amplitude=max(float(np.median(amp[400:-400] if policy=='global_core' else amp[first:last])),1e-12)
            for tracker in (('legacy','high_rejection') if high_rejection else ('legacy',)):
                key=mode+'_'+policy+('_'+tracker if high_rejection else '')
                try:
                    if tracker=='high_rejection' and np.max(abs(original))>=.999:
                        raise ValueError('Original input clipped; do not hide this by transforming it')
                    function=track_array if tracker=='legacy' else track_array_high_rejection
                    speed,support=function(extract,sr,hz,capture,amplitude);speed=speed[offset:end];support=support[offset:end]
                    measured=evaluate_events(target,sr,speed,cal)
                    item=dict(key=key,mode=mode,tracker=tracker,amplitude_policy=policy,amplitude_reference=amplitude,
                        calibration_loss=loss(measured),continuous_calibration_support=bool(np.all(support[first:last]>=.95)),
                        calibration_support_fraction=float(np.mean(support[first:last]>=.95)),
                        motion_rms_in_calibration_span=float(np.std(speed[first:last])),extractor=meta,
                        metrics=measured,validation_evaluated=False,reserved_evaluated=False)
                    curves[key]=(speed,support)
                    print('ablation',key,item['calibration_loss'],item['calibration_support_fraction'],flush=True)
                except ValueError as exc:
                    item=dict(key=key,status='tracking_failed',error=str(exc),validation_evaluated=False,reserved_evaluated=False)
                    print('ablation',key,'failed',str(exc),flush=True)
                results.append(item)
    comparisons=[]
    for key,(speed,support) in curves.items():
        base,basegate=curves['denoised_global_core_legacy' if high_rejection else 'denoised_global_core'];keep=(support[first:last]>=.95)&(basegate[first:last]>=.95)
        a=speed[first:last][keep];b=base[first:last][keep]
        comparisons.append(dict(key=key,common_samples=int(keep.sum()),
            correlation_with_cleaned=float(np.corrcoef(a,b)[0,1]) if len(a)>2 and min(np.std(a),np.std(b))>1e-12 else None,
            gain_against_cleaned=float(np.dot(a,b)/np.dot(b,b)) if len(a)>2 and np.dot(b,b)>1e-20 else None))
    write(folder/'results.json',results);write(folder/'source_comparisons.json',comparisons)
    write(folder/'source_manifest.json',dict(original_context=rawsource,denoised_context=cleansource,target=targetsource))
    exposure(iteration,[rawsource,cleansource,targetsource],'exposed_development_calibration_only','No validation or reserved measurements; no qualification rescue')
    finish(folder,dict(calibration_rows=[{k:v for k,v in r.items() if k not in ('metrics','extractor')} for r in results],
        wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak(),validation_evaluated=False,reserved_evaluated=False),
        'mechanism_assessed','Exposed-source calibration ablation completed; no independent outcome or promotion claim.',
        'Use mechanism results to design a separately frozen extractor generation with fresh controls and unused real response families; keep current I011 unchanged.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I012');p.add_argument('--high-rejection',action='store_true');a=p.parse_args();run(a.iteration,a.high_rejection)
