"""Matched differential injection diagnosis. No target/reference fitted calibration."""
from nested_controls import *

OUT=ROOT/'reports/nested_amplitude_20261017'

def tracker(x,sr,win=320,frozen=None,uniform=False):
    f,t,z=signal.stft(x,sr,nperseg=win,noverlap=win-40,nfft=1024,boundary='zeros')
    power=abs(z)**2;med=np.median(power,axis=1)
    persistent=(med/(ndimage.median_filter(med,size=21)+1e-20)>8)&(np.mean(power>power.max(axis=0)[None,:]*.03,axis=1)>.35)
    persistent=ndimage.maximum_filter1d(persistent.astype(float),size=11)>0
    allowed=(f>300)&(f<3500)&~((f>800)&(f<1200))&~((f>2150)&(f<2850))&~persistent
    if frozen is not None:allowed=frozen
    q=np.linspace(np.log(300),np.log(3500),512);freq=np.exp(q);dq=q[1]-q[0]
    spec=np.array([np.interp(freq,f,np.log(power[:,i]+power[:,i].max()*1e-5+1e-20)) for i in range(len(t))])
    mask=np.interp(freq,f,allowed.astype(float))>.99;spec-=ndimage.gaussian_filter1d(spec,8,axis=1)
    mid=(spec[1:]+spec[:-1])/2;grad=np.gradient(mid,dq,axis=1);delta=np.diff(spec,axis=0)
    weight=mask[None,:]*(np.ones_like(mid) if uniform else np.minimum(np.exp(np.clip(mid,-10,2)),5))
    den=np.sum(weight*grad**2,axis=1);shift=-np.sum(weight*grad*delta,axis=1)/(den+1e-12)
    trace=np.r_[0,np.cumsum(np.clip(shift,-.03,.03))]
    e=signal.sosfiltfilt(signal.butter(3,[2,30],fs=200,btype='bandpass',output='sos'),trace)
    energy=np.sqrt(power[allowed].mean(axis=0));active=energy>max(np.percentile(energy,80)*.18,1e-7);n=len(x)//40
    return t[:n],e[:n],active[:n],allowed,dict(clip_fraction=float(np.mean(abs(shift)>.03)),active_fraction=float(active[:n].mean()),energy_dynamic_db=float(20*np.log10(np.percentile(energy,95)/max(np.percentile(energy,10),1e-15))),bins=int(allowed.sum()))

def coeff(e,t,hz):
    k=(t>3)&(t<t[-1]-3);u=t[k];a=np.column_stack([np.ones(len(u)),u-u.mean(),np.cos(2*np.pi*hz*u),np.sin(2*np.pi*hz*u)])
    c=np.linalg.lstsq(a,e[k],rcond=None)[0];return complex(c[2],-c[3])

def measure(e,truth,t,hz):
    a=coeff(e,t,hz);b=coeff(truth,t,hz);r=a/b if abs(b)>1e-12 else 0j
    return dict(real=float(a.real),imag=float(a.imag),gain=float(abs(r)),signed_gain=float(r.real),phase_deg=float(np.angle(r,deg=True)),rms=float(np.std(e[600:-600])))

def rigid_spectrum_control():
    t=np.arange(4800)/200;q=np.linspace(np.log(300),np.log(3500),512);dq=q[1]-q[0]
    base=sum(np.exp(-.5*((q-np.log(h*145))/.012)**2) for h in range(3,24));truth=.01*np.sin(2*np.pi*6.5*t+.4)
    spec=np.array([np.interp(q-v,q,base) for v in truth]);mid=(spec[1:]+spec[:-1])/2;g=np.gradient(mid,dq,axis=1)
    shifts=-np.sum(g*np.diff(spec,axis=0),axis=1)/(np.sum(g*g,axis=1)+1e-20);e=np.r_[0,np.cumsum(shifts)]
    return measure(e,truth,t,6.5)

