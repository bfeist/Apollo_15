"""Same-source raw versus production-cleanup nested-analysis audit."""
from nested_amplitude import *
from audio_processing import detect,remove,denoise
from cyclic_diagnose import trajectory,burst_track,beeps,fm_measure
import hashlib,os,sys
OUTC=ROOT/'reports/nested_cleanup_20261017'

def cleanup(x,sr,passes=None):
    y=x.copy();used=[]
    if passes is not None:
        for lines in passes:y=remove(y,sr,lines);used.append(lines)
    else:
        for _ in range(6):
            lines=detect(y,sr)
            if not lines:break
            y=remove(y,sr,lines);used.append(lines)
    y,profile=denoise(y,sr,reduction_cap_db=6)
    return y,dict(passes=used,noise_profile_seconds=profile,reduction_cap_db=6)

def summarize(e,ref):
    tt=np.arange(len(e))/200;f,p=signal.welch(e[600:-600],200,nperseg=4000);band=(f>=6.3)&(f<=6.8);side=((f>=4)&(f<6))|((f>7)&(f<=10))
    r=signal.sosfiltfilt(signal.butter(3,[4,10],fs=200,btype='bandpass',output='sos'),ref)
    ee=signal.sosfiltfilt(signal.butter(3,[4,10],fs=200,btype='bandpass',output='sos'),e)
    return dict(**metrics(ee,r,tt),band_to_sideband_density=float(np.mean(p[band])/np.mean(p[side])),peak_4_10_hz=float(f[(f>=4)&(f<=10)][np.argmax(p[(f>=4)&(f<=10)])]))

def main():
    OUTC.mkdir(exist_ok=True)
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());tape=next(x for x in inv if x['tape']=='A15_T510077');by={c['channel']:c for c in tape['channels']}
    approved=json.loads((ROOT/'reports/cyclic_flutter_20261017/listening.json').read_text());results=[];sources=[];arrays={};beep_rows=[]
    for start in [19715.34,44595.55]:
        refx,sr,m=get(by[30],tape,start,60);sources.append(m);ref=trajectory(refx,sr,1005.5)[1];rec=next(r for r in approved if r['window_start']==start)
        for ch in ([15,23,13] if start==19715.34 else [15,23]):
            x,sr,m=get(by[ch],tape,start,60);sources.append(m)
            clean,cm=cleanup(x,sr,rec['cleanup_passes'] if ch==15 else None)
            er=estimate(x,sr)[1];ec=estimate(clean,sr)[1];tag=f'{start}_{ch}'
            arrays[tag+'_raw']=x;arrays[tag+'_clean']=clean;arrays[tag+'_raw_motion']=er;arrays[tag+'_clean_motion']=ec
            row=dict(start=start,channel=ch,cleanup=cm,raw=summarize(er,ref),clean=summarize(ec,ref),raw_rms=float(np.std(x)),clean_rms=float(np.std(clean)))
            if ch==15:
                offset=rec['clip_offset'];lo=round(offset*sr);hi=lo+12*sr
                previous,rate=sf.read(ROOT/'reports/cyclic_flutter_20261017'/rec['files']['before'])
                row['approved_before_reproduction_max_error']=float(np.max(abs(clean[lo:hi]*rec['gain']-previous)));assert row['approved_before_reproduction_max_error']<2e-7
                events=[b for b in beeps(x,sr) if offset+.2<b['start'] and b['end']<offset+11.9]
                row['raw_estimator_correction_beeps']=fm_measure(clean,sr,np.clip(er,-.049,.049),events)
                row['clean_estimator_correction_beeps']=fm_measure(clean,sr,np.clip(ec,-.049,.049),events)
                corrected,rate=sf.read(ROOT/'reports/cyclic_flutter_20261017'/rec['files']['new_experimental'])
                warp=np.load(ROOT/'reports/cyclic_flutter_20261017'/f'T510077_{start:.2f}_explicit_warp.npz')
                from flutter import positions
                pos=positions(warp['speed_error'],sr,len(x))/sr
                traces=[]
                for name,y in [('raw',x[lo:hi]),('clean',clean[lo:hi]),('approved_corrected',corrected)]:
                    amp,freq=burst_track(y,sr);tt=offset+np.arange(len(freq))/1000
                    mapped=tt if name!='approved_corrected' else np.interp(tt,np.arange(len(pos))/sr,pos)
                    for i,b in enumerate(events):
                        valid=(mapped>b['start']+.04)&(mapped<b['end']-.04)
                        tt_valid=tt[valid];fv=freq[valid]
                        traces.append(dict(version=name,event=i,relative_time=(tt_valid-b['start']).tolist(),frequency_hz=fv.tolist(),center_hz=float(np.median(fv)),rms_hz=float(np.std(fv))))
                beep_rows.append(dict(start=start,offset=offset,events=events,traces=traces))
            results.append(row);print('CLEANUP',start,ch,'correlation raw/clean',row['raw']['correlation'],row['clean']['correlation'],'gain',row['raw']['signed_gain'],row['clean']['signed_gain'],flush=True)
        arrays[f'{start}_reference_evaluation_only']=ref
    np.savez_compressed(OUTC/'data.npz',**arrays)
    (OUTC/'results.json').write_text(json.dumps(results,indent=2));(OUTC/'beep_traces.json').write_text(json.dumps(beep_rows,indent=2));(OUTC/'sources.json').write_text(json.dumps(sources,indent=2))
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)

if __name__=='__main__':main()
