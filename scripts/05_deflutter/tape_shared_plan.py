"""Build one shared regional review plan from existing all-channel evidence."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np

from flutter_plan_store import checked_artifact, load_saved_plan
from channel_review import RATE, region_curve

ELIGIBLE = {'render_and_review', 'group_render_and_review'}
V5_ALLOWED = {'insufficient_evidence', 'partial_improvement_at_measured_events',
              'restoration_at_measured_events', 'supported_improvement'}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')


def regional_choice(region, preferred=None):
    """No output channel argument: witnesses contribute availability, not votes."""
    witnesses = defaultdict(list)
    for target in region['targets']:
        if target['action'] in ELIGIBLE and target.get('selected'):
            witnesses[target['selected']].append(target['channel'])
    if not witnesses:
        return None, []
    variants = {v['key']:v for v in region.get('variants', [])}
    def rank(key):
        variant = variants.get(key, {})
        return (-float(variant.get('tracking', {}).get('support', 0)),
                -float(variant.get('reference_rank', 0)), key)
    key = preferred if preferred in witnesses else min(witnesses, key=rank)
    return key, sorted(witnesses[key])


def build_shared_plan(regional_path, continuous_path, assessment_path, output,
                      start, end, preferred=None):
    """Materialize identical choices for all channels on canonical tape time.

    Source plans are immutable. Output is a new bounded plan, not acceptance.
    The caller integrates it once and reuses that map for every output channel.
    """
    output = Path(output)
    if output.exists():
        raise ValueError('Shared-plan output must be new')
    regional_path, continuous_path = Path(regional_path), Path(continuous_path)
    legacy = load_saved_plan(regional_path, expand_regions=False)
    continuous = load_saved_plan(continuous_path, expand_regions=False)
    if legacy['inventory']['tape'] != continuous['inventory']['tape']:
        raise ValueError('Source plans refer to different tapes')
    if end <= start:
        raise ValueError('Invalid shared time range')
    assessment = json.loads(Path(assessment_path).read_text())
    if assessment['plan_sha256'] != digest(continuous_path):
        raise ValueError('Continuous assessment belongs to another plan')
    # A single canonical coordinate system; differing source identities need review.
    identities = lambda p: {r['channel']:(r['path'],r['bytes'],r['mtime_ns'],r['start_seconds'])
                           for r in p['inventory']['channels']}
    if identities(legacy) != identities(continuous):
        raise ValueError('Source plans disagree on channel identity or alignment')
    legacy_regions = [r for r in legacy['regions'] if r['end_seconds']>start and r['start_seconds']<end]
    pieces, boundaries = [], {start,end}
    for summary in legacy_regions:
        _, raw = checked_artifact(regional_path.parent, summary['detail_file'], summary['detail_sha256'])
        region = json.loads(raw)
        a,b = max(start,region['start_seconds']),min(end,region['end_seconds'])
        boundaries.update((a,b))
        key, witnesses = regional_choice(region, (preferred or {}).get(region['id']))
        if key:
            curve = region_curve(regional_path.parent,summary,witnesses[0],False)
            assert curve['target']['selected']==key
            pieces.append(dict(a=a,b=b,priority=0,key=f"regional__{region['id']}__{key}",
                source_key=key,source_region=region['id'],kind='regional',witnesses=witnesses,
                analysis_start=region['analysis_start'],analysis_end=region['analysis_end'],
                speed=curve['speed'],support=curve['support'],rank=0.,
                detail_sha256=summary['detail_sha256'],maps_sha256=region['maps']['sha256'],
                approved_choice=(preferred or {}).get(region['id'])==key))
    region_summary = continuous['regions'][0]
    _, raw = checked_artifact(continuous_path.parent,region_summary['detail_file'],region_summary['detail_sha256'])
    cr = json.loads(raw)
    variants = {v['key']:v for v in cr['variants']}
    assessments = {t['channel']:t for t in assessment['channels']}
    available = defaultdict(list)
    for target in cr['targets']:
        for span in target.get('reference_spans', []):
            a,b = max(start,span['start_seconds']),min(end,span['end_seconds'])
            if a>=b or span['action']!='use_reference':
                continue
            check = next((s for s in assessments[target['channel']]['spans']
                          if abs(s['start_seconds']-span['start_seconds'])<1e-6
                          and abs(s['end_seconds']-span['end_seconds'])<1e-6), None)
            if check is None or check['outcome'] not in V5_ALLOWED:
                continue
            metrics = check.get('event_metrics', [])
            if any(e.get('status')=='failed' for e in metrics):
                continue
            available[(span['selected'],a,b)].append(dict(channel=target['channel'],outcome=check['outcome']))
    streams = {}
    for (key,a,b), witnesses in available.items():
        spec = cr['maps']['streams'][key]
        if key not in streams:
            _, data = checked_artifact(continuous_path.parent,spec['file'],spec['sha256'])
            values = np.frombuffer(data,dtype='<f8').reshape(-1,2).copy()
            if len(values)!=spec['frames'] or cr['maps']['rate']!=RATE:
                raise ValueError('Continuous stream shape/rate mismatch')
            if not np.isfinite(values).all() or np.max(abs(values[:,0]))>.05:
                raise ValueError('Invalid continuous speed')
            if np.any((values[:,1]<0)|(values[:,1]>1)) or np.any(abs(values[values[:,1]==0,0])>1e-12):
                raise ValueError('Invalid continuous support')
            streams[key]=values
        boundaries.update((a,b))
        values=streams[key]
        pieces.append(dict(a=a,b=b,priority=1,key='v5__'+key,source_key=key,
            source_region='continuous',kind='continuous_v5',witnesses=sorted(w['channel'] for w in witnesses),
            assessment_outcomes=witnesses,analysis_start=spec['start_seconds'],
            analysis_end=spec['start_seconds']+len(values)/RATE,speed=values[:,0],support=values[:,1],
            rank=float(variants[key].get('rank',0)),detail_sha256=region_summary['detail_sha256'],
            maps_sha256=spec['sha256'],approved_choice=False))
    points=sorted(boundaries)
    chosen=[]
    for a,b in zip(points,points[1:]):
        eligible=[p for p in pieces if p['a']<=a+1e-8 and p['b']>=b-1e-8]
        candidate=min(eligible,key=lambda p:(p['priority'],-p['rank'],p['key'])) if eligible else None
        if chosen and (chosen[-1]['piece']['key'] if chosen[-1]['piece'] else None)==(candidate['key'] if candidate else None):
            chosen[-1]['b']=b
        else:
            chosen.append(dict(a=a,b=b,piece=candidate))
    if chosen[0]['a']!=start or chosen[-1]['b']!=end:
        raise ValueError('Shared selection does not cover requested range')
    output.mkdir(parents=True)
    inventory=deepcopy(legacy['inventory'])
    channels=[r['channel'] for r in inventory['channels'] if r['role']!='timing_only']
    summaries, decisions=[] , []
    for i,selection in enumerate(chosen):
        a,b,p=selection['a'],selection['b'],selection['piece']
        name=f'tape{i:05d}'
        selected=p['key'] if p else None
        maps={}
        if p:
            filename=name+'_maps.npz'
            np.savez_compressed(output/filename,**{selected:p['speed'],selected+'__support':p['support']})
            maps=dict(file=filename,rate=RATE,sha256=digest(output/filename))
        targets=[dict(channel=ch,action='render_and_review' if p else 'leave_uncorrected',
            selected=selected,reason='Shared tape correction from saved all-channel evidence' if p else 'No eligible tape-level curve',
            reference_spans=[]) for ch in channels]
        detail=dict(id=name,start_seconds=a,end_seconds=b,analysis_start=p['analysis_start'] if p else a,
            analysis_end=p['analysis_end'] if p else b,status='analyzed',targets=targets,maps=maps)
        write(output/(name+'.json'),detail)
        summaries.append(dict(detail,detail_file=name+'.json',detail_sha256=digest(output/(name+'.json'))))
        decisions.append(dict(start_seconds=a,end_seconds=b,selected=selected,
            provenance={k:v for k,v in p.items() if k not in {'speed','support'}} if p else None))
    shared=dict(schema=1,complete=True,source_validation='verified',full_tape=False,
        kind='shared_tape_regional',inventory=inventory,regions=summaries,
        tape_level=True,selection_depends_on_output_channel=False,
        source_plan_hashes=dict(regional=digest(regional_path),continuous=digest(continuous_path),assessment=digest(assessment_path)),
        start_seconds=start,end_seconds=end,production_accepted=False)
    write(output/'plan.json',shared)
    write(output/'selection.json',decisions)
    return shared,decisions
