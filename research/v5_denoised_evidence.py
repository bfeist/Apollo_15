import sys,json
from pathlib import Path
import numpy as np, soundfile as sf
from scipy import signal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from flutter_planning import inventory,read_channel
from flutter_tracking import beeps,evaluate_events
from continuous_tracking import track_array
root=Path(r'D:\Apollo_15_30-Track'); inv=inventory(root/'orig_concatenated/A15_T510073'); out=[]
for start in (10920,13140):
 xs={}
 for ch in (1,3,5,13,14):
  row=next(r for r in inv['channels'] if r['channel']==ch); orig,sr,_=read_channel(row,start,start+40)
  with sf.SoundFile(root/f'denoised/A15_T510073/CH{ch:02}/denoised.wav') as f:
   f.seek(start*sr); den=f.read(40*sr,dtype='float64')
  xs[ch]={'original':orig,'denoised':den}
  spectra={}
  for kind,x in xs[ch].items():
   freq,p=signal.welch(x,sr,nperseg=sr*4)
   spectra[kind]={str(h):float(p[np.argmin(abs(freq-h))]) for h in (60,117.5,120,180,300)}
  out.append(dict(start=start,channel=ch,spectra=spectra))
 if True:
  traces={}
  for ch in (1,3,5):
   for kind,x in xs[ch].items():
    t=np.arange(len(x))/sr; z=signal.resample_poly(x*np.exp(-2j*np.pi*117.5*t),1,sr//1000)
    z=signal.sosfiltfilt(signal.butter(4,35,fs=1000,output='sos'),z); amp=float(np.median(abs(z[2000:-2000])))
    for cap in (20,35):
     v,s=track_array(x,sr,117.5,cap,amp); traces[f'{ch}_{kind}_{cap}']=(v,s)
  comparisons=[]
  for key,(v,s) in traces.items():
   ref,rs=traces['3_original_35']; k=(s>0)&(rs>0); k[:400]=False;k[-400:]=False
   if k.sum()>200: comparisons.append(dict(key=key,seconds=float(k.sum()/200),correlation=float(np.corrcoef(v[k],ref[k])[0,1]),rms=float(np.std(v[k])),support=float(np.mean(s[400:-400]))))
  evaluations=[]
  for key in ('3_original_20','3_original_35','3_denoised_20','3_denoised_35'):
   v,s=traces[key]
   for ch in (13,14):
    ev=beeps(xs[ch]['original'],sr)
    ev=[e for e in ev if e['start']>.3 and e['end']<39.7]
    evaluations.append(dict(reference=key,target=ch,events=evaluate_events(xs[ch]['original'],sr,v,ev)))
  out.append(dict(start=start,motion=comparisons,evaluations=evaluations))
Path('reports/research_v5_allchannel_20261027/evidence/denoised_comparison.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
