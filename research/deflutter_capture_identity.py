"""Source-only spectral diagnosis of already-failed mixed mains captures.

No new frequency exclusion, oscillator authentication or correction decision.
Do not use these exposed examples as fresh qualification for a later method.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure
from deflutter_extractors import project_stationary_mains


def spectrum_measurements(x,sr,hz,capture):
    n=8*sr;nfft=2**int(np.ceil(np.log2(n)))
    f=np.fft.rfftfreq(nfft,1/sr);window=np.hanning(n)
    harmonic=60*round(hz/60)
    band=(f>=hz-capture)&(f<=hz+capture)
    search=np.flatnonzero((abs(f-harmonic)<=3)&band)
    rows=[]
    if not len(search):return dict(status='nearest_mains_not_in_capture')
    for a in range(0,len(x)-n+1,4*sr):
        p=abs(np.fft.rfft(x[a:a+n]*window,nfft))**2
        k=int(search[np.argmax(p[search])]);peak=float(f[k])
        if 0<k<len(f)-1:
            yy=np.log(p[k-1:k+2]+1e-300);den=yy[0]-2*yy[1]+yy[2]
            peak+=float(np.clip(.5*(yy[0]-yy[2])/den,-.5,.5)*(f[1]-f[0])) if den else 0
        core=(abs(f-peak)<=.5)&band
        rows.append(dict(start_seconds=a/sr,peak_near_harmonic_hz=peak,
            energy_fraction_within_half_hz=float(p[core].sum()/max(p[band].sum(),1e-300))))
    peaks=[r['peak_near_harmonic_hz'] for r in rows]
    fractions=[r['energy_fraction_within_half_hz'] for r in rows]
    return dict(nearest_nominal_mains_harmonic=harmonic,windows=rows,
        peak_median_hz=float(np.median(peaks)),peak_mad_hz=float(np.median(abs(np.asarray(peaks)-np.median(peaks)))),
        capture_energy_fraction_median=float(np.median(fractions)),
        capture_energy_fraction_p10=float(np.percentile(fractions,10)),
        caveat='Half-Hz energy accounting is diagnostic only, not a universal eligibility/exclusion band or proof of oscillator origin')


def run():
    folder=begin('I017_capture_identity',
        'Broad near-mains captures selected by failed gates are dominated by a stable contamination-compatible line',
        'Original, cached denoised and exact nominal stationary projection on three already-exposed failed references; source spectrum only',
        dict(interpretation='Measure peak stability and energy concentration; no source-origin proof, new exclusion width or correction qualification'),
        retrospective_development_only=True,response_outcomes_evaluated=False,
        inputs=['I014_gate0','I014_gate1','I014_gate2'])
    invs={i['tape']:i for i in read(ROOT/'inventory.json')['tapes']};out=[];tick=time.perf_counter()
    for iteration in ('I014_gate0','I014_gate1','I014_gate2'):
        result=read(ROOT/'iterations'/iteration/'results.json')[0];selected=result['selected']
        inv=invs[result['tape']];row=next(r for r in inv['channels'] if r['channel']==selected['reference_channel'])
        for version in ('original','denoised','original_stationary_projection'):
            source_version='original' if version=='original_stationary_projection' else version
            x,sr,source=load_excerpt(inv,row,result['start'],result['end'],source_version)
            if source_version=='original' and source['decoded_sha256']!=selected['source']['decoded_sha256']:
                raise ValueError('Selected original source differs from frozen gate')
            projection=None
            if version=='original_stationary_projection':
                x,projection=project_stationary_mains(x,sr,selected['carrier_hz'],selected['capture_hz'])
            record=dict(parent_iteration=iteration,tape=result['tape'],reference_channel=row['channel'],
                nominal_hz=selected['carrier_hz'],capture_hz=selected['capture_hz'],version=version,
                source=source,projection=projection,measured_sha256=sha(x.tobytes()),
                measurements=spectrum_measurements(x,sr,selected['carrier_hz'],selected['capture_hz']))
            out.append(record);write(folder/'results.json',out)
            print(iteration,version,{k:v for k,v in record['measurements'].items() if k not in ('windows','caveat')},flush=True)
    exposure(folder.name,[dict(tape=r['tape'],channel=r['reference_channel'],source=r['source']) for r in out],
        'predictor_spectra_only','Diagnose already-failed mixed captures, no response measurement or future gate selection')
    finish(folder,dict(source_spectra=len(out),wall_seconds=time.perf_counter()-tick,response_outcomes_evaluated=False,real_correction_qualified=0),
        'diagnostic_only','Source-bound spectral measurements retained. Dominance/steady peaks cannot authenticate historical motion or justify discarding nearby genuine tones.',
        'Finish unchanged I016 batch. A later general contamination-separation hypothesis needs independent controlled near-mains tones, hum-only and weak-carrier cases; do not fit these three failed gates.')


if __name__=='__main__':run()
