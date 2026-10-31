"""Known-motion controls with offset/drifting digitization hum and real context."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter
import time
import numpy as np
from deflutter_campaign import ROOT,SEED,begin,finish,write,sha,memory_peak
from deflutter_motion_experiment import fidelity,assess
from deflutter_extractors import project_stationary_mains
from deflutter_adaptive_mains import remove_adaptive_comb
from flutter_tracking import trajectory,evaluate_events,RATE
from continuous_tracking import track_array
from carrier_capture import capture_candidates
from flutter_planning import loss


def cases(generation,count):
    rng=np.random.default_rng(int(sha((SEED+generation+'adaptive').encode())[:16],16))
    sr=8000;duration=44;context=40;t=np.arange((duration+2*context)*sr)/sr-context
    core=slice(context*sr,(context+duration)*sr)
    kinds=('offset_comb','drifting_comb','no_hum','near_valid','broadband','dropout','am_only','zero','wrong_motion','hum_only')
    events=[dict(start=float(s),end=float(s+.25)) for s in (4,8,12,16,24,28,32,36,40)]
    for index in range(count):
        kind=kinds[index%len(kinds)];rate=float(rng.choice([3.,6.5,13.,24.]))
        hz=float(rng.uniform(160,3750))
        while 1950<hz<3050 or abs(hz-round(hz/60)*60)<1:hz=float(rng.uniform(160,3750))
        if kind in ('offset_comb','drifting_comb'):
            harmonic=int(rng.choice([*range(4,33),*range(52,62)]));hz=60*harmonic+float(rng.uniform(.8,6.))
        if kind=='near_valid':hz=59.2;rate=6.5
        speed=.004*np.sin(2*np.pi*rate*t+.31)
        displacement=-.004/(2*np.pi*rate)*np.cos(2*np.pi*rate*t+.31)
        if kind=='broadband':
            speed+=.002*np.sin(2*np.pi*9.3*t+.7);displacement-=.002/(2*np.pi*9.3)*np.cos(2*np.pi*9.3*t+.7)
        if kind in ('am_only','zero','hum_only'):speed=np.zeros_like(t);displacement=np.zeros_like(t)
        reference_displacement=displacement if kind!='wrong_motion' else -.004/(2*np.pi*5.17)*np.cos(2*np.pi*5.17*t+.8)
        amplitude=np.full_like(t,.025)
        if kind=='am_only':amplitude*=1+.75*np.sin(2*np.pi*5.7*t)
        if kind=='dropout':amplitude[(t>18)&(t<21)]=0
        if kind=='hum_only':amplitude[:]=0
        x=amplitude*np.cos(2*np.pi*hz*(t+reference_displacement))+rng.normal(0,.0001,len(t))
        offset=float(rng.uniform(-.055,.055))
        base=2*np.pi*(60+offset)*t
        if kind=='drifting_comb':base-=.008/.03*np.cos(2*np.pi*.03*t)
        if kind in ('offset_comb','drifting_comb','hum_only'):
            harmonic=max(2,round(hz/60))
            x+=.12*np.cos(base)+.4*np.cos(harmonic*base+.7)
        envelope=np.zeros_like(t)
        for e in events:envelope[(t>=e['start'])&(t<e['end'])]=1
        x+=.15*envelope*np.cos(2*np.pi*2500*(t+displacement))
        truth=np.interp(np.arange(duration*RATE)/RATE,t,speed)
        yield dict(index=index,kind=kind,sr=sr,carrier_hz=hz,rate_hz=rate,hum_offset_hz=offset,
            extended=x,x=x[core],truth=truth,context_seconds=context,calibration=events[:4],validation=events[4:8])


def run(iteration,generation,count):
    folder=begin(iteration,'Narrow adaptive comb extraction handles measured mains offsets without learning target timing',
        'Raw and exact stationary projection versus adaptive electrical-phase comb; identical 40s source context on both sides',
        dict(easy_fidelity='signed corr>=.9, gain .8..1.2',negative_controls='Zero accepted AM/zero/wrong-motion/hum-only',
             support='Dropout cannot pass continuous correction',validation='One calibration-selected family choice per case'),
        synthetic=dict(generation=generation,count=count,sr=8000,duration=44,context_seconds=40,
            carrier_amplitude=.025,noise_sigma=.0001,hum_amplitude=.4,pilot_amplitude=.12,
            electrical_base_offset_range_hz=[-.055,.055],slow_drift_hz=.008,drift_rate_hz=.03,
            carriers='Random 160..3750Hz outside response plus capture guard 1950..3050, near-comb neighbors, legitimate 59.2Hz control',
            real_source_data=False),extractor_version='adaptive-mains-comb-research-v1')
    results=[];tick=time.perf_counter()
    for c in cases(generation,count):
        x=c['x'];extended=c['extended'];sr=c['sr'];hz=c['carrier_hz'];start=c['context_seconds']*RATE;end=start+44*RATE
        trials=[];arrays={}
        for capture in capture_candidates(hz,sr,(2.,30.)):
            for mode in ('raw','stationary','adaptive'):
                try:
                    if mode=='raw':extract=extended;meta=None
                    elif mode=='stationary':extract,meta=project_stationary_mains(extended,sr,hz,capture)
                    else:extract,meta=remove_adaptive_comb(extended,sr,hz,capture)
                    trace=trajectory(extract,sr,hz,capture)
                    amplitude=max(float(np.median(trace[3][start:end])),1e-12)
                    curve,support=track_array(extract,sr,hz,capture,amplitude)
                    curve=curve[start:end];support=support[start:end];amp=trace[3][start:end]
                    score=loss(evaluate_events(x,sr,curve,c['calibration']))
                    key=f'{mode}_{capture:g}';arrays[key]=(curve,support,amp)
                    trials.append(dict(key=key,mode=mode,capture_hz=capture,calibration_loss=score,
                        fidelity=fidelity(curve,c['truth'],support),extractor=meta))
                except (ValueError,FloatingPointError) as exc:trials.append(dict(mode=mode,capture_hz=capture,error=str(exc)))
        scored=[r for r in trials if r.get('calibration_loss') is not None]
        selected=min(scored,key=lambda r:(r['calibration_loss'],r['key'])) if scored else None
        row={k:v for k,v in c.items() if k not in ('extended','x','truth')}
        row.update(source_sha256=sha(extended.tobytes()),trials=trials,selected=selected)
        if selected:
            curve,support,amp=arrays[selected['key']]
            row['assessment']=assess(x,sr,curve,amp,support,c['calibration'],c['validation'])
        results.append(row);write(folder/'results.json',results)
        print(iteration,c['index'],c['kind'],None if selected is None else selected['key'],
            None if selected is None else selected['fidelity'],row.get('assessment',{}).get('quality',{}).get('quality_level'),flush=True)
    summary={}
    for kind in sorted({r['kind'] for r in results}):
        rs=[r for r in results if r['kind']==kind]
        summary[kind]=dict(cases=len(rs),fidelity_pass=sum(r['selected'].get('fidelity',{}).get('pass_fidelity',False) for r in rs if r['selected']),
            accepted=sum(r.get('assessment',{}).get('quality',{}).get('passed',False) for r in rs),
            selected_modes=dict(Counter(r['selected']['mode'] for r in rs if r['selected'])))
    controls=sum(summary[k]['accepted'] for k in ('am_only','zero','wrong_motion','hum_only'))
    finish(folder,dict(summary=summary,negative_control_accepted=controls,wall_seconds=time.perf_counter()-tick,
        peak_working_set_bytes=memory_peak()),'development_assessed' if controls==0 else 'reject',
        f'Offset/drifting-hum known-motion experiment completed; {controls} negative controls accepted. Hard failures remain visible.',
        'Inspect fidelity and controls; only a passing frozen extractor may enter fresh synthetic and unused real validation gates.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I004e');p.add_argument('--generation',default='development');p.add_argument('--count',type=int,default=40)
    a=p.parse_args();run(a.iteration,a.generation,a.count)
