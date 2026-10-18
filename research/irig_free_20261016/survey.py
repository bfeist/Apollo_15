"""Bounded, isolated cross-tape flutter research; never writes delivery audio."""
import os
for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[key]='1'
import argparse, hashlib, json, re, sys, time, wave
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal, ndimage
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from flutter import estimate, positions, resample_at
from audio_processing import remove
# Do not import listening-report generators: this standalone tracker has no writes.
def burst_track(x,sr):
    t=np.arange(len(x))/sr
    z=signal.resample_poly(x*np.exp(-2j*np.pi*2500*t),1,sr//1000)
    z=signal.sosfiltfilt(signal.butter(5,280,fs=1000,output='sos'),z)
    amp=ndimage.gaussian_filter1d(abs(z),2)
    freq=ndimage.gaussian_filter1d(2500+np.gradient(np.unwrap(np.angle(z)))*1000/(2*np.pi),3)
    return amp,freq

def stable(path):
    s=path.stat()
    if time.time()-s.st_mtime<600: raise ValueError('recent source')
    with wave.open(str(path),'rb') as w:
        frames=w.getnframes();sr=w.getframerate()
        if w.getnchannels()!=1 or sr%1000 or sr<6000 or sr>48000:raise ValueError('unsupported format')
        # wave's declared data chunk must physically fit within the file.
        data_start=w._data_chunk.offset+8
        if data_start+frames*w.getsampwidth()>s.st_size:raise ValueError('truncated data chunk')
        w.setpos(max(0,frames-1))
        if len(w.readframes(1))!=w.getsampwidth():raise ValueError('missing last frame')
    return s.st_size,s.st_mtime_ns,sr,frames

def read(path,start,duration):
    before=stable(path)
    with sf.SoundFile(path) as f:
        f.seek(round(start*f.samplerate));x=f.read(round(duration*f.samplerate),dtype='float64')
    if before!=stable(path) or len(x)!=round(duration*before[2]):raise ValueError('changed or short read')
    return x,before[2],dict(path=str(path),start_seconds=start,duration_seconds=duration,
        bytes=before[0],mtime_ns=before[1],sample_rate=before[2],frames=before[3],
        excerpt_float64_sha256=hashlib.sha256(x.tobytes()).hexdigest())

def regions(mask):
    d=np.diff(np.r_[False,mask,False].astype(int))
    return zip(np.flatnonzero(d==1),np.flatnonzero(d==-1))

def beeps(x,sr):
    amp,f=burst_track(x,sr);floor=max(float(np.percentile(amp,25)),1e-15)
    mask=ndimage.binary_closing(amp>max(4*floor,2*np.median(amp)),iterations=3)
    out=[]
    for lo,hi in regions(mask):
        if not .18<(hi-lo)/1000<.36 or lo<300 or hi>len(amp)-300:continue
        core=slice(lo+20,hi-20);fr=f[core]
        ratio=float(np.percentile(amp[core],90)/max(np.percentile(amp[core],10),1e-15))
        rough=float(np.std(fr-ndimage.gaussian_filter1d(fr,8)))
        if ratio>2.3 or rough>18 or not 2360<np.median(fr)<2600:continue
        out.append(dict(start=lo/1000,end=hi/1000,mean_hz=float(np.mean(fr)),
            amplitude_ratio=ratio,roughness_hz=rough,amplitude_over_floor=float(np.median(amp[core])/floor)))
    return out

def candidates(x,sr):
    f,tt,p=signal.spectrogram(x,sr,nperseg=sr*2,noverlap=sr)
    db=10*np.log10(p+1e-30)
    # Broad 80-Hz neighborhoods retain flutter sidebands; narrow stable hum may still win.
    smooth=ndimage.gaussian_filter1d(np.median(p,axis=1),4)
    floor=ndimage.median_filter(smooth,size=321)
    score=10*np.log10((smooth+1e-30)/(floor+1e-30))
    ix,_=signal.find_peaks(score,distance=100)
    ix=[i for i in ix if 120<f[i]<1800]
    ix=sorted(ix,key=lambda i:score[i],reverse=True)[:3]
    out=[]
    for i in ix:
        band=abs(f-f[i])<12;side=(abs(f-f[i])>45)&(abs(f-f[i])<90)
        contrast=10*np.log10((np.max(p[band],axis=0)+1e-30)/(np.median(p[side],axis=0)+1e-30))
        out.append(dict(carrier_hz=float(f[i]),spectral_score_db=float(score[i]),
            contrast_db=float(np.median(contrast)),occupancy=float(np.mean(contrast>10))))
    return out

def measure(x,sr,c,channel):
    hz=c['carrier_hz']
    hum=tuple(float(h) for h in range(60,int(sr/2-4),60) if 23<abs(h-hz)<110)
    tt,e,stats,raw=estimate(x,sr,hz,hum)
    return dict(c,channel=channel,tracking=stats),e

def evaluate(x,sr,e,bursts):
    if not bursts:return []
    pos=positions(e,sr,len(x));y=resample_at(x,pos)
    result=[]
    for b in bursts:
        lo=max(0,int((b['start']-.15)*sr));hi=min(len(x),int((b['end']+.15)*sr))
        _,fb=burst_track(x[lo:hi],sr);_,fa=burst_track(y[lo:hi],sr)
        t=lo/sr+np.arange(len(fb))/1000
        mt=np.interp(t*sr,np.arange(len(pos)),pos)/sr
        kb=(t>b['start']+.05)&(t<b['end']-.05)
        ka=(mt>b['start']+.05)&(mt<b['end']-.05)
        if min(kb.sum(),ka.sum())<60:continue
        before=float(np.std(fb[kb]));after=float(np.std(fa[ka]))
        frac=e[np.clip((t[kb]*200).astype(int),0,len(e)-1)]
        corr=float(np.corrcoef(frac,fb[kb])[0,1]) if np.std(frac)>1e-10 else 0.
        result.append(dict(b,before_rms_hz=before,after_rms_hz=after,
            reduction_percent=100*(1-after/max(before,1e-12)),zero_lag_correlation=corr))
    return result

def inventory(tape,channels):
    groups={};reject=[]
    for folder in sorted(tape.iterdir()):
        m=re.search(r'CH(\d+)_',folder.name)
        if not folder.is_dir() or not m or int(m[1]) not in channels:continue
        ch=int(m[1])
        for path in folder.glob('*.wav'):
            match=re.search(r'_(\d+-\d+-\d+-\d+_\d+-\d+-\d+-\d+-\d+)\.wav$',path.name)
            if not match:continue
            try:
                stat=stable(path)
                if stat[3]/stat[2]<900:continue
            except Exception as ex:
                reject.append(dict(path=str(path),reason=str(ex)));continue
            groups.setdefault(match[1],{})[ch]=path
    good=[(k,v) for k,v in groups.items() if 12 in v and 14 in v and len(v)>=4]
    good.sort(key=lambda kv:int(kv[0].rsplit('-',1)[1]))
    return good,reject

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--tapes',default='510073,510075,510080,510084,510088,510092,510096,510100,510106,510112')
    ap.add_argument('--output',type=Path,default=ROOT/'reports/irig_free_research_20261016')
    ap.add_argument('--starts',default='80,800');ap.add_argument('--duration',type=int,default=50)
    a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    channels=[2,7,12,13,14,15,17,20];summary=[]
    for tape_id in a.tapes.split(','):
        tape=Path('D:/Apollo_15_30-Track/orig_delivery')/('A15_T'+tape_id)
        groups,rejected=inventory(tape,channels)
        if not groups:
            summary.append(dict(tape=tape.name,status='no matching stable group',rejected=rejected));continue
        # Accepted calibration uses fragment 14; other tapes use middle available matching group.
        group=next((g for g in groups if tape_id=='510088' and g[0].endswith('-14')),groups[len(groups)//2])
        key,paths=group
        for start in map(float,a.starts.split(',')):
            tag=f'{tape.name}_{int(start)}';dest=a.output/tag
            if dest.exists():
                print('SKIP existing',tag,flush=True);summary.append(json.loads((dest/'result.json').read_text()));continue
            dest.mkdir();data={};provenance=[];errors=[]
            for ch,p in paths.items():
                try:x,sr,meta=read(p,start,a.duration);data[ch]=x;provenance.append(dict(meta,channel=ch))
                except Exception as ex:errors.append(dict(channel=ch,error=str(ex)))
            if 12 not in data or 14 not in data:continue
            # Filename timing, frame counts and rates must agree; not proof of physical head alignment.
            if len({(m['sample_rate'],m['frames']) for m in provenance})!=1:raise ValueError('unaligned header group')
            allc=[];curves={}
            for ch,x in sorted(data.items()):
                cs=candidates(x,sr)
                if ch==14:cs=[dict(carrier_hz=631.5,spectral_score_db=None,contrast_db=None,occupancy=None,method='fixed_accepted')]+cs
                for c in cs:
                    try:
                        row,e=measure(x,sr,c,ch);idx=len(allc);row['id']=idx;allc.append(row);curves[idx]=e
                    except Exception as ex:errors.append(dict(channel=ch,carrier=c['carrier_hz'],error=str(ex)))
            # Rank using reference spectra only; Quindar outcomes never influence this selection.
            eligible=[c for c in allc if c.get('occupancy') is not None and c['occupancy']>=.7 and c['contrast_db']>=10]
            ranked=sorted(eligible,key=lambda c:c['spectral_score_db'],reverse=True)
            auto=ranked[0]['id'] if ranked else None
            fixed=next(c['id'] for c in allc if c.get('method')=='fixed_accepted')
            pairs=[]
            for i,c in enumerate(allc):
                for d in allc[i+1:]:
                    if c['channel']==d['channel'] or abs(c['carrier_hz']-d['carrier_hz'])<80:continue
                    ec,ed=curves[c['id']][400:-400],curves[d['id']][400:-400]
                    corr=float(np.corrcoef(ec,ed)[0,1]) if min(np.std(ec),np.std(ed))>1e-10 else 0
                    pairs.append(dict(a=c['id'],b=d['id'],correlation=corr))
            pairs.sort(key=lambda p:p['correlation'],reverse=True)
            qs=beeps(data[12],sr)
            # Freeze candidate spectra, selected maps, and zero-lag pairs before evaluation.
            result=dict(tape=tape.name,group=key,start=start,duration=a.duration,sample_rate=sr,
                stable_matching_groups=len(groups),rejected_sources=rejected,sources=provenance,errors=errors,
                candidates=allc,coherence_pairs=pairs[:10],selected_auto=auto,selected_fixed=fixed,
                candidate_beeps=qs,evaluations={})
            (dest/'selection.json').write_text(json.dumps(result,indent=2))
            np.savez_compressed(dest/'curves.npz',**{f'candidate_{i}':e for i,e in curves.items()})
            for label,i in [('fixed_accepted',fixed),('spectral_auto',auto)]:
                if i is not None:result['evaluations'][label]=evaluate(data[12],sr,curves[i],qs)
            (dest/'result.json').write_text(json.dumps(result,indent=2));summary.append(result)
            print(tag,'channels',len(data),'beeps',len(qs),'auto',auto,'best_pair',pairs[0] if pairs else None,
                  'median reductions',{k:round(float(np.median([q['reduction_percent'] for q in v])),1) if v else None for k,v in result['evaluations'].items()},flush=True)
            (a.output/'summary.json').write_text(json.dumps(summary,indent=2))
    (a.output/'summary.json').write_text(json.dumps(summary,indent=2))

if __name__=='__main__':main()
