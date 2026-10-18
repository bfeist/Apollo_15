"""Accepted CH14 reference correction and matched adjacent-channel listening report."""
import json
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal, ndimage
from scipy.integrate import cumulative_trapezoid
from flutter import read_excerpt, resample_at, estimate, positions
from audio_processing import remove
def burst_track(x,sr):
    """Wide capture: a badly fluttered beep need not remain near its nominal bin."""
    if sr%1000 or sr<6000:
        raise ValueError('Burst tracker requires a sample rate divisible by 1000 and >=6000 Hz')
    t=np.arange(len(x))/sr
    z=signal.resample_poly(x*np.exp(-2j*np.pi*2500*t),1,sr//1000)
    z=signal.sosfiltfilt(signal.butter(5,280,fs=1000,output='sos'),z)
    amp=ndimage.gaussian_filter1d(abs(z),2)
    freq=2500+np.gradient(np.unwrap(np.angle(z)))*1000/(2*np.pi)
    freq=ndimage.gaussian_filter1d(freq,3)
    return amp,freq



ROOT=Path(__file__).resolve().parents[1]
PRIOR=ROOT/'reports/accepted_flutter/inputs'
OUT=ROOT/'reports/accepted_flutter'
RATE=200


def synthetic():
    sr=8000;t=np.arange(12*sr)/sr
    truth=.025*np.sin(2*np.pi*6.5*t)
    tau=t-.025/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
    rng=np.random.default_rng(42)
    ref=np.sin(2*np.pi*631.5*tau)+2*np.sin(2*np.pi*600*t)+2*np.sin(2*np.pi*660*t)+.15*rng.normal(size=len(t))
    tt,e,meta,_=estimate(ref,sr);keep=(tt>2)&(tt<10)
    original=np.sin(2*np.pi*2475*tau);corrected=resample_at(original,positions(e,sr,len(t)))
    _,b=burst_track(original,sr);_,a=burst_track(corrected,sr)
    result=dict(correlation=float(np.corrcoef(e[keep],np.interp(tt[keep],t,truth))[0,1]),
        rendered_independent_tone_residual_ratio=float(np.std(a[2000:-2000])/np.std(b[2000:-2000])))
    assert result['correlation']>.95 and result['rendered_independent_tone_residual_ratio']<.4,result
    return result


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    meta=json.loads((PRIOR/'results.json').read_text());sources={c['channel']:c['source'] for c in meta['channels']}
    x,sr=read_excerpt(sources[14],80,50);tt,e,stats,freq=estimate(x,sr);pos=positions(e,sr,len(x))
    np.savez_compressed(OUT/'timing_map.npz',source_seconds=tt+80,speed_error=e,input_sample_positions=pos,raw_frequency_hz=freq)
    result=dict(acceptance=dict(date='2026-10-16',status='Accepted by user listening review; adopted as pipeline stage 05'),reference_source=sources[14],start=80,duration=50,tracking=stats,synthetic=synthetic(),examples=[],quindar_checks=[])
    cards=[]
    names={13:'SURGEON RT',15:'CAPCOM RT',14:'CAPCOM LT Â· reference track'}
    for ch in [13,15,14,12]:
        clean=np.load(PRIOR/f'clean_CH{ch:02d}.npy')
        assert len(clean)==len(x)
        # Check alignment metadata before transferring the common map.
        info=sf.info(sources[ch]);assert info.samplerate==sr and info.frames==sf.info(sources[14]).frames
        assert sources[ch].split('_212-')[1]==sources[14].split('_212-')[1]
        corrected=resample_at(clean,pos)
        if ch==12:
            bursts=json.loads((PRIOR/'tone_extrapolation.json').read_text())['fits'][:2]
            for q in bursts:
                lo=int((q['start']-80-.15)*sr);hi=int((q['end']-80+.15)*sr)
                _,b=burst_track(clean[lo:hi],sr);_,a=burst_track(corrected[lo:hi],sr)
                bt=80+lo/sr+np.arange(len(b))/1000
                mapped=80+np.interp((np.arange(len(a))/1000+lo/sr)*sr,np.arange(len(pos)),pos)/sr
                kb=(bt>q['start']+.05)&(bt<q['end']-.05)
                ka=(mapped>q['start']+.05)&(mapped<q['end']-.05)
                before=float(np.std(b[kb]));after=float(np.std(a[ka]))
                result['quindar_checks'].append(dict(start=q['start'],before_rms_hz=before,after_rms_hz=after,reduction_percent=100*(1-after/before)))
            continue
        b=clean[5*sr:40*sr];a=corrected[5*sr:40*sr];gain=.85/max(np.max(abs(b)),np.max(abs(a)),1e-12)
        files={}
        for label,audio in [('before',b),('after',a)]:
            name=f'CH{ch:02d}_{label}.wav';sf.write(OUT/name,audio*gain,sr,subtype='PCM_24');files[label]=name
            check,fs=sf.read(OUT/name);assert fs==sr and len(check)==35*sr and np.isfinite(check).all() and max(abs(check))<.851
        # Choose a speech-rich 12-second listening window from BEFORE only.
        offset=max(range(24),key=lambda k:float(np.sum(b[k*sr:(k+12)*sr]**2)))
        ab=f'CH{ch:02d}_before_then_after.wav'
        sf.write(OUT/ab,np.r_[b[offset*sr:(offset+12)*sr]*gain,np.zeros(sr),a[offset*sr:(offset+12)*sr]*gain],sr,subtype='PCM_24')
        result['examples'].append(dict(channel=ch,source=sources[ch],gain=gain,source_start=85,duration=35,short_start=85+offset,files=files,ab=ab))
        cards.append(f'<section><h2>CH{ch:02d} Â· {names[ch]}</h2><p>Quick comparison: before, one second silence, after. Source {85+offset}â€“{97+offset} seconds.</p><audio controls src="{ab}"></audio><p>Full 35 seconds Â· source 85â€“120 seconds</p><label>Before <audio controls src="{files["before"]}"></audio></label><label>After <audio controls src="{files["after"]}"></audio></label></section>')
    result['method']='CH14 original only; nearby 2 Hz hum notches; Â±40 Hz complex capture; amplitude-weighted robust 0.8-second local sine fits at CH14-derived rate; support gate, 4% speed bound, endpoint fade. Same inverse map on all channels; prior accepted cleaned arrays and one shared gain per pair. No Quindar tuning or target-derived lag.'
    (OUT/'results.json').write_text(json.dumps(result,indent=2))
    checks=''.join(f'<li>{q["start"]:.3f} s beep: {q["before_rms_hz"]:.1f} â†’ {q["after_rms_hz"]:.1f} Hz RMS ({q["reduction_percent"]:.1f}% reduction)</li>' for q in result['quindar_checks'])
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CH14 tone Â· adjacent-track flutter trial</title><style>body{font:17px system-ui;background:#101722;color:#e8edf5;max-width:960px;margin:40px auto;padding:20px}h1{color:#8ed8ff}p{line-height:1.6}section{background:#1c2939;padding:24px;border-radius:14px;margin:24px 0}audio{display:block;width:100%;margin:12px 0 24px}label{display:block}a{color:#8ed8ff}</style><h1>One tone, neighboring tracks</h1><p>Accepted by listening review on 2026-10-16. Correction derived only from the CH14 background tone. Listen for steadier voices, and for any added warbling. Before and after have identical noise cleanup and matched gain. CH13 and CH15 are the adjacent tracks; CH14 is included for comparison.</p>'''+''.join(cards)+f'<h2>Measured check</h2><ul>{checks}</ul><p>These two CH12 beeps were not used to fit the correction, but were inspected in earlier experiments. They do not establish improvement of independent voices across a whole tape. The local sinusoidal model can miss irregular flutter or confuse tone interference with speed variation. No full-tape processing changed.</p><p><a href="results.json">Measurements and method</a></p><script>document.addEventListener("play",e=>document.querySelectorAll("audio").forEach(a=>{{if(a!==e.target)a.pause()}}),true)</script></html>'
    (OUT/'index.html').write_text(html,encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
