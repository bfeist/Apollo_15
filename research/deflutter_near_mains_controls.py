"""Fresh low-frequency/near-mains separation stress test; no real audio."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
from collections import Counter
import argparse
import time
import numpy as np
from deflutter_campaign import SEED,begin,finish,write,sha,memory_peak
from deflutter_adaptive_mains import remove_adaptive_comb
from deflutter_high_rejection import track_array_high_rejection
from deflutter_motion_experiment import fidelity
from deflutter_local_experiment import evaluate
from deflutter_real_experiment import make_map
from flutter_tracking import RATE,trajectory,evaluate_events
from continuous_tracking import track_array
from flutter_planning import loss


def cases(generation='near-fundamental-v1',include_reserved=False):
    rng=np.random.default_rng(int(sha((SEED+generation).encode())[:16],16))
    sr=8000;context=40;duration=48 if include_reserved else 44;t=np.arange((duration+2*context)*sr)/sr-context
    times=(4,8,12,16,24,28,32,36,40,44) if include_reserved else (4,8,12,16,24,28,32,36)
    events=[dict(start=float(s),end=float(s+.25),synchronized_group_id=f'controlled_family_{s}') for s in times]
    for index in range(48):
        kind=('near','far','am_only','zero','wrong_motion','hum_only','dropout','coincident')[index%8]
        tier=index//8;db=(40,60,80)[tier%3];pcm=tier>=3
        delta=float(rng.choice([-1.,1.])*rng.uniform(1.5,3.))
        hz=60.02+delta
        if kind=='far':
            hz=float(rng.uniform(300,1800))
            while abs(hz-round(hz/60)*60)<15:hz=float(rng.uniform(300,1800))
        if kind=='coincident':hz=60.02+.1
        rates=rng.uniform([3.2,10.2],[7.8,19.2]);phases=rng.uniform(-np.pi,np.pi,2)
        displacement=sum(-a/(2*np.pi*f)*np.cos(2*np.pi*f*t+p) for a,f,p in zip((.003,.0015),rates,phases))
        speed=sum(a*np.sin(2*np.pi*f*t+p) for a,f,p in zip((.003,.0015),rates,phases))
        if kind in ('am_only','zero'):displacement=np.zeros_like(t);speed=np.zeros_like(t)
        donor=displacement if kind!='wrong_motion' else -.004/(2*np.pi*8.17)*np.cos(2*np.pi*8.17*t+.8)
        amplitude=np.full_like(t,.4/10**(db/20))
        if kind=='am_only':amplitude*=1+.6*np.sin(2*np.pi*5.7*t)
        if kind=='hum_only':amplitude[:]=0
        if kind=='dropout':amplitude[(t>18)&(t<21)]=0
        electrical=2*np.pi*60.02*t-.003/.02*np.cos(2*np.pi*.02*t)
        x=.4*np.cos(electrical)+amplitude*np.cos(2*np.pi*hz*(t+donor))+rng.normal(0,1e-7,len(t))
        burst=np.zeros_like(t)
        for e in events:burst[(t>=e['start'])&(t<e['end'])]=1
        reference=x.copy()
        x+=.1*burst*np.cos(2*np.pi*2500*(t+displacement))
        if pcm:
            x=np.rint(x*32768)/32768;reference=np.rint(reference*32768)/32768
        core=slice(context*sr,(context+duration)*sr)
        yield dict(index=index,kind=kind,hum_to_carrier_db=db,pcm16=pcm,sr=sr,hz=hz,
            extended=x,reference_extended=reference,x=x[core],truth=np.interp(np.arange(duration*RATE)/RATE,t,speed),
            calibration=events[:4],validation=events[4:8],reserved=events[8:],rates_hz=rates.tolist())


def run(iteration='I018_near_mains',separate_reference=False):
    folder=begin(iteration,
        'Adaptive contamination separation preserves independently fluttered weak near-fundamental tones without inventing motion',
        'Raw/adaptive extraction x legacy/high-rejection tracker; identical predeclared26/35Hz captures; calibration-only selection',
        dict(easy='Unquantized40dB separable near/far: signed corr>=.9 and gain.8..1.2 plus unchanged response gate',
             hard='60/80dB and PCM16 reported by tier, no relaxed fidelity/support/response thresholds',
             controls='Zero accepted AM/zero/wrong-motion/hum-only and no continuous dropout correction',
             ambiguous='0.1Hz coincident case reported separately; no identifiability claim'),
        synthetic=dict(generation='near-fundamental-v1',count=48,context_each_side=40,duration=44,sr=8000,
            separate_reference=separate_reference,
            comparison_parent='I018_near_mains matched development draws; no fresh qualification claim' if separate_reference else None,
            near_offset_hz=[1.5,3.],ground_hz=60.02,slow_ground_drift_hz=.003,
            hum_to_carrier_db=[40,60,80],pcm16='second three tiers',
            calibration_support='Require unchanged .95 support across all calibration events before selection',
            source_amplitude='Median source-only trace over core, no truth selection',
            response='Separate reference channel has same motion/hum and independent PCM16 quantization but no response bursts; target waveform unchanged from I018' if separate_reference else 'Same physical samples;2500Hz independent oscillator outside predictor capture',
            reserved_outcomes='None; development component stress test only'))
    tick=time.perf_counter();rows=[]
    for case in cases():
        sr=case['sr'];hz=case['hz'];x=case['x'];original=case['reference_extended'] if separate_reference else case['extended'];a=40*RATE;b=a+44*RATE
        adaptive,metadata=remove_adaptive_comb(original,sr,hz,35.)
        trials=[];arrays={}
        for source,extract in [('raw',original),('adaptive',adaptive)]:
            for capture in (26.,35.):
                trace=trajectory(extract,sr,hz,capture);amplitude=max(float(np.median(trace[3][a:b])),1e-12)
                for tracker,function in [('legacy',track_array),('high',track_array_high_rejection)]:
                    key=f'{source}:{tracker}:{capture:g}'
                    try:
                        curve,support=function(extract,sr,hz,capture,amplitude);curve=curve[a:b];support=support[a:b]
                        physical=bool(np.all(support[4*RATE:round(16.25*RATE)]>=.95))
                        score=None
                        if physical:
                            pos,_,_=make_map(curve,support,sr,len(x))
                            score=loss(evaluate_events(x,sr,curve,case['calibration'],sample_positions=pos))
                        trials.append(dict(key=key,calibration_loss=score,calibration_supported=physical,
                            fidelity=fidelity(curve,case['truth'],support)))
                        arrays[key]=(curve,support,trace[3][a:b])
                    except ValueError as exc:trials.append(dict(key=key,error=str(exc)))
        scored=[r for r in trials if r.get('calibration_loss') is not None]
        selected=min(scored,key=lambda r:(r['calibration_loss'],r['key'])) if scored else None
        row={k:v for k,v in case.items() if k not in ('extended','reference_extended','x','truth')}
        row.update(source_sha256=sha(original.tobytes()),target_sha256=sha(x.tobytes()),adaptive_metadata=metadata,trials=trials,selected=selected)
        if selected:
            curve,support,amp=arrays[selected['key']]
            try:row['assessment']=evaluate(x,sr,curve,support,amp,case['calibration'],case['validation'])
            except ValueError as exc:row['invalid_map_or_control']=str(exc)
        rows.append(row);write(folder/'results.json',rows)
        print(folder.name,case['index'],case['kind'],case['hum_to_carrier_db'],case['pcm16'],
              selected['key'] if selected else None,row.get('assessment',{}).get('quality',{}).get('passed'),flush=True)
    summary={}
    for kind in sorted({r['kind'] for r in rows}):
        group=[r for r in rows if r['kind']==kind]
        summary[kind]=dict(cases=len(group),fidelity_pass=sum(r['selected']['fidelity'].get('pass_fidelity',False) for r in group if r['selected']),
            accepted=sum(r.get('assessment',{}).get('quality',{}).get('passed',False) for r in group),
            failures=dict(Counter(p for r in group for p in r.get('assessment',{}).get('quality',{}).get('failed_predicates',[]))))
    negative=sum(summary[k]['accepted'] for k in ('am_only','zero','wrong_motion','hum_only','dropout'))
    finish(folder,dict(summary=summary,negative_accepted=negative,wall_seconds=time.perf_counter()-tick,
        peak_working_set_bytes=memory_peak(),real_data_accessed=False),
        'development_assessed' if negative==0 else 'reject',
        f'Fresh near-mains component controls completed; negative accepted {negative}. Preserve every tier and ambiguous case; no real promotion.',
        'Assess per-tier signed fidelity and response failures. Freeze any revised separation protocol on fresh draws before another real gate; do not change active I016 batch.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I018_near_mains');p.add_argument('--separate-reference',action='store_true')
    a=p.parse_args();run(a.iteration,a.separate_reference)
