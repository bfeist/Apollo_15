"""Frozen tone-free estimator check on a second tape; no tuning on held-out tones."""
from nested_controls import *
from cyclic_diagnose import trajectory,fm_measure,beeps

def run():
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());rows=[];sources=[]
    tape=next(x for x in inv if x['tape']=='A15_T510076');start=11311.58;data={};curves={};active={};meta={}
    for c in tape['channels']:
        x,sr,m=get(c,tape,start,60);sources.append(m);ch=c['channel'];data[ch]=x
        tt,e,a,mm=estimate(x,sr);curves[ch]=e;active[ch]=a;meta[ch]=mm
    eligible=[ch for ch in curves if ch not in [1,12,15,20,30]];selected=[];duplicates=[]
    env={ch:np.sqrt(np.mean(data[ch].reshape(-1,800)**2,axis=1)) for ch in eligible}
    for ch in eligible:
        peer=next((p for p in selected if np.corrcoef(env[ch],env[p])[0,1]>.90),None)
        if peer is None:selected.append(ch)
        else:duplicates.append([ch,peer])
    ref=trajectory(data[30],sr,1000)[1];ref=signal.sosfiltfilt(signal.butter(3,[4,10],fs=FS,btype='bandpass',output='sos'),ref)
    saved={}
    for method in ['average','coherent']:
        cs=[curves[ch] for ch in selected];ac=[active[ch] for ch in selected]
        e=combine(cs,ac)[0] if method=='average' else coherent(cs,ac)[0]
        ff,pp=signal.welch(e[600:-600],FS,nperseg=6000);sel=(ff>=4)&(ff<=10)
        events=[]
        for ch in [12,15,20]:
            for b in fm_measure(data[ch],sr,e,beeps(data[ch],sr)):events.append(dict(channel=ch,**b))
        rows.append(dict(tape=tape['tape'],start=start,method=method,channels=selected,excluded_duplicates=duplicates,peak_hz=float(ff[sel][np.argmax(pp[sel])]),**metrics(e,ref,tt),heldout_events=events))
        saved[method]=e
    np.savez_compressed(DEST/'T510076_curves.npz',time=tt,reference_evaluation_only=ref,**saved)
    (DEST/'transfer.json').write_text(json.dumps(rows,indent=2));(DEST/'transfer_sources.json').write_text(json.dumps(sources,indent=2))
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)
    print('TRANSFER',[(r['method'],r['correlation'],r['signed_gain'],r['peak_hz']) for r in rows],flush=True)

if __name__=='__main__':run()
