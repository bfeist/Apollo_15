import sys,json, itertools
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
from flutter_planning import inventory,read_channel
from flutter_tracking import discover, beeps
from continuous_tracking import track_array
root=Path(r'D:\Apollo_15_30-Track\orig_concatenated')
out=Path('reports/research_v5_allchannel_20261027/evidence')
allinv=[]
for d in sorted(root.iterdir()):
 if d.is_dir():
  try: allinv.append(inventory(d))
  except Exception as e: allinv.append(dict(tape=d.name,error=repr(e)))
(out/'tape_inventories.json').write_text(json.dumps(allinv,indent=2),encoding='utf-8')
inv=next(x for x in allinv if x['tape']=='A15_T510073')
windows=[]
for start in (0,10920,13140,22000):
 traces=[]; wav=[]; candidates=[]; events=[]; errors=[]
 for row in inv['channels']:
  ch=row['channel']
  try:
   x,sr,_=read_channel(row,start,start+40)
   y=signal.sosfiltfilt(signal.butter(4,[300,3000],btype='bandpass',fs=sr,output='sos'),x)
   wav.append((ch,signal.resample_poly(y,1,sr//4000)))
   events.append(dict(channel=ch,events=beeps(x,sr)))
   cs=discover(x,sr,limit=4)
   if ch==3 and start==13140 and not any(abs(c['carrier_hz']-117.5)<.01 for c in cs): cs=[dict(carrier_hz=117.5,manual_known=True)]+cs
   for c in cs:
    hz=c['carrier_hz']
    if hz<=40: continue
    cap=min(35.,hz*.45)
    t=np.arange(len(x))/sr
    z=signal.resample_poly(x*np.exp(-2j*np.pi*hz*t),1,sr//1000)
    z=signal.sosfiltfilt(signal.butter(4,cap,fs=1000,output='sos'),z)
    amp=float(np.median(abs(z[2000:-2000])))
    if amp<1e-10: continue
    for bandwidth in ([20.,35.] if ch==3 and abs(hz-117.5)<.01 and start==13140 else [cap]):
     v,s=track_array(x,sr,hz,bandwidth,amp)
     key=f'{ch}:{hz}:{bandwidth}'
     traces.append((key,ch,hz,v,s))
     candidates.append(dict(key=key,channel=ch,frequency=hz,capture=bandwidth,details=c,support_fraction=float(np.mean(s[400:-400])),motion_rms=float(np.std(v[400:-400]))))
  except Exception as e: errors.append(dict(channel=ch,error=repr(e)))
 pairs=[]
 for a,b in itertools.combinations(traces,2):
  if a[1]==b[1]: continue
  keep=(a[4]>0)&(b[4]>0); keep[:400]=False; keep[-400:]=False
  if keep.sum()<1000: continue
  av,bv=a[3][keep],b[3][keep]
  if np.std(av)<1e-6 or np.std(bv)<1e-6: continue
  corr=float(np.corrcoef(av,bv)[0,1])
  pairs.append(dict(a=a[0],b=b[0],correlation=corr,seconds=float(keep.sum()/200)))
 waveform=[]
 for a,b in itertools.combinations(wav,2):
  aa,bb=a[1],b[1]; den=np.linalg.norm(aa)*np.linalg.norm(bb)
  if den==0: continue
  cc=signal.correlate(aa,bb,mode='full',method='fft'); mid=len(bb)-1; section=cc[mid-200:mid+201]/den
  k=int(np.argmax(abs(section))); waveform.append(dict(a=a[0],b=b[0],correlation=float(section[k]),lag_seconds=(k-200)/4000))
 loss=[]
 if start==13140:
  for a in traces:
   if a[1]==3 and a[2]==117.5:
    for lo,hi in [(13150,13158),(13158,13163),(13163,13178)]:
     sl=slice((lo-start)*200,(hi-start)*200)
     loss.append(dict(key=a[0],interval=[lo,hi],support_fraction=float(np.mean(a[4][sl])),motion_rms=float(np.std(a[3][sl]))))
 windows.append(dict(start=start,duration=40,candidates=candidates,motion_pairs=pairs,waveform_pairs=waveform,events=events,loss=loss,errors=errors))
 print(start,len(candidates),len(pairs),len(errors),flush=True)
(out/'motion_experiment.json').write_text(json.dumps(dict(windows=windows,notes='Development exploratory correlations; lag is high-pass waveform similarity, not authenticated head offset. Shared feed and line noise can cause agreement. Correlation is not prediction or independent validation.'),indent=2),encoding='utf-8')
