"""Experimental excerpt cleanup, not a full-tape restoration command.

Run: uv run --with numpy --with scipy --with soundfile python scripts/preview_tape_cleanup.py
Noise profiles are unapproved quiet-window candidates. No speech detector is used.
"""
import html
import json
from pathlib import Path
from audio_processing import denoise
import numpy as np
import soundfile as sf
from scipy import signal, ndimage

OUT = Path(__file__).resolve().parents[1] / 'reports' / 'T510073_noise'

def dehum(x, sr, frequencies):
    y = x.copy()
    for f in frequencies:
        b, a = signal.iirnotch(f, f/1.5, fs=sr)
        y = signal.filtfilt(b, a, y)
    return y


def rms(x):
    return float(np.sqrt(np.mean(x*x)))

def plot_spectrum(series, sr):
    colors = ['#9ca3af','#38bdf8','#4ade80']
    parts = ['<svg viewBox="0 0 900 240" role="img" aria-label="Power spectra, 0 to 2000 Hz, minus 120 to minus 20 dB per Hz"><rect width="900" height="240" fill="#111827"/>']
    for hz in range(0,2001,250):
        px=40+hz/2000*840
        parts.append(f'<path d="M{px},10V210" stroke="#374151"/><text x="{px}" y="230" fill="#aaa" font-size="12">{hz}</text>')
    for y,c in zip(series,colors):
        f,p=signal.welch(y,sr,nperseg=sr*2)
        ids=np.where(f<=2000)[0]
        pts=' '.join(f'{40+f[i]/2000*840:.1f},{210-np.clip((10*np.log10(p[i]+1e-30)+120)/100,0,1)*200:.1f}' for i in ids)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="1"/>')
    return ''.join(parts)+'</svg>'

