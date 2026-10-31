import sys,json
from pathlib import Path
import numpy as np,soundfile as sf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from flutter_planning import inventory,read_channel
from flutter_tracking import discover
root=Path(r'D:\Apollo_15_30-Track'); inv=inventory(root/'orig_concatenated/A15_T510073'); results=[]
for start in (0,13140):
 rows=[]
 for row in inv['channels']:
  ch=row['channel']
  try:
   x,sr,_=read_channel(row,start,start+40)
   with sf.SoundFile(root/f'denoised/A15_T510073/CH{ch:02}/denoised.wav') as f:
    f.seek(start*sr); y=f.read(40*sr,dtype='float64')
   a=discover(x,sr,limit=8); b=discover(y,sr,limit=8)
   for c in b: c['near_original_candidate']=any(abs(c['carrier_hz']-v['carrier_hz'])<=3 for v in a)
   rows.append(dict(channel=ch,original=a,denoised=b))
  except Exception as e: rows.append(dict(channel=ch,error=repr(e)))
 results.append(dict(start=start,duration=40,channels=rows)); print(start,flush=True)
Path('reports/research_v5_allchannel_20261027/evidence/dual_discovery.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
