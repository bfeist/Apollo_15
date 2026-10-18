"""Generic bounded reference discovery. Canonical channel streams or explicit chunk maps.

Writes research manifests/reports only. Never concatenates or changes delivery audio.
"""
from survey import *
from datetime import datetime, timezone

STAMP=re.compile(r'_(\d+-\d+-\d+-\d+)_(\d+-\d+-\d+-\d+)(?:-(\d+))?\.wav$',re.I)
CHANNEL=re.compile(r'CH(\d+)_',re.I)

def seconds(s):
    day,hour,minute,second=map(int,s.split('-'))
    # Delivery uses 207-24-05-00 for 208-00-05-00 at midnight boundaries.
    # Normalize arithmetically; overlap and physical-duration checks still apply.
    if not 0<=hour<=24 or not 0<=minute<60 or not 0<=second<60:raise ValueError('invalid filename clock')
    return day*86400+hour*3600+minute*60+second

def gaps(segments):
    end=None;out=[];overlaps=[]
    for s in sorted(segments,key=lambda s:s['start']):
        if end is not None:
            if s['start']<end-1e-6:overlaps.append([s['start'],end])
            elif s['start']>end+1e-6:out.append([end,s['start']])
        end=max(end or s['start'],s['start']+s['duration'])
    return out,overlaps

def make_manifest(delivery,canonical,output,canonical_common_start=False):
    tape_names=sorted({p.name for root in [delivery,canonical] if root.exists() for p in root.iterdir() if p.is_dir() and re.fullmatch(r'A15_T\d+',p.name)})
    manifest=dict(schema=1,created_utc=datetime.now(timezone.utc).isoformat(),delivery_root=str(delivery),canonical_root=str(canonical),
        canonical_common_start_declared=canonical_common_start,
        policy='One logical stream per tape/channel. Valid unique canonical replaces chunks, never combines with them. Timestamp-map offsets preserve missing spans. Head alignment remains unverified.',tapes=[])
    for tape in tape_names:
        channels={};invalid=[]
        for kind,root in [('chunks',delivery/tape),('canonical',canonical/tape)]:
            if not root.exists():continue
            for path in sorted(root.rglob('*.wav')):
                m=CHANNEL.search(path.name)
                if not m:invalid.append(dict(path=str(path),reason='no channel identifier'));continue
                ch=int(m[1]);rec=channels.setdefault(ch,dict(channel=ch,label=path.parent.name,chunks=[],canonical=[]))
                s=path.stat();stamp=STAMP.search(path.name)
                entry=dict(path=str(path.resolve()),bytes=s.st_size,mtime_ns=s.st_mtime_ns,
                    eligible_age=time.time()-s.st_mtime>=600,header_validated=False)
                if stamp:
                    try:
                        begin=seconds(stamp[1]);end=seconds(stamp[2])
                        if end<=begin:raise ValueError('non-increasing timestamp range')
                        entry.update(absolute_start=begin,duration=end-begin,fragment_number=None if stamp[3] is None else int(stamp[3]),
                            filename_start=stamp[1],filename_end=stamp[2],normalized_hour24=any(int(t.split('-')[1])==24 for t in [stamp[1],stamp[2]]))
                    except ValueError as ex:entry['error']=str(ex)
                elif kind=='chunks':entry['error']='fragment lacks timestamp range'
                rec[kind].append(entry)
        origins=[s['absolute_start'] for c in channels.values() for s in c['chunks']+c['canonical'] if 'absolute_start' in s]
        origin=min(origins) if origins else None
        tr=dict(tape=tape,origin_filename_seconds=origin,time_basis='filename day/hour/minute/second; not authenticated GET',
            channels=[],missing_expected_channels=[ch for ch in range(1,31) if ch not in channels],unmapped_files=invalid,scan_status='not_scanned')
        for ch,c in sorted(channels.items()):
            canonical_files=c['canonical'];chunks=c['chunks'];segs=[];reasons=[];kind='chunks'
            if len(canonical_files)>1:
                reasons.append('multiple canonical files for one channel; explicit resolution required');kind='ambiguous'
            elif len(canonical_files)==1:
                s=canonical_files[0]
                if canonical_common_start and 'absolute_start' not in s and s['eligible_age']:
                    try:
                        size,mtime,rate,frames=stable(Path(s['path']))
                        if size!=s['bytes'] or mtime!=s['mtime_ns']:raise ValueError('canonical changed during mapping')
                        s.update(absolute_start=origin if origin is not None else 0,duration=frames/rate,header_validated=True,alignment='operator declared common start')
                        if origin is None:origin=0;tr['origin_filename_seconds']=None;tr['time_basis']='operator-declared common start; no absolute time anchor'
                    except (ValueError,OSError,RuntimeError) as ex:s['error']=str(ex)
                # Never infer alignment of a timestamp-free canonical stream from its length.
                if not s['eligible_age']:reasons.append('canonical recent; defer channel instead of falling back to duplicate fragments');kind='deferred'
                elif 'absolute_start' not in s or origin is None:reasons.append('canonical start unknown; explicit start/duration mapping required');kind='unmapped'
                elif 'error' in s:reasons.append(s['error']);kind='unmapped'
                else:segs=[dict(s,start=s['absolute_start']-origin,source_start=0)];kind='canonical'
            else:
                for s in chunks:
                    if 'error' in s or 'absolute_start' not in s:reasons.append(s.get('error','unmapped fragment'));continue
                    # Recent segments remain visible as deferred coverage, not collapsed away.
                    segs.append(dict(s,start=s['absolute_start']-origin,source_start=0))
            gs,overlaps=gaps(segs)
            if overlaps:reasons.append('overlapping segments; channel blocked to avoid double counting')
            tr['channels'].append(dict(channel=ch,label=c['label'],representation=kind,segments=segs,gaps=gs,overlaps=overlaps,
                issues=reasons,discovered_chunk_files=len(chunks),discovered_canonical_files=len(canonical_files),
                unmapped_sources=[s for s in chunks if 'error' in s or 'absolute_start' not in s]+(canonical_files if kind in ['ambiguous','deferred','unmapped'] else []),
                superseded_chunk_files=len(chunks) if kind=='canonical' else 0,
                physical_head_alignment='unverified',scan_status='not_scanned'))
        manifest['tapes'].append(tr)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(manifest,indent=2))
    print('Manifest:',len(manifest['tapes']),'tapes',sum(len(t['channels']) for t in manifest['tapes']),'channels',flush=True)
    return manifest

