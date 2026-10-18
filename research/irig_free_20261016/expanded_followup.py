"""Per-tape alternatives selected on calibration, then fresh confirmation only."""
from bespoke_expanded import *
from flutter import estimate as legacy_estimate

def stats(events):
    reductions=[];after=[];before=[]
    for e in events:
        m=e['margins']['0.04'];b=m['before']['rms_hz'];a=m['after']['rms_hz']
        reductions.append(100*(1-a/max(b,1e-12)));before.append(b);after.append(a)
    return dict(event_count=len(events),median_reduction=float(np.median(reductions)) if reductions else None,worst_reduction=float(min(reductions)) if reductions else None,median_before_hz=float(np.median(before)) if before else None,median_after_hz=float(np.median(after)) if after else None)

def map_for(x,sr,r,mode):
    if mode=='legacy_local':
        tt,e,m,f=legacy_estimate(x,sr,carrier_hz=r['carrier_hz'],hum_hz=());return e,dict(rate_hz=m['modulation_hz'],mode=mode)
    return reference(x,sr,r['carrier_hz'],mode)

def run(tape):
    folder=DEST/tape['tape'];out=folder/'followup.json'
    if out.exists():return
    survey=json.loads((folder/'survey.json').read_text());validation=json.loads((folder/'validation.json').read_text());by={c['channel']:c for c in tape['channels']}
    ranked=sorted([r for w in survey['windows'] for r in w['candidates']],key=lambda r:r['score'],reverse=True);refs=[]
    for r in ranked:
        if any(abs(r['carrier_hz']-s['carrier_hz'])<80 for s in refs):continue
        refs.append(dict(channel=r['channel'],carrier_hz=r['carrier_hz']))
        if len(refs)>=3:break
    low=next((r for r in ranked if 100<r['carrier_hz']<180),None)
    if low and not any(abs(low['carrier_hz']-s['carrier_hz'])<50 for s in refs):refs.append(dict(channel=low['channel'],carrier_hz=low['carrier_hz']))
    if not refs:out.write_text(json.dumps(dict(status='no_reference_candidates')));return
    # Pick the first event-bearing window, without reference to correction outcomes.
    windows=validation['windows'] or [w for w in survey['windows'] if w['events']]
    cal=None
    for w in windows:
        ev=[b for b in w['events'] if b['channel'] not in {r['channel'] for r in refs} and b['start']>2 and b['end']<28]
        if ev:cal=(w,ev);break
    if cal is None:out.write_text(json.dumps(dict(status='no_calibration_events',references=refs)));return
    w,events=cal;start=w['start'];target=max({b['channel'] for b in events},key=lambda ch:sum(b['channel']==ch for b in events));ev=[b for b in events if b['channel']==target]
    x,sr,m=get(by[target],tape,start,30);sources=[m];clean,cm=cleaned(x,sr);trials=[]
    for r in refs:
        refx,sr,m=get(by[r['channel']],tape,start,30);sources.append(m)
        for mode in ['broad','narrow','legacy_local']:
            try:
                e,meta=map_for(refx,sr,r,mode);measured=fm_measure(clean,sr,e,ev,meta['rate_hz']);trials.append(dict(reference=r,mode=mode,events=measured,**stats(measured)))
            except ValueError as ex:trials.append(dict(reference=r,mode=mode,error=str(ex)))
    valid=[r for r in trials if 'error' not in r];best=max(valid,key=lambda r:r['median_reduction']) if valid else None
    plan=dict(calibration_start=start,calibration_target=target,calibration_sources=sources,calibration_cleanup=cm,trials=trials,selected=best,confirmation_offsets_seconds=[180,480,900,1800],status='calibration_failed' if not best or best['median_reduction']<20 or best['worst_reduction']<-10 else 'awaiting_fresh_confirmation')
    (folder/'followup_plan.json').write_text(json.dumps(plan,indent=2))
    if plan['status']=='calibration_failed':out.write_text(json.dumps(plan,indent=2));print('ALTERNATIVES',tape['tape'],'failed calibration',None if not best else round(best['median_reduction'],1),flush=True);return
    ref=best['reference'];targets=list(dict.fromkeys([target]+[ch for ch in [12,15,20] if ch!=ref['channel']]))[:4];confirmed=[];search=[]
    prior=[ww['start'] for ww in survey['windows']]+[ww['start'] for ww in validation['search']]
    for offset in plan['confirmation_offsets_seconds']:
        st=round(start+offset,2)
        if st+30>tape['duration'] or any(abs(st-v)<35 for v in prior):continue
        sources=[];data={};ee=[]
        for ch in targets:
            xx,sr,m=get(by[ch],tape,st,30);sources.append(m);data[ch]=xx;ee += [dict(b,channel=ch) for b in beeps(xx,sr) if b['start']>2 and b['end']<28]
        search.append(dict(start=st,sources=sources,events=ee))
        if not ee:continue
        refx,sr,m=get(by[ref['channel']],tape,st,30);sources.append(m);e,meta=map_for(refx,sr,ref,best['mode']);outcomes=[]
        for ch in targets:
            ev=[b for b in ee if b['channel']==ch]
            if not ev:continue
            clean,cm=cleaned(data[ch],sr);measured=fm_measure(clean,sr,e,ev,meta['rate_hz']);controls={k:fm_measure(clean,sr,v,ev,meta['rate_hz']) for k,v in [('wrong_sign',-e),('shift_1s',np.roll(e,200))]}
            outcomes.append(dict(channel=ch,cleanup=cm,events=measured,controls=controls,**stats(measured)))
        confirmed.append(dict(start=st,sources=sources,reference=ref,mode=best['mode'],tracking=meta,outcomes=outcomes));np.savez_compressed(folder/f'fresh_{st:.2f}.npz',curve=e)
        print('FRESH',tape['tape'],st,[(o['channel'],round(o['median_reduction'],1)) for o in outcomes],flush=True)
        if len(confirmed)>=2:break
    plan.update(confirmation_search=search,confirmation=confirmed,status='fresh_results_available' if confirmed else 'no_fresh_events');out.write_text(json.dumps(plan,indent=2))

if __name__=='__main__':
    inv=json.loads((DEST/'frozen_inventory.json').read_text())
    for t in inv:
        if (DEST/t['tape']/'validation.json').exists():run(t)
