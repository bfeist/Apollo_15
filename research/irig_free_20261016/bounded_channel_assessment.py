"""Seven short mapped reads; no full-channel decoding or production imports."""
import os
for k in ['OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS']:os.environ[k]='1'
import json,sys,hashlib
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal

ROOT=Path(__file__).resolve().parents[2]
D=Path('D:/Apollo_15_30-Track');R=D/'defulterred_retimed/A15_T510083/CH20';F=D/'defluttered/A15_T510083/CH20'
OUT=ROOT/'reports/bounded_CH20_assessment_20261018'

def main():
    OUT.mkdir(exist_ok=True)
    ret=json.loads((R/'retime.json').read_text());rev=json.loads((F/'review.json').read_text());curve=json.loads((R/'curve.json').read_text());events=json.loads((R/'candidates.json').read_text())['events']
    assert ret['status']==rev['status']=='complete'
    nom=np.load(F/'nominal_seconds.npy',mmap_mode='r');tm=np.load(R/'timing_map.npz');it,ot=tm['input_seconds'],tm['output_seconds']
    def original_time(t):
        k=int(np.searchsorted(nom,t));lo=max(0,k-2);hi=min(len(nom),k+3)
        return float(np.interp(t,nom[lo:hi],np.arange(lo,hi)/200))
    def final_time(t):return float(np.interp(t,it,ot))
    files={'original':Path(rev['source']['path']),'denoised':Path(rev['denoised_directory'])/'denoised.wav','defluttered':F/'deflutter.wav','retimed':R/'retimed.wav'}
    before={k:(p.stat().st_size,p.stat().st_mtime_ns) for k,p in files.items()}
    anchors=curve['anchors'];chosen=[]
    for frac in [.02,.2,.4,.6,.8,.98]:
        a=min(anchors,key=lambda a:abs(a['input_seconds']-frac*rev['duration_seconds']))
        b=min(events,key=lambda e:abs((e['start_seconds']+e['end_seconds'])/2-a['input_seconds']))
        chosen.append(dict(center=a['input_seconds'],event=b,anchor=a,label='calibration_tone'))
    chosen.append(dict(center=13980.,label='approved_transition'))
    rows=[];traces=[];snippets=[]
    for i,c in enumerate(chosen):
        center=c['center'];start=center-7.5;end=center+7.5;bounds={'original':(original_time(start),original_time(end)),'denoised':(original_time(start),original_time(end)),'defluttered':(start,end),'retimed':(final_time(start),final_time(end))}
        row=dict(index=i,defluttered_center=center,label=c['label'],versions={});audio={};freqs={}
        for name,p in files.items():
            a,b=bounds[name]
            with sf.SoundFile(p) as f:
                sr=f.samplerate;first=round(a*sr);f.seek(first);x=f.read(round(b*sr)-first,dtype='float64')
            assert np.isfinite(x).all();audio[name]=x;t=first/sr+np.arange(len(x))/sr;dx=np.abs(np.diff(x));q=float(np.quantile(dx,.999))
            # Test exact 5-second render block boundaries against nearby sample differences.
            boundaries=np.arange(np.ceil(a/5)*5,b,5);j=np.rint(boundaries*sr-first).astype(int);j=j[(j>0)&(j<len(x))]
            checks=[dict(seconds=float((first+k)/sr),step=float(abs(x[k]-x[k-1])),relative_to_p999=float(abs(x[k]-x[k-1])/max(q,1e-12))) for k in j]
            m=dict(path=str(p),start_seconds=first/sr,duration_seconds=len(x)/sr,frames=len(x),sample_rate=sr,peak=float(np.max(abs(x))),samples_at_or_above_one=int(np.sum(abs(x)>=1)),adjacent_step_max=float(dx.max()),adjacent_step_p999=q,render_boundary_checks=checks,excerpt_sha256=hashlib.sha256(x.tobytes()).hexdigest())
            if 'event' in c:
                ev=c['event'];maptime=original_time if name in ['original','denoised'] else final_time if name=='retimed' else float
                ea,eb=maptime(ev['start_seconds']+.04),maptime(ev['end_seconds']-.04)
                z=signal.sosfiltfilt(signal.butter(5,280,fs=sr,output='sos'),x*np.exp(-2j*np.pi*2500*(t-t[0])))
                freq=2500+np.gradient(np.unwrap(np.angle(z)))*sr/(2*np.pi);freq=signal.sosfiltfilt(signal.butter(2,100,fs=sr,output='sos'),freq)
                keep=(t>ea)&(t<eb);v=freq[keep];amp=abs(z[keep]);cents=1200*np.log2(np.maximum(v,1)/np.median(v))
                m['tone']=dict(mean_hz=float(np.mean(v)),rms_hz=float(np.std(v)),rms_cents=float(np.std(cents)),minimum_to_median_amplitude=float(amp.min()/max(np.median(amp),1e-15)),nominal_hz=c['anchor']['nominal_hz'],mean_error_hz=float(np.mean(v)-c['anchor']['nominal_hz']),calibration_event=True)
                freqs[name]=(np.linspace(0,1,len(v)),cents)
            row['versions'][name]=m
        if 'event' in c:
            row['tone_rms_reduction_percent']=100*(1-row['versions']['defluttered']['tone']['rms_cents']/row['versions']['denoised']['tone']['rms_cents']);traces.append(freqs)
        if i in [2,6]:
            # Undo the known retiming normalization; one shared listening gain.
            audio['retimed']/=ret['normalization']['gain'];gain=.8/max(np.max(abs(audio[k])) for k in ['denoised','defluttered','retimed']);names=[]
            for name in ['denoised','defluttered','retimed']:
                p=OUT/f'sample_{i}_{name}.wav';sf.write(p,audio[name]*gain,sr,subtype='FLOAT');names.append(p.name)
            snippets.append(dict(index=i,shared_gain=gain,files=names))
        rows.append(row)
    assert before=={k:(p.stat().st_size,p.stat().st_mtime_ns) for k,p in files.items()}
    provenance=dict(file=str(files['retimed']),retime_method=ret['settings']['method'],experimental_transfer=rev['experimental_transfer'],target_actions=rev['target_actions'],stored_input_hash_matches=ret['input_audio']['decoded_float32_sha256']==rev['audio']['deflutter']['decoded_float32_sha256'],source_stat_matches=(files['defluttered'].stat().st_size,files['defluttered'].stat().st_mtime_ns)==(ret['source']['bytes'],ret['source']['mtime_ns']),map_hash_matches=hashlib.sha256((R/'timing_map.npz').read_bytes()).hexdigest()==ret['artifacts']['timing_map.npz'],retime_map_monotone=bool(np.all(np.diff(it)>0) and np.all(np.diff(ot)>0)),sources_unchanged=True,full_audio_hashes_recomputed=False,ai_audio_used=False,samples=rows,snippets=snippets)
    (OUT/'assessment.json').write_text(json.dumps(provenance,indent=2,allow_nan=False))
    sys.path.append(str(ROOT/'reports/nested_amplitude_20261017/plot_deps'));os.environ['MPLCONFIGDIR']=str(OUT/'plot_config')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(3,2,figsize=(10,8))
    for i,(ax,tr) in enumerate(zip(axes.flat,traces)):
        for name in ['denoised','defluttered','retimed']:ax.plot(*tr[name],label=name,lw=1)
        ax.set_title(f"{rows[i]['defluttered_center']:.1f}s, calibration event");ax.set_ylabel('Frequency deviation (cents)');ax.set_xlabel('Fraction of same tone interior');ax.grid(alpha=.2)
    axes.flat[0].legend(fontsize=8);fig.suptitle('Measured local pitch motion; each tone median removed');fig.tight_layout();fig.savefig(OUT/'tone_motion.png',dpi=140);plt.close(fig)
    lines=['# Bounded CH20 assessment — 2026-10-18','',f"Assessed `{files['retimed']}`.",'','Seven 15-second content windows: six indexed calibration tones spread across the recording and the approved transition near canonical 13,980 s. Approximately 105 seconds per version (<0.2% of the channel); four versions read by bounded seeks. Original/denoised content was located by inverting the saved deflutter map; retimed content used the saved second timing map. No full audio scan, production edits, uploads or audio AI evaluation.','', '## Measured tone results','','All six events were calibration anchors. These are rendering consistency checks, **not independent held-out retiming validation**. Existing 4.96 Hz block-cross-validation RMSE helped select smoothing; it is not a new untouched test set. Nominal tone-family assignments are inherited, not authenticated absolute clock references.','','| Defluttered time (s) | Clean RMS cents | Deflutter RMS cents | Retimed RMS cents | Flutter reduction | Retimed mean error (Hz) |','|---:|---:|---:|---:|---:|---:|']
    for r in rows[:6]:
        v=r['versions'];lines.append(f"| {r['defluttered_center']:.1f} | {v['denoised']['tone']['rms_cents']:.2f} | {v['defluttered']['tone']['rms_cents']:.2f} | {v['retimed']['tone']['rms_cents']:.2f} | {r['tone_rms_reduction_percent']:.1f}% | {v['retimed']['tone']['mean_error_hz']:.2f} |")
    lines+=['','Retiming intentionally raises mean pitch and shortens duration by 783.887 seconds. Fractional pitch-motion (cents) separates that mean-speed change from flutter reduction. Retiming is not expected to remove the remaining rapid wobble.','', '## Integrity and scope','',f"Saved upstream audio hashes agree: {provenance['stored_input_hash_matches']}; source size/mtime matches: {provenance['source_stat_matches']}; retiming map hash matches: {provenance['map_hash_matches']}; retiming map monotone: {provenance['retime_map_monotone']}. Full audio hashes were not recomputed.",'',f"Sampled clipped/full-scale samples: {sum(v['samples_at_or_above_one'] for r in rows for v in r['versions'].values())}. Boundary step measurements are in assessment.json; large differences can be genuine transients and are not automatically clicks. Absence of sampled spikes does not prove absence of artifacts or faint-speech loss.",'','This actual file used experimental transfer: 852 regional decisions needed transfer validation, 168 passed local numeric checks, and six were left uncorrected. It used the earlier single-channel curve, not the newer frozen tape-wide plan. Approval of one three-minute transition example does not validate the whole file.','', 'No supported audio-listening/model tool was available. Local signal analysis was used; no text-only model was treated as a listener. The most useful next step is matched listening to the two saved comparisons for voice naturalness and transition clicks before considering any broader analysis.','', '[Frequency-motion plot](tone_motion.png) · [Metrics and exact sample provenance](assessment.json) · [Two matched listening sets](index.html)']
    (OUT/'assessment.md').write_text('\n'.join(lines),encoding='utf-8')
    html=['<!doctype html><meta charset="utf-8"><title>Bounded CH20 review</title><h1>Two mapped comparisons</h1><p>Same content and shared gain. Retimed duration and pitch intentionally differ. No AI listening verdict.</p>']
    for s in snippets:
        html.append(f"<h2>Sample {s['index']}: defluttered centre {rows[s['index']]['defluttered_center']:.3f}s</h2>")
        for name in s['files']:html.append(f'<p>{name}</p><audio controls preload="none" src="{name}"></audio>')
    (OUT/'index.html').write_text('\n'.join(html),encoding='utf-8')
    print('\n'.join(lines[8:20]));print('Saved',OUT)

if __name__=='__main__':main()
