"""Outcome-independent grouping of delayed potential response-feed copies.

Fixed-passage research protocol. Timing-pattern agreement establishes a
potential response dependency, never shared transport. Ambiguous periodic
matches are withheld rather than counted as independent evidence. No correction
metrics, candidate frequencies, channel labels or winning scores are inputs.
"""
from collections import defaultdict
import hashlib
import json
import numpy as np

VERSION='lag-aware-response-groups-research-v2'
ROLE_CYCLE=('calibration','calibration','validation','validation','final')


def _pairs(a,b,lag,tolerance):
    options=[]
    for i,t in enumerate(a):
        j=int(np.searchsorted(b,t+lag))
        for k in (j-1,j):
            if 0<=k<len(b) and abs(b[k]-t-lag)<=tolerance:
                options.append((abs(b[k]-t-lag),i,k))
    used_a=set();used_b=set();matches=[]
    for _,i,j in sorted(options):
        if i not in used_a and j not in used_b:
            matches.append((i,j));used_a.add(i);used_b.add(j)
    return sorted(matches)


def fit_delay(a,b,maximum_lag=20.,tolerance=.06,minimum_matches=3):
    a=np.asarray(a);b=np.asarray(b)
    differences=(b[:,None]-a[None,:]).ravel()
    differences=differences[abs(differences)<=maximum_lag]
    if not len(differences):return dict(status='no_delayed_model',matches=[])
    centers=np.unique(np.round(differences/(tolerance/2))*(tolerance/2))
    models={}
    for center in centers:
        matches=_pairs(a,b,float(center),tolerance)
        if len(matches)<minimum_matches:continue
        lag=float(np.median([b[j]-a[i] for i,j in matches]))
        matches=_pairs(a,b,lag,tolerance)
        if len(matches)<minimum_matches:continue
        spread=float(np.median([abs(b[j]-a[i]-lag) for i,j in matches]))
        models[tuple(matches)]=dict(delay_seconds=lag,matches=matches,matched_events=len(matches),median_absolute_deviation_seconds=spread)
    if not models:return dict(status='no_delayed_model',matches=[])
    ordered=sorted(models.values(),key=lambda r:(-r['matched_events'],r['median_absolute_deviation_seconds'],abs(r['delay_seconds']),r['delay_seconds']))
    best=ordered[0]
    competing=[r for r in ordered[1:] if abs(r['delay_seconds']-best['delay_seconds'])>2*tolerance
               and r['matched_events']>=max(minimum_matches,.8*best['matched_events'])]
    if competing:return dict(status='ambiguous_periodic_dependency',best=best,competing=competing,matches=[])
    return dict(status='potential_delayed_copy',**best)