def stream_read(channel,start,duration):
    if channel.get('overlaps') or channel.get('representation') in ['ambiguous','deferred','unmapped']:raise ValueError('ambiguous/deferred/unmapped stream')
    end=start+duration;cursor=start;pieces=[];provenance=[];rate=None
    for s in sorted(channel['segments'],key=lambda s:s['start']):
        se=s['start']+s['duration']
        if se<=cursor+1e-8:continue
        if s['start']>=end:break
        if s['start']>cursor+1e-6:raise ValueError(f'gap at {cursor:g} seconds')
        if not s.get('eligible_age',True):raise ValueError('recent segment')
        path=Path(s['path']);stat=stable(path)
        if s.get('bytes',stat[0])!=stat[0] or s.get('mtime_ns',stat[1])!=stat[1]:raise ValueError('file changed since manifest snapshot')
        # Metadata duration and physical frames must agree; never stretch a fragment silently.
        if abs(stat[3]/stat[2]-(s.get('source_start',0)+s['duration']))>1/stat[2]:raise ValueError('declared interval does not match physical WAV duration')
        take=min(end,se)-cursor
        x,sr,m=read(path,s.get('source_start',0)+cursor-s['start'],take)
        if rate is not None and rate!=sr:raise ValueError('sample rate changes across boundary')
        rate=sr;pieces.append(x);provenance.append(dict(m,stream_start=cursor));cursor+=take
        if cursor>=end-1e-6:break
    if cursor<end-1e-6:raise ValueError('requested interval not covered')
    x=np.concatenate(pieces)
    if len(x)!=round(duration*rate):raise ValueError('mapped frame count mismatch')
    return x,rate,provenance

