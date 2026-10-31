"""Controlled dynamic-range gate for stronger carrier rejection, not production."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter
import time
import numpy as np
from scipy import signal
from deflutter_campaign import SEED,begin,finish,write,sha,memory_peak
from deflutter_motion_experiment import fidelity,assess
from deflutter_adaptive_mains import remove_adaptive_comb
from deflutter_high_rejection import track_array_high_rejection,HighRejectionStream
from continuous_tracking import track_array,CarrierStream
from flutter_tracking import trajectory,evaluate_events,RATE
from flutter_planning import loss
from carrier_capture import capture_candidates


def cases(generation,count):
    rng=np.random.default_rng(int(sha((SEED+generation+'high-rejection').encode())[:16],16))
    sr=8000;context=40;duration=44;t=np.arange((duration+2*context)*sr)/sr-context;core=slice(context*sr,(context+duration)*sr)
    events=[dict(start=float(s),end=float(s+.25)) for s in (4,8,12,16,24,28,32,36,40)]
    kinds=('remote_hum','near_remote','no_hum','dropout','am_only','zero','wrong_motion','hum_only','clipped')
    for index in range(count):
        kind=kinds[index%9];tier=index//9;db=(40,60,80,80)[tier%4];pcm=tier%4==3
        hz=float(rng.uniform(160,1900));nominal=hz-8;rate=float(rng.choice([3.,6.5,13.,24.]))
        while abs(hz-round(hz/60.027)*60.027)<3:hz=float(rng.uniform(160,1900));nominal=hz-8
        speed=.004*np.sin(2*np.pi*rate*t+.31);displacement=-.004/(2*np.pi*rate)*np.cos(2*np.pi*rate*t+.31)
        if kind in ('am_only','zero','hum_only','clipped'):speed=np.zeros_like(t);displacement=np.zeros_like(t)
        reference_displacement=displacement if kind!='wrong_motion' else -.004/(2*np.pi*5.17)*np.cos(2*np.pi*5.17*t+.8)
        amplitude=.55/10**(db/20) if kind in ('remote_hum','near_remote') else .025
        envelope=np.full_like(t,amplitude)
        if kind=='am_only':envelope*=1+.75*np.sin(2*np.pi*5.7*t)
        if kind=='dropout':envelope[(t>18)&(t<21)]=0
        if kind=='hum_only':envelope[:]=0
        x=envelope*np.cos(2*np.pi*hz*(t+reference_displacement))+rng.normal(0,1e-7,len(t))
        if kind in ('remote_hum','near_remote','hum_only'):
            electrical=2*np.pi*60.027*t-.003/.02*np.cos(2*np.pi*.02*t)
            x+=.55*np.cos(electrical)
            if kind in ('near_remote','hum_only'):x+=.2*np.cos(round(hz/60.027)*electrical+.7)
        burst=np.zeros_like(t)
        for e in events:burst[(t>=e['start'])&(t<e['end'])]=1
        x+=.1*burst*np.cos(2*np.pi*2500*(t+displacement))
        if kind=='clipped':x[(t>18)&(t<21)]=1.
        if pcm:x=np.rint(x*32768)/32768
        yield dict(index=index,kind=kind,sr=sr,x=x[core],extended=x,truth=np.interp(np.arange(duration*RATE)/RATE,t,speed),
            true_carrier_hz=hz,carrier_hz=nominal,rate_hz=rate,hum_to_carrier_db=db,pcm16=pcm,
            context_seconds=context,calibration=events[:4],validation=events[4:8])


def run(iteration,generation,count):
    folder=begin(iteration,'Stronger finite-filter rejection preserves weak motion under much louder out-of-band hum',
        'Legacy versus high-rejection stream, raw versus adaptive-comb original samples; identical context/capture menu',
        dict(easy='Unquantized40/60dB pure, remote-hum and separable-neighbor cases: corr>=.9 and gain .8..1.2',
             hard='80dB and PCM16 tiers reported separately without lowering difficulty',
             controls='Zero accepted AM/zero/wrong-motion/hum-only/clipped; dropout blocks continuous correction',
             selection='One calibration-selected variant before validation; truth fidelity never selects'),
        synthetic=dict(generation=generation,count=count,sr=8000,duration=44,real_context_each_side=40,
            carrier_domain_hz=[160,1900],nominal_detuning_hz=8,hum_to_carrier_db=[40,60,80],
            electrical_hz=60.027,slow_electrical_drift_hz=.003,quantization='Fourth draw per condition PCM16',
            capture_menu='Legacy choices plus min(180,.8*nominal,42+detuning); identical for both trackers'),
        changed_delay_tracking_samples=245,original_clip_flags_propagated=True,real_data=False)
    results=[];tick=time.perf_counter()
    for c in cases(generation,count):
        original=c['extended'];sr=c['sr'];hz=c['carrier_hz'];a=40*RATE;b=a+44*RATE
        options=list(capture_candidates(hz,sr,(2.,30.)));options.append(min(180.,.8*hz,50.))
        adaptive,meta=remove_adaptive_comb(original,sr,hz,max(options));trials=[];arrays={}
        for source,extract in (('raw',original),('adaptive',adaptive)):
            for capture in sorted(set(round(v,6) for v in options)):
                try:
                    trace=trajectory(extract,sr,hz,capture);amplitude=max(float(np.median(trace[3][a:b])),1e-12)
                except ValueError as exc:
                    trials.append(dict(source=source,capture_hz=capture,error=str(exc)));continue
                for tracker,function in (('legacy',track_array),('high_rejection',track_array_high_rejection)):
                    key=f'{source}:{tracker}:{capture:g}'
                    try:
                        if tracker=='high_rejection' and np.max(abs(original))>=.999:
                            raise ValueError('Original clip flag retained after transform')
                        curve,support=function(extract,sr,hz,capture,amplitude);curve=curve[a:b];support=support[a:b]
                        score=loss(evaluate_events(c['x'],sr,curve,c['calibration']))
                        trials.append(dict(key=key,source=source,tracker=tracker,capture_hz=capture,
                            calibration_loss=score,fidelity=fidelity(curve,c['truth'],support)))
                        arrays[key]=(curve,support,trace[3][a:b])
                    except ValueError as exc:trials.append(dict(key=key,source=source,tracker=tracker,error=str(exc)))
        scored=[t for t in trials if t.get('calibration_loss') is not None]
        selected=min(scored,key=lambda t:(t['calibration_loss'],t['key'])) if scored else None
        row={k:v for k,v in c.items() if k not in ('x','extended','truth')}
        row.update(source_sha256=sha(original.tobytes()),trials=trials,selected=selected,adaptive_metadata=meta)
        if selected:
            curve,support,amp=arrays[selected['key']]
            row['assessment']=assess(c['x'],sr,curve,amp,support,c['calibration'],c['validation'])
        results.append(row);write(folder/'results.json',results)
        print(iteration,c['index'],c['kind'],c['hum_to_carrier_db'],c['pcm16'],None if not selected else selected['key'],
            None if not selected else selected['fidelity'],row.get('assessment',{}).get('quality',{}).get('quality_level'),flush=True)
    summary={}
    for kind in sorted({r['kind'] for r in results}):
        rs=[r for r in results if r['kind']==kind]
        summary[kind]=dict(cases=len(rs),fidelity_pass=sum(r['selected'].get('fidelity',{}).get('pass_fidelity',False) for r in rs if r['selected']),
            accepted=sum(r.get('assessment',{}).get('quality',{}).get('passed',False) for r in rs))
    controls=sum(summary[k]['accepted'] for k in ('am_only','zero','wrong_motion','hum_only','clipped'))
    finish(folder,dict(summary=summary,negative_control_accepted=controls,wall_seconds=time.perf_counter()-tick,
        peak_working_set_bytes=memory_peak()),'controlled_assessed' if controls==0 else 'reject',
        f'High dynamic-range tracking experiment completed; negative controls accepted {controls}. Every weak/quantized case retained.',
        'Inspect per-tier fidelity/support and source ablation. No real promotion without a frozen combined-method unused-family gate.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I013b');p.add_argument('--generation',default='development');p.add_argument('--count',type=int,default=36)
    a=p.parse_args();run(a.iteration,a.generation,a.count)
