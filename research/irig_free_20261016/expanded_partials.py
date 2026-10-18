"""Separate tone-free phase-vocoder partial trajectories and multichannel consensus."""
from bespoke_expanded import *
from nested_tonefree import speech,metrics,combine

def partial_motion(x,sr):
    f,t,z=signal.stft(x,sr,nperseg=320,noverlap=280,nfft=1024,boundary='zeros');a=abs(z)
    expected=2*np.pi*f*40/sr;inst=f[:,None]+np.angle(z[:,1:]*np.conj(z[:,:-1])*np.exp(-1j*expected[:,None]))/(2*np.pi*.005)
    allowed=(f>300)&(f<3500)&~((f>800)&(f<1200))&~((f>2150)&(f<2850))
    med=np.median(a,axis=1);persistent=(med>4*ndimage.median_filter(med,size=21))&(np.mean(a>a.max(axis=0)[None,:]*.15,axis=1)>.4)
    allowed &= ~(ndimage.maximum_filter1d(persistent.astype(float),size=11)>0)
    tracks=[];live=[]
    for j in range(1,len(t)):
        peaks,_=signal.find_peaks(a[:,j],distance=4);peaks=[k for k in peaks if allowed[k] and a[k,j]>.12*a[:,j].max() and abs(inst[k,j-1]-f[k])<25]
        observations=[(float(inst[k,j-1]),float(a[k,j])) for k in peaks];used=set();new=[]
        for tr in live:
            candidates=[(abs(hz-tr[-1][1]),i) for i,(hz,amp) in enumerate(observations) if i not in used]
            distance,i=min(candidates,default=(1e9,-1))
            if distance<max(15,tr[-1][1]*.035):
                hz,amp=observations[i];tr.append((j,hz,amp));used.add(i);new.append(tr)
            else:tracks.append(tr)
        new += [[(j,hz,amp)] for i,(hz,amp) in enumerate(observations) if i not in used];live=new
    tracks+=live;n=len(x)//40;contributions=[[] for _ in range(n)];kept=[]
    for tr in tracks:
        # Long uninterrupted lines are deliberately not used as speech evidence.
        if not 80<=len(tr)<=400:continue
        ids=np.array([v[0] for v in tr]);hz=np.array([v[1] for v in tr]);good=ids<n;ids=ids[good];hz=hz[good]
        if len(ids)<80:continue
        u=(ids-ids.mean())/200;logs=np.log(np.maximum(hz,1));basis=np.column_stack([np.ones(len(u)),u,u*u]);resid=logs-basis@np.linalg.lstsq(basis,logs,rcond=None)[0]
        if not .001<np.std(resid)<.06:continue
        edge=np.minimum(np.arange(len(ids)),np.arange(len(ids))[::-1]);valid=edge>=8
        for i,v in zip(ids[valid],resid[valid]):contributions[i].append(float(v))
        kept.append(dict(start=float(ids[0]/200),end=float(ids[-1]/200),median_hz=float(np.median(hz)),residual_rms=float(np.std(resid))))
    support=np.array([len(v)>=3 for v in contributions]);e=np.array([np.median(v) if len(v)>=3 else 0 for v in contributions]);e=signal.sosfiltfilt(signal.butter(3,[2,20],fs=200,btype='bandpass',output='sos'),e)
    return bound(e),support,dict(partials=len(kept),support_fraction=float(support.mean()),tracks=kept)

def consensus(curves,support):
    if not curves:return None,None
    e=combine(curves,support,band=(2,20))[0];odd=np.mean(curves[::2],axis=0);even=np.mean(curves[1::2],axis=0) if len(curves)>1 else np.zeros(len(e));corr=float(np.corrcoef(odd[400:-400],even[400:-400])[0,1]) if min(np.std(odd),np.std(even))>1e-10 else 0.
    f,p=signal.welch(e[400:-400],200,nperseg=min(4000,len(e)-800));sel=(f>=2)&(f<=20);fm=float(f[sel][np.argmax(p[sel])]);con=float(p[abs(f-fm)<.5].sum()/max(p[sel].sum(),1e-20));coverage=float(np.mean(np.sum(support,axis=0)>=2));qualified=bool(corr>.5 and con>.2 and coverage>.2)
    return bound(e),dict(rate_hz=fm,odd_even_correlation=corr,concentration=con,multichannel_support_fraction=coverage,qualified=qualified,note='Internal gate only; no calibrated probability or listening approval')