def discover(x,sr,limit=3):
    # Discover across the usable band; no preferred historical carrier frequency.
    f,t,p=signal.spectrogram(x,sr,nperseg=2*sr,noverlap=sr)
    power=ndimage.gaussian_filter1d(np.median(p,axis=1),4)
    background=ndimage.median_filter(power,size=321)
    score=10*np.log10((power+1e-30)/(background+1e-30))
    peaks,_=signal.find_peaks(score,distance=80)
    records=[]
    for i in peaks:
        hz=float(f[i])
        if not 100<hz<min(sr/2-100,3900):continue
        band=abs(f-hz)<15;side=(abs(f-hz)>45)&(abs(f-hz)<90)
        contrast=10*np.log10((p[band].max(axis=0)+1e-30)/(np.median(p[side],axis=0)+1e-30))
        occupancy=float(np.mean(contrast>10));snr=float(np.median(contrast))
        if occupancy<.6 or snr<8:continue
        records.append(dict(carrier_hz=hz,spectral_score_db=float(score[i]),contrast_db=snr,occupancy=occupancy,
            near_mains_family=abs(hz/60-round(hz/60))*60<5))
    records.sort(key=lambda r:r['spectral_score_db'],reverse=True)
    # Reserve space for non-mains peaks so a harmonic comb cannot occupy every slot.
    nonmains=[r for r in records if not r['near_mains_family']][:max(1,limit-1)]
    chosen=nonmains+[r for r in records if r not in nonmains][:limit-len(nonmains)]
    return chosen

