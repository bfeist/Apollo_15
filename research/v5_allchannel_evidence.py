import sys, json
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from _bootstrap import install as _install
_install()
from flutter_planning import inventory, read_channel

root = Path(r'D:\Apollo_15_30-Track\orig_concatenated\A15_T510073')
out = Path('reports/research_v5_allchannel_20261027/evidence/allchannel_spectra.json')
inv = inventory(root)
results = []
for start in (0, 10920, 13150, 22000):
    rows=[]
    for ch in inv['channels']:
        try:
            x,sr,_=read_channel(ch,start,start+20)
            f,p=signal.welch(x,fs=sr,nperseg=sr*2)
            peaks,_=signal.find_peaks(p)
            peaks=peaks[(f[peaks]>=40)&(f[peaks]<=3000)]
            top=sorted(peaks,key=lambda i:p[i],reverse=True)[:8]
            rows.append(dict(channel=ch['channel'], rms=float(np.sqrt(np.mean(x*x))), peaks=[dict(hz=float(f[i]),power=float(p[i])) for i in top]))
        except Exception as e:
            rows.append(dict(channel=ch['channel'],error=repr(e)))
    results.append(dict(start=start,duration=20,channels=rows))
out.write_text(json.dumps(dict(inventory=inv,windows=results,interpretation='Spectral peaks are discovery only; not independent or validated timing references.'),indent=2),encoding='utf-8')
print(out)
