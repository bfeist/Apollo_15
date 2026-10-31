"""Frozen known-motion and same-channel independent-response experiments.

No real reserved audio is loaded. Synthetic truth uses recorded y(t)=s(t+d(t));
therefore relative speed is d'(t), and the existing inverse-map convention must
reduce a separately generated response oscillator's jitter.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter
import json
import sys
import time

import numpy as np
from scipy import signal

from deflutter_campaign import ROOT, SEED, begin, finish, sha, write, memory_peak
from flutter_tracking import trajectory, signed_map, evaluate_events, RATE
from continuous_tracking import track_array
from carrier_capture import capture_candidates
from flutter_policy import LIMITS, shifted_curve, evidence_strength
from continuous_quality import classify_numeric_quality
from flutter_planning import loss
from flutter import positions
from deflutter_extractors import project_stationary_mains


def controlled_cases(generation, count=32):
    rng=np.random.default_rng(int(sha((SEED+generation+'motion').encode())[:16],16))
    sr=8000;duration=44.;t=np.arange(round(sr*duration))/sr
    for index in range(count):
        kind=('pure','close_hum','am_only','wrong_motion','dropout','zero','broadband_motion','outside_band')[index%8]
        hz=float(rng.uniform(160,1900))
        while abs(hz-round(hz/60)*60)<10:hz=float(rng.uniform(160,1900))
        rate=float(rng.choice([3.,6.5,13.,24.]))
        if kind=='outside_band':rate=float(rng.choice([.7,38.]))
        speed=.004*np.sin(2*np.pi*rate*t+.31)
        displacement=-.004/(2*np.pi*rate)*np.cos(2*np.pi*rate*t+.31)
        if kind=='broadband_motion':
            speed+=.002*np.sin(2*np.pi*9.3*t+.7)
            displacement-=.002/(2*np.pi*9.3)*np.cos(2*np.pi*9.3*t+.7)
        if kind in ('zero','am_only'):
            speed=np.zeros_like(t);displacement=np.zeros_like(t)
        carrier_displacement=displacement.copy()
        if kind=='wrong_motion':carrier_displacement=-.004/(2*np.pi*5.17)*np.cos(2*np.pi*5.17*t+.8)
        if kind=='close_hum':hz=float(60*rng.integers(4,29)+rng.uniform(6,19))
        amplitude=.025*(1+.75*np.sin(2*np.pi*5.7*t)) if kind=='am_only' else np.full_like(t,.025)
        if kind=='dropout':amplitude[(t>18)&(t<21)]=0
        x=amplitude*np.cos(2*np.pi*hz*(t+carrier_displacement))+rng.normal(0,.0001,len(t))
        if kind=='close_hum':
            nearest=round(hz/60)*60
            x+=.4*np.cos(2*np.pi*nearest*t)
        # Same physical channel, separate carrier and response frequencies.
        events=[dict(start=float(s),end=float(s+.25)) for s in (4,8,12,16,24,28,32,36,40)]
        envelope=np.zeros_like(t)
        for event in events:
            envelope[(t>=event['start'])&(t<event['end'])]=1
        response=.15*envelope*np.cos(2*np.pi*2500*(t+displacement))
        yield dict(index=index,kind=kind,sr=sr,x=x+response,carrier_hz=hz,
            truth=np.interp(np.arange(round(duration*RATE))/RATE,t,speed),
            calibration=events[:4],validation=events[4:8],reserved=events[8:],rate_hz=rate)


def fidelity(curve, truth, support):
    keep=(np.arange(len(curve))/RATE>=3)&(np.arange(len(curve))/RATE<(len(curve)/RATE-3))&(support>=.95)
    a=curve[keep];b=truth[keep]
    if len(a)<400:return dict(status='insufficient_support',support=float(np.mean(keep)))
    rms=float(np.sqrt(np.mean(a*a)));true_rms=float(np.sqrt(np.mean(b*b)))
    if true_rms<1e-10:
        return dict(status='zero_truth',estimated_rms=rms,support=float(np.mean(keep)))
    correlation=float(np.corrcoef(a,b)[0,1]) if np.std(a)>1e-12 else 0.
    gain=float(np.dot(a,b)/np.dot(b,b))
    return dict(status='measured',correlation=correlation,signed_gain=gain,
        rms_error=float(np.sqrt(np.mean((a-b)**2))),amplitude_ratio=rms/true_rms,
        support=float(np.mean(keep)),pass_fidelity=correlation>=.9 and .8<=gain<=1.2)


def assess(x,sr,curve,amp_curve,support,cal,val):
    metrics=evaluate_events(x,sr,curve,val)
    amp=amp_curve-np.median(amp_curve)
    amp*=np.std(curve)/max(np.std(amp),1e-12)
    controls=dict(no_motion=evaluate_events(x,sr,np.zeros_like(curve),val),
                  wrong_sign=evaluate_events(x,sr,-curve,val),
                  amplitude=evaluate_events(x,sr,np.clip(amp,-.049,.049),val))
    shifted={str(s):evaluate_events(x,sr,shifted_curve(curve,s),val) for s in LIMITS['wrong_time_seconds']}
    lo=round(min(e['start'] for e in cal+val)*RATE);hi=round(max(e['end'] for e in cal+val)*RATE)
    supported=bool(np.all(support[lo:hi]>=.95))
    quality=classify_numeric_quality(metrics,controls,shifted,supported=supported,
        independent_windows=evidence_strength(cal,val)['level']=='multiple_time_windows')
    return dict(quality=quality,main=metrics,controls=controls,wrong_time=shifted)


def run(iteration,generation,count,projection=False):
    folder=begin(iteration,'Same-channel spectrally independent carrier predicts response after frozen calibration',
        'Existing streaming capture choices vs hum-distance-limited capture; exact same target events',
        dict(easy_fidelity='signed corr >=0.9, gain 0.8..1.2',
             controls='zero accepted AM-only/zero/wrong-motion; unchanged classify_numeric_quality',
             independent_response='2500Hz oscillator, carrier outside 2150..2850Hz',
             continuity='dropouts must not qualify continuous correction'),
        synthetic=dict(generation=generation,count=count,sr=8000,duration_seconds=44,
            warp_convention='y(t)=s(t+d(t)); speed=dprime(t); inversion uses saved positions()',
            calibration=[4,8,12,16],validation=[24,28,32,36],reserved=[40],
            reserved_outcomes='not evaluated; insufficient final two-event audit, no final qualification claim'),
        extraction='Original mixed audio plus stationary-mains projection' if projection else 'Original mixed audio; no fabricated denoised copy',
        candidate_selection='All extraction variants scored on calibration only; freeze once before validation')
    results=[];tick=time.perf_counter()
    for case in controlled_cases(generation,count):
        sr=case['sr'];x=case['x'];hz=case['carrier_hz'];trials=[];arrays={}
        choices=[('legacy',c) for c in capture_candidates(hz,sr,(2.,30.))]
        distance=abs(hz-round(hz/60)*60)
        if distance>1:choices.append(('hum_distance',min(35.,distance*.65)))
        if projection:
            choices += [('projected_'+mode,capture) for mode,capture in choices]
        for mode,capture in choices:
            try:
                extract,projection_meta=project_stationary_mains(x,sr,hz,capture) if mode.startswith('projected_') else (x,None)
                trace=trajectory(extract,sr,hz,capture)
                amplitude=max(float(np.median(trace[3][400:-400])),1e-12)
                curve,support=track_array(extract,sr,hz,capture,amplitude)
                key=f'{mode}_{capture:.6f}'
                measured=evaluate_events(x,sr,curve,case['calibration'])
                trials.append(dict(key=key,capture_hz=capture,mode=mode,calibration_loss=loss(measured),
                    fidelity=fidelity(curve,case['truth'],support),projection=projection_meta))
                arrays[key]=(curve,support,trace[3])
            except (ValueError,FloatingPointError) as exc:
                trials.append(dict(mode=mode,capture_hz=capture,error=str(exc)))
        scored=[r for r in trials if r.get('calibration_loss') is not None]
        selected=min(scored,key=lambda r:(r['calibration_loss'],r['key'])) if scored else None
        result={k:v for k,v in case.items() if k not in ('x','truth')}
        result.update(source_sha256=sha(x.tobytes()),trials=trials,selected=selected)
        if selected:
            curve,support,amp=arrays[selected['key']]
            result['assessment']=assess(x,sr,curve,amp,support,case['calibration'],case['validation'])
            # Check exact full map once; no silently clipped/reanchored repairs.
            pos=positions(curve,sr,len(x));speed=np.diff(pos)
            result['map']=dict(finite=bool(np.isfinite(pos).all()),strictly_increasing=bool(np.all(speed>0)),
                min_speed=float(speed.min()),max_speed=float(speed.max()),sha256=sha(pos.tobytes()))
        results.append(result)
        write(folder/'results.json',results)
        print(iteration,case['index'],case['kind'],None if not selected else selected['fidelity'],
              None if not selected else result['assessment']['quality']['quality_level'],flush=True)
    summary={}
    for kind in sorted({r['kind'] for r in results}):
        subset=[r for r in results if r['kind']==kind]
        selected=[r['selected']['fidelity'] for r in subset if r['selected']]
        summary[kind]=dict(cases=len(subset),fidelity_pass=sum(s.get('pass_fidelity',False) for s in selected),
            accepted=sum(r.get('assessment',{}).get('quality',{}).get('passed',False) for r in subset),
            failures=dict(Counter(k for r in subset for k in r.get('assessment',{}).get('quality',{}).get('failed_predicates',[]))))
    controls=sum(summary[k]['accepted'] for k in ('am_only','zero','wrong_motion'))
    finish(folder,dict(summary=summary,wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak(),
        negative_control_accepted=controls,real_correction_qualified=0),
        'retain_for_development' if controls==0 else 'reject',
        f'Known-motion and independent-response results saved; negative-control accepted count {controls}. Hard-rate and contamination failures are retained.',
        'Use actual real catalog on development channels with separate immutable calibration/validation response events; no final reserved outcomes.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I004');p.add_argument('--generation',default='development');p.add_argument('--count',type=int,default=32);p.add_argument('--stationary-projection',action='store_true')
    a=p.parse_args();run(a.iteration,a.generation,a.count,a.stationary_projection)
