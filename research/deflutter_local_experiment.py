"""Controlled audio test of bounded source-island acquisition, not real promotion."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter
import time
import numpy as np

from deflutter_campaign import ROOT, SEED, begin, finish, sha, write, memory_peak
from deflutter_local_acquisition import opportunities, source_amplitude_reference, common_event_acquisition
from deflutter_real_experiment import make_map
from flutter_tracking import RATE, trajectory, evaluate_events
from continuous_tracking import track_array
from continuous_quality import classify_numeric_quality
from flutter_policy import LIMITS, shifted_curve, evidence_strength


def events():
    # Frozen before generation/measurement. No duplicated response families.
    return [dict(start=float(t),end=float(t+.25),role=role,synchronized_group_id=f'g{t}')
            for role,ts in [('calibration',(10,20,90,100)),('validation',(110,119,250,260)),
                            ('final',(130,140))] for t in ts]


def cases(generation,count):
    rng=np.random.default_rng(int(sha((SEED+generation+'source-island').encode())[:16],16))
    sr=8000;t=np.arange(sr*300)/sr
    for index in range(count):
        kind=('intermittent','continuous','split_gap','wrong_motion','am_only','zero')[index%6]
        hz=float(rng.uniform(300,1800))
        while abs(hz-round(hz/60)*60)<20:hz=float(rng.uniform(300,1800))
        phases=rng.uniform(-np.pi,np.pi,2)
        rates=rng.uniform([3.1,10.1],[7.9,19.1])
        displacement=sum(-a/(2*np.pi*f)*np.cos(2*np.pi*f*t+p)
                         for a,f,p in zip((.003,.0015),rates,phases))
        if kind in ('zero','am_only'):displacement=np.zeros_like(t)
        donor=displacement
        if kind=='wrong_motion':donor=-.003/(2*np.pi*8.17)*np.cos(2*np.pi*8.17*t+.83)
        envelope=np.ones_like(t) if kind=='continuous' else ((t>=80)&(t<180)).astype(float)
        if kind=='split_gap':envelope[(t>=105)&(t<108)]=0
        amplitude=.025*envelope
        if kind=='am_only':amplitude*=1+.6*np.sin(2*np.pi*5.7*t)
        source=amplitude*np.cos(2*np.pi*hz*(t+donor))+rng.normal(0,.00003,len(t))
        response=np.zeros_like(t)
        for event in events():
            k=(t>=event['start'])&(t<event['end'])
            response[k]+=.15*np.cos(2*np.pi*2500*(t[k]+displacement[k]))
        # Same physical samples, independent spectral predictor/response.
        yield dict(index=index,kind=kind,hz=hz,sr=sr,x=source+response,rates=rates.tolist())


def evaluate(x,sr,curve,support,amp,cal,val):
    pos,_,stats=make_map(curve,support,sr,len(x))
    main=evaluate_events(x,sr,curve,val,sample_positions=pos)
    amplitude=amp-np.median(amp)
    amplitude*=np.std(curve)/max(np.std(amplitude),1e-12)
    controls={}
    # Match the existing research runner's predeclared AM control, not a repair
    # to a failed candidate map. The main speed is never clipped here.
    for name,c in [('no_motion',np.zeros_like(curve)),('wrong_sign',-curve),('amplitude',np.clip(amplitude,-.049,.049))]:
        control_pos,_,_=make_map(c,support,sr,len(x))
        controls[name]=evaluate_events(x,sr,c,val,sample_positions=control_pos)
    shifts={}
    for shift in LIMITS['wrong_time_seconds']:
        c=shifted_curve(curve,shift);p,_,_=make_map(c,support,sr,len(x))
        shifts[str(shift)]=evaluate_events(x,sr,c,val,sample_positions=p)
    lo=round(min(e['start'] for e in cal+val)*RATE)
    hi=round(max(e['end'] for e in cal+val)*RATE)
    quality=classify_numeric_quality(main,controls,shifts,
        supported=bool(np.all(support[lo:hi]>=.95)),
        independent_windows=evidence_strength(cal,val)['level']=='multiple_time_windows')
    return dict(quality=quality,main=main,controls=controls,wrong_time=shifts,map_stats=stats)


def run(iteration,generation,count,bounded_map=False):
    folder=begin(iteration,
        'A reference lasting 100s can qualify a bounded acquisition without spanning all events in 300s',
        'Identical waveform, source tracker, thresholds and frozen roles; whole-passage versus first physical-source-supported acquisition',
        dict(positive='intermittent and continuous pass validation and two forward synthetic final events',
             negatives='no correction accepted for split-gap, wrong-motion, AM-only or zero',
             unsupported='saved-map speed exactly zero outside measured support; no gap bridging'),
        synthetic=dict(generation=generation,count=count,sr=8000,duration_seconds=300,
            bounded_map=bounded_map,
            source_interval_seconds=[80,180],capture_hz=35,roles=events(),
            amplitude_reference='maximum median of fixed8s source windows ending <=120s, from first124s context only',
            selection='first source-supported opportunity in time, no scoring/window search',
            amplitude_control='existing research convention: normalize to motion SD, then bound control to +/-0.049 before mapping',
            reserved='two synthetic final events after acquisition; consumed once after map freeze',
            causality_limit='offline full-source map anchoring; forward event coordinates do not establish causal map-prefix behavior',
            real_audio_accessed=False))
    tick=time.perf_counter();rows=[]
    write(ROOT/'active_jobs.json',dict(jobs=[dict(pid=os.getpid(),iteration=iteration,status='running')],
        analysis_in_progress='Known-motion audio source-island versus whole-passage evidence test',promotion_withheld=True))
    for case in cases(generation,count):
        x=case['x'];sr=case['sr'];hz=case['hz']
        common=common_event_acquisition(events(),300.) if bounded_map else None
        a=common['analysis_start'] if common else 0.
        b=common['analysis_end'] if common else 124.
        acquisition_trace=trajectory(x[:round(b*sr)],sr,hz,35)
        source_trace=acquisition_trace[3][round(a*RATE):round(b*RATE)]
        reference,amplitude_meta=source_amplitude_reference(source_trace,rate=RATE,available_end=(b-a) if common else 120.)
        amplitude_meta['source_origin_seconds']=a
        tr=trajectory(x,sr,hz,35)
        curve,support=track_array(x,sr,hz,35,reference)
        if common:
            allowed=(np.arange(len(support))/RATE>=common['start_seconds'])&(np.arange(len(support))/RATE<common['end_seconds'])
            support=support*allowed
        amp=np.interp(np.arange(len(curve))/RATE,np.arange(len(tr[3]))/RATE,tr[3])
        proposals=opportunities(events(),support)
        row={k:v for k,v in case.items() if k!='x'}
        row.update(source_sha256=sha(x.tobytes()),amplitude_reference=reference,amplitude_metadata=amplitude_meta,
                   proposals=proposals,global_required_support=bool(np.all(support[10*RATE:round(260.25*RATE)]>=.95)))
        if common:
            p=dict(common,available_final_events_in_island=[e for e in events() if e['role']=='final' and common['start_seconds']<=e['start'] and e['end']<=common['end_seconds']])
            roles={r:[dict(e,start=e['start_seconds'],end=e['end_seconds']) for e in p['roles'][r]] for r in ('calibration','validation')}
            required=roles['calibration']+roles['validation']
            ready=bool(np.all(support[round(min(e['start'] for e in required)*RATE):round(max(e['end'] for e in required)*RATE)]>=.95))
        else:
            ready=bool(proposals['opportunities'])
            p=proposals['opportunities'][0] if ready else None
        if ready:
            row['acquisition_end_seconds']=p['end_seconds']
            roles={r:[dict(e,start=e['start_seconds'],end=e['end_seconds']) for e in p['roles'][r]]
                   for r in ('calibration','validation')}
            # Freeze interval/map before any response outcome, reuse exact full map for all crops.
            write(folder/f"case_{case['index']:03d}_frozen.json",dict(proposal=p,source_sha256=row['source_sha256']))
            try:
                row['validation']=evaluate(x,sr,curve,support,amp,roles['calibration'],roles['validation'])
                final=p['available_final_events_in_island']
                row['final_after_acquisition']=bool(final and all(e['start']>=p['end_seconds'] for e in final))
                if len(final)>=2:
                    row['synthetic_final']=evaluate(x,sr,curve,support,amp,roles['calibration'],final)
                if common:
                    outside=[e for e in events() if e['role']=='final' and e not in final]
                    row['outside_window_response']=evaluate_events(x,sr,curve,outside,sample_positions=make_map(curve,support,sr,len(x))[0])
            except ValueError as exc:
                row['rejected_invalid_map']=str(exc)
        rows.append(row);write(folder/'results.json',rows)
        print(iteration,case['index'],case['kind'],len(proposals['opportunities']),
              row.get('validation',{}).get('quality',{}).get('passed'),flush=True)
    summary={}
    for kind in sorted({r['kind'] for r in rows}):
        selected=[r for r in rows if r['kind']==kind]
        summary[kind]=dict(cases=len(selected),opportunities=sum(bool(r['proposals']['opportunities']) for r in selected),
            global_supported=sum(r['global_required_support'] for r in selected),
            validation_pass=sum(r.get('validation',{}).get('quality',{}).get('passed',False) for r in selected),
            final_pass=sum(r.get('synthetic_final',{}).get('quality',{}).get('passed',False) for r in selected),
            failures=dict(Counter(p for r in selected for p in r.get('validation',{}).get('quality',{}).get('failed_predicates',[]))))
    negatives=sum(v['validation_pass'] for k,v in summary.items() if k not in ('continuous','intermittent'))
    finish(folder,dict(summary=summary,negative_accepted=negatives,wall_seconds=time.perf_counter()-tick,
        peak_working_set_bytes=memory_peak(),real_correction_qualified=0),
        'retain_for_development' if negatives==0 else 'reject',
        f'Synthetic support-scope comparison completed; negative acceptances {negatives}; inspect all positive misses before real use.',
        'Inspect controlled cases and retain failures. Freeze any local real acquisition protocol on unused families before outcomes.')
    write(ROOT/'active_jobs.json',dict(jobs=[],analysis_in_progress='Assess completed synthetic source-island results',promotion_withheld=True))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--iteration',default='I015');p.add_argument('--generation',default='development-v1')
    p.add_argument('--count',type=int,default=24)
    p.add_argument('--bounded-map',action='store_true')
    a=p.parse_args();run(a.iteration,a.generation,a.count,a.bounded_map)
