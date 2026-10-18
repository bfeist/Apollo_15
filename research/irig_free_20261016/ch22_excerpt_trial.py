"""Isolated T510083 CH22 listening experiment; source and prior reports stay frozen."""
import os
for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[key] = '1'
from canonical_bespoke import *
from cyclic_diagnose import trajectory, bound, fm_measure, harmonic_map
from shutil import copyfile
import html

DEST = ROOT / 'reports/flutter_T510083_CH22_20261017'
WINDOW_START = 27090.
DURATION = 60.
CLIP_OFFSET = 15.
CLIP_DURATION = 30.


def inputs():
    review = json.loads((ROOT/'reports/noise_review_20261017_speech/results.json').read_text())
    sample = next(s for s in review['samples'] if s['id'] == '13_A15_T510083_CH22')
    tape = next(t for t in json.loads((ROOT/'reports/bespoke_expanded_20261017/frozen_inventory.json').read_text()) if t['tape'] == 'A15_T510083')
    by = {c['channel']: c for c in tape['channels']}
    original, sr, meta = read(Path(sample['source']), sample['offset_seconds'], 30)
    canonical, rate, cm = get(by[22], tape, WINDOW_START, DURATION)
    assert rate == sr
    assert np.array_equal(original, canonical[15*sr:45*sr]), 'Excerpt mapping mismatch'
    assert hashlib.sha256(original.tobytes()).hexdigest() == sample['source_excerpt_sha256_float64']
    return sample, tape, by


def scan():
    DEST.mkdir(exist_ok=True)
    sample, tape, by = inputs()
    rows, sources, events, curves = [], [], [], {}
    for ch in sorted(by):
        x, sr, m = get(by[ch], tape, WINDOW_START, DURATION)
        sources.append(m)
        if ch == 1:
            continue  # Inventory IRIG, but never derive this experiment's correction from it.
        events.extend(dict(b, channel=ch) for b in beeps(x, sr))
        candidates = discover(x[:30*sr], sr, 6)
        if ch in (22, 27) and all(abs(c['carrier_hz']-(414.9 if ch == 22 else 118.5)) > 15 for c in candidates):
            candidates.append(dict(carrier_hz=414.9 if ch == 22 else 118.5, historical_candidate=True))
        for c in candidates:
            hz = c['carrier_hz']
            if 2150 < hz < 2850:
                continue
            capture = max(12, min(140, hz*.085))
            tr = trajectory(x, sr, hz, capture)
            tt, error, disp, amp, rate, meta = tr
            e = bound(signal.sosfiltfilt(signal.butter(3, [2,30], fs=200, btype='bandpass', output='sos'), error))
            f, p = signal.welch(e[400:-400], 200, nperseg=4000)
            sel = (f>=2)&(f<=20); fm = float(f[sel][np.argmax(p[sel])])
            concentration = float(p[abs(f-fm)<.5].sum()/max(p[sel].sum(),1e-20))
            key = f'ch{ch}_{hz:.1f}'
            curves[key] = e
            score = concentration*meta['support']*min(np.std(e[400:-400])/.004, 1)
            if hz<300: score *= .4
            rows.append(dict(c, **meta, channel=ch, key=key, rate_hz=fm, concentration=concentration, speed_rms_percent=float(np.std(e[400:-400])*100), score=score))
        print('SCANNED', ch, len(candidates), flush=True)
    for r in rows:
        peers = [(float(np.corrcoef(curves[r['key']][400:-400],curves[s['key']][400:-400])[0,1]),s['key']) for s in rows if s['channel']!=r['channel'] and abs(s['carrier_hz']-r['carrier_hz'])>80 and not(s['carrier_hz']<300 and r['carrier_hz']<300)]
        r['best_peer'] = max(peers) if peers else None
        r['score'] *= .5+max(0,r['best_peer'][0] if peers else 0)
    rows.sort(key=lambda r:r['score'],reverse=True)
    (DEST/'scan.json').write_text(json.dumps(dict(sample=sample, window_start=WINDOW_START, duration=DURATION, sources=sources, candidates=rows, events=events, note='All 30 tracks inventoried; CH1 IRIG excluded from reference estimation. Coherence is exploratory, not independent validation.'),indent=2))
    np.savez_compressed(DEST/'candidate_maps.npz', **curves)
    print('TOP', [(r['key'],round(r['rate_hz'],2),round(r['speed_rms_percent'],2),r['best_peer']) for r in rows[:15]])
    print('EVENTS',[(e['channel'],round(e['start'],2),round(e['end'],2)) for e in events])