def assign_roles(batches,*,maximum_lag=20.,tolerance=.06,minimum_matches=3,coincidence_seconds=.5,independent_seconds=3.):
    """Freeze response groups/roles for a complete fixed passage before scoring.

    This API is intentionally not an online append operation: a future stream
    must preserve its fitted delay models and already assigned group roles.
    """
    by_channel={};rejected=0
    for channel,events,origin in batches:
        accepted=[]
        for e in sorted(events,key=lambda r:r.get('start_seconds',origin+r.get('start',0.))):
            start=float(e.get('start_seconds',origin+e.get('start',0.)))
            end=float(e.get('end_seconds',origin+e.get('end',0.)))
            if not np.isfinite([start,end]).all() or end<=start:raise ValueError('Invalid response coordinates')
            if accepted and start-accepted[-1]['start_seconds']<independent_seconds:
                rejected+=1;continue
            identifier=hashlib.sha256(json.dumps([str(channel),start,end]).encode()).hexdigest()[:24]
            accepted.append(dict(e,channel=channel,start_seconds=start,end_seconds=end,event_id=identifier))
        by_channel[channel]=accepted
    channels=sorted(by_channel,key=str);all_events=[e for ch in channels for e in by_channel[ch]]
    positions={e['event_id']:i for i,e in enumerate(all_events)};parents=list(range(len(all_events)))
    def root(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    def union(i,j):
        a,b=root(i),root(j)
        if a!=b:parents[max(a,b)]=min(a,b)
    models=[];quarantined=set();edges=0
    for ai,a_channel in enumerate(channels):
        a=by_channel[a_channel];ta=[e['start_seconds'] for e in a]
        for b_channel in channels[ai+1:]:
            b=by_channel[b_channel];tb=[e['start_seconds'] for e in b]
            model=fit_delay(ta,tb,maximum_lag,tolerance,minimum_matches) if ta and tb else dict(status='no_delayed_model',matches=[])
            if model['status']=='ambiguous_periodic_dependency':
                quarantined.update(e['event_id'] for e in a+b)
            elif model['status']=='potential_delayed_copy':
                for i,j in model['matches']:
                    union(positions[a[i]['event_id']],positions[b[j]['event_id']]);edges+=1
            if model['status']!='no_delayed_model':models.append(dict(channel_a=a_channel,channel_b=b_channel,**model))
            # A fitted delayed feed uses its own timebase. Applying a second
            # zero-lag rule would merge different events when the lag happens
            # to resemble an inter-event interval. Unknown pairs retain the
            # conservative near-coincidence rule; periodic ambiguity withholds.
            if model['status']=='potential_delayed_copy' and abs(model['delay_seconds'])>coincidence_seconds:
                continue
            for i,t in enumerate(ta):
                left=np.searchsorted(tb,t-coincidence_seconds,side='left')
                right=np.searchsorted(tb,t+coincidence_seconds,side='right')
                for j in range(left,right):union(positions[a[i]['event_id']],positions[b[j]['event_id']]);edges+=1
    groups=defaultdict(list)
    for i,e in enumerate(all_events):groups[root(i)].append(e)
    ordered=sorted(groups.values(),key=lambda es:(min(e['start_seconds'] for e in es),sorted(e['event_id'] for e in es)))
    assigned={ch:[] for ch in channels};group_rows=[];cycle_index=0
    for members in ordered:
        ids=sorted(e['event_id'] for e in members)
        gid=hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:24]
        duplicate_channel=len({e['channel'] for e in members})<len(members)
        withheld=duplicate_channel or any(i in quarantined for i in ids)
        role='withheld_ambiguous_dependency' if withheld else ROLE_CYCLE[cycle_index%len(ROLE_CYCLE)]
        index=cycle_index
        if not withheld:cycle_index+=1
        group_rows.append(dict(group_id=gid,role=role,index=index,members=ids,
            start_seconds=min(e['start_seconds'] for e in members),end_seconds=max(e['end_seconds'] for e in members),
            raw_event_count=len(members),withheld=withheld,duplicate_channel=duplicate_channel))
        for event in members:
            e={k:v for k,v in event.items() if k not in ('start','end','role','_synchronized_role')}
            assigned[e['channel']].append(dict(e,role=role,_synchronized_role=role,
                synchronized_event_index=index,synchronized_group_id=gid))
    for es in assigned.values():es.sort(key=lambda e:e['start_seconds'])
    state=dict(version=VERSION,mode='fixed_passage_before_scoring',maximum_lag_seconds=maximum_lag,
        tolerance_seconds=tolerance,minimum_matches=minimum_matches,coincidence_seconds=coincidence_seconds,
        independent_seconds=independent_seconds,raw_events=len(all_events),response_groups=len(ordered),
        usable_response_groups=cycle_index,withheld_groups=sum(g['withheld'] for g in group_rows),
        rejected_close_events=rejected,potential_dependency_edges=edges,models=models,groups=group_rows,
        qualification='Potential response dependence only; no transport/common-head claim',
        streaming_limitation='Do not recompute earlier roles on extension; online model/role persistence not implemented')
    return assigned,state
