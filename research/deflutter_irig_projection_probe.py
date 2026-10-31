"""One frozen stationary-comb preprocessing test for coded-clock availability."""
import sys
sys.dont_write_bytecode=True
import time
from pathlib import Path
import numpy as np
from scipy import signal,optimize
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak,sha
from irig_clock import legacy_decoder,decode_excerpt,legacy_hashes


def stationary_projection(x,sr):
    t=np.arange(len(x))/sr;window=signal.windows.hann(len(x),sym=False)
    f=np.fft.rfftfreq(len(x),1/sr);a=abs(np.fft.rfft(x*window));band=np.flatnonzero((f>=55)&(f<=65))
    coarse=float(f[band[np.argmax(a[band])]])
    def objective(hz):
        basis=np.column_stack((np.cos(2*np.pi*hz*t),np.sin(2*np.pi*hz*t)))
        gram=basis.T@basis;b=basis.T@x
        return -float(b@np.linalg.solve(gram,b))
    fitted=optimize.minimize_scalar(objective,bounds=(max(55,coarse-2*sr/len(x)),min(65,coarse+2*sr/len(x))),
        method='bounded',options=dict(xatol=1e-8,maxiter=60))
    if not fitted.success:raise ValueError('Frozen lowband clock estimate failed')
    clock=float(fitted.x);harmonics=np.arange(1,int(3900//clock)+1)
    angles=2*np.pi*t[:,None]*(clock*harmonics[None,:]);basis=np.concatenate((np.cos(angles),np.sin(angles)),axis=1)
    del angles
    gram=basis.T@basis;condition=float(np.linalg.cond(gram))
    if condition>100:raise ValueError('Stationary projection ill conditioned')
    coefficients=np.linalg.solve(gram,basis.T@x);out=x-basis@coefficients
    return out,dict(methodology='global-stationary-comb-availability-v1',coarse_lowband_hz=coarse,clock_hz=clock,
        clock_search_hz=[55,65],clock_fit='Fundamental-only constant sinusoid; fixed bounded minimizer, no decoder/coded-band loss',
        maximum_harmonic_hz=3900,harmonics=harmonics.tolist(),coefficients=coefficients.tolist(),gram_condition=condition,
        original_rms=float(np.sqrt(np.mean(x*x))),residual_rms=float(np.sqrt(np.mean(out*out))),
        limitation='Global fixed comb can erase coincident code components; no timing correction, sliding fit or source authentication')


def run():
    scopes=[r['scope'] for r in read(ROOT/'iterations/I049_irig_availability/results.json')]
    folder=begin('I052_irig_projection','A fixed stationary electrical-comb projection may reveal structured code masked in the two declared excerpts',
        'Preservation controls first; one globally fixed lowband-fit harmonic subtraction then same six legacy attempts once/excerpt; no resampling, bandpass sweep or decoder tuning',
        dict(controls='AM2/5/8ms100pps waveforms at980.375/1006.125Hz normalized reconstruction error<=.15; comb-only cannot emit4valid sequential frames',
             availability='>=4valid progressing coded frames; then raw symbol/marker audit still required',
             stop='If controls or real decode fail, stop this path without retuning frequencies/widths/windows',budget='24real source seconds,1GiB/180s,oneworker'),scopes=scopes)
    started=time.perf_counter();sr=8000;t=np.arange(12*sr)/sr
    decoder=legacy_decoder(Path('F:/_repos/Apollo_Audio_Processing'));write(folder/'dependency_manifest.json',legacy_hashes())
    comb=sum(.15/k*np.cos(2*np.pi*60.018*k*t+.1*k) for k in range(1,65))
    rng=np.random.default_rng(5853001);control_rows=[]
    widths=np.array([2,5,8,2,8,5,5,2,8,2])/1000
    envelope=np.where(t% .01<widths[(np.floor(100*t).astype(int))%len(widths)],.1,.02)
    for carrier in (980.375,1006.125):
        truth=envelope*np.cos(2*np.pi*carrier*t+.3);out,meta=stationary_projection(truth+comb,sr)
        error=float(np.linalg.norm(out-truth)/np.linalg.norm(truth))
        control_rows.append(dict(kind='known_AM_symbol_transfer',carrier_hz=carrier,relative_error=error,passed=error<=.15,projection=meta))
    out,meta=stationary_projection(comb+rng.normal(0,1e-6,len(t)),sr)
    attempts=decode_excerpt(out,sr,0,decoder);longest=max(a['longest_run'] for a in attempts)
    control_rows.append(dict(kind='comb_only_false_frame_control',longest_run=longest,passed=longest<4,attempts=attempts,projection=meta))
    write(folder/'controls.json',control_rows)
    if not all(r['passed'] for r in control_rows):
        finish(folder,dict(controls_passed=False,real_waveform_accessed=False,wall_seconds=time.perf_counter()-started),
            'preprocessing_control_failed','Preservation/null prerequisite failed before real access.','Stop this preprocessing path; preserve failed controls, no decoder tuning.')
        return
    rows=[];sources=[]
    for scope in scopes:
        inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==scope['tape'])
        channel=next(c for c in inv['channels'] if c['channel']==scope['channel'])
        x,sr,source=load_excerpt(inv,channel,scope['start'],scope['end'],'original');sources.append(source)
        old=next(r for r in read(ROOT/'iterations/I049_irig_availability/results.json') if r['scope']==scope)
        if source['decoded_sha256']!=old['source']['decoded_sha256']:raise ValueError('I049 source excerpt changed')
        measured,projection=stationary_projection(x,sr)
        attempts=decode_excerpt(measured,sr,round(scope['start']*sr),decoder);longest=max(a['longest_run'] for a in attempts)
        rows.append(dict(scope=scope,source=source,projection=projection,attempts=attempts,longest_run=longest,
            processed_sha256=sha(measured.tobytes()),status='provisional_code_lead' if longest>=4 else 'unavailable_under_frozen_preprocessing'))
        write(folder/'results.json',rows);print(scope['tape'],longest,rows[-1]['status'],flush=True)
        if memory_peak()>2**30 or time.perf_counter()-started>180:raise RuntimeError('Declared projection probe budget exceeded')
    write(folder/'source_manifest.json',sources)
    exposure(folder.name,sources,'coded_clock_preprocessing_features','SameI049excerpts, fixedsourceonlyprojection, no target/map timing-error outcomes')
    finish(folder,dict(controls_passed=True,windows=len(rows),available_windows=sum(r['longest_run']>=4 for r in rows),
        wall_seconds=time.perf_counter()-started,peak_working_set_bytes=memory_peak(),new_target_frequency_outcomes_accessed=False),
        'code_lead_requires_raw_structure_audit' if any(r['longest_run']>=4 for r in rows) else 'stop_frozen_timecode_path',
        'One fixed preprocessing path, all attempts preserved. Negative is not archive-wide absence; positive is not transport authentication.',
        'Stop this path if unavailable. If code lead: audit raw marker/field evidence and transfer provenance before any timing correction test.')


if __name__=='__main__':run()
