"""Reprocess the CH22 trial with residual-hum cleanup; reuse frozen flutter maps."""
from ch22_excerpt_trial import *
from audio_processing import denoise

PREVIOUS = DEST
OUT = ROOT/'reports/flutter_T510083_CH22_20261017_dehum'


def residual_hum_lines(x, sr):
    """Longer spectral windows expose persistent hum hidden from the 2 s detector."""
    f,t,p=signal.spectrogram(x,sr,nperseg=4*sr,noverlap=3*sr)
    db=10*np.log10(p+1e-30)
    background=ndimage.median_filter(db,size=(161,1))
    excess=np.median(db-background,axis=1)
    peaks,_=signal.find_peaks(excess,distance=6)
    lines=[]
    for k in peaks:
        hz=float(f[k]); occupancy=float(np.mean(db[k]-background[k]>4))
        if not (80<hz<1600 and abs(hz/60-round(hz/60))*60<3 and excess[k]>6 and occupancy>.6):
            continue
        lo=hi=k
        while lo>1 and excess[lo-1]>3 and k-lo<16:lo-=1
        while hi<len(f)-2 and excess[hi+1]>3 and hi-k<16:hi+=1
        width=float(np.clip((f[hi]-f[lo])*1.5,2,6))
        if hz<130:width=max(width,4.)
        lines.append(dict(hz=hz,width_hz=width,prominence_db=float(excess[k]),occupancy=occupancy))
    return lines


def fixed_profile_denoise(x,sr,profile,cap=3):
    _,times,z=signal.stft(x,sr,nperseg=512,noverlap=384)
    power=abs(z)**2;k=int(np.argmin(abs(times-profile[0])));width=round(4*sr/128)
    assert abs(times[k]-profile[0])<1e-8
    noise=np.mean(power[:,k:k+width],axis=1,keepdims=True)
    gain=np.maximum(np.sqrt(np.maximum(1-noise/(power+1e-20),0)),10**(-cap/20))
    gain=ndimage.gaussian_filter(gain,sigma=(1,2))
    _,y=signal.istft(z*gain,sr,nperseg=512,noverlap=384)
    return y[:len(x)]


def band_power(x,sr,lo,hi):
    f,p=signal.welch(x,sr,nperseg=4*sr)
    keep=(f>=lo)&(f<=hi)
    return float(10*np.log10(np.trapezoid(p[keep],f[keep])+1e-30))


