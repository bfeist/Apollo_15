"""Tape-wide Quindar findings, local duplicate evidence and frozen shared plans.

Timing offsets are used to recognize repeated events, never to realign audio.
Discovery and calibration both use completed post-deflutter observations,
collapsed to one vote per event. Original-audio surveys are not accepted.
"""
from collections import defaultdict
from itertools import combinations
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

from quindar_speed import quality_events
from quindar_retime import curve_model, timing_map, write_json, audio_signature
from flutter_plan_store import planning_lock
from quindar_survey import DEFAULT_ANALYSIS_ROOT

MATCH_SETTINGS = dict(max_lag_seconds=15., region_seconds=1800., lag_tolerance_seconds=.15,
                      frequency_tolerance_hz=18., duration_tolerance_seconds=.055,
                      minimum_matches=4, minimum_chance_ratio=4., minimum_pair_coverage=.25)


def pair_matches(left, right, settings=None):
    """Find locally supported lag modes, allowing their sign/value to change.

    Sequence support, frequency, duration, one-to-one correspondence and an
    accidental-coincidence bound are all required. Periodic ambiguous sequences
    are retained as ambiguous, not arbitrarily aligned. No channel/head label
    assigns the lag. Search includes zero, +/-1.3 and +/-13 seconds.
    """
    s = settings or MATCH_SETTINGS
    if min(len(left),len(right)) < s['minimum_matches']:
        return [],[],set()
    lt=np.array([e['tape_seconds'] for e in left]); rt=np.array([e['tape_seconds'] for e in right])
    rf=np.array([e['frequency_hz'] for e in right]); rd=np.array([e['duration_seconds'] for e in right])
    buckets=defaultdict(list)
    for i,e in enumerate(left):
        lo,hi=np.searchsorted(rt,[lt[i]-s['max_lag_seconds'],lt[i]+s['max_lag_seconds']])
        for j in range(lo,hi):
            if abs(rf[j]-e['frequency_hz']) <= s['frequency_tolerance_hz'] and abs(rd[j]-e['duration_seconds']) <= s['duration_tolerance_seconds']:
                buckets[int(lt[i]//s['region_seconds'])].append((i,j,float(rt[j]-lt[i])))
    links,regions,ambiguous=[],[],set()
    bin_width=.05
    for block,candidates in sorted(buckets.items()):
        if len(candidates)<s['minimum_matches']:continue
        deltas=np.array([x[2] for x in candidates])
        edges=np.arange(-s['max_lag_seconds']-.025,s['max_lag_seconds']+.075,bin_width)
        histogram,_=np.histogram(deltas,edges)
        smoothed=ndimage.uniform_filter1d(histogram.astype(float),7,mode='constant')*7
        peaks,_=signal.find_peaks(np.r_[0.,smoothed,0.],distance=9)
        peaks=peaks-1
        hypotheses=[]
        for peak in sorted(peaks,key=lambda p:smoothed[p],reverse=True)[:8]:
            center=(edges[peak]+edges[peak+1])/2
            near=np.flatnonzero(abs(deltas-center)<=.20)
            if len(near)<s['minimum_matches']:continue
            center=float(np.median(deltas[near]))
            ids=np.flatnonzero(abs(deltas-center)<=s['lag_tolerance_seconds'])
            used_l,used_r,pairs=set(),set(),[]
            for k in sorted(ids,key=lambda k:abs(deltas[k]-center)):
                i,j,lag=candidates[k]
                if i in used_l or j in used_r:continue
                used_l.add(i);used_r.add(j);pairs.append((i,j,lag))
            count=len(pairs)
            # Uniform-lag accidental baseline is deliberately conservative; it
            # is a screening ratio, not a calibrated statistical p-value.
            chance=len(candidates)*(2*s['lag_tolerance_seconds'])/(2*s['max_lag_seconds'])
            a,b=block*s['region_seconds'],(block+1)*s['region_seconds']
            smaller=min(np.sum((lt>=a)&(lt<b)),np.sum((rt>=a-s['max_lag_seconds'])&(rt<b+s['max_lag_seconds'])))
            if count<max(s['minimum_matches'],s['minimum_chance_ratio']*chance) or count/max(1,smaller)<s['minimum_pair_coverage']:
                continue
            actual=np.array([x[2] for x in pairs]); lag=float(np.median(actual)); mad=float(np.median(abs(actual-lag)))
            if mad>.08:continue
            hypotheses.append(dict(lag_seconds=lag,count=count,mad_seconds=mad,pairs=pairs,
                                   chance_ratio=count/max(chance,1e-12),coverage=count/max(1,smaller)))
        # A pair can be supported at multiple lags after a head/channel switch.
        # Only ambiguity involving the same events prevents merging.
        competing=set()
        for h1,h2 in combinations(hypotheses,2):
            if abs(h1['lag_seconds']-h2['lag_seconds'])<.3:continue
            common={x[0] for x in h1['pairs']}&{x[0] for x in h2['pairs']}
            if common and min(h1['count'],h2['count'])>=.5*max(h1['count'],h2['count']):
                competing.update(common)
        summary=dict(start_seconds=block*s['region_seconds'],end_seconds=(block+1)*s['region_seconds'],
                     lag_modes=[],ambiguous_left_events=len(competing))
        for h in hypotheses:
            summary['lag_modes'].append({k:v for k,v in h.items() if k!='pairs'})
            for i,j,lag in h['pairs']:
                if i in competing:
                    ambiguous.update((left[i]['event_id'],right[j]['event_id']))
                else:
                    links.append(dict(left=left[i]['event_id'],right=right[j]['event_id'],
                                      lag_seconds=lag,mode_lag_seconds=h['lag_seconds'],support=h['count']))
        if summary['lag_modes']:regions.append(summary)
    return links,regions,ambiguous


def consolidate(scans,post=True):
    if not post:raise ValueError('Quindar findings must come from defluttered audio')
    channels={}; event_lookup={}; inventories=[]
    for scan in scans:
        row=scan['source']; source_id=row['id']; inventories.append(row)
        if row.get('start_seconds') is None:continue
        selected=quality_events(scan['events'])
        values=[]
        for index,e in enumerate(selected):
            event_id=f'{source_id}:{index}'
            e.update(event_id=event_id,source_id=source_id,channel=row['channel'],
                     tape_seconds=row['start_seconds']+(e['start_seconds']+e['end_seconds'])/2)
            values.append(e);event_lookup[event_id]=e
        channels[source_id]=sorted(values,key=lambda e:e['tape_seconds'])
    all_links,pair_evidence,ambiguous=[],[],set()
    for a,b in combinations(channels,2):
        links,regions,uncertain=pair_matches(channels[a],channels[b])
        all_links.extend(links);ambiguous.update(uncertain)
        if regions:
            modes=sorted({round(mode['lag_seconds'],2) for r in regions for mode in r['lag_modes']})
            pair_evidence.append(dict(left_source=a,right_source=b,regions=regions,observed_lag_modes_seconds=modes))
    parent={e:e for e in event_lookup}; members={e:{e} for e in event_lookup}
    def root(e):
        while parent[e]!=e:
            parent[e]=parent[parent[e]];e=parent[e]
        return e
    rejected=0
    for link in sorted(all_links,key=lambda e:e['support'],reverse=True):
        if link['left'] in ambiguous or link['right'] in ambiguous:continue
        a,b=root(link['left']),root(link['right'])
        if a==b:continue
        merged=members[a]|members[b]
        ids=[event_lookup[e]['source_id'] for e in merged]
        times=[event_lookup[e]['tape_seconds'] for e in merged]
        # Prevent transitive chains from joining separate transmissions.
        if len(set(ids))!=len(ids) or max(times)-min(times)>MATCH_SETTINGS['max_lag_seconds']+.3:
            rejected+=1;continue
        parent[b]=a;members[a]=merged;del members[b]
    groups=[]
    for ids in members.values():
        observations=[event_lookup[e] for e in sorted(ids)]
        # One copy is chosen for calibration; correlated copies are not votes.
        best=min(observations,key=lambda e:(e['residual_fm_sd_hz']+e['trim_range_hz']*2)/max(.1,e['band_power_fraction']))
        times=[e['tape_seconds'] for e in observations]
        groups.append(dict(id='',tape_seconds=float(np.median(times)),time_spread_seconds=float(np.ptp(times)),
                           copies=len(observations),representative=best['event_id'],
                           ambiguous=any(e in ambiguous for e in ids),observations=observations))
    groups.sort(key=lambda e:e['tape_seconds'])
    for i,g in enumerate(groups):g['id']=f'q{i:06d}'
    return dict(schema=2,stage='post_deflutter',
                settings=MATCH_SETTINGS,channels=inventories,
                quality_detections=len(event_lookup),event_groups=len(groups),duplicate_copies_removed=len(event_lookup)-len(groups),
                ambiguous_detections=len(ambiguous),rejected_transitive_links=rejected,
                pair_evidence=pair_evidence,groups=groups,
                timing_note='Historical content lags recognize duplicate observations only. Modern playback flutter is simultaneous across channels. No waveform or correction curve is shifted; every measurement retains its actual playback time. Group median times are display labels only.')


def load_scans(tape_folder,stage):
    scans=[]
    for path in sorted((Path(tape_folder)/'survey'/stage).glob('*.json')):
        if path.name.endswith('.progress.json'):continue
        scan=json.loads(path.read_text())
        if scan.get('status')!='complete':continue
        if hashlib.sha256(json.dumps(scan['events'],separators=(',',':')).encode()).hexdigest()!=scan['events_sha256']:
            raise ValueError(f'Candidate inventory changed: {path}')
        scans.append(scan)
    return scans


def summarize_tape(folder,require_complete=True):
    folder=Path(folder)
    survey_path=folder.parent/'survey.json'
    survey=json.loads(survey_path.read_text())
    if survey.get('schema')!=2 or survey.get('input_stage')!='defluttered' or not survey.get('complete_defluttered_coverage'):
        raise ValueError('A completed-stage-05b survey is required; original-audio surveys are ineligible')
    if survey.get('status')=='cancelled':raise ValueError('Cancelled survey is ineligible')
    expected=[r for r in survey['targets'] if r['tape']==folder.name]
    post=load_scans(folder,'defluttered')
    expected_post=[r for r in survey['inventory'] if r['tape']==folder.name and r['stage']=='defluttered']
    if ({r['channel'] for r in expected_post}!={r['channel'] for r in expected}
            or len(expected_post)!=len(expected)):
        raise ValueError('Not every expected tape channel has a completed defluttered source')
    for scans, inventory in ((post,expected_post),):
        indexed={r['id']:r for r in inventory}
        for scan in scans:
            row=scan['source']
            if row != indexed.get(row['id']) or scan['implementation_sha256']!=survey['implementation_sha256']:
                raise ValueError('Survey source/version mismatch; use a new analysis root')
            stat=Path(row['path']).stat()
            if (stat.st_size,stat.st_mtime_ns)!=(row['bytes'],row['mtime_ns']):
                raise ValueError('Survey source changed since scanning')
    complete_post=bool(expected_post) and {r['source']['id'] for r in post}=={r['id'] for r in expected_post}
    tape_issues=[r for r in survey.get('issues',[]) if r.get('tape')==folder.name]
    if require_complete and (not complete_post or tape_issues):
        raise ValueError(f'Post-deflutter survey incomplete or invalid for {folder.name}: {len(post)}/{len(expected_post)} scanned')
    calibration=consolidate(post,post=True)
    calibration.update(tape=folder.name,complete_defluttered_coverage=complete_post,
                       expected_files=len(expected_post),scanned_files=len(post))
    write_json(folder/'findings.json',calibration)
    return calibration,expected


def build_shared_plan(folder,bandwidth=None):
    """Freeze a single model per tape. Later targets reuse it, never re-estimate."""
    folder=Path(folder).resolve(); destination=folder/'shared_plan'
    with planning_lock(destination):
        if (destination/'plan.json').exists():
            return load_shared_plan(destination/'plan.json')
        post,targets=summarize_tape(folder)
        if not post['channels']:
            raise ValueError('Awaiting completed post-deflutter audio')
        if any(r['start_seconds'] is None for r in post['channels']):
            raise ValueError('Post-deflutter reference lacks a tape-clock origin')
        # If repeated sequences cannot be resolved, retain one reference channel
        # for those ambiguous events. Unambiguous duplicate groups remain pooled.
        source_counts=defaultdict(int)
        for g in post['groups']:
            for e in g['observations']:source_counts[e['source_id']]+=1
        fallback=max(source_counts,key=source_counts.get) if source_counts else None
        events=[];group_ids=[]
        for g in post['groups']:
            observations=g['observations']
            if g['ambiguous']:
                observations=[e for e in observations if e['source_id']==fallback]
                if not observations:continue
            e=next((e for e in observations if e['event_id']==g['representative']),observations[0]).copy()
            # Content duplicates occurred at different modern playback instants.
            # Keep the chosen measurement at ITS observed time, never the group
            # median or a content-aligned time. The slow curve uses playback time.
            length=e['duration_seconds'];center=e['tape_seconds']
            e.update(start_seconds=center-length/2,end_seconds=center+length/2)
            # Exact simultaneous independent groups get one robust observation,
            # rather than duplicate knots or extra weight in the smoothing fit.
            events.append(e);group_ids.append(g['id'])
        unique={}
        for e,gid in zip(events,group_ids):
            key=round((e['start_seconds']+e['end_seconds'])/2,3)
            if key not in unique or e['residual_fm_sd_hz']<unique[key][0]['residual_fm_sd_hz']:
                unique[key]=(e,gid)
        events=[v[0] for _,v in sorted(unique.items())]
        origins={r['origin_filename_seconds'] for r in targets if r['origin_filename_seconds'] is not None}
        if len(origins)!=1:raise ValueError('Tape origin is not established')
        duration=max(r['start_seconds']+r['duration_seconds'] for r in targets if r['start_seconds'] is not None)
        model=curve_model(events,duration,bandwidth)
        tt,oo,ss=timing_map(model)
        destination.mkdir(parents=True,exist_ok=True)
        write_json(destination/'curve.json',model)
        np.savez_compressed(destination/'timing_map.npz',input_seconds=tt,output_seconds=oo,recorded_speed_ratio=ss)
        record=dict(schema=2,status='complete',input_stage='defluttered',tape=folder.name,scope='one frozen Quindar speed curve for every target on this tape',
                    origin_filename_seconds=origins.pop(),input_duration_seconds=duration,
                    complete_defluttered_coverage=post['complete_defluttered_coverage'],
                    reference_count=len(post['channels']),references=post['channels'],targets=targets,
                    unique_calibration_events=len(events),duplicate_copies_removed=post['duplicate_copies_removed'],
                    ambiguous_single_channel_fallback=fallback if post['ambiguous_detections'] else None,
                    kernel_sigma_seconds=model['kernel_sigma_seconds'],
                    timing_offsets_policy='Historical content offsets identify duplicate Quindars only. Measurements retain observed playback times. Modern flutter is simultaneous across channels: never shift its map to align content. Apply one common playback-clock speed map, retaining existing head offsets/swaps.',
                    time_basis='Canonical acquisition/file timeline used by stage 05b; no content-lag adjustment',
                    caveats=['Nominal tone generator accuracy remains unknown.',
                             'All Quindar discovery and curve measurements use completed defluttered audio only.',
                             'One available post-deflutter reference is a single-source calibration, not multichannel frequency validation.' if len(post['channels'])==1 else 'Multiple post-deflutter channels contribute after duplicate suppression.'],
                    findings_sha256=hashlib.sha256((folder/'findings.json').read_bytes()).hexdigest(),
                    artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir() if p.name in {'curve.json','timing_map.npz'}})
        write_json(destination/'plan.json',record)
        return record


def load_shared_plan(path):
    path=Path(path).resolve()
    if path.is_dir():path=path/'plan.json'
    plan=json.loads(path.read_text())
    if plan.get('schema')!=2 or plan.get('status')!='complete' or not plan.get('complete_defluttered_coverage') or plan.get('input_stage')!='defluttered':
        raise ValueError('Shared Quindar plan is incomplete')
    for name,digest in plan['artifacts'].items():
        artifact=(path.parent/name).resolve()
        if artifact.parent!=path.parent or hashlib.sha256(artifact.read_bytes()).hexdigest()!=digest:
            raise ValueError('Shared Quindar timing artifact changed')
    for row in plan['references']:
        stat=Path(row['path']).stat()
        if (stat.st_size,stat.st_mtime_ns)!=(row['bytes'],row['mtime_ns']):
            raise ValueError('Shared-plan post-deflutter reference changed')
        if hashlib.sha256((Path(row['path']).parent/'review.json').read_bytes()).hexdigest()!=row['review_sha256']:
            raise ValueError('Shared-plan post-deflutter manifest changed')
    return plan


def target_map(plan_path,source):
    """Slice the SAME frozen tape map using a target's declared tape-clock start."""
    plan_path=Path(plan_path).resolve()
    if plan_path.is_dir():plan_path=plan_path/'plan.json'
    plan=load_shared_plan(plan_path)
    source=Path(source).resolve()
    review_path=source.parent/'review.json'
    review=json.loads(review_path.read_text())
    if review.get('status')!='complete' or review.get('tape')!=plan['tape']:
        raise ValueError('Target requires a completed stage-05b manifest for this tape')
    candidates=[r for r in plan['targets'] if r['channel']==review['channel'] and Path(r['path']).resolve()==Path(review['source']['path']).resolve()]
    if len(candidates)!=1:raise ValueError('Target source is not uniquely inventoried on this tape')
    row=candidates[0]; original=review['source']
    if (original['bytes'],original['mtime_ns'])!=(row['bytes'],row['mtime_ns']):
        raise ValueError('Target and shared survey original-source versions disagree')
    if original['absolute_start']-original['start_seconds']!=plan['origin_filename_seconds']:
        raise ValueError('Target uses a different tape-clock origin')
    info=audio_signature(source)
    if info['frames']!=review['frames'] or info['sample_rate']!=review['sample_rate']:
        raise ValueError('Defluttered target header differs from its manifest')
    start=float(review['start_seconds']);duration=info['frames']/info['sample_rate'];end=start+duration
    if start<0 or end>plan['input_duration_seconds']+1e-8:
        raise ValueError('Target exceeds shared timing-map coverage')
    with np.load(plan_path.parent/'timing_map.npz') as archive:
        tt,oo,ss=(archive[k] for k in ('input_seconds','output_seconds','recorded_speed_ratio'))
        keep=(tt>start)&(tt<end)
        local_t=np.r_[start,tt[keep],end]
        global_o=np.interp(local_t,tt,oo)
        speed=np.interp(local_t,tt,ss)
    metadata=dict(path=str(plan_path),sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),tape=plan['tape'],
                  input_start_seconds=start,output_start_seconds=float(global_o[0]),
                  original_head_offsets_preserved=True,channel=review['channel'])
    model=json.loads((plan_path.parent/'curve.json').read_text())
    return model,local_t-start,global_o-global_o[0],speed,metadata