def scan_window(tape,start,duration,max_candidates):
    data={};coverage=[];allc=[];curves={};sr=None
    for ch in tape['channels']:
        try:
            x,rate,meta=stream_read(ch,start,duration)
            if sr is not None and rate!=sr:raise ValueError('rate differs from other channels; no shared analysis')
            sr=rate;data[ch['channel']]=x
            coverage.append(dict(channel=ch['channel'],status='sampled',sources=meta))
            # Frequencies discovered using first half only. Second half is held out.
            for c in discover(x[:len(x)//2],rate,max_candidates):
                try:
                    row,e=measure(x,rate,c,ch['channel']);row['id']=len(allc)
                    row['signal_eligible']=c['occupancy']>=.7 and row['tracking']['support_gate_mean']>=.8 and .1<=row['tracking']['rms_percent']<=3
                    curves[row['id']]=e;allc.append(row)
                except ValueError:pass
        except (ValueError,OSError,RuntimeError) as ex:coverage.append(dict(channel=ch['channel'],status='deferred_or_uncovered',reason=str(ex)))
    n=round(duration*200);mid=n//2;pairs=[]
    for i,c in enumerate(allc):
        for d in allc[i+1:]:
            if c['channel']==d['channel'] or abs(c['carrier_hz']-d['carrier_hz'])<80:continue
            ec=curves[c['id']];ed=curves[d['id']]
            dc=float(np.corrcoef(ec[400:mid-200],ed[400:mid-200])[0,1]) if min(np.std(ec),np.std(ed))>1e-10 else 0
            hc=float(np.corrcoef(ec[mid+200:-400],ed[mid+200:-400])[0,1]) if min(np.std(ec),np.std(ed))>1e-10 else 0
            ratio=max(c['carrier_hz'],d['carrier_hz'])/min(c['carrier_hz'],d['carrier_hz'])
            independent_candidate=not(c['near_mains_family'] and d['near_mains_family']) and abs(ratio-round(ratio))>.03
            pairs.append(dict(a=c['id'],b=d['id'],discovery_correlation=dc,holdout_correlation=hc,
                distinct_family_candidate=independent_candidate,signal_eligible=c['signal_eligible'] and d['signal_eligible']))
    for c in allc:
        peers=[p for p in pairs if c['id'] in [p['a'],p['b']] and p['distinct_family_candidate'] and p['signal_eligible']]
        best=max(peers,key=lambda p:p['discovery_correlation']) if peers else None
        c['best_peer']=best
        co=max(0,best['discovery_correlation']) if best else 0
        c['rank_score']=c['occupancy']*min(1,c['contrast_db']/30)*c['tracking']['support_gate_mean']*(.2+co) if c['signal_eligible'] else 0
    ranked=sorted(allc,key=lambda c:(c['rank_score'],c['spectral_score_db']),reverse=True)
    selected=ranked[0] if ranked and ranked[0]['rank_score']>0 else None
    r=dict(tape=tape['tape'],start=start,duration=duration,coverage=coverage,candidates=ranked,selected=selected,
        status='no_trustworthy_reference',validation=[],note='Research evidence only; no automatic production approval. Channels/oscillators and head alignment unverified.')
    if selected:
        e=curves[selected['id']]
        for ch,x in sorted(data.items()):
            if ch==selected['channel']:continue
            qs=[q for q in beeps(x,sr) if q['start']>duration/2+1 and q['end']<duration-2]
            if not qs:continue
            evaluation=evaluate(x,sr,e,qs)
            # Both controls are fixed, one near half-cycle and one long displacement.
            controls={str(shift):evaluate(x,sr,np.roll(e,round(shift*200)),qs) for shift in [.071,5.]}
            r['validation'].append(dict(channel=ch,events=evaluation,controls=controls))
        # Never turn correlation alone into permission. Retain an explicit research-only outcome.
        peer=selected['best_peer']
        if peer and peer['discovery_correlation']>=.6 and peer['holdout_correlation']>=.6:
            r['candidate_status']='coherent_in_both_halves_needs_independent_validation'
        else:r['candidate_status']='insufficient_shared_motion'
        events=[q for v in r['validation'] for q in v['events']]
        if events:
            r['target_summary']=dict(measurements=len(events),median_reduction_percent=float(np.median([q['reduction_percent'] for q in events])),worst_reduction_percent=float(min(q['reduction_percent'] for q in events)),independent_event_count='not established; channel replicas retained')
            tagged=sorted([(q,v,i) for v in r['validation'] for i,q in enumerate(v['events'])],key=lambda t:t[0]['start'])
            clusters=[]
            for item in tagged:
                if clusters and item[0]['start']-clusters[-1][0][0]['start']<=.1:clusters[-1].append(item)
                else:clusters.append([item])
            unique=[max(g,key=lambda t:t[0]['amplitude_over_floor']) for g in clusters]
            med=float(np.median([q['reduction_percent'] for q,v,i in unique]));worst=min(q['reduction_percent'] for q,v,i in unique)
            best_control=max(float(np.median([v['controls'][str(shift)][i]['reduction_percent'] for q,v,i in unique])) for shift in [.071,5.])
            r['target_summary'].update(unique_time_clusters=len(unique),deduplicated_median_reduction_percent=med,deduplicated_worst_reduction_percent=worst,best_shift_median_reduction_percent=best_control)
            if len(unique)>=3 and med>=20 and worst>=-10 and med>best_control and r.get('candidate_status')=='coherent_in_both_halves_needs_independent_validation':
                r['status']='locally_supported_research_candidate'
                r['note']='Provisional numeric interval support only; verify event identity, common speed provenance, head alignment and speech listening before production use.'
        else:r['target_summary']=dict(measurements=0,reason='no qualifying held-out candidate tone event')
    return r

def scan(manifest,output,tapes=None,windows=2,duration=50,max_tape_windows=6,max_candidates=3):
    output.mkdir(parents=True,exist_ok=True)
    if (output/'results.json').exists():raise ValueError('Use a fresh output directory for each scan')
    results=[];coverage=[];budget=0
    for tape in manifest['tapes']:
        selected=tapes is None or tape['tape'] in tapes
        ends=[s['start']+s['duration'] for c in tape['channels'] for s in c['segments']]
        horizon=max(ends,default=0)
        starts=[round(float(x),3) for x in np.linspace(.15,.85,windows)*max(0,horizon-duration)]
        tc=dict(tape=tape['tape'],missing_expected_channels=tape['missing_expected_channels'],horizon_seconds=horizon,planned_starts=starts,
            channel_coverage=[dict(channel=c['channel'],representation=c['representation'],issues=c['issues'],gaps=c['gaps'],segments=len(c['segments']),status='not_scanned',sampled_windows=0,deferred_windows=0) for c in tape['channels']])
        if not selected:tc['status']='outside_requested_tape_subset'
        elif horizon<duration:tc['status']='insufficient_mapped_duration'
        else:
            tc['status']='sampled' if budget<max_tape_windows else 'budget_not_scanned'
            for start in starts:
                if budget>=max_tape_windows:tc['status']='budget_limited';break
                result=scan_window(tape,start,duration,max_candidates);results.append(result);budget+=1
                for c in result['coverage']:
                    cr=next(x for x in tc['channel_coverage'] if x['channel']==c['channel'])
                    if c['status']=='sampled':cr['sampled_windows']+=1;cr['status']='sampled'
                    else:cr['deferred_windows']+=1;cr.setdefault('reasons',[]).append(c['reason'])
                candidate=result['selected'];print(tape['tape'],start,'sampled',len([c for c in result['coverage'] if c['status']=='sampled']),
                    'candidates',len(result['candidates']),'selected',None if not candidate else (candidate['channel'],candidate['carrier_hz']),
                    result.get('candidate_status'),result.get('target_summary'),flush=True)
                (output/'results.json').write_text(json.dumps(results,indent=2))
        coverage.append(tc)
        (output/'coverage.json').write_text(json.dumps(coverage,indent=2))
    (output/'manifest_snapshot.json').write_text(json.dumps(manifest,indent=2))
    (output/'scan_config.json').write_text(json.dumps(dict(tapes=tapes,windows=windows,duration=duration,max_tape_windows=max_tape_windows,max_candidates=max_candidates),indent=2))
    return results,coverage

def main():
    ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='action',required=True)
    m=sub.add_parser('manifest');m.add_argument('--delivery',type=Path,default=Path('D:/Apollo_15_30-Track/orig_delivery'));m.add_argument('--canonical',type=Path,default=Path('D:/Apollo_15_30-Track/orig_concatenated'));m.add_argument('--output',type=Path,required=True);m.add_argument('--canonical-common-start',action='store_true',help='Explicitly declare timestamp-free canonical streams start at the same tape origin; never infer this from matching lengths')
    s=sub.add_parser('scan');s.add_argument('--manifest',type=Path,required=True);s.add_argument('--output',type=Path,required=True);s.add_argument('--tapes');s.add_argument('--windows',type=int,default=2);s.add_argument('--duration',type=int,default=50);s.add_argument('--max-tape-windows',type=int,default=6);s.add_argument('--candidates',type=int,default=3)
    a=ap.parse_args()
    if a.action=='manifest':make_manifest(a.delivery,a.canonical,a.output,a.canonical_common_start)
    else:
        if not 30<=a.duration<=120 or not 1<=a.windows<=20 or not 1<=a.candidates<=8 or a.max_tape_windows<1:ap.error('Invalid bounded sampling options')
        scan(json.loads(a.manifest.read_text()),a.output,None if not a.tapes else ['A15_T'+s.removeprefix('A15_T') for s in a.tapes.split(',')],a.windows,a.duration,a.max_tape_windows,a.candidates)

if __name__=='__main__':main()
