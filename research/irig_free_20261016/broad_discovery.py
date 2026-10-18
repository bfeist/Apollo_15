"""Finite all-channel snapshot screening; quick phase tracks are NOT correction maps."""
from generic_scan import *
from time import perf_counter
from datetime import datetime,timezone
BASE=ROOT/'reports/irig_free_research_20261016'

def quick_track(x,sr,c):
    hz=c['carrier_hz'];tt=np.arange(len(x))/sr
    # No carrier preference, no fitted motion or target outcome. Broad capture preserves sidebands.
    z=signal.resample_poly(x*np.exp(-2j*np.pi*hz*tt),1,sr//200)
    z=signal.sosfiltfilt(signal.butter(6,40,fs=200,output='sos'),z)
    amp=abs(z);freq=hz+np.gradient(np.unwrap(np.angle(z)))*200/(2*np.pi)
    centre=float(np.median(freq[400:-400]));raw=(freq-centre)/max(abs(centre),1.)
    valid=(amp>max(np.median(amp)*.3,1e-12))&(abs(freq-centre)<45)
    # Explicitly report bad phase support; clipping is a numerical diagnostic bound, not trust.
    clipped=np.clip(raw,-.08,.08)
    curve=signal.sosfiltfilt(signal.butter(3,[4,10],fs=200,btype='bandpass',output='sos'),clipped)
    f,p=signal.welch(clipped[400:-400],200,nperseg=min(2000,len(clipped)-800))
    band=(f>=4)&(f<=10);wide=(f>=1)&(f<=30)
    concentration=float(p[band].sum()/max(p[wide].sum(),1e-20));rms=float(np.std(curve[400:-400])*100)
    support=float(valid[400:-400].mean());clip=float(np.mean(abs(raw[400:-400])>.04))
    eligible=c['occupancy']>=.7 and support>=.8 and .1<=rms<=3 and clip<=.05 and concentration>=.2
    return dict(c,centre_hz=centre,modulation_peak_hz=float(f[band][np.argmax(p[band])]),
        rms_percent=rms,phase_support=support,raw_over_4percent_fraction=clip,modulation_concentration=concentration,
        screening_eligible=bool(eligible),analysis='quick_phase_screening_not_accepted_estimator'),curve

def reusable(prior,tape,channel,start):
    r=next((r for r in prior if r['tape']==tape and abs(r['start']-start)<.001),None)
    if r is None:return None
    cov=next((c for c in r['coverage'] if c['channel']==channel['channel'] and c['status']=='sampled'),None)
    if cov is None:return None
    for s in cov['sources']:
        p=Path(s['path']);st=p.stat()
        if st.st_size!=s['bytes'] or st.st_mtime_ns!=s['mtime_ns']:return None
        matching=[seg for seg in channel['segments'] if seg['path']==s['path'] and abs(seg['start']+s['start_seconds']-seg.get('source_start',0)-s['stream_start'])<1e-6]
        if len(matching)!=1:return None
    return dict(start=start,duration=r['duration'],status='reused_audio_analysis',sources=cov['sources'],
        analysis='previous_generic_full_tracker',candidates=[c for c in r['candidates'] if c['channel']==channel['channel']],
        prior_window_status=r['status'],prior_target_summary=r.get('target_summary'),prior_selected_channel=r['selected']['channel'] if r['selected'] else None)

def try_window(channel,start,duration,already):
    choices=[(start,'common_window')]
    # Fallback is deterministic and records its own time; never pretend it aligns with peers.
    segs=[s for s in channel['segments'] if s.get('eligible_age',True) and s['duration']>=duration+4]
    segs.sort(key=lambda s:abs(s['start']+s['duration']/2-(start+duration/2)))
    extra=sorted(segs,key=lambda s:s['start'])
    choicesegs=segs[:3]+(extra[:1]+extra[-1:] if extra else [])
    for s in choicesegs:
        pos=s['start']+min(max(2,start-s['start']),s['duration']-duration-2)
        if all(abs(pos-x)>1 for x in already):choices.append((pos,'fallback_available_segment'))
        elif s['duration']>=duration+4:
            # If both common tape windows fall beyond a short available channel,
            # sample a distinct interior point rather than counting the same excerpt twice.
            alternate=s['start']+(s['duration']-duration)/2
            if all(abs(alternate-x)>1 for x in already):choices.append((alternate,'fallback_distinct_interior'))
    errors=[]
    for pos,kind in choices:
        if any(abs(pos-x)<1 for x in already):continue
        try:
            x,sr,sources=stream_read(channel,pos,duration)
            return x,sr,dict(start=pos,duration=duration,status='analyzed_audio',sampling=kind,requested_start=start,sources=sources,prior_attempts=errors)
        except (ValueError,OSError,RuntimeError) as ex:errors.append(dict(start=pos,reason=str(ex)))
    return None,None,dict(start=start,duration=duration,status='skipped_unavailable_or_incomplete',attempts=errors)

def run_tape(tape,prior,out,duration=30):
    started=perf_counter();ends=[s['start']+s['duration'] for c in tape['channels'] for s in c['segments']];horizon=max(ends,default=0)
    previous=sorted([r['start'] for r in prior if r['tape']==tape['tape']])
    starts=previous if len(previous)==2 else [round(x*max(0,horizon-duration),3) for x in [.15,.85]]
    reports={c['channel']:dict(channel=c['channel'],representation=c['representation'],issues=c['issues'],
        gaps=c['gaps'],unmapped_sources=c.get('unmapped_sources',[]),file_count=c['discovered_chunk_files']+c['discovered_canonical_files'],windows=[]) for c in tape['channels']}
    candidate_id=0;coherence_records=[];shortlist=[]
    for window_index,start in enumerate(starts):
        curves={};screen=[]
        for c in tape['channels']:
            ch=c['channel'];reused=reusable(prior,tape['tape'],c,start)
            if reused:
                reports[ch]['windows'].append(reused);continue
            used=[w['start'] for w in reports[ch]['windows'] if w['status'] in ['analyzed_audio','reused_audio_analysis']]
            x,sr,row=try_window(c,start,duration,used)
            row['candidates']=[]
            if x is not None:
                for cand in discover(x[:len(x)//2],sr,2):
                    rec,curve=quick_track(x,sr,cand);rec.update(id=candidate_id,channel=ch,window_start=row['start'],window_duration=duration)
                    row['candidates'].append(rec);screen.append(rec);curves[candidate_id]=curve;candidate_id+=1
            reports[ch]['windows'].append(row)
        for i,c in enumerate(screen):
            peers=[]
            if c['screening_eligible']:
                for d in screen:
                    if d['id']==c['id'] or d['channel']==c['channel'] or not d['screening_eligible'] or abs(d['window_start']-c['window_start'])>.001:continue
                    if abs(d['carrier_hz']-c['carrier_hz'])<80:continue
                    ratio=max(d['carrier_hz'],c['carrier_hz'])/min(d['carrier_hz'],c['carrier_hz'])
                    if c['near_mains_family'] and d['near_mains_family'] or abs(ratio-round(ratio))<.03:continue
                    a=curves[c['id']];b=curves[d['id']];mid=len(a)//2
                    discovery=float(np.corrcoef(a[400:mid-200],b[400:mid-200])[0,1]);heldout=float(np.corrcoef(a[mid+200:-400],b[mid+200:-400])[0,1])
                    peers.append(dict(channel=d['channel'],carrier_hz=d['carrier_hz'],candidate_id=d['id'],discovery_correlation=discovery,holdout_correlation=heldout))
            best=max(peers,key=lambda p:p['discovery_correlation']) if peers else None;c['best_peer']=best
            coherent=best and best['discovery_correlation']>=.6 and best['holdout_correlation']>=.6
            quality=c['occupancy']*min(1,c['contrast_db']/30)*c['phase_support']*min(1,c['modulation_concentration']/.4)
            c['rank_score']=float(quality*(.2+max(0,best['discovery_correlation'] if best else 0))) if c['screening_eligible'] else 0.
            c['status']='unvalidated_coherent_candidate' if coherent else 'unvalidated_candidate' if c['screening_eligible'] else 'fails_screening_gate'
        ranked=sorted(screen,key=lambda c:c['rank_score'],reverse=True)
        for c in ranked[:3]:
            if c['rank_score']>0:shortlist.append(c)
        selected={f"candidate_{c['id']}":curves[c['id']].astype('float32') for c in ranked[:3] if c['rank_score']>0}
        if selected:np.savez_compressed(out/f"{tape['tape']}_window{window_index}_diagnostic_curves.npz",**selected)
        coherence_records.append(dict(requested_start=start,new_candidates=len(screen),ranking=[c['id'] for c in ranked]))
    result=dict(tape=tape['tape'],horizon_seconds=horizon,requested_starts=starts,missing_expected_channels=tape['missing_expected_channels'],
        channels=list(reports.values()),shortlist=sorted(shortlist,key=lambda c:c['rank_score'],reverse=True),
        screen_status='discovery_only_no_new_validation',window_ranking=coherence_records,elapsed_seconds=perf_counter()-started)
    (out/f"{tape['tape']}.json").write_text(json.dumps(result,indent=2));return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--manifest',type=Path,default=BASE/'broad_manifest.json');ap.add_argument('--output',type=Path,default=BASE/'broad_discovery');ap.add_argument('--max-tapes',type=int);a=ap.parse_args()
    manifest=json.loads(a.manifest.read_text());prior=json.loads((BASE/'generic_trial/results.json').read_text());a.output.mkdir(parents=True,exist_ok=True)
    config=dict(manifest_sha256=hashlib.sha256(a.manifest.read_bytes()).hexdigest(),duration_seconds=30,windows_per_channel=2,candidates_per_channel=2,method='quick-phase-v1',scope='finite snapshot; discovery only')
    configpath=a.output/'config.json'
    if configpath.exists() and json.loads(configpath.read_text())!=config:raise ValueError('Snapshot/config changed; use fresh output')
    configpath.write_text(json.dumps(config,indent=2));results=[];started=perf_counter()
    for idx,tape in enumerate(manifest['tapes']):
        if a.max_tapes is not None and idx>=a.max_tapes:break
        saved=a.output/f"{tape['tape']}.json"
        r=json.loads(saved.read_text()) if saved.exists() else run_tape(tape,prior,a.output)
        results.append(r)
        windows=[w for c in r['channels'] for w in c['windows']]
        counts={s:sum(w['status']==s for w in windows) for s in ['analyzed_audio','reused_audio_analysis','skipped_unavailable_or_incomplete']}
        analyzed=sum(any(w['status'] in ['analyzed_audio','reused_audio_analysis'] for w in c['windows']) for c in r['channels'])
        print(tape['tape'],f"channels {analyzed}/{len(r['channels'])}",counts,'seconds',round(r['elapsed_seconds'],1),
            'top',[(c['channel'],c['carrier_hz'],c['status']) for c in r['shortlist'][:2]],flush=True)
        summaries=[dict(tape=x['tape'],channels=len(x['channels']),channels_analyzed=sum(any(w['status'] in ['analyzed_audio','reused_audio_analysis'] for w in c['windows']) for c in x['channels']),
            new_windows=sum(w['status']=='analyzed_audio' for c in x['channels'] for w in c['windows']),reused_windows=sum(w['status']=='reused_audio_analysis' for c in x['channels'] for w in c['windows']),skipped_windows=sum(w['status']=='skipped_unavailable_or_incomplete' for c in x['channels'] for w in c['windows']),
            missing_expected_channels=x['missing_expected_channels'],elapsed_seconds=x['elapsed_seconds'],top_candidates=x['shortlist'][:3]) for x in results]
        (a.output/'summary.json').write_text(json.dumps(summaries,indent=2))
    (a.output/'completion.json').write_text(json.dumps(dict(completed_utc=datetime.now(timezone.utc).isoformat(),snapshot_tapes=len(manifest['tapes']),processed_tapes=len(results),this_invocation_seconds=perf_counter()-started),indent=2))

if __name__=='__main__':main()