def synthetic():
    out=[];sr=8000;t=np.arange(24*sr)/sr
    for kind,rate in [('warp',3.),('warp',6.5),('warp',11.),('drift',6.5),('AM',6.5),('independent_F0',6.5),('unwarped',6.5)]:
        phase=2*np.pi*(rate*t if kind!='drift' else 5*t+.06*t*t);truth=.025*np.sin(phase+.4) if kind in ['warp','drift'] else np.zeros(len(t));tau=t+cumulative_trapezoid(truth,t,initial=0);curves=[];supports=[]
        for i in range(4):
            x=speech(i+1,natural_vibrato=kind=='independent_F0');y=np.interp(tau,t,x)
            if kind=='AM':y*=1+.65*np.sin(phase)
            e,a,m=partial_motion(y,sr);curves.append(e);supports.append(a)
        e,gate=consensus(curves,supports);tt=np.arange(len(e))/200;tr=np.interp(tt,t,truth);row=dict(kind=kind,injected_rate=rate,**gate,**metrics(e,tr,tt));out.append(row)
        print('PARTIAL SYNTHETIC',kind,rate,'gain',row['signed_gain'],'qualified',gate['qualified'],flush=True)
    (DEST/'partial_synthetic.json').write_text(json.dumps(out,indent=2))

def real(tape,survey):
    folder=DEST/tape['tape'];path=folder/'partial_analysis.json'
    if path.exists():return
    by={c['channel']:c for c in tape['channels']};refchs={r['channel'] for r in survey['frozen_references']};rows=[]
    for window in [survey['windows'][0],survey['windows'][-1]]:
        start=window['start'];available=[r for r in window['content'] if r['channel'] not in refchs|{1,12,15,20}];available.sort(key=lambda r:r['dynamic_db'],reverse=True);chosen=[r['channel'] for r in available[:3]];noise=available[-1]['channel'];curves=[];support=[];details=[];sources=[]
        for ch in chosen+[noise]:
            x,sr,m=get(by[ch],tape,start,30);sources.append(m);clean,cm=cleaned(x,sr);e,a,meta=partial_motion(clean,sr);raw,ra,rm=partial_motion(x,sr)
            details.append(dict(channel=ch,role='background proxy' if ch==noise else 'activity proxy',cleanup=cm,raw_support=rm['support_fraction'],clean=meta))
            if ch!=noise:curves.append(e);support.append(a)
        e,gate=consensus(curves,support);targets=[]
        for ch in [12,15,20]:
            ev=[b for b in window['events'] if b['channel']==ch]
            if not ev or ch in refchs:continue
            x,sr,m=get(by[ch],tape,start,30);sources.append(m);clean,cm=cleaned(x,sr)
            targets.append(dict(channel=ch,cleanup=cm,diagnostic_events=fm_measure(clean,sr,e,ev,gate['rate_hz']),wrong_sign=fm_measure(clean,sr,-e,ev,gate['rate_hz'])))
        rows.append(dict(start=start,sources=sources,selected_channels=chosen,background_proxy=noise,details=details,consensus=gate,targets=targets,estimation='No carrier trajectory used. Target tones held out; candidate traces may still contain unidentified interference.'))
        np.savez_compressed(folder/f'partial_{start:.2f}.npz',curve=e,support=np.array(support))
        print('PARTIAL REAL',tape['tape'],start,'qualified',gate['qualified'],'rate',round(gate['rate_hz'],2),flush=True)
    path.write_text(json.dumps(rows,indent=2))

if __name__=='__main__':
    setup();synthetic()
    inv=json.loads((DEST/'frozen_inventory.json').read_text())
    for t in inv:
        p=DEST/t['tape']/'survey.json'
        if t['status']=='eligible_canonical_snapshot' and p.exists():real(t,json.loads(p.read_text()))
