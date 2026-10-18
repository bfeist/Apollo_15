"""Frozen broad canonical study, isolated from production and approved artifacts.

Three stratified discovery windows on every channel of the frozen completed-tape
snapshot. Reference-only selection, then prespecified held-out event searches.
"""
import os
for key in ['OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS']:os.environ[key]='1'
from canonical_bespoke import *
import canonical_bespoke as canonical
from cyclic_diagnose import trajectory,bound,fm_measure
from audio_processing import detect,remove,denoise
from scipy.integrate import cumulative_trapezoid

DEST=ROOT/'reports/bespoke_expanded_20261017'
TAPES=[73,75,76,77,79,80,81,82,83,85,86,87,90,91,92,93,94]
DISCOVERY=[.10,.45,.80]
HOLDOUT=[.22,.34,.58,.68,.90,.96]

def setup():
    DEST.mkdir(exist_ok=True)
    plan=dict(tapes=[f'A15_T510{n:03}' for n in TAPES],discovery_fractions=DISCOVERY,holdout_fractions=HOLDOUT,duration=30,discovery_channels='all available completed canonical channels',holdout_targets=[12,15,20],holdout_stop='first two windows with non-reference candidate beeps, otherwise all six',selection='Reference-only discovery ranking. No holdout outcomes choose channel or parameters.',scope='Frozen initial completed-marker snapshot; later arrivals excluded from this pass.',excluded_snapshot={'T510078':'31 WAVs/no marker/ambiguous channel','T510084':'29 WAVs/no marker','T510088':'no marker','T510089':'no marker','T510095':'partial/in progress'})
    p=DEST/'plan.json'
    if not p.exists():p.write_text(json.dumps(plan,indent=2))
    else:assert json.loads(p.read_text())==plan

def make_inventory():
    prior=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());old={t['tape']:t for t in prior};chosen=[]
    for number in TAPES:
        name=f'A15_T510{number:03}';folder=Path('D:/Apollo_15_30-Track/orig_concatenated')/name;files=sorted(folder.glob('*.wav'));markers=list(folder.glob('.timerange-*'))
        row=dict(tape=name,status='deferred',channels=[],reasons=[],verification='Completed marker, mono WAV headers/physical end and stable size/mtime; not a fresh delivery-fragment audit')
        if len(files)!=30 or len(markers)!=1 or list(folder.glob('*.partial')):
            row['reasons'].append('requires 30 WAVs, one completion marker and no partial');chosen.append(row);continue
        for p in files:
            try:
                b=stable(p);stamp=STAMP.search(p.name);ch=int(CHANNEL.search(p.name)[1])
                row['channels'].append(dict(channel=ch,path=str(p),head=re.search(r'_(HR\d+[UL])_',p.name)[1],bytes=b[0],mtime_ns=b[1],sample_rate=b[2],frames=b[3],duration=b[3]/b[2],absolute_start=seconds(stamp[1]),filename_end=seconds(stamp[2])))
            except Exception as ex:row['reasons'].append(dict(path=str(p),reason=str(ex)))
        if len(row['channels'])==30 and not row['reasons']:
            row['status']='eligible_canonical_snapshot';row['origin']=min(c['absolute_start'] for c in row['channels']);row['duration']=min(c['absolute_start']+c['duration'] for c in row['channels'])-row['origin']
            previous=old.get(name)
            if previous and previous['status']=='verified_complete_against_local_delivery' and all(any(c['path']==o['path'] and c['bytes']==o['bytes'] and c['mtime_ns']==o['mtime_ns'] for o in previous['channels']) for c in row['channels']):row['verification']+='; additionally matches earlier delivery-fragment-verified inventory'
        chosen.append(row);print('FROZEN INVENTORY',name,row['status'],len(row['channels']),flush=True)
    (DEST/'frozen_inventory.json').write_text(json.dumps(chosen,indent=2))
    return chosen

def cleaned(x,sr):
    y=x.copy();passes=[]
    for _ in range(6):
        lines=detect(y,sr)
        if not lines:break
        y=remove(y,sr,lines);passes.append(lines)
    y,profile=denoise(y,sr,reduction_cap_db=6)
    return y,dict(passes=passes,noise_profile=profile,reduction_cap_db=6)

def reference(x,sr,hz,mode='broad'):
    capture=max(12,min(140,hz*(.085 if mode=='broad' else .025)))
    tr=trajectory(x,sr,hz,capture_override=capture);tt,e,disp,amp,rate,meta=tr
    # Search 2-20 Hz; never impose the benchmark's 6.5 Hz rate.
    ff,p=signal.welch(e[400:-400],200,nperseg=min(4000,len(e)-800));sel=(ff>=2)&(ff<=20);fm=float(ff[sel][np.argmax(p[sel])]);peak=(abs(ff-fm)<.5)
    concentration=float(p[peak].sum()/max(p[sel].sum(),1e-20))
    speed=bound(signal.sosfiltfilt(signal.butter(3,[2,30],fs=200,btype='bandpass',output='sos'),e))
    meta.update(rate_hz=fm,concentration=concentration,capture=capture,speed_rms=float(np.std(speed[400:-400])),mode=mode)
    return speed,meta

