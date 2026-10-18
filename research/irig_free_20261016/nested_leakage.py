"""Strong line-exclusion and deterministic cross-track phase-shuffle controls."""
from nested_controls import *
from cyclic_diagnose import trajectory, fm_measure, beeps

def run():
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());tape=next(x for x in inv if x['tape']=='A15_T510077');rows=[];sources=[]
    for w in json.loads((DEST/'real.json').read_text()):
        start=w['start'];curves=[];active=[];detail=[];data={}
        for c in tape['channels']:
            if c['channel'] not in w['selected']+[15,30]:continue
            x,sr,m=get(c,tape,start,60);sources.append(m);data[c['channel']]=x
            if c['channel'] in [15,30]:continue
            tt,e,a,mm=estimate(x,sr,strict=True);curves.append(e);active.append(a);detail.append(dict(channel=c['channel'],**mm))
        ref=trajectory(data[30],sr,1005.5)[1];ref=signal.sosfiltfilt(signal.butter(3,[4,10],fs=200,btype='bandpass',output='sos'),ref)
        e=combine(curves,active)[0];ff,pp=signal.welch(e[600:-600],200,nperseg=6000);sel=(ff>=6.3)&(ff<=6.8);power=float(pp[sel].sum())
        rng=np.random.default_rng(177);null=[]
        for _ in range(40):
            shifts=rng.integers(500,11000,len(curves));shuffled=[np.roll(c,k) for c,k in zip(curves,shifts)];weights=[np.roll(a,k) for a,k in zip(active,shifts)]
            ee=combine(shuffled,weights)[0];_,p=signal.welch(ee[600:-600],200,nperseg=6000);null.append(float(p[sel].sum()))
        rows.append(dict(start=start,tracks=detail,**metrics(e,ref,tt),band_power=power,phase_shuffle_null_power=null,phase_shuffle_exceedances=sum(x>=power for x in null),heldout_beeps=fm_measure(data[15],sr,e,beeps(data[15],sr))))
        np.savez_compressed(DEST/f'{start}_strict.npz',time=tt,estimate=e)
        print('STRICT',start,rows[-1]['correlation'],rows[-1]['signed_gain'],'null exceedances',rows[-1]['phase_shuffle_exceedances'],flush=True)
    (DEST/'leakage.json').write_text(json.dumps(rows,indent=2));(DEST/'leakage_sources.json').write_text(json.dumps(sources,indent=2))
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)

if __name__=='__main__':run()
