"""Source-bound predictor-only characterization of stationary hum centers."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak
from flutter_tracking import trajectory


def run():
    passages=read(ROOT/'split_manifest.json')['development_passages']
    folder=begin('I004d','Known digitization lines may be stationary but offset from exact integer 60Hz harmonics',
        'Exact harmonic coherent projection versus measured narrow-capture center; predictor-only diagnostics',
        'Record frequency offsets/coherence without using response outcomes or changing current frozen challenger',
        passages=passages,channels=[3,14],harmonics_hz=[60.,120.],capture_hz=.5,
        assessment='Characterization only; no correction qualification')
    invs={i['tape']:i for i in read(ROOT/'inventory.json')['tapes']};rows=[];tick=time.perf_counter()
    for p in passages:
        inv=invs[p['tape']]
        for ch in (3,14):
            row=next(r for r in inv['channels'] if r['channel']==ch)
            x,sr,source=load_excerpt(inv,row,p['start_seconds'],p['start_seconds']+180,'original')
            t=np.arange(len(x))/sr
            for hz in (60.,120.):
                trace=trajectory(x,sr,hz,.5);centre=trace[-1]['centre_hz']
                a=float(2/len(x)*abs(np.dot(x,np.exp(-2j*np.pi*hz*t))))
                b=float(2/len(x)*abs(np.dot(x,np.exp(-2j*np.pi*centre*t))))
                item=dict(tape=p['tape'],channel=ch,start=p['start_seconds'],source=source,
                    nominal_hz=hz,measured_center_hz=centre,offset_hz=centre-hz,
                    exact_frequency_amplitude=a,measured_center_amplitude=b,
                    exact_over_center_coherent_amplitude=a/max(b,1e-12),
                    source_context_seconds=180,tracker_metadata=trace[-1])
                rows.append(item)
                print(p['tape'],ch,hz,centre,item['exact_over_center_coherent_amplitude'],flush=True)
    write(folder/'measurements.json',rows)
    exposure('I004d',[r['source'] for r in rows],'predictor_hum_frequency_features','Characterize stationary contamination; no response metrics or new predictor selection')
    ratios=[r['exact_over_center_coherent_amplitude'] for r in rows]
    finish(folder,dict(lines=len(rows),median_exact_over_center_amplitude=float(np.median(ratios)),
        minimum_exact_over_center_amplitude=min(ratios),maximum_abs_offset_hz=max(abs(r['offset_hz']) for r in rows),
        wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak()),
        'characterized_no_challenger_change','Fixed-frequency projection limitations measured on already exposed development predictors. Current real gates remain frozen.',
        'If real contamination remains limiting, predeclare a frequency-refined stationary extractor and test new synthetic offset-hum controls before a fresh real gate.')


if __name__=='__main__':run()