def run():
    OUT.mkdir(exist_ok=True)
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());tape=next(x for x in inv if x['tape']=='A15_T510077');by={c['channel']:c for c in tape['channels']}
    channels=[12,13,14,17,18,23,26,29];sources=[];results=[];diagnostics=[];arrays={}
    for start in [19715.34,44595.55]:
        bases={};baseline={};act={};masks={};details={}
        for ch in channels:
            x,sr,m=get(by[ch],tape,start,24);sources.append(m);bases[ch]=x
            tt,e,a,mask,detail=tracker(x,sr);baseline[ch]=e;act[ch]=a;masks[ch]=mask;details[ch]=detail
            # Ensure the instrumented tracker reproduces the original estimator.
            assert np.max(abs(e-estimate(x,sr)[1]))<1e-10
        t=np.arange(len(x))/sr
        cases=[(d,f,.4,'warp') for d in [.0025,.01,.025] for f in [5.,6.5,8.]]+[(.01,6.5,1.9,'warp'),(.65,6.5,.4,'AM'),(0,6.5,.4,'identity')]
        for depth,hz,phase,kind in cases:
            error=depth*np.sin(2*np.pi*hz*t+phase) if kind=='warp' else np.zeros(len(t));tau=t+cumulative_trapezoid(error,t,initial=0)
            truth=np.interp(tt,t,np.log1p(error));curves=[];plain=[];activity=[];per=[]
            for ch in channels:
                y=np.interp(tau,t,bases[ch])
                if kind=='AM':y*=1+depth*np.sin(2*np.pi*hz*t+phase)
                _,e,a,mask,detail=tracker(y,sr)
                diff=e-np.interp(np.interp(tt,t,tau),tt,baseline[ch]);curves.append(diff);plain.append(e-baseline[ch]);activity.append(a)
                per.append(dict(channel=ch,**measure(diff,truth,tt,hz),mask_changed_bins=int(np.sum(mask!=masks[ch])),**detail))
            mean=np.mean(curves,axis=0);weighted=combine(curves,activity)[0]
            cs=np.array([coeff(c,tt,hz) for c in curves]);cancel=float(abs(cs.sum())/max(np.sum(abs(cs)),1e-20))
            row=dict(start=start,depth=depth,hz=hz,phase=phase,kind=kind,per_channel=per,cancellation_ratio=cancel,mean=measure(mean,truth,tt,hz),weighted=measure(weighted,truth,tt,hz),plain_difference=measure(np.mean(plain,axis=0),truth,tt,hz))
            results.append(row)
            if depth==.01 and hz==6.5 and phase==.4:
                arrays[f'{start}_{kind}_difference']=mean
            print('DIFFERENTIAL',start,kind,depth,hz,phase,'gain',round(row['mean']['gain'],3),'phase',round(row['mean']['phase_deg'],1),'cancel',round(cancel,3),flush=True)
        # Ablations all use the same known injection and matched variant baseline.
        err=.01*np.sin(2*np.pi*6.5*t+.4);tau=t+cumulative_trapezoid(err,t,initial=0);truth=np.interp(tt,t,np.log1p(err))
        for name,win,uniform,frozen in [('frozen_mask',320,False,True),('uniform_bins',320,True,True),('20ms',160,False,True),('60ms',480,False,True)]:
            per=[];curves=[];basecurves=[]
            for ch in channels:
                _,base,aa,mask,detail=tracker(bases[ch],sr,win=win,uniform=uniform)
                y=np.interp(tau,t,bases[ch]);_,e,a,_,_=tracker(y,sr,win=win,uniform=uniform,frozen=mask if frozen else None)
                diff=e-np.interp(np.interp(tt,t,tau),tt,base);curves.append(diff);basecurves.append(base);per.append(dict(channel=ch,**measure(diff,truth,tt,6.5)))
            diagnostics.append(dict(start=start,variant=name,mean=measure(np.mean(curves,axis=0),truth,tt,6.5),per_channel=per))
            arrays[f'{start}_{name}_baseline']=np.mean(basecurves,axis=0)
        # Baseline coefficients before aggregation; reference is read only now for evaluation.
        from cyclic_diagnose import trajectory
        refx,sr,m=get(by[30],tape,start,24);sources.append(m);ref=trajectory(refx,sr,1005.5)[1]
        baseline_stats=[]
        for ch in channels:baseline_stats.append(dict(channel=ch,**measure(baseline[ch],ref,tt,6.55),**details[ch]))
        cs=np.array([coeff(baseline[ch],tt,6.55) for ch in channels])
        diagnostics.append(dict(start=start,variant='baseline',per_channel=baseline_stats,cancellation_ratio=float(abs(cs.sum())/max(np.sum(abs(cs)),1e-20)),mean=measure(np.mean(list(baseline.values()),axis=0),ref,tt,6.55)))
        arrays[f'{start}_baseline']=np.mean(list(baseline.values()),axis=0);arrays[f'{start}_reference_evaluation_only']=ref
        (OUT/'results.json').write_text(json.dumps(dict(results=results,diagnostics=diagnostics,rigid_spectrum_control=rigid_spectrum_control()),indent=2),encoding='utf-8')
    np.savez_compressed(OUT/'curves.npz',time=tt,**arrays);(OUT/'sources.json').write_text(json.dumps(sources,indent=2))
    extra=[]
    for start in [19715.34,44595.55]:
        ref=signal.sosfiltfilt(signal.butter(3,[4,10],fs=200,btype='bandpass',output='sos'),arrays[f'{start}_reference_evaluation_only'])
        for variant in ['baseline','frozen_mask_baseline','uniform_bins_baseline','20ms_baseline','60ms_baseline']:
            e=signal.sosfiltfilt(signal.butter(3,[4,10],fs=200,btype='bandpass',output='sos'),arrays[f'{start}_{variant}'])
            extra.append(dict(start=start,variant=variant,**metrics(e,ref,tt)))
    (OUT/'results.json').write_text(json.dumps(dict(results=results,diagnostics=diagnostics,rigid_spectrum_control=rigid_spectrum_control(),baseline_broadband_evaluation=extra),indent=2),encoding='utf-8')
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)

if __name__=='__main__':run()