def survey(tape):
    folder=DEST/tape['tape'];folder.mkdir(exist_ok=True);p=folder/'survey.json'
    if p.exists():return json.loads(p.read_text())
    windows=[];ranked=[]
    for fraction in DISCOVERY:
        start=round(fraction*(tape['duration']-30),2);sources=[];rows=[];events=[];curves={};content=[]
        for c in tape['channels']:
            x,sr,m=get(c,tape,start,30);sources.append(m);ch=c['channel']
            envelope=np.sqrt(np.mean(x.reshape(-1,sr//10)**2,axis=1));dynamic=float(20*np.log10(np.percentile(envelope,95)/max(np.percentile(envelope,10),1e-12)))
            content.append(dict(channel=ch,head=c['head'],rms=float(np.std(x)),dynamic_db=dynamic,label='speech/activity candidate' if dynamic>12 else 'low-variation/background candidate',classification='automatic proxy, not listening-certified'))
            if ch!=1:
                events += [dict(b,channel=ch) for b in beeps(x,sr)]
            for cand in discover(x[:15*sr],sr,3):
                hz=cand['carrier_hz']
                if hz<80 or 2150<hz<2850 or ch==1:continue
                try:
                    e,meta=reference(x,sr,hz);k=f'{ch}_{hz}';curves[k]=e
                    score=meta['support']*meta['concentration']*min(meta['speed_rms']/.004,1)
                    if not .0002<meta['speed_rms']<.035:score*=.05
                    # Crowded low carriers are not forbidden, but penalized until validated.
                    if hz<300:score*=.4
                    record=dict(meta);record.update(channel=ch,carrier_hz=hz,key=k,score=float(score));rows.append(record)
                except (ValueError,FloatingPointError):pass
        for r in rows:
            peers=[(float(np.corrcoef(curves[r['key']][400:-400],curves[s['key']][400:-400])[0,1]),s['key']) for s in rows if s['channel']!=r['channel'] and abs(s['carrier_hz']-r['carrier_hz'])>80 and not (r['carrier_hz']<300 and s['carrier_hz']<300)]
            r['best_peer']=max(peers) if peers else None
            if peers:r['score']*=.5+max(0,r['best_peer'][0])
        rows.sort(key=lambda r:r['score'],reverse=True);ranked+= [dict(r,discovery_start=start) for r in rows[:8]]
        windows.append(dict(start=start,sources=sources,candidates=rows,events=events,content=content))
        np.savez_compressed(folder/f'discovery_{start:.2f}.npz',**curves)
        (folder/'survey_progress.json').write_text(json.dumps(dict(windows=windows),indent=2))
        print('DISCOVERY',tape['tape'],start,'channels',len(sources),'events',len(events),'top',[(r['channel'],round(r['carrier_hz'],1),round(r['rate_hz'],2)) for r in rows[:3]],flush=True)
    ranked.sort(key=lambda r:r['score'],reverse=True);chosen=[]
    for r in ranked:
        if any(r['channel']==s['channel'] for s in chosen):continue
        chosen.append(r)
        if len(chosen)==2:break
    result=dict(tape=tape['tape'],windows=windows,frozen_references=chosen)
    p.write_text(json.dumps(result,indent=2));return result

def validate(tape,survey):
    folder=DEST/tape['tape'];p=folder/'validation.json'
    if p.exists():return
    by={c['channel']:c for c in tape['channels']};refs=survey['frozen_references'];refchs={r['channel'] for r in refs};targets=[ch for ch in [12,15,20] if ch in by and ch not in refchs];windows=[];search=[]
    for frac in HOLDOUT:
        start=round(frac*(tape['duration']-30),2);data={};sources=[];events=[]
        for ch in targets:
            x,sr,m=get(by[ch],tape,start,30);data[ch]=x;sources.append(m);events += [dict(b,channel=ch) for b in beeps(x,sr)]
        search.append(dict(start=start,sources=sources,events=events))
        if not events:continue
        maps={};refmeta=[]
        for r in refs:
            ch=r['channel'];x,sr,m=get(by[ch],tape,start,30);sources.append(m)
            for mode in ['broad','narrow']:
                e,meta=reference(x,sr,r['carrier_hz'],mode);key=f"ch{ch}_{mode}";maps[key]=e;record=dict(meta);record.update(key=key,channel=ch,carrier_hz=r['carrier_hz']);refmeta.append(record)
        broad=[maps[k] for k in maps if k.endswith('_broad')]
        if len(broad)==2:
            corr=float(np.corrcoef(broad[0][400:-400],broad[1][400:-400])[0,1])
            if corr>.6:maps['reference_consensus']=np.mean(broad,axis=0)
        outcomes=[];cleanup={}
        for ch in targets:
            ev=[b for b in events if b['channel']==ch]
            if not ev:continue
            clean,cm=cleaned(data[ch],sr);cleanup[ch]=cm
            for key,e in maps.items():
                fm=next((r['rate_hz'] for r in refmeta if r['key']==key),float(np.median([r['rate_hz'] for r in refmeta])))
                metrics=fm_measure(clean,sr,e,ev,fm)
                controls={name:fm_measure(clean,sr,curve,ev,fm) for name,curve in [('wrong_sign',-e),('shift_1s',np.roll(e,200))]}
                outcomes.append(dict(channel=ch,method=key,events=metrics,controls=controls))
        windows.append(dict(start=start,sources=sources,references=refmeta,cleanup=cleanup,outcomes=outcomes,events=events))
        np.savez_compressed(folder/f'holdout_{start:.2f}.npz',**maps)
        print('HOLDOUT',tape['tape'],start,'events',len(events),'methods',list(maps),flush=True)
        if len(windows)>=2:break
    p.write_text(json.dumps(dict(tape=tape['tape'],frozen_references=refs,search=search,windows=windows),indent=2))

def main():
    setup();inv=json.loads((DEST/'frozen_inventory.json').read_text()) if (DEST/'frozen_inventory.json').exists() else make_inventory()
    for t in inv:
        if t['status']!='eligible_canonical_snapshot':print('DEFER',t['tape'],t['reasons'],flush=True);continue
        s=survey(t);validate(t,s)

if __name__=='__main__':main()
