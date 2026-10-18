"""Iteratively remove persistent narrow tones across the full recorded band.

Excerpt experiment: preserves original sources and previous preview files.
Run with uv run --with numpy --with scipy --with soundfile python scripts/iterate_tonal_cleanup.py
"""
import json
from pathlib import Path
from audio_processing import detect, remove
import numpy as np
import soundfile as sf
from scipy import signal, ndimage
from preview_tape_cleanup import denoise

ROOT = Path(__file__).resolve().parents[1] / 'reports' / 'T510073_noise'
OUT = ROOT/'iteration2'



def spectrum(x,sr):
    f,p=signal.welch(x,sr,nperseg=sr*2)
    return f,10*np.log10(p+1e-30)

def chart(series,sr):
    parts=['<svg viewBox="0 0 1000 260"><rect width="1000" height="260" fill="#111827"/>']
    for hz in range(0,4001,500):
        px=40+hz/4000*920
        parts.append(f'<path d="M{px},10V230" stroke="#374151"/><text x="{px}" y="250" fill="#aaa" font-size="12">{hz}</text>')
    for x,c in zip(series,['#9ca3af','#fbbf24','#4ade80']):
        f,db=spectrum(x,sr)
        pts=' '.join(f'{40+hz/4000*920:.2f},{230-np.clip((v+130)/110,0,1)*210:.2f}' for hz,v in zip(f,db))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="0.8"/>')
    return ''.join(parts)+'</svg>'

def main():
    OUT.mkdir(exist_ok=True)
    rows=json.loads((ROOT/'previews.json').read_text())
    results=[]; cards=[]
    for row in rows:
        sr=row['sample_rate']; start=row['offset_seconds']; length=row['duration_seconds']
        left=max(0,start-5)
        with sf.SoundFile(row['source']) as f:
            f.seek(left*sr)
            raw=f.read((start-left+length+5)*sr,dtype='float64')
        a=(start-left)*sr; b=a+length*sr
        y=raw.copy(); passes=[]; first=detect(y,sr)
        for iteration in range(1,7):
            lines=detect(y,sr)
            if not lines: break
            y=remove(y,sr,lines)
            passes.append(dict(iteration=iteration,lines=lines))
        residual=detect(y,sr)
        clean,profile=denoise(y,sr,reduction_cap_db=6)  # Frozen historical comparison.
        gain=10**(row['shared_gain_db']/20)
        original=raw[a:b]*gain; tonal=y[a:b]*gain; clean=clean[a:b]*gain
        previous,_=sf.read(ROOT/row['files']['dehum_denoise'])
        variants={'new_cleaned':clean,'previous_cleaned':previous,'original':original,
                  'new_tonal_only':tonal,'removed_tones':original-tonal,'removed_broadband':tonal-clean}
        peak=max(float(np.max(np.abs(v))) for v in variants.values())
        # Preserve the earlier comparison gain. Float WAV avoids any clipping of residuals.
        files={}
        for name,v in variants.items():
            assert len(v)==sr*length and np.all(np.isfinite(v))
            files[name]=f'{row["id"]}_{name}.wav'
            sf.write(OUT/files[name],v,sr,subtype='FLOAT')
        assert np.max(abs(original-(tonal+variants['removed_tones'])))<1e-12
        f,old=spectrum(original,sr); _,new=spectrum(tonal,sr)
        reductions=[dict(hz=l['hz'],reduction_db=float(old[np.argmin(abs(f-l['hz']))]-new[np.argmin(abs(f-l['hz']))])) for l in first]
        result=dict(id=row['id'],source=row['source'],offset_seconds=start,duration_seconds=length,
                    sample_rate=sr,shared_gain_db=row['shared_gain_db'],passes=passes,residual_lines=residual,
                    original_line_reductions=reductions,peak=peak,files=files,
                    noise_profile_source_seconds=[left+t for t in profile],noise_profile_approved=False)
        results.append(result)
        options=''.join(f'<option value="{v}">{k.replace("_"," ")}</option>' for k,v in files.items())
        high=[round(l['hz'],1) for l in first if l['hz']>1000]
        cards.append(f'<section><h2>{row["id"]}</h2><p>{len(first)} initial persistent lines; {len(passes)} processing rounds; {len(residual)} remaining above the detection threshold. High-frequency targets: {high} Hz.</p><select onchange="swap(this)">{options}</select><audio controls preload="none" src="{files["new_cleaned"]}"></audio><p>Gray: original · Yellow: previous cleanup · Green: new cleanup. 0–4000 Hz, −130 to −20 dB/Hz.</p>{chart([original,previous,clean],sr)}</section>')
        print(row['id'], 'initial',len(first),'passes',[len(p['lines']) for p in passes], 'remaining',len(residual),'high',high,'peak',round(peak,3),flush=True)
    (OUT/'results.json').write_text(json.dumps(results,indent=2))
    page='''<!doctype html><html><meta charset="utf-8"><title>T510073 iterative tonal cleanup</title><style>body{background:#0b1220;color:#e5e7eb;font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}section{background:#172033;padding:24px;margin:24px 0;border-radius:12px}p{line-height:1.6}select{padding:10px;margin-right:16px}audio{vertical-align:middle;width:60%}svg{width:100%}h1{color:#7dd3fc}</style><h1>Iterative hum and ringing removal</h1><p>New cleanup scans the entire recorded frequency band, including non-harmonic ringing. Each pass detects persistent narrow peaks, filters them, and measures the residual again. Broadband reduction remains limited to 6 dB. Original source files and prior previews are unchanged.</p><p>Switch versions at the same playback position. All versions retain the original preview gain. Removed tones and removed broadband are available for checking speech loss. Zero detected peaks means below this detector's threshold, not proof of complete audible noise removal. Persistent useful tones could also be removed; these are review excerpts.</p><p><a href="../index.html">Previous comparison</a></p>'''+''.join(cards)+'''<script>function swap(s){let a=s.parentElement.querySelector('audio'),t=a.currentTime,p=!a.paused;a.pause();a.src=s.value;a.addEventListener('loadedmetadata',()=>{a.currentTime=Math.min(t,a.duration);if(p)a.play();},{once:true});a.load();}document.addEventListener('play',e=>{document.querySelectorAll('audio').forEach(a=>{if(a!==e.target)a.pause()})},true)</script></html>'''
    (OUT/'index.html').write_text(page,encoding='utf-8')
    print('Verified duration, finite samples, and residual reconstruction for all excerpts.')

if __name__=='__main__': main()