def refine():
    sample, tape, by = inputs()
    scan = json.loads((DEST/'scan.json').read_text())
    x, sr, _ = get(by[22], tape, WINDOW_START, DURATION)
    events = [b for b in scan['events'] if b['channel']==22]
    calibration = [b for b in events if b['end']<CLIP_OFFSET]
    holdout = [b for b in events if CLIP_OFFSET<b['start'] and b['end']<45]
    rows, curves = [], {}
    # Explicit local follow-up: choose settings on the preceding event only.
    for ch in (21,22,27):
        ref, sr, _ = get(by[ch], tape, WINDOW_START, DURATION)
        for hz in (59.5,119.,414.9):
            for capture in (6.,9.,12.,18.,25.,40.):
                tr = trajectory(ref,sr,hz,capture)
                for band in ([4,10],[2,15],[2,30]):
                    e = bound(signal.sosfiltfilt(signal.butter(3,band,fs=200,btype='bandpass',output='sos'),tr[1]))
                    key=f'ch{ch}_{hz}_cap{capture}_band{band[0]}_{band[1]}'
                    metrics=fm_measure(x,sr,e,calibration,6.45)
                    loss=float(np.mean([b['margins'][str(m)]['after']['rms_hz'] for b in metrics for m in (.025,.04,.055)]))
                    rows.append(dict(key=key,channel=ch,hz=hz,capture=capture,band=band,calibration_loss_hz=loss,trajectory=tr[-1],calibration=metrics))
                    curves[key]=e
    rows.sort(key=lambda r:r['calibration_loss_hz'])
    # Freeze ranking before inspecting the two events in the requested clip.
    (DEST/'refinement_frozen.json').write_text(json.dumps(rows,indent=2))
    np.savez_compressed(DEST/'refinement_maps.npz',**curves)
    for row in rows[:12]:
        row['holdout']=fm_measure(x,sr,curves[row['key']],holdout,6.45)
        row['wrong_sign']=fm_measure(x,sr,-curves[row['key']],holdout,6.45)
        row['shift_71ms']=fm_measure(x,sr,np.roll(curves[row['key']],14),holdout,6.45)
    (DEST/'refinement_evaluation.json').write_text(json.dumps(rows[:12],indent=2))
    for r in rows[:12]:
        print(r['key'],'cal',round(r['calibration_loss_hz'],2),'holdout',[(round(b['margins']['0.04']['before']['rms_hz'],1),round(b['margins']['0.04']['after']['rms_hz'],1)) for b in r['holdout']],flush=True)


def alltrack_followup():
    sample,tape,by=inputs()
    scan=json.loads((DEST/'scan.json').read_text())
    x,sr,_=get(by[22],tape,WINDOW_START,DURATION)
    events=[b for b in scan['events'] if b['channel']==22]
    cal=[b for b in events if b['end']<15]
    hold=[b for b in events if 15<b['start'] and b['end']<45]
    rows=[];maps={};sources=[]
    for ch in sorted(by):
        if ch==1:continue
        ref,sr,source=get(by[ch],tape,WINDOW_START,DURATION);sources.append(source)
        candidates=discover(ref[:30*sr],sr,16)
        # Include the low-frequency candidate family across all tracks, not just neighbors.
        if all(abs(c['carrier_hz']-119)>8 for c in candidates):
            candidates.append(dict(carrier_hz=119.,explicit_low_carrier_followup=True))
        for c in candidates:
            hz=c['carrier_hz']
            if 2150<hz<2850:continue
            for capture in sorted({max(9,min(140,hz*.025)),max(12,min(140,hz*.085))}):
                tr=trajectory(ref,sr,hz,capture)
                for band in ([4,10],[2,15],[2,30]):
                    e=bound(signal.sosfiltfilt(signal.butter(3,band,fs=200,btype='bandpass',output='sos'),tr[1]))
                    key=f'ch{ch}_{hz:.1f}_cap{capture:.2f}_band{band[0]}_{band[1]}'
                    metrics=fm_measure(x,sr,e,cal,6.45)
                    loss=float(np.mean([b['margins'][str(m)]['after']['rms_hz'] for b in metrics for m in (.025,.04,.055)]))
                    rows.append(dict(key=key,channel=ch,carrier_hz=hz,capture=capture,band=band,calibration_loss_hz=loss,discovery=c,trajectory=tr[-1],calibration=metrics));maps[key]=e
        print('ALL-TRACK FOLLOW-UP',ch,'candidates',len(candidates),flush=True)
    rows.sort(key=lambda r:r['calibration_loss_hz'])
    (DEST/'alltrack_frozen.json').write_text(json.dumps(dict(sources=sources,ranking=rows,selection='Preceding CH22 check event only. Requested-clip outcomes do not choose rank. Discovery spectra first 30 seconds; references always original.'),indent=2))
    np.savez_compressed(DEST/'alltrack_maps.npz',**maps)
    for row in rows[:12]:
        e=maps[row['key']]
        row['holdout']=fm_measure(x,sr,e,hold,6.45)
        row['wrong_sign']=fm_measure(x,sr,-e,hold,6.45)
        row['shift_71ms']=fm_measure(x,sr,np.roll(e,14),hold,6.45)
        print(row['key'],'cal',round(row['calibration_loss_hz'],1),'hold',[(round(b['margins']['0.04']['after']['rms_hz'],1)) for b in row['holdout']],flush=True)
    (DEST/'alltrack_evaluation.json').write_text(json.dumps(rows[:12],indent=2))