def main():
    OUT.mkdir(exist_ok=True)
    previous_manifest_bytes=(PREVIOUS/'listening.json').read_bytes()
    previous=json.loads(previous_manifest_bytes)
    previous_hashes={v['file']:hashlib.sha256((PREVIOUS/v['file']).read_bytes()).hexdigest() for v in previous['validation']}
    sample,tape,by=inputs()
    raw,sr,source=read(Path(sample['source']),105,30)
    gain=10**(sample['shared_gain_db']/20)
    tonal=raw.copy()
    for p in sample['stage03_passes']:tonal=remove(tonal,sr,p['lines'])
    old_clean,old_profile=denoise(tonal,sr,reduction_cap_db=3)
    frozen_before,rate=sf.read(PREVIOUS/'before.wav')
    assert rate==sr and np.max(abs(old_clean*gain-frozen_before))<1e-7
    fixed_old=fixed_profile_denoise(tonal,sr,old_profile)
    assert np.max(abs(fixed_old-old_clean))<1e-12
    enhanced_tonal=tonal.copy(); extra_passes=[]
    for iteration in range(1,4):
        lines=residual_hum_lines(enhanced_tonal,sr)
        if not lines:break
        enhanced_tonal=remove(enhanced_tonal,sr,lines)
        extra_passes.append(dict(iteration=iteration,lines=lines))
    assert extra_passes
    enhanced=fixed_profile_denoise(enhanced_tonal,sr,old_profile)*gain
    clips={'before':enhanced,'previous_before':frozen_before,
           'removed_extra_hum':(tonal-enhanced_tonal)*gain,
           'removed_broadband':(enhanced_tonal*gain-enhanced)}
    maps=np.load(PREVIOUS/'listening_maps.npz')
    records=[]
    for old in previous['methods']:
        key=old['key'];pos=positions(maps[key],sr,len(enhanced))
        clips[key]=resample_at(enhanced,pos)
        old_audio,rate=sf.read(PREVIOUS/(key+'.wav'));assert rate==sr
        clips['previous_'+key]=old_audio
        clips[key+'_additional_removed']=old_audio-clips[key]
        metrics=fm_measure(enhanced,sr,maps[key],[dict(start=b['start'],end=b['end']) for b in old['events']],6.45)
        records.append(dict(key=key,label=old['label'],reference=old['reference'],settings=old['settings'],events=metrics,
                            map_sha256=hashlib.sha256(maps[key].tobytes()).hexdigest(),
                            previous_map_sha256=hashlib.sha256(maps[key].tobytes()).hexdigest()))
        clips[key+'_previous_then_reprocessed']=np.r_[old_audio,np.zeros(sr//2),clips[key]]
    validation=[]
    for name,x in clips.items():
        assert np.isfinite(x).all() and np.max(abs(x))<1
        path=OUT/(name+'.wav')
        if name.startswith('previous_'):
            copyfile(PREVIOUS/(name.removeprefix('previous_')+'.wav'),path)
        else:sf.write(path,x,sr,subtype='FLOAT')
        written,rate=sf.read(path)
        assert rate==sr and len(written)==len(x) and np.max(abs(written-x))<1e-7
        validation.append(dict(file=path.name,frames=len(written),sample_rate=rate,duration_seconds=len(written)/rate,
                               peak=float(np.max(abs(written))),write_error=float(np.max(abs(written-x))),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    removed,_=sf.read(OUT/'removed_extra_hum.wav');new_before,_=sf.read(OUT/'before.wav');broadband,_=sf.read(OUT/'removed_broadband.wav')
    reconstruction=float(np.max(abs(tonal*gain-new_before-removed-broadband)))
    assert reconstruction<2e-7
    measurements=[]
    for line in extra_passes[0]['lines']:
        hz=line['hz'];lo,hi=hz-2.5,hz+2.5
        a=band_power(frozen_before,sr,lo,hi);b=band_power(enhanced,sr,lo,hi)
        measurements.append(dict(hz=hz,band_hz=[lo,hi],previous_dbfs=a,reprocessed_dbfs=b,reduction_db=a-b))
    assert np.mean([m['reduction_db'] for m in measurements])>3
    speech_before=band_power(frozen_before,sr,300,3400);speech_after=band_power(enhanced,sr,300,3400)
    assert abs(speech_after-speech_before)<1
    for filename,digest in previous_hashes.items():assert hashlib.sha256((PREVIOUS/filename).read_bytes()).hexdigest()==digest
    assert (PREVIOUS/'listening.json').read_bytes()==previous_manifest_bytes
    result=dict(source=source,sample_id=sample['id'],canonical_clip_start_seconds=27105,duration_seconds=30,
                cleanup=dict(base_tonal_passes=sample['stage03_passes'],additional_hum_passes=extra_passes,
                             residual_hum_lines=residual_hum_lines(enhanced_tonal,sr),broadband_cap_db=3,
                             noise_profile_excerpt_seconds=old_profile,noise_profile_approved=False,
                             profile_policy='Same four-second source interval as the prior demo, recomputed after enhanced tonal cleanup.',
                             hum_detector='4 s spectrogram, 0.25 Hz bins; 161-bin local median background; median excess >6 dB, >4 dB in >60% of frames; peaks within 3 Hz of a 60 Hz harmonic, 80–1600 Hz; 2–6 Hz notches, at least 4 Hz near 120 Hz; at most three passes.'),
                original_reference_audio_preserved=True,flutter_maps_unchanged=True,
                previous_report='reports/flutter_T510083_CH22_20261017/index.html',previous_files_unchanged=True,
                shared_gain_db=sample['shared_gain_db'],additional_gain_db=0,methods=records,
                measurements=dict(hum_bands=measurements,speech_band_300_3400_dbfs_before=speech_before,speech_band_300_3400_dbfs_after=speech_after,
                                  interpretation='Band energy is not a speech-preservation or intelligibility score. Hum-like lines are selected spectrally; historical noise origin is not established.'),
                removed_signal_reconstruction_error=reconstruction,validation=validation,listening_approved=False)
    (OUT/'listening.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    methods={r['key']:r['label'] for r in records}
    methods['before']='Cleanup only · no flutter correction'
    options=''.join(f'<option value="{k}">{html.escape(v)}</option>' for k,v in methods.items())
    rows=''.join(f'<tr><td>{r["hz"]:.2f} Hz</td><td>{r["reduction_db"]:.1f} dB</td></tr>' for r in measurements)
    downloads=''.join(f'<li>{html.escape(r["label"])}: <a href="{r["key"]}.wav">Reprocessed</a> · <a href="{r["key"]}_previous_then_reprocessed.wav">Previous then reprocessed</a> · <a href="{r["key"]}_additional_removed.wav">Additional removed signal</a></li>' for r in records)
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>T510083 CH22 · enhanced hum cleanup</title>
<style>:root{color-scheme:dark;font:16px/1.6 system-ui;background:#0c1422;color:#edf2fa}body{max-width:1000px;margin:36px auto;padding:0 22px}section{padding:22px;background:#152338;border:1px solid #354761;border-radius:12px;margin:22px 0}h1{line-height:1.2}button,select{font:inherit;background:#223954;color:inherit;border:1px solid #728bac;border-radius:7px;padding:10px 14px;margin:4px;max-width:100%}button{cursor:pointer}button[aria-pressed=true]{background:#146b78;border-color:#abf2fa}audio{width:100%;margin:15px 0}a{color:#99dfef}table{border-collapse:collapse}th,td{text-align:left;padding:8px 20px;border-bottom:1px solid #43516a}.muted{color:#b9c8dc}#status{min-height:26px}</style>
<h1>T510083 CH22 · enhanced hum cleanup</h1><p>The same 30-second excerpt and the same flutter corrections, reprocessed with targeted residual-hum removal before the existing 3 dB broadband reduction. All versions retain the previous playback gain.</p>
<section><h2>Compare the cleanup</h2><label for="method">Flutter method</label> <select id="method">OPTIONS</select><p>Switch between the previous and reprocessed versions during the same phrase. Start with A, then compare B and C. D retains the earlier mixed-result flutter candidate.</p><div><button data-mode="new">Reprocessed · less hum</button><button data-mode="previous">Previous demo</button><button data-mode="removed">Additional removed signal</button></div><p id="status" aria-live="polite"></p><audio id="player" controls preload="metadata" src="adjacent_harmonic.wav"></audio><p><a id="download" href="adjacent_harmonic.wav" download>Download this WAV</a> · <a href="adjacent_harmonic_previous_then_reprocessed.wav">A: previous then reprocessed</a></p><label><input id="loop" type="checkbox"> Loop the excerpt</label></section>
<section><h2>What changed</h2><p>A longer spectral analysis exposed persistent hum-like lines missed by the previous detector. A targeted pass removes those lines and nearby low hum; broadband attenuation stays capped at 3 dB. The timing references remain untouched, and all four saved flutter maps are reused exactly.</p><table><thead><tr><th>Residual hum-like line</th><th>Additional reduction near line</th></tr></thead><tbody>ROWS</tbody></table><p class="muted">Measured before flutter correction over a ±2.5 Hz band. These measurements describe band energy; listen for voice preservation and remaining hum.</p><p><a href="removed_extra_hum.wav">Extra hum removed, before flutter</a> · <a href="removed_broadband.wav">Broadband removed, before flutter</a> · <a href="listening.json">Settings and verification</a> · <a href="../flutter_T510083_CH22_20261017/index.html">Previous report</a></p></section>
<section><h2>Download comparisons</h2><ul>DOWNLOADS</ul></section>
<section><h2>Workflow for long tapes</h2><p>Reassess reference quality and shared timing evidence throughout each tape. Different regions may need different reference channels, capture settings or correction techniques. Validate each region, record the choice, and check transitions for continuous timing and audible artifacts. A locally successful method does not establish one solution for an entire 17-hour tape.</p></section>
<script>const methods=METHODS;const player=document.querySelector('#player'),method=document.querySelector('#method'),status=document.querySelector('#status'),download=document.querySelector('#download');let mode='new',token=0;async function swap(){const my=++token,key=method.value,t=player.currentTime||0,playing=!player.paused;let file=key;if(mode==='previous')file='previous_'+key;if(mode==='removed')file=key==='before'?'removed_extra_hum':key+'_additional_removed';player.pause();player.src=file+'.wav';download.href=file+'.wav';status.textContent=methods[key]+' · '+({new:'Reprocessed',previous:'Previous demo',removed:'Additional removed signal'}[mode]);document.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mode===mode)));player.load();await new Promise((resolve,reject)=>{player.addEventListener('loadedmetadata',resolve,{once:true});player.addEventListener('error',reject,{once:true})});if(my!==token)return;player.currentTime=Math.min(t,player.duration-.001);if(playing)await player.play()}document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;swap().catch(()=>status.textContent='Unable to load audio. Use the download links below.')});method.onchange=()=>swap().catch(()=>status.textContent='Unable to load audio. Use the download links below.');document.querySelector('#loop').onchange=e=>player.loop=e.target.checked;status.textContent=methods.adjacent_harmonic+' · Reprocessed';document.querySelector('[data-mode=new]').setAttribute('aria-pressed','true');</script></html>'''
    (OUT/'index.html').write_text(page.replace('OPTIONS',options).replace('ROWS',rows).replace('DOWNLOADS',downloads).replace('METHODS',json.dumps(methods)),encoding='utf-8')
    print('Extra hum passes',[[l['hz'] for l in p['lines']] for p in extra_passes])
    print('Band reductions',[(m['hz'],round(m['reduction_db'],1)) for m in measurements])
    print('Speech-band energy change',round(speech_after-speech_before,3),'dB (not a preservation score)')
    print('Verified',len(validation),'audio files; unchanged prior exports and maps; residual reconstruction',reconstruction)


if __name__=='__main__':main()
