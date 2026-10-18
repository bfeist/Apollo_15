"""Tone-excluded spectral-motion -> complex modulation-STFT experiments.

Read-only canonical inputs; outputs confined to a new research report directory.
No carrier phase tracker is used in estimation. Carrier tracking is evaluation only.
"""
from canonical_bespoke import get, ROOT, np, signal, ndimage, json
from pathlib import Path
from scipy.integrate import cumulative_trapezoid

DEST = ROOT/'reports/nested_tonefree_20261017'
FS = 200

def estimate(x, sr, strict=False):
    # 40 ms Hann analysis resolves speech harmonics while sampling modulation at 200 Hz.
    f,t,z=signal.stft(x,sr,nperseg=320,noverlap=280,nfft=1024,boundary='zeros')
    power=abs(z)**2
    # Fixed exclusions include both reference and Quindar bands, with FM sideband margins.
    allowed=(f>300)&(f<3500)&~((f>800)&(f<1200))&~((f>2150)&(f<2850))
    med=np.median(power,axis=1)
    contrast=med/(ndimage.median_filter(med,size=21)+1e-20)
    persistent=(contrast>8)&(np.mean(power>np.max(power,axis=0)[None,:]*.03,axis=1)>.35)
    persistent=ndimage.maximum_filter1d(persistent.astype(float),size=11)>0
    allowed &= ~persistent
    if strict:
        # Leakage stress test: discard neighborhoods of both common mains families.
        allowed &= (abs(f-60*np.round(f/60))>12)&(abs(f-50*np.round(f/50))>8)
    # Log-frequency coordinates turn a common fractional frequency shift into translation.
    q=np.linspace(np.log(300),np.log(3500),512);freq=np.exp(q);dq=q[1]-q[0]
    spec=np.array([np.interp(freq,f,np.log(power[:,i]+np.max(power[:,i])*1e-5+1e-20)) for i in range(len(t))])
    mask=np.interp(freq,f,allowed.astype(float))>.99
    spec-=ndimage.gaussian_filter1d(spec,8,axis=1)
    # Linearized phase-aware spectral registration: dS/dt = -v*dS/d(log f).
    mid=(spec[1:]+spec[:-1])/2
    gradient=np.gradient(mid,dq,axis=1)
    delta=spec[1:]-spec[:-1]
    weight=mask[None,:]*np.minimum(np.exp(np.clip(mid,-10,2)),5)
    den=np.sum(weight*gradient**2,axis=1)
    shifts=-np.sum(weight*gradient*delta,axis=1)/(den+1e-12)
    shifts=np.clip(shifts,-.03,.03)
    log_motion=np.r_[0,np.cumsum(shifts)]
    # Highpass removes accumulated registration drift; does not impose a 6.5 Hz phase.
    motion=signal.sosfiltfilt(signal.butter(3,[2,30],fs=FS,btype='bandpass',output='sos'),log_motion)
    energy=np.sqrt(np.mean(power[allowed],axis=0)) if allowed.any() else np.zeros(len(t))
    active=energy>max(np.percentile(energy,80)*.18,1e-7)
    keep=t<len(x)/sr
    return t[keep],motion[keep],active[keep],dict(allowed_bins=int(allowed.sum()),persistent_mask_hz=f[persistent].tolist(),active_fraction=float(active.mean()))

def combine(curves,active,band=(4,10)):
    a=np.asarray(curves);w=np.asarray(active,dtype=float)
    # Equal per-track weights prevent a loud track or a bank of its partials dominating.
    raw=np.sum(a*w,axis=0)/np.maximum(w.sum(axis=0),1)
    f,t,z=signal.stft(raw,FS,nperseg=800,noverlap=700,boundary='zeros')
    keep=(f>=band[0])&(f<=band[1]);z[~keep]=0
    _,out=signal.istft(z,FS,nperseg=800,noverlap=700,boundary=True)
    return out[:a.shape[1]],f,t,z

