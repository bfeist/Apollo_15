"""Frozen candidate validation on new fragments; no production changes."""
from survey import *
OUT=ROOT/'reports/irig_free_research_20261016/candidate_holdout'
PLAN=ROOT/'research/irig_free_20261016/holdout_plan.json'

def gate(x,sr,c,e,meta):
    f,t,p=signal.spectrogram(x,sr,nperseg=2*sr,noverlap=sr)
    band=abs(f-c['carrier_hz'])<12;side=(abs(f-c['carrier_hz'])>45)&(abs(f-c['carrier_hz'])<90)
    contrast=10*np.log10((p[band].max(axis=0)+1e-30)/(np.median(p[side],axis=0)+1e-30))
    core=e[400:-400];clip=float(np.mean(abs(core)>=.0399))
    checks=dict(occupancy=float(np.mean(contrast>10))>=.7,support=meta['tracking']['support_gate_mean']>=.8,
        motion=.1<=meta['tracking']['rms_percent']<=3,clipping=clip<=.01)
    sensitivity=[]
    for shift in [-5,5]:
        alt=dict(c,carrier_hz=c['carrier_hz']+shift);_,v=measure(x,sr,alt,c['channel']);v=v[400:-400]
        corr=float(np.corrcoef(core,v)[0,1]);ratio=float(np.std(v)/max(np.std(core),1e-15))
        sensitivity.append(dict(carrier_shift_hz=shift,correlation=corr,rms_ratio=ratio,passed=corr>=.9 and .8<=ratio<=1.25))
    return dict(contrast_db=float(np.median(contrast)),occupancy=float(np.mean(contrast>10)),clip_fraction=clip,
        signal_checks=checks,signal_pass=all(checks.values()),sensitivity=sensitivity,
        sensitivity_pass=all(s['passed'] for s in sensitivity),reference_only_pass=all(checks.values()) and all(s['passed'] for s in sensitivity))

def trial(tape,key,paths,start,c,label):
    duration=50;ref,sr,meta=read(paths[c['channel']],start,duration);info,e=measure(ref,sr,c,c['channel'])
    r=dict(tape=tape,group=key,start=start,label=label,reference=meta,configuration=c,tracking=info['tracking'],
        gates=gate(ref,sr,c,e,info),targets=[],results={},source_snapshots=[meta])
    # Save gates before reading target event outcomes.
    stem=f'{tape}_{key.rsplit("-",1)[1]}_{int(start)}_{label}'
    (OUT/(stem+'_prevalidation.json')).write_text(json.dumps(r,indent=2))
    for ch in c['targets']:
        if ch not in paths:continue
        x,fs,m=read(paths[ch],start,duration)
        if fs!=sr or len(x)!=len(ref):raise ValueError('rate/frame mismatch')
        qs=beeps(x,sr);r['targets'].append(dict(channel=ch,source=m,candidate_events=qs));r['source_snapshots'].append(m)
        result={'correct':evaluate(x,sr,e,qs)}
        for shift in [.071,.137,1.,5.]:result[f'shift_{shift}']=evaluate(x,sr,np.roll(e,round(shift*200)),qs)
        r['results'][str(ch)]=result
    (OUT/(stem+'.json')).write_text(json.dumps(r,indent=2))
    print(tape,key.rsplit('-',1)[1],start,label,'gate',r['gates']['reference_only_pass'],
        {ch:(len(v['correct']),round(float(np.median([q['reduction_percent'] for q in v['correct']])),1) if v['correct'] else None) for ch,v in r['results'].items()},flush=True)
    return r

def main():
    OUT.mkdir(parents=True,exist_ok=True);plan=json.loads(PLAN.read_text());allrows=[];selection=[]
    (OUT/'frozen_plan.json').write_text(json.dumps(plan,indent=2))
    for tape,c in plan['candidates'].items():
        groups,rejected=inventory(Path('D:/Apollo_15_30-Track/orig_delivery')/tape,[c['channel']]+c['targets']+[12,14])
        choices=[]
        for idx in [len(groups)//4,3*len(groups)//4,len(groups)-1]:
            key,paths=groups[idx]
            if key.endswith('-17') or key in [x[0] for x in choices]:continue
            choices.append((key,paths))
        selection.append(dict(tape=tape,groups=[dict(key=k,paths={str(ch):str(p) for ch,p in ps.items()}) for k,ps in choices],rejected=rejected))
        (OUT/'selected_groups.json').write_text(json.dumps(selection,indent=2))
        for key,paths in choices:
            for start in plan['starts_seconds']:
                try:allrows.append(trial(tape,key,paths,start,c,'new_fragment'))
                except (ValueError,RuntimeError) as ex:allrows.append(dict(tape=tape,group=key,start=start,error=str(ex)))
                (OUT/'results.json').write_text(json.dumps(allrows,indent=2))
    # The same frozen gates apply to known positive/negative controls, without retuning.
    base=json.loads((ROOT/'reports/irig_free_research_20261016/summary.json').read_text())
    for tape,start,hz,ch,target,label in [('A15_T510088',80,631.5,14,12,'known_success'),('A15_T510092',200,631.5,14,15,'known_failure'),('A15_T510106',1550,631.5,14,15,'known_failure'),('A15_T510073',650,404.,17,12,'candidate_discovery'),('A15_T510106',1550,406.,12,15,'candidate_discovery')]:
        b=next(r for r in base if r['tape']==tape);paths={m['channel']:Path(m['path']) for m in b['sources']}
        allrows.append(trial(tape,b['group'],paths,start,dict(channel=ch,carrier_hz=hz,targets=[target]),label))
        (OUT/'results.json').write_text(json.dumps(allrows,indent=2))

if __name__=='__main__':main()
