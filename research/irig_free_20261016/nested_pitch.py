"""Independent partial-family/F0 trajectory route to signed modulation spectra."""
from nested_controls import *

def pitch_motion(x,sr):
    x,_=cleaned_excerpt(x,sr)
    win=480;hop=40
    padded=np.pad(x,(win//2,win//2));frames=np.lib.stride_tricks.sliding_window_view(padded,win)[::hop][:len(x)//hop]
    frames=frames-frames.mean(axis=1,keepdims=True)
    spec=np.fft.rfft(frames,n=1024,axis=1);acf=np.fft.irfft(abs(spec)**2,n=1024,axis=1)[:,:win]
    sums=np.cumsum(frames**2,axis=1);lags=np.arange(20,115)
    den=np.sqrt(np.maximum(sums[:,win-lags-1]*(sums[:,-1,None]-sums[:,lags-1]),1e-20))
    corr=acf[:,lags]/den
    pitch=[];confidence=[];previous=160
    for row in corr:
        peaks,_=signal.find_peaks(row)
        if not len(peaks):peaks=np.array([np.argmax(row)])
        best=max(row[peaks]);choices=peaks[row[peaks]>max(.4,best*.88)]
        if not len(choices):k=peaks[np.argmax(row[peaks])]
        else:k=choices[np.argmin(abs(np.log((sr/lags[choices])/previous)))]
        delta=.5*(row[k-1]-row[k+1])/(row[k-1]-2*row[k]+row[k+1]+1e-12) if 0<k<len(row)-1 else 0
        hz=sr/(lags[k]+np.clip(delta,-.5,.5));pitch.append(hz);confidence.append(row[k])
        if row[k]>.5:previous=hz
    pitch=np.array(pitch);confidence=np.array(confidence);energy=np.sqrt(np.mean(frames**2,axis=1))
    active=(confidence>.55)&(energy>max(np.percentile(energy,85)*.15,1e-6))
    # Reject octave jumps before band analysis; interpolate only to define filter input.
    logs=np.log(pitch);spikes=abs(logs-ndimage.median_filter(logs,size=9))>.15;active &= ~spikes
    tt=np.arange(len(pitch))/FS
    if active.sum()>20:logs=np.interp(tt,tt[active],logs[active])
    else:logs=np.zeros(len(tt))
    e=signal.sosfiltfilt(signal.butter(3,[2,30],fs=FS,btype='bandpass',output='sos'),logs)
    return tt,e,active,dict(voiced_fraction=float(active.mean()),median_pitch_hz=float(np.median(pitch[active])) if active.any() else None)

def run():
    sr=8000;t=np.arange(24*sr)/sr;rows=[]
    for kind in ['common_6.5','drift_harmonics','AM_only','unwarped','independent_pitch']:
        ph=2*np.pi*(6*t+.025*t*t if kind=='drift_harmonics' else 6.5*t);truth=.025*np.sin(ph+.4)
        if kind=='drift_harmonics':truth+=.007*np.sin(2*ph+.9)
        if kind in ['AM_only','unwarped','independent_pitch']:truth*=0
        curves=[];active=[];details=[]
        for i in range(8):
            x=speech(i+1,natural_vibrato=kind=='independent_pitch');err=truth
            tau=t+cumulative_trapezoid(err,t,initial=0);y=np.interp(tau,t,x)
            if kind=='AM_only':y*=1+.65*np.sin(2*np.pi*6.5*t+.4)
            tt,e,a,m=pitch_motion(y,sr);curves.append(e);active.append(a);details.append(m)
        for method in ['average','coherent']:
            e=combine(curves,active)[0] if method=='average' else coherent(curves,active)[0]
            e=np.clip(e,-.049,.049)
            rows.append(dict(kind=kind,method=method,tracks=details,**metrics(e,np.interp(tt,t,truth),tt)))
        print('PITCH SYNTH',kind,[(r['method'],r['signed_gain'],r['speed_rmse']) for r in rows[-2:]],flush=True)
    (DEST/'pitch_synthetic.json').write_text(json.dumps(rows,indent=2))
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());tape=next(x for x in inv if x['tape']=='A15_T510077');real=[];sources=[]
    from cyclic_diagnose import trajectory,fm_measure,beeps
    for start in [19715.34,44595.55]:
        curves=[];active=[];details=[];data={}
        for c in tape['channels']:
            x,sr,m=get(c,tape,start,60);sources.append(m);data[c['channel']]=x
            if c['channel'] in [1,15,20,30]:continue
            tt,e,a,mm=pitch_motion(x,sr);details.append(dict(channel=c['channel'],**mm));curves.append(e);active.append(a)
        ref=trajectory(data[30],sr,1005.5)[1];ref=signal.sosfiltfilt(signal.butter(3,[4,10],fs=FS,btype='bandpass',output='sos'),ref)
        for method in ['average','coherent']:
            e=combine(curves,active)[0] if method=='average' else coherent(curves,active)[0];e=np.clip(e,-.049,.049)
            real.append(dict(start=start,method=method,tracks=details,**metrics(e,ref,tt),heldout_beeps=fm_measure(data[15],sr,e,beeps(data[15],sr))))
            np.savez_compressed(DEST/f'{start}_pitch_{method}.npz',time=tt,estimate=e,tracks=np.array(curves),active=np.array(active))
        print('PITCH REAL',start,[(r['method'],r['correlation'],r['signed_gain']) for r in real[-2:]],flush=True)
    (DEST/'pitch_real.json').write_text(json.dumps(real,indent=2));(DEST/'pitch_sources.json').write_text(json.dumps(sources,indent=2))
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)

if __name__=='__main__':run()