def metrics(e,truth,t):
    k=(t>3)&(t<t[-1]-3);a=e[k];b=truth[k]
    gain=float(np.dot(a,b)/max(np.dot(b,b),1e-20))
    displacement=cumulative_trapezoid(e-truth,t,initial=0);displacement-=np.median(displacement[k])
    return dict(correlation=float(np.corrcoef(a,b)[0,1]) if min(np.std(a),np.std(b))>1e-9 else None,
                signed_gain=gain if np.std(b)>1e-9 else None,
                speed_rmse=float(np.sqrt(np.mean((a-b)**2))),estimated_speed_rms=float(np.std(a)),
                displacement_rmse_us=float(1e6*np.sqrt(np.mean(displacement[k]**2))),
                oracle_residual_fraction=float(np.std(a-b)/max(np.std(b),1e-20)) if np.std(b)>1e-9 else None)

def speech(seed,duration=24,sr=8000,natural_vibrato=False):
    # Independent formant-shaped voiced synthesis with syllabic gating, consonant noise,
    # natural F0 movement and time-varying vowels; not a claim of human speech fidelity.
    rng=np.random.default_rng(seed);t=np.arange(duration*sr)/sr
    knots=np.arange(0,duration+1,.12);v=rng.normal(size=len(knots))
    pitch=115+seed*7+18*np.sin(2*np.pi*(.31+.027*seed)*t+seed)+10*np.interp(t,knots,v)
    pitch=ndimage.gaussian_filter1d(pitch,100)
    if natural_vibrato: pitch*=1+.025*np.sin(2*np.pi*(5.4+.31*(seed-1))*t+seed)
    phase=2*np.pi*cumulative_trapezoid(pitch,t,initial=0)
    syll=np.maximum(0,np.sin(2*np.pi*(2.1+.09*seed)*t+seed))**.7
    formants=[550+180*np.sin(2*np.pi*.4*t+seed),1500+350*np.sin(2*np.pi*.27*t+seed),2900+200*np.sin(2*np.pi*.6*t)]
    x=np.zeros(len(t))
    for h in range(1,26):
        hz=h*pitch;env=sum(np.exp(-.5*((hz-c)/w)**2) for c,w in zip(formants,[100,170,230]))
        x+=env*np.sin(h*phase+rng.uniform(-np.pi,np.pi))/h
    noise=signal.sosfilt(signal.butter(3,[1400,3300],fs=sr,btype='bandpass',output='sos'),rng.normal(size=len(t)))
    x=x*syll+.025*noise*(1-syll)
    return x/max(np.std(x),1e-9)*.08

def synthetic():
    sr=8000;duration=24;t=np.arange(duration*sr)/sr
    bases=[speech(i) for i in range(1,9)];rows=[];saved={}
    for kind in ['common_6.5','drift_harmonics','AM_only','unwarped','independent_pitch']:
        phase=2*np.pi*(6.5*t if kind!='drift_harmonics' else 6*t+.025*t*t)
        truth=.025*np.sin(phase+.4)
        if kind=='drift_harmonics':truth+=.007*np.sin(2*phase+.9)
        if kind in ['AM_only','unwarped','independent_pitch']:truth*=0
        curves=[];active=[]
        for i,x in enumerate(bases):
            e=truth.copy()
            if kind=='independent_pitch':x=speech(i+1,natural_vibrato=True)
            tau=t+cumulative_trapezoid(e,t,initial=0)
            y=np.interp(tau,t,x)
            if kind=='AM_only':y*=1+.65*np.sin(2*np.pi*6.5*t+.4)
            tt,c,a,m=estimate(y,sr);curves.append(c);active.append(a)
        for count in [1,4,8]:
            for band in [(4,10),(2,30)]:
                e,f,mt,z=combine(curves[:count],active[:count],band)
                tr=np.interp(tt,t,truth);row=dict(kind=kind,tracks=count,band=list(band),**metrics(e,tr,tt))
                co=np.mean(e[600:-600]*np.exp(-2j*np.pi*6.5*tt[600:-600]));ct=np.mean(tr[600:-600]*np.exp(-2j*np.pi*6.5*tt[600:-600]))
                row['phase_error_deg']=float(np.angle(co/ct,deg=True)) if kind=='common_6.5' else None
                ff,pp=signal.welch(e[600:-600],FS,nperseg=2400);sel=(ff>=4)&(ff<=10);row['peak_hz']=float(ff[sel][np.argmax(pp[sel])])
                rows.append(row)
                saved[f'{kind}_{count}_{band[0]}']=e
        print('SYNTHETIC',kind,rows[-2],flush=True)
    (DEST/'synthetic.json').write_text(json.dumps(rows,indent=2));np.savez_compressed(DEST/'synthetic_curves.npz',time=tt,**saved)
    return rows