def render():
    sample,tape,by=inputs()
    prior=ROOT/'reports/noise_review_20261017_speech'/sample['id']
    before,sr=sf.read(prior/'full_b3.wav',dtype='float64')
    original,rate=sf.read(prior/'off_b0.wav',dtype='float64')
    assert sr==rate and len(before)==240000
    alltrack=json.loads((DEST/'alltrack_evaluation.json').read_text())
    broad=np.load(DEST/'refinement_maps.npz')
    adjacent,sr,_=get(by[21],tape,WINDOW_START,DURATION)
    harmonic_curve=harmonic_map(trajectory(adjacent,sr,119.,9.),3)
    _,legacy_curve,_,_=estimate(adjacent,sr,119.,[])
    searched=np.load(DEST/'alltrack_maps.npz')
    settings=[
        ('adjacent_harmonic','A: CH21 harmonic tracking',harmonic_curve,dict(channel=21,carrier_hz=119,capture_hz=9,method='robust local displacement regression, three harmonics, locally measured rate')),
        ('adjacent_wide','B: CH21 wider signed-frequency tracking',broad['ch21_119.0_cap18.0_band2_15'],dict(channel=21,carrier_hz=119,capture_hz=18,method='signed fractional-frequency tracking, 2–15 Hz modulation band')),
        ('adjacent_legacy','C: CH21 accepted local-sinusoid technique',legacy_curve,dict(channel=21,carrier_hz=119,capture_hz=40,method='production local-sinusoid estimator, measured 4–10 Hz rate, no interference notches')),
        ('alltrack_control','D: All-channel search candidate (mixed result)',searched[alltrack[0]['key']],dict(channel=alltrack[0]['channel'],carrier_hz=alltrack[0]['carrier_hz'],capture_hz=alltrack[0]['capture'],method='calibration-ranked candidate from all 29 non-IRIG tracks',band=alltrack[0]['band']))]
    scan=json.loads((DEST/'scan.json').read_text())
    ev=[dict(b,start=b['start']-15,end=b['end']-15) for b in scan['events'] if b['channel']==22 and 15<b['start'] and b['end']<45]
    clips={'original':original,'before':before}; records=[]; saved_maps={}
    for key,label,full,meta in settings:
        e=full[3000:9000].copy()
        # The surrounding analysis has 2 s fades outside the requested passage.
        # The exact 30 s export uses its own short fades and duration-preserving map.
        tt=np.arange(len(e))/200
        e*=np.clip(np.minimum(tt,tt[-1]-tt)/.1,0,1)
        pos=positions(e,sr,len(before)); assert np.all(np.diff(pos)>0)
        clips[key]=resample_at(before,pos)
        saved_maps[key]=e
        metrics=fm_measure(before,sr,e,ev,6.45)
        controls={name:fm_measure(before,sr,curve,ev,6.45) for name,curve in [('wrong_sign',-e),('shift_71ms',np.roll(e,14))]}
        ref,sr,rm=get(by[meta['channel']],tape,WINDOW_START,DURATION)
        records.append(dict(key=key,label=label,settings=meta,reference=rm,events=metrics,controls=controls,speed_rms_percent=float(np.std(e)*100),max_speed_percent=float(np.max(abs(e))*100),input_position_bounds=pos[[0,-1]].tolist()))
    # One gain for every version; retain the existing review level whenever possible.
    gain=min(1.,.95/max(float(np.max(abs(v))) for v in clips.values()))
    validation=[]
    for key,clip in clips.items():
        path=DEST/(key+'.wav')
        if gain==1 and key in ('original','before'):
            copyfile(prior/('off_b0.wav' if key=='original' else 'full_b3.wav'),path)
        else:
            sf.write(path,clip*gain,sr,subtype='FLOAT')
        written,rate=sf.read(path,dtype='float64')
        assert rate==sr and len(written)==len(before) and np.isfinite(written).all()
        error=float(np.max(abs(written-clip*gain))); assert error<1e-7
        validation.append(dict(file=path.name,sample_rate=rate,frames=len(written),duration_seconds=len(written)/rate,peak=float(np.max(abs(written))),write_error=error,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    for r in records:
        key=r['key']; combined=np.r_[before*gain,np.zeros(sr//2),clips[key]*gain]
        pairpath=DEST/(key+'_before_then_after.wav')
        sf.write(pairpath,combined,sr,subtype='PCM_24')
        pair,rate=sf.read(pairpath)
        assert rate==sr and len(pair)==484000 and np.max(abs(pair-combined))<1.3e-7
        validation.append(dict(file=pairpath.name,sample_rate=rate,frames=len(pair),duration_seconds=len(pair)/rate,peak=float(np.max(abs(pair))),write_error=float(np.max(abs(pair-combined))),sha256=hashlib.sha256(pairpath.read_bytes()).hexdigest()))
        # Verify the check events against the actual exported waveform as well.
        written,_=sf.read(DEST/(key+'.wav'))
        pos=positions(saved_maps[key],sr,len(before))
        actual=[]
        for b in ev:
            lo=max(0,int((b['start']-.15)*sr)); hi=min(len(written),int((b['end']+.15)*sr))
            amp,freq=burst_track(written[lo:hi],sr); time=lo/sr+np.arange(len(freq))/1000
            mapped=np.interp(time*sr,np.arange(len(pos)),pos)/sr
            measured={str(m):float(np.std(freq[(mapped>b['start']+m)&(mapped<b['end']-m)])) for m in [.025,.04,.055]}
            expected=r['events'][len(actual)]['margins']
            assert all(abs(measured[str(m)]-expected[str(m)]['after']['rms_hz'])<.05 for m in [.025,.04,.055])
            actual.append(dict(start=b['start'],end=b['end'],rms_hz=measured))
        r['written_event_checks']=actual
    np.savez_compressed(DEST/'listening_maps.npz',**saved_maps)
    result=dict(sample_id=sample['id'],source=sample['source'],source_start_seconds=105,canonical_clip_start_seconds=27105,analysis_window_start_seconds=WINDOW_START,analysis_window_duration_seconds=DURATION,canonical_identity_verified=True,sample=sample,cleanup='Frozen full tonal + 3 dB broadband WAV from the existing review, before flutter',shared_additional_gain=gain,reference_search=dict(inventoried_channels=30,reference_channels=29,irig_excluded=True,candidate_count=len(json.loads((DEST/'alltrack_frozen.json').read_text())['ranking']),scope='All tracks of T510083 at the excerpt time; 60 seconds with surrounding context, not a full-duration tape survey'),methods=records,validation=validation,listening_approved=False,selection_notes='All-channel spectrum discovery and preceding-event ranking retained, including the mixed CH6 result. CH21 harmonic and capture alternatives were explored on the preceding check event; the two requested-clip events were evaluated afterward. Prior local-refinement holdout outcomes were already visible before the broader search, so this is exploratory follow-up, not a blind preregistered validation. No gains or phase offsets fitted to the requested clip.',boundary='Each 30-second export retains duration, with 100 ms endpoint fades on the correction. No absolute speed/GET restoration.')
    (DEST/'listening.json').write_text(json.dumps(result,indent=2))
    names={'original':'Original from noise review','before':'Before: full tonal + 3 dB broadband',**{r['key']:r['label'] for r in records}}
    buttons=''.join(f'<button data-key="{k}">{html.escape(v)}</button>' for k,v in names.items())
    rows=''
    for r in records:
        vals=[]
        for b in r['events']:
            m=b['margins']['0.04'];vals.append(f"{m['before']['rms_hz']:.1f} → {m['after']['rms_hz']:.1f}")
        rows+=f'<tr><td>{html.escape(r["label"])}</td><td>{"; ".join(vals)}</td><td><a href="{r["key"]}.wav">WAV</a> · <a href="{r["key"]}_before_then_after.wav">Before / after</a></td></tr>'
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>T510083 CH22 · flutter comparison</title>
<style>:root{color-scheme:dark;font:16px/1.6 system-ui;background:#0c1422;color:#edf2fa}body{max-width:1050px;margin:36px auto;padding:0 22px}h1{line-height:1.2}p{max-width:900px}section{padding:22px;background:#152338;border:1px solid #354761;border-radius:12px;margin:22px 0}button{font:inherit;background:#223954;color:inherit;border:1px solid #728bac;border-radius:7px;padding:10px 14px;margin:4px;cursor:pointer}button[aria-pressed=true]{background:#146b78;border-color:#abf2fa}audio{width:100%;margin:15px 0}a{color:#99dfef}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:12px 8px;border-bottom:1px solid #43516a}.muted{color:#b9c8dc}code{overflow-wrap:anywhere}details{margin-top:20px}#status{min-height:26px}</style>
<h1>T510083 · CH22 · flutter comparison</h1><p>The exact 30-second excerpt <strong>13_A15_T510083_CH22</strong>, with reference evidence searched across all 30 channels at the same tape time. IRIG is excluded from correction.</p>
<section><h2>Listen at the same point</h2><p>Start with “Before,” then try A, B and C during a sustained phrase. Switching versions keeps the playback position. Every corrected version starts with the same saved full-tonal + 3 dB cleanup and uses the same gain.</p><div>BUTTONS</div><p id="status" aria-live="polite"></p><audio id="player" controls preload="metadata" src="before.wav"></audio><p><a id="download" href="before.wav" download>Download this WAV</a></p><label><input id="loop" type="checkbox"> Loop the excerpt</label></section>
<section><h2>What the check tones show</h2><p>These are experimental listening candidates. The CH21 methods reduce measured variation, but leave substantial residual flutter. The all-channel calibration winner, D, improves one check tone and worsens the other. Check-tone measurements do not establish correction of every voice.</p><table><thead><tr><th>Method</th><th>Two check tones: before → after RMS Hz</th><th>Files</th></tr></thead><tbody>ROWS</tbody></table><p class="muted">40 ms event-edge margins. The first event is within the excerpt’s first second. Other margins, wrong-sign/time-shift controls, reference paths and rendered-file checks are saved in <a href="listening.json">listening.json</a>.</p></section>
<details><summary>Sources and method</summary><p>Canonical tape seconds 27105–27135; analysis seconds 27090–27150. The original delivery excerpt and canonical excerpt were checked for exact sample identity. These are file-relative times, not authenticated GET.</p><p>A: adjacent CH21, 119 Hz reference, ±9 Hz capture; robust local three-harmonic displacement tracking. B: CH21, 119 Hz reference, ±18 Hz capture; signed frequency motion at 2–15 Hz. C: existing local-sinusoid implementation, CH21 reference. D: CH6 candidate from the broader all-track search; kept as a mixed-result comparison.</p><p>No source, production processor or earlier listening report was changed. Baseline and original preserve the existing noise review’s gain. Correction maps have a ±4.9% limit, 100 ms endpoint fades and duration-preserving inverse resampling.</p><p><a href="scan.json">Initial all-channel scan</a> · <a href="alltrack_frozen.json">Broader search and calibration ranking</a> · <a href="alltrack_evaluation.json">Broader-search follow-up</a> · <a href="listening_maps.npz">Export correction curves</a></p></details>
<script>const names=NAMES;const player=document.querySelector('#player'),status=document.querySelector('#status'),download=document.querySelector('#download');let token=0;async function select(key){const my=++token,time=player.currentTime||0,playing=!player.paused;player.pause();player.src=key+'.wav';status.textContent=names[key];download.href=key+'.wav';document.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.key===key)));player.load();await new Promise((resolve,reject)=>{player.addEventListener('loadedmetadata',resolve,{once:true});player.addEventListener('error',reject,{once:true})});if(my!==token)return;player.currentTime=Math.min(time,player.duration-.001);if(playing)await player.play()}document.querySelectorAll('button').forEach(b=>b.onclick=()=>select(b.dataset.key).catch(()=>status.textContent='Unable to load audio. Open a WAV link below.'));document.querySelector('#loop').onchange=e=>player.loop=e.target.checked;status.textContent=names.before;document.querySelector('[data-key=before]').setAttribute('aria-pressed','true');</script></html>'''
    page=page.replace('BUTTONS',buttons).replace('ROWS',rows).replace('NAMES',json.dumps(names))
    (DEST/'index.html').write_text(page,encoding='utf-8')
    print('RENDERED',len(clips),'versions; gain',gain)
    for r in records:
        print(r['key'],[(round(b['margins']['0.04']['before']['rms_hz'],1),round(b['margins']['0.04']['after']['rms_hz'],1)) for b in r['events']])


if __name__ == '__main__':
    {'scan':scan,'refine':refine,'alltrack':alltrack_followup,'render':render}[sys.argv[1] if len(sys.argv)>1 else 'scan']()
