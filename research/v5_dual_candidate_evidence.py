import sys,json
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
from flutter_planning import inventory,read_channel
from flutter_tracking import beeps,evaluate_events
from continuous_tracking import track_array
root=Path(r'D:\Apollo_15_30-Track\orig_concatenated\A15_T510073'); inv=inventory(root); p=json.load(open('reports/research_v5_allchannel_20261027/evidence/dual_discovery.json')); win=p[1]; start=win['start']; xs={}
for r in inv['channels']: xs[r['channel']],sr,_=read_channel(r,start,start+40)
events={ch:[e for e in beeps(xs[ch],sr) if e['start']>.3 and e['end']<39.7] for ch in (13,14)}
res=[]
for row in win['channels']:
 for c in row.get('denoised',[]):
  if c['near_original_candidate']: continue
  hz=c['carrier_hz']; x=xs[row['channel']]; cap=min(35.,hz*.45); t=np.arange(len(x))/sr
  z=signal.resample_poly(x*np.exp(-2j*np.pi*hz*t),1,sr//1000); z=signal.sosfiltfilt(signal.butter(4,cap,fs=1000,output='sos'),z)
  amp=float(np.median(abs(z[2000:-2000]))); v,s=track_array(x,sr,hz,cap,amp)
  tests=[]
  for ch in (13,14):
   e=[e for e in events[ch] if np.all(s[max(0,int((e['start']-.15)*200)):min(len(s),int((e['end']+.15)*200))]>0)]
   met=evaluate_events(xs[ch],sr,v,e)
   rr=[m['residual_ratio'] for ev in met for m in ev['margins']]
   tests.append(dict(target=ch,events=len(met),median_ratio=float(np.median(rr)) if rr else None,worst_ratio=float(max(rr)) if rr else None))
  res.append(dict(channel=row['channel'],frequency=hz,capture=cap,support=float(np.mean(s[400:-400])),tests=tests))
Path('reports/research_v5_allchannel_20261027/evidence/dual_candidates_original_test.json').write_text(json.dumps(res,indent=2),encoding='utf-8')