def main():
    probes=json.loads((OUT/'probe.json').read_text())
    selected=[('CH1_',0),('CH2_',0),('CH7_',0),('CH7_',2),('CH14_',2),('CH17_',2)]
    results=[]
    cards=[]
    for chan, epoch in selected:
        row=next(r for r in probes if chan in r['channel'])
        p=row['probes'][epoch]
        path=Path(p['file'])
        with sf.SoundFile(path) as f:
            sr=f.samplerate
            # Search this fragment for an active candidate using band energy variation.
            x=f.read(dtype='float64',always_2d=True)[:,0]
        sos=signal.butter(4,[350,3000],btype='bandpass',fs=sr,output='sos')
        band=signal.sosfiltfilt(sos,x)
        sec=np.mean(band[:len(band)//sr*sr].reshape(-1,sr)**2,axis=1)
        variability=ndimage.maximum_filter1d(sec, size=15)-ndimage.minimum_filter1d(sec,size=15)
        variability[:25]=0; variability[-25:]=0
        start=max(0,min(len(sec)-45,int(np.argmax(variability))-20))
        left=max(0,start-3); right=min(len(x),(start+48)*sr)
        padded=x[left*sr:right]
        frequencies=[]
        for hz in range(60,1201,60):
            found=[next((l['hz'] for l in pr['lines'] if abs(l['hz']-hz)<=1),None) for pr in row['probes']]
            good=[f for f in found if f is not None]
            if len(good)>=2:
                frequencies.append(float(np.median(good)))
        yh=dehum(padded,sr,frequencies)
        yn, profile=denoise(yh,sr,reduction_cap_db=6)  # Frozen historical comparison.
        a=(start-left)*sr; b=a+45*sr
        raw=padded[a:b]; yh=yh[a:b]; yn=yn[a:b]
        # One shared gain for all variants, including residuals; never normalize variants separately.
        variants={'original':raw,'dehum':yh,'dehum_denoise':yn,'removed_hum':raw-yh,'removed_denoise':yh-yn}
        gain=min(10**(30/20), 0.89/max(np.max(np.abs(v)) for v in variants.values()))
        stem=row['channel']+('_early' if epoch==0 else '_late')
        names={}
        for name, v in variants.items():
            assert len(v)==45*sr and np.all(np.isfinite(v))
            names[name]=f'{stem}_{name}.wav'
            sf.write(OUT/names[name],v*gain,sr,subtype='PCM_24')
        assert np.max(abs(raw-(yh+(raw-yh))))<1e-12
        # Quantify energy at selected lines, not an SNR or intelligibility score.
        f,pr=signal.welch(raw,sr,nperseg=sr*2)
        _,ph=signal.welch(yh,sr,nperseg=sr*2)
        mask=np.zeros(len(f),dtype=bool)
        for hz in frequencies: mask |= abs(f-hz)<=2
        reduction=float(10*np.log10(pr[mask].sum()/ph[mask].sum())) if mask.any() else 0
        result=dict(id=stem,source=str(path),offset_seconds=start,duration_seconds=45,sample_rate=sr,notches_hz=frequencies,notch_design_bandwidth_hz=1.5,zero_phase=True,shared_gain_db=float(20*np.log10(gain)),noise_profile_source_seconds=[left+t for t in profile],noise_profile_approved=False,line_band_reduction_db=reduction,files=names)
        results.append(result)
        opts=''.join(f'<option value="{v}">{k.replace("_"," ")}</option>' for k,v in names.items())
        cards.append(f'<section><h2>{stem}</h2><p>{html.escape(path.name)}<br>Excerpt: {start}–{start+45} seconds into fragment. Shared preview gain: {20*np.log10(gain):.1f} dB.</p><p>Notches: {frequencies} Hz. Selected line-band power reduction: {reduction:.1f} dB.</p><select onchange="swap(this)">{opts}</select><audio controls preload="none" src="{names["original"]}"></audio><p>Gray: original · Blue: dehum · Green: dehum + denoise. Spectrum 0–2000 Hz; vertical range −120 to −20 dB/Hz, before preview gain.</p>{plot_spectrum([raw,yh,yn],sr)}</section>')
        print(stem, 'notches',frequencies,'line reduction',round(reduction,1),flush=True)
    (OUT/'previews.json').write_text(json.dumps(results,indent=2))
    page='''<!doctype html><html><meta charset="utf-8"><title>Apollo 15 — T510073 cleanup trial</title><style>body{background:#0b1220;color:#e5e7eb;font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}section{background:#172033;padding:24px;margin:24px 0;border-radius:12px}p{line-height:1.6}select{padding:10px;margin-right:16px}audio{vertical-align:middle;width:60%}svg{width:100%}h1{color:#7dd3fc}</style><h1>T510073: cleanup comparison</h1><p>Experimental excerpts only. Originals are untouched. These are selected by signal statistics, not confirmed speech detection. The denoise profile is an unapproved quiet-window candidate; reduction is limited to 6 dB. No flutter correction or timing alignment has been applied.</p><p>Switch versions while playing to compare the same position. All variants within an excerpt use the same gain. Listen particularly to removed hum and removed denoise: speech there means the treatment needs revision. A lower noise level or cleaner spectrum does not prove better intelligibility.</p>'''+''.join(cards)+'''<script>function swap(s){let a=s.parentElement.querySelector('audio'),t=a.currentTime,p=!a.paused;a.pause();a.src=s.value;a.addEventListener('loadedmetadata',()=>{a.currentTime=Math.min(t,a.duration);if(p)a.play();},{once:true});a.load();}document.addEventListener('play',e=>{document.querySelectorAll('audio').forEach(a=>{if(a!==e.target)a.pause()})},true)</script></html>'''
    (OUT/'index.html').write_text(page,encoding='utf-8')
    # A sanity check on known synthetic components, independent of measured tape spectra.
    t=np.arange(10*8000)/8000
    x=np.sin(2*np.pi*60*t)+0.3*np.sin(2*np.pi*440*t)
    y=dehum(x,8000,[60])[16000:-16000]; tt=t[16000:-16000]
    a60=abs(2*np.mean(y*np.exp(-2j*np.pi*60*tt)))
    a440=abs(2*np.mean(y*np.exp(-2j*np.pi*440*tt)))
    assert a60<0.01 and abs(a440-0.3)<0.001,(a60,a440)
    print('PASS: known-tone rejection/preservation; excerpt lengths; finite samples; residual reconstruction')

if __name__=='__main__':
    main()