def real():
    from cyclic_diagnose import trajectory, fm_measure, beeps
    inventory=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text())
    tape=next(x for x in inventory if x['tape']=='A15_T510077');rows=[];sources=[]
    for start in [19715.34,44595.55]:
        data={};curves={};active={};meta={}
        for c in tape['channels']:
            x,sr,m=get(c,tape,start,60);data[c['channel']]=x;sources.append(m)
            tt,e,a,mm=estimate(x,sr);ch=c['channel'];curves[ch]=e;active[ch]=a;meta[ch]=dict(mm,head=c['head'])
        # Exclude reference, held-out target, known event replica and clock/timecode track.
        eligible=[ch for ch in curves if ch not in [1,15,20,30] and meta[ch]['active_fraction']>.25]
        # Remove near-identical audio duplicates using normalized envelope similarity.
        selected=[];duplicates=[]
        env={ch:np.sqrt(np.mean(data[ch][:len(data[ch])//800*800].reshape(-1,800)**2,axis=1)) for ch in eligible}
        for ch in eligible:
            peer=next((p for p in selected if np.corrcoef(env[ch],env[p])[0,1]>.90),None)
            if peer is None:selected.append(ch)
            else:duplicates.append([ch,peer])
        reference=trajectory(data[30],sr,1005.5)[1];reference=np.interp(tt,np.arange(len(reference))/200,reference)
        events=beeps(data[15],sr);cases=[];arrays={f'ch{ch}':v for ch,v in curves.items()}
        for group,channels in [('all_independent',selected),('HR2U',[ch for ch in selected if meta[ch]['head']=='HR2U']),('odd',[ch for ch in selected if ch%2]),('even',[ch for ch in selected if not ch%2])]:
            if not channels:continue
            for band in [(4,10),(2,30)]:
                e,f,mt,z=combine([curves[ch] for ch in channels],[active[ch] for ch in channels],band)
                ref=signal.sosfiltfilt(signal.butter(3,band,fs=200,btype='bandpass',output='sos'),reference)
                ff,pp=signal.welch(e[600:-600],FS,nperseg=6000);sel=(ff>=4)&(ff<=10)
                cohf,coh=signal.coherence(e[600:-600],ref[600:-600],FS,nperseg=800)
                cases.append(dict(group=group,channels=channels,band=list(band),peak_hz=float(ff[sel][np.argmax(pp[sel])]),coherence_near_6_5=float(coh[np.argmin(abs(cohf-6.5))]),**metrics(e,ref,tt),heldout_beeps=fm_measure(data[15],sr,e,events)))
                key=f'{group}_{band[0]}';arrays[key]=e;arrays[key+'_complex_stft']=z
        arrays['reference_evaluation_only']=reference;arrays['time']=tt
        np.savez_compressed(DEST/f'{start}_curves.npz',**arrays)
        rows.append(dict(start=start,metadata=meta,selected=selected,excluded_duplicates=duplicates,cases=cases))
        (DEST/'real.json').write_text(json.dumps(rows,indent=2))
        print('REAL',start,'independent',selected,'cases',[(c['group'],c['band'],round(c['correlation'],3),round(c['signed_gain'],3)) for c in cases],flush=True)
    for m in sources:
        p=Path(m['path']);s=p.stat();assert s.st_size==m['bytes'] and s.st_mtime_ns==m['mtime_ns']
    (DEST/'sources.json').write_text(json.dumps(sources,indent=2))

if __name__=='__main__':
    DEST.mkdir(exist_ok=True)
    synthetic()
    real()
