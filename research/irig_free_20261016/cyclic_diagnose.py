"""Phase-preserving second-level modulation analysis and residual-FM diagnostics."""
from bespoke_validate import *
DEST=ROOT/'reports/cyclic_flutter_20261017';DEST.mkdir(exist_ok=True)

def trajectory(x,sr,hz,capture_override=None):
    t=np.arange(len(x))/sr
    z=signal.resample_poly(x*np.exp(-2j*np.pi*hz*t),1,sr//1000)
    capture=min(180,max(55,hz*.09)) if capture_override is None else capture_override
    z=signal.sosfiltfilt(signal.butter(6,capture,fs=1000,output='sos'),z)
    tt=np.arange(round(len(x)/sr*200))/200
    theta=np.interp(tt,np.arange(len(z))/1000,np.unwrap(np.angle(z)))
    amp=np.interp(tt,np.arange(len(z))/1000,abs(z));freq=hz+np.gradient(theta)*200/(2*np.pi)
    centre=float(np.median(freq[400:-400]));displacement=(theta+2*np.pi*(hz-centre)*tt)/(2*np.pi*centre)
    error=np.clip((freq-centre)/centre,-.10,.10)
    f,p=signal.welch(error[400:-400],200,nperseg=min(4000,len(error)-800))
    mask=(f>=4)&(f<=10);fm=float(f[mask][np.argmax(p[mask])])
    freqs,times,spectrum=signal.stft(error,200,nperseg=800,noverlap=700,boundary='zeros')
    ids=np.flatnonzero((freqs>=4)&(freqs<=10));peak=ids[np.argmax(abs(spectrum[ids]),axis=0)]
    rates=[]
    for col,k in enumerate(peak):
        yy=np.log(abs(spectrum[k-1:k+2,col])+1e-15);delta=.5*(yy[0]-yy[2])/(yy[0]-2*yy[1]+yy[2]+1e-15)
        rates.append(float(freqs[k]+np.clip(delta,-.5,.5)*.25))
    rate=np.interp(tt,times,ndimage.median_filter(rates,size=3))
    peaks,_=signal.find_peaks(p);peaks=sorted([k for k in peaks if 2<f[k]<35],key=lambda k:p[k],reverse=True)[:8]
    # Complex coefficients retain phase; AM spectrum is separate and never supplies a warp.
    am=amp/max(np.median(amp),1e-15)-1;coeffs=[]
    for harmonic in [1,2,3]:
        basis=np.exp(-2j*np.pi*fm*harmonic*tt[400:-400]);co=2*np.mean(error[400:-400]*basis);ac=2*np.mean(am[400:-400]*basis)
        coeffs.append(dict(harmonic=harmonic,fm_real=float(co.real),fm_imag=float(co.imag),am_real=float(ac.real),am_imag=float(ac.imag)))
    meta=dict(carrier_hz=hz,centre_hz=centre,capture_hz=capture,dominant_modulation_hz=fm,rate_percentiles_hz=np.percentile(rate[400:-400],[5,50,95]).tolist(),modulation_peaks=[dict(hz=float(f[k]),power=float(p[k])) for k in peaks],complex_coefficients=coeffs,support=float(np.mean(amp[400:-400]>.3*np.median(amp))))
    return tt,error,displacement,amp,rate,meta

def harmonic_map(trace,harmonics=3):
    tt,error,disp,amp,rate,meta=trace;grid=np.arange(0,len(tt),5);values=[]
    for i in grid:
        lo=max(0,i-120);hi=min(len(tt),i+121);u=tt[lo:hi]-tt[i];fm=rate[i]
        a=np.column_stack([np.ones(len(u)),u,u*u]+[fn(2*np.pi*h*fm*u) for h in range(1,harmonics+1) for fn in [np.sin,np.cos]])
        base=np.minimum(amp[lo:hi]/max(np.median(amp),1e-15),1)**2*np.exp(-.5*(u/.3)**2);w=base.copy()
        for _ in range(3):
            c=np.linalg.lstsq(a*np.sqrt(w[:,None]),disp[lo:hi]*np.sqrt(w),rcond=None)[0]
            residual=disp[lo:hi]-a@c;scale=max(1e-6,1.4826*np.median(abs(residual-np.median(residual))));w=base*np.minimum(1,2*scale/(abs(residual)+1e-15))
        values.append(sum(2*np.pi*h*fm*c[3+2*(h-1)] for h in range(1,harmonics+1)))
    e=np.interp(tt,tt[grid],values)
    return bound(e)

def bound(e):
    tt=np.arange(len(e))/200
    return np.clip(e,-.049,.049)*np.clip(np.minimum(tt,tt[-1]-tt)/2,0,1)

def fm_measure(x,sr,e,events,rate=6.55):
    pos=positions(e,sr,len(x));out=[]
    for b in events:
        lo=int((b['start']-.15)*sr);hi=int((b['end']+.15)*sr);requested=pos[lo:hi]
        left=max(0,int(requested.min())-sr//5);right=min(len(x),int(requested.max())+sr//5)
        y=resample_at(x[left:right],requested-left)
        _,fb=burst_track(x[lo:hi],sr);_,fa=burst_track(y,sr);t=lo/sr+np.arange(len(fb))/1000;mt=np.interp(t*sr,np.arange(len(pos)),pos)/sr
        record=dict(b,margins={})
        for margin in [.025,.04,.055]:
            kb=(t>b['start']+margin)&(t<b['end']-margin);ka=(mt>b['start']+margin)&(mt<b['end']-margin)
            metrics=[]
            for freq,time,keep in [(fb,t,kb),(fa,t,ka)]:
                a=np.column_stack([np.ones(keep.sum()),np.sin(2*np.pi*rate*time[keep]),np.cos(2*np.pi*rate*time[keep])]);c=np.linalg.lstsq(a,freq[keep],rcond=None)[0]
                metrics.append(dict(rms_hz=float(np.std(freq[keep])),rms_cents=float(np.std(1200*np.log2(np.maximum(freq[keep],1)/np.median(freq[keep])))),fundamental_peak_hz=float(np.hypot(c[1],c[2])),fit_residual_rms_hz=float(np.std(freq[keep]-a@c)),condition=float(np.linalg.cond(a))))
            record['margins'][str(margin)]=dict(before=metrics[0],after=metrics[1])
        out.append(record)
    return out

def calibrate(e,x,sr,events):
    # Fit only designated early events; never infer transport lag from dialogue delay.
    amp,freq=burst_track(x,sr);times=np.arange(len(freq))/1000;xs=[];ys=[]
    for b in events:
        keep=(times>b['start']+.04)&(times<b['end']-.04);ys.append((freq[keep]-np.mean(freq[keep]))/np.mean(freq[keep]));xs.append(times[keep])
    if not xs:return None
    target=np.concatenate(ys);times=np.concatenate(xs);tt=np.arange(len(e))/200;fits=[]
    for lag in np.arange(-.20,.2001,.002):
        vec=np.interp(times+lag,tt,e);cursor=0
        for y in ys:vec[cursor:cursor+len(y)]-=np.mean(vec[cursor:cursor+len(y)]);cursor+=len(y)
        gain=float(np.clip(np.dot(vec,target)/max(np.dot(vec,vec),1e-15),-2,2));loss=float(np.mean((target-gain*vec)**2));fits.append((loss,float(lag),gain))
    loss,lag,gain=min(fits)
    return dict(lag_seconds=lag,gain=gain,calibration_rmse=float(np.sqrt(loss))),bound(gain*np.interp(tt+lag,tt,e))

def synthetic():
    sr=8000;t=np.arange(20*sr)/sr;out=[]
    for fm,depth,kind in [(6.5,.025,'pure'),(6.5,.04,'pure'),(8.,.025,'pure'),(6.5,.025,'harmonic'),(6.5,.025,'AM_only'),(6.5,.025,'drifting')]:
        phase=2*np.pi*(fm*t if kind!='drifting' else 6*t+.025*t*t);truth=depth*np.sin(phase)
        if kind=='harmonic':truth+=.007*np.sin(2*phase+.4)
        if kind=='AM_only':truth*=0
        from scipy.integrate import cumulative_trapezoid
        tau=t+cumulative_trapezoid(truth,dx=1/sr,initial=0);ref=np.sin(2*np.pi*1005.5*tau)
        if kind=='AM_only':ref*=1+.6*np.sin(phase)
        target=np.sin(2*np.pi*2500*tau);trace=trajectory(ref,sr,1005.5)
        maps={'old_4_10':phase_map(ref,sr,1005.5,[4,10]),'phase_h1':harmonic_map(trace,1),'phase_h3':harmonic_map(trace,3),'wide_2_30':bound(signal.sosfiltfilt(signal.butter(3,[2,30],fs=200,btype='bandpass',output='sos'),trace[1]))}
        for name,e in maps.items():
            tt=np.arange(len(e))/200;tr=np.interp(tt,t,truth);keep=(tt>3)&(tt<17)
            co=np.mean(e[keep]*np.exp(-2j*np.pi*fm*tt[keep]));ct=np.mean(tr[keep]*np.exp(-2j*np.pi*fm*tt[keep]));gain=abs(co)/max(abs(ct),1e-15);angle=float(np.angle(co/(ct+1e-15),deg=True))
            ev=fm_measure(target,sr,e,[dict(start=8,end=8.25)],fm)[0]['margins']['0.04'];out.append(dict(kind=kind,fm=fm,depth=depth,method=name,gain=None if kind=='AM_only' else float(gain),phase_deg=None if kind=='AM_only' else angle,estimated_speed_rms=float(np.std(e[keep])),short_tone=ev))
        # Correct sign must beat reversed sign in the exact rendering chain.
        if kind=='pure':
            good=sparse_evaluate(target,sr,maps['phase_h3'],[dict(start=8,end=8.25)])[0];bad=sparse_evaluate(target,sr,-maps['phase_h3'],[dict(start=8,end=8.25)])[0];assert good['after_rms_hz']<bad['after_rms_hz']
        if kind=='AM_only':assert np.std(maps['phase_h3'][600:-600])<1e-5
    (DEST/'synthetic.json').write_text(json.dumps(out,indent=2));print('SYNTHETIC',len(out),'cases',flush=True)

def main():
    synthetic();inventory=json.loads((OUT/'inventory.json').read_text());tape=next(t for t in inventory if t['tape']=='A15_T510077');bych={c['channel']:c for c in tape['channels']};start=19715.34;data={};sources=[];records=[];traces={}
    for ch,c in bych.items():
        x,sr,m=get(c,tape,start,60);sources.append(m);data[ch]=x
        cs=discover(x[:30*sr],sr,3)
        if ch==30:cs=[dict(carrier_hz=1005.5)]+cs
        for cand in cs:
            hz=cand['carrier_hz']
            if 2200<hz<2800:continue
            tr=trajectory(x,sr,hz);key=f'{ch}_{hz}';traces[key]=tr;records.append(dict(tr[-1],channel=ch,key=key))
    primary=traces['30_1005.5'];h3=harmonic_map(primary,3);h1=harmonic_map(primary,1);old=phase_map(data[30],sr,1005.5,[4,10]);tt=np.arange(len(old))/200
    peers=[]
    for r in records:
        if r['channel']==30 or abs(r['carrier_hz']-1005.5)<80:continue
        e=harmonic_map(traces[r['key']],3);corr=float(np.corrcoef(h3[400:5600],e[400:5600])[0,1]);peers.append(dict(r,correlation=corr,error=e))
    peers.sort(key=lambda r:r['correlation'],reverse=True);chosen=[]
    for p in peers:
        if p['correlation']>.6 and p['channel'] not in [c['channel'] for c in chosen]:chosen.append(p)
        if len(chosen)==3:break
    maps={'old_4_10':old,'phase_h1':h1,'phase_h3':h3,'wide_2_30':bound(signal.sosfiltfilt(signal.butter(3,[2,30],fs=200,btype='bandpass',output='sos'),primary[1]))}
    if chosen:maps['common_mode_h3']=bound(np.median(np.array([h3]+[p['error'] for p in chosen]),axis=0))
    events=beeps(data[15],sr);cal=[b for b in events if b['end']<23];hold=[b for b in events if b['start']>=23]
    calibration={}
    for name in ['old_4_10','phase_h3']:
        fitted=calibrate(maps[name],data[15],sr,cal)
        if fitted:calibration[name],maps[name+'_calibrated']=fitted
    results={name:fm_measure(data[15],sr,e,events,primary[-1]['dominant_modulation_hz']) for name,e in maps.items()}
    for name,e in list(maps.items()):
        if 'calibrated' in name:continue
        results[name+'_wrong_sign']=fm_measure(data[15],sr,-e,hold,primary[-1]['dominant_modulation_hz'])
    np.savez_compressed(DEST/'last_window_curves.npz',**maps)
    out=dict(tape=tape['tape'],start=start,sources=sources,carriers=records,peers=[{k:v for k,v in p.items() if k!='error'} for p in peers[:10]],chosen_common_mode=[{k:v for k,v in p.items() if k!='error'} for p in chosen],events=events,calibration_events=cal,holdout_events=hold,calibration=calibration,results=results)
    (DEST/'last_window.json').write_text(json.dumps(out,indent=2))
    print('LAST WINDOW',primary[-1]['dominant_modulation_hz'],'cal',calibration,flush=True)
    for name,rr in results.items():print(name,[(round(r['start'],3),round(r['margins']['0.04']['after']['rms_hz'],1),round(r['margins']['0.04']['after']['fundamental_peak_hz'],1)) for r in rr],flush=True)
    # Freeze primary carrier and methods; no refitting on other passages.
    general=[]
    for position in [32710.81,16729.21,24623.89]:
        ref,sr,m=get(bych[30],tape,position,60);target,sr,tm=get(bych[15],tape,position,60);tr=trajectory(ref,sr,1005.5)
        em={'old_4_10':phase_map(ref,sr,1005.5,[4,10]),'phase_h1':harmonic_map(tr,1),'phase_h3':harmonic_map(tr,3)}
        for name,fit in calibration.items():em[name+'_calibrated']=bound(fit['gain']*np.interp(tt+fit['lag_seconds'],tt,em[name]))
        qs=beeps(target,sr);rr={name:fm_measure(target,sr,e,qs,tr[-1]['dominant_modulation_hz']) for name,e in em.items()}
        general.append(dict(start=position,sources=[m,tm],trajectory=tr[-1],events=qs,results=rr));print('TRANSFER',position,'events',len(qs),flush=True)
    (DEST/'transfer.json').write_text(json.dumps(general,indent=2))

if __name__=='__main__':main()
