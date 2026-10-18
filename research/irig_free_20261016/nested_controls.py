"""Additional independent-content tests for nested_tonefree, including real speech inputs."""
from nested_tonefree import *
from canonical_bespoke import sf

def coherent(curves,active,band=(4,10)):
    zs=[]
    for e,a in zip(curves,active):
        f,t,z=signal.stft(e*a,FS,nperseg=800,noverlap=700,boundary='zeros');zs.append(z)
    zs=np.array(zs);n=len(zs);mean=zs.mean(axis=0)
    coh=abs(zs.sum(axis=0))**2/(n*np.sum(abs(zs)**2,axis=0)+1e-20)
    # Fixed threshold, never selected on real held-out reference outcomes.
    gate=(coh>.45)&((f>=band[0])&(f<=band[1]))[:,None]
    _,e=signal.istft(mean*gate,FS,nperseg=800,noverlap=700,boundary=True)
    return e[:len(curves[0])],dict(coherent_fraction=float(np.mean(gate[(f>=band[0])&(f<=band[1])]))),coh

def cleaned_excerpt(x,sr):
    f,t,z=signal.stft(x,sr,nperseg=2048,noverlap=1536)
    power=abs(z)**2;med=np.median(power,axis=1)
    persistent=(med>8*ndimage.median_filter(med,size=41))&(np.mean(power>power.max(axis=0)[None,:]*.02,axis=1)>.25)
    persistent=ndimage.maximum_filter1d(persistent.astype(float),size=31)>0
    allowed=(f>300)&(f<3500)&~((f>800)&(f<1200))&~((f>2150)&(f<2850))&~persistent
    z[~allowed]=0
    _,y=signal.istft(z,sr,nperseg=2048,noverlap=1536)
    return y[:len(x)],int(persistent.sum())

def run(clean=True):
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());tape=next(x for x in inv if x['tape']=='A15_T510077');by={c['channel']:c for c in tape['channels']}
    bases=[];sources=[]
    # Different non-simultaneous channel excerpts reduce pre-existing common flutter/content.
    for i,ch in enumerate([12,13,14,15,18,20,27,30]):
        x,sr,m=get(by[ch],tape,19715.34+137*i,24)
        y,n=cleaned_excerpt(x,sr) if clean else (x,0)
        bases.append(y);sources.append(dict(m,masked_bins=n))
    t=np.arange(len(bases[0]))/sr;rows=[];arrays={}
    for kind in ['common_6.5','drift_harmonics','AM_only','unwarped','independent_pitch']:
        phase=2*np.pi*(6*t+.025*t*t if kind=='drift_harmonics' else 6.5*t)
        truth=.025*np.sin(phase+.4)
        if kind=='drift_harmonics':truth+=.007*np.sin(2*phase+.9)
        if kind in ['AM_only','unwarped','independent_pitch']:truth*=0
        curves=[];active=[]
        for i,x in enumerate(bases):
            err=truth if kind!='independent_pitch' else .025*np.sin(2*np.pi*(5.4+.31*i)*t+i)
            tau=t+cumulative_trapezoid(err,t,initial=0);y=np.interp(tau,t,x)
            if kind=='AM_only':y*=1+.65*np.sin(2*np.pi*6.5*t+.4)
            tt,e,a,m=estimate(y,sr);curves.append(e);active.append(a)
        for name in ['average','coherent']:
            for band in [(4,10),(2,30)]:
                if name=='average':e,*_=combine(curves,active,band);extra={}
                else:e,extra,coh=coherent(curves,active,band)
                tr=np.interp(tt,t,truth);row=dict(kind=kind,method=name,band=list(band),**metrics(e,tr,tt),**extra)
                # A separate ninth signal is a held-out ground check, never an estimator input.
                tau=t+cumulative_trapezoid(truth,t,initial=0)
                from cyclic_diagnose import fm_measure
                held=np.sin(2*np.pi*2500*tau)
                row['heldout_tone']=fm_measure(held,sr,e,[dict(start=8,end=8.25),dict(start=16,end=16.25)],6.5)
                rows.append(row);arrays[kind+'_'+name+'_'+str(band[0])]=e
        print('ARCHIVE INJECTION',kind,[(r['method'],r['band'],r['signed_gain'],r['speed_rmse']) for r in rows[-4:]],flush=True)
    stem='archive_injection' if clean else 'archive_raw_injection'
    (DEST/(stem+'.json')).write_text(json.dumps(dict(precleaned=clean,sources=sources,results=rows),indent=2));np.savez_compressed(DEST/(stem+'_curves.npz'),time=tt,**arrays)
    if not clean:
        assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)
        return
    # Coherence-gated re-analysis of real benchmarks, without using reference in estimation.
    real=json.loads((DEST/'real.json').read_text());out=[]
    for window in real:
        d=np.load(DEST/f"{window['start']}_curves.npz");channels=window['selected'];curves=[d[f'ch{ch}'] for ch in channels]
        for band in [(4,10),(2,30)]:
            e,extra,coh=coherent(curves,[np.ones(len(c)) for c in curves],band)
            tt=d['time'];ref=signal.sosfiltfilt(signal.butter(3,band,fs=FS,btype='bandpass',output='sos'),d['reference_evaluation_only'])
            x,sr,m=get(by[15],tape,window['start'],60)
            from cyclic_diagnose import beeps
            out.append(dict(start=window['start'],band=list(band),**metrics(e,ref,tt),**extra,heldout_beeps=fm_measure(x,sr,e,beeps(x,sr))))
            np.savez_compressed(DEST/f"{window['start']}_coherent_{band[0]}.npz",time=tt,estimate=e,coherence=coh)
        print('REAL COHERENT',window['start'],[(r['band'],r['correlation'],r['signed_gain']) for r in out[-2:]],flush=True)
    (DEST/'real_coherent.json').write_text(json.dumps(out,indent=2))
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)

if __name__=='__main__':run()
