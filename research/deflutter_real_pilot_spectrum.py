"""Off-event harmonic pilot catalog, source features only on exposed audio."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import numpy as np
from scipy import signal
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure,memory_peak


def run():
    parent=ROOT/'iterations/I024_gate2';r=read(parent/'results.json')[0]
    folder=begin('I046_real_pilot_spectrum','Higher electrical harmonics can supply cleaner phase observations than the ambiguous fundamental',
        'Frozen original target, all harmonics1..12 equally cataloged; off-event300sHann spectrum, no timing curve, calibration loss or event-frequency outcome',
        dict(scope='Source features only; spectral presence is not pilot origin or correction qualification',budget='One300second8kHzoriginal array,1GiB/2minutes'),
        tape=r['tape'],target=r['target_channel'],pilot_search='60h+/-.15hHz, h1..12; no timing-reference allocation slots or special circuit labels')
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==r['tape']);ch=next(c for c in inv['channels'] if c['channel']==r['target_channel'])
    x,sr,source=load_excerpt(inv,ch,r['start'],r['end'],'original')
    if source['decoded_sha256']!=r['target_source']['decoded_sha256']:raise ValueError('Frozen target original binding changed')
    roles=read(parent/'roles_0.json')['roles'][str(r['target_channel'])];events=[e for values in roles.values() for e in values]
    t=np.arange(len(x))/sr;allowed=np.ones(len(x),bool)
    for e in events:allowed&=~((t>=e['start']-.55)&(t<=e['end']+.55))
    window=signal.windows.hann(len(x),sym=False)*allowed;amplitude=2*abs(np.fft.rfft(x*window))/np.sum(window);f=np.fft.rfftfreq(len(x),1/sr)
    rows=[]
    for h in range(1,13):
        k=np.flatnonzero(abs(f-60*h)<=.15*h);peaks,_=signal.find_peaks(amplitude[k],distance=3)
        chosen=sorted((int(k[i]) for i in peaks),key=lambda i:amplitude[i],reverse=True)[:3]
        background=(abs(f-60*h)>max(.15*h,.3))&(abs(f-60*h)<5)
        floor=float(np.median(amplitude[background]))
        rows.append(dict(harmonic=h,peaks=[dict(hz=float(f[i]),base_hz=float(f[i]/h),amplitude=float(amplitude[i]),
            contrast_db=float(20*np.log10(max(amplitude[i],1e-30)/max(floor,1e-30)))) for i in chosen],background_amplitude=floor))
    write(folder/'results.json',rows);write(folder/'source_manifest.json',dict(source=source,events=events,off_event_fraction=float(np.mean(allowed))))
    exposure(folder.name,[dict(source=source,features='Off-event low-band spectral amplitudes only')],'pilot_source_spectrum',
        'No timing map, beep-frequency error or candidate-outcome ranking; no real promotion')
    finish(folder,dict(harmonics=len(rows),peak_working_set_bytes=memory_peak(),new_real_frequency_outcomes_accessed=False,qualification_upgraded=False),
        'source_features_recorded','All12declared harmonic neighborhoods cataloged on the original; field ambiguity retained.',
        'Freeze a generalized harmonic finite-phase estimator on independent controls before testing real model-input availability; never tune against consumed event outcomes.')
    print([(a['harmonic'],[(b['hz'],round(b['amplitude'],6)) for b in a['peaks']]) for a in rows],flush=True)


if __name__=='__main__':run()
