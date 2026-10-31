"""Source-supported acquisition opportunities, with unchanged evidence counts.

The 300s measurement runner demands one reference across every selected event
in that passage. This adapter instead supplies the existing acquisition ledger
with each contiguous source-support island. It proposes metadata only; it does
not evaluate, accept, join or extend a correction.
"""
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from continuous_acquisition import AcquisitionEvidence

VERSION='source-island-acquisition-research-v1'


def source_amplitude_reference(amplitude,*,rate=200,available_end=120.,window_seconds=8.):
    """Strongest complete fixed source window, without response outcomes.

    This is an acquisition hypothesis, not proof of a historical oscillator.
    Restrict input to source context available at the proposed acquisition time.
    An eight-second median rejects isolated bursts while preserving a short
    persistent reference that would be lost in a whole-passage median.
    """
    x=np.asarray(amplitude,dtype=float)
    width=round(rate*window_seconds);end=min(len(x),int(np.floor(available_end*rate)))
    if width<1 or end<width or not np.isfinite(x[:end]).all() or np.any(x[:end]<0):
        raise ValueError('Need finite nonnegative complete source acquisition windows')
    starts=np.arange(0,end-width+1,width)
    medians=np.asarray([np.median(x[a:a+width]) for a in starts])
    best=int(np.argmax(medians));reference=float(medians[best])
    if reference<=0:raise ValueError('No measurable source amplitude')
    return reference,dict(methodology='fixed-source-window-amplitude-research-v1',
        window_seconds=window_seconds,available_end_seconds=end/rate,
        selected_window_seconds=[float(starts[best]/rate),float((starts[best]+width)/rate)],
        complete_windows=len(starts),response_outcomes_accessed=False)


class PresetRoles(AcquisitionEvidence):
    def event_role(self,event,index):
        role=event['role']
        if role not in ('calibration','validation','final'):
            raise ValueError('Only already-frozen usable response roles enter acquisition')
        return role


def common_event_acquisition(events,duration,monitor_seconds=60.,minimum_final_events=0):
    """Freeze earliest event-ready interval before looking at any predictor.

    This bounded alternative cannot pick later/better event subsets from
    candidate losses. If its source support fails, that attempt stays failed.
    """
    roles={};converted=[]
    for event in events:
        if event['role']=='withheld_ambiguous_dependency':continue
        group=event.get('synchronized_group_id',event.get('synchronized_event_index'))
        if group is None:raise ValueError('Response dependency group identity is required')
        if group in roles and roles[group]!=event['role']:raise ValueError('Response family role conflict')
        if group in roles:continue
        roles[group]=event['role']
        converted.append({k:v for k,v in event.items() if k not in ('start_seconds','end_seconds')})
    if not isinstance(minimum_final_events,int) or minimum_final_events<0:
        raise ValueError('Minimum final-event availability must be a nonnegative integer')
    ledger=PresetRoles();pending=sorted(converted,key=lambda e:e['start'])
    horizons=ledger.horizons
    for end in np.unique(np.r_[np.arange(monitor_seconds,duration,monitor_seconds),duration]):
        available=[e for e in pending if e['end']<=end]
        pending=[e for e in pending if e['end']>end]
        ledger.horizons=horizons;ledger.ingest('target',available)
        for horizon in horizons:
            ledger.horizons=(horizon,)
            proposal=ledger.propose('target',float(end),available_end=duration)
            if proposal['status']=='ready_for_search' and len(proposal['roles']['final'])>=minimum_final_events:
                proposal['minimum_final_event_availability']=minimum_final_events
                return proposal
    return None


def source_islands(support,rate=200,minimum_support=.95):
    mask=np.asarray(support)>=minimum_support
    edges=np.diff(np.r_[False,mask,False].astype(int))
    return [(a/rate,b/rate) for a,b in zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1))]


def qualified_support_island(support,calibration,validation,final=(),*,rate=200,minimum_final_events=0):
    """Keep one continuous C/V-supported source island; never auto-reacquire.

    Final coordinates can impose availability, never a response-score choice.
    This proposes an experimental support mask, not final numeric approval.
    """
    evidence=list(calibration)+list(validation)
    if len(calibration)<2 or len(validation)<2:return None,dict(status='insufficient_independent_event_counts')
    lo=int(np.floor(min(e.get('start',e.get('start_seconds')) for e in evidence)*rate))
    hi=int(np.ceil(max(e.get('end',e.get('end_seconds')) for e in evidence)*rate))
    mask=np.asarray(support)>=.95
    if lo<0 or hi>len(mask) or hi<=lo or not np.all(mask[lo:hi]):
        return None,dict(status='no_contiguous_support_across_calibration_validation')
    edges=np.diff(np.r_[False,mask,False].astype(int));starts=np.flatnonzero(edges==1);ends=np.flatnonzero(edges==-1)
    selected=next((tuple((int(a),int(b))) for a,b in zip(starts,ends) if a<=lo and hi<=b),None)
    if selected is None:raise ValueError('Continuous support component not found')
    a,b=selected;reserved=[];families=set()
    for event in final:
        group=event.get('synchronized_group_id',event.get('synchronized_event_index'))
        if group is None:raise ValueError('Reserved family identity is required')
        start=event.get('start',event.get('start_seconds'));end=event.get('end',event.get('end_seconds'))
        if a/rate<=start and end<=b/rate and group not in families:
            reserved.append(event);families.add(group)
    info=dict(status='single_source_supported_island',interval_seconds=[a/rate,b/rate],
        retained_final_families=len(reserved),minimum_final_availability=minimum_final_events,
        retained_final_family_ids=sorted(families,key=str),
        removed_other_islands=sum(not (x==a and y==b) for x,y in zip(starts,ends)),
        reserved_outcomes_accessed=False,correction_qualified=False)
    if len(reserved)<minimum_final_events:return None,dict(info,status='insufficient_final_families_in_same_island')
    result=np.zeros_like(support);result[a:b]=support[a:b]
    return result,info


def opportunities(events,support,*,rate=200,monitor_seconds=60.,horizons=(60.,120.,180.,300.)):
    """Use fixed roles/physical support only; never read response outcomes.

    A caller comparing candidates must freeze a common attempt window and its
    event sets before scoring, consume its selected validation once, and leave
    unsupported/reacquired islands unqualified. These proposals alone do not
    supply a usable timing reference.
    """
    roles={}
    usable=[]
    for event in events:
        if event['role']=='withheld_ambiguous_dependency':continue
        group=event.get('synchronized_group_id',event.get('synchronized_event_index'))
        if group is None:raise ValueError('Response dependency group identity is required')
        if group in roles and roles[group]!=event['role']:
            raise ValueError('One response family has conflicting roles; refuse acquisition')
        if group in roles:continue
        roles[group]=event['role'];usable.append(event)
    ledger=PresetRoles(horizons=horizons);ledger.ingest('target',usable)
    duration=len(support)/rate;proposals=[];waiting=[]
    for lo,hi in source_islands(support,rate):
        if hi-lo<3:continue
        ends=np.unique(np.r_[np.arange(monitor_seconds,duration+monitor_seconds,monitor_seconds),hi])
        proposal=None
        for cursor in sorted(float(e) for e in ends if lo<e<=duration):
            end=min(cursor,hi)
            p=ledger.propose('target',end,available_start=lo,available_end=hi)
            if p['status']=='ready_for_search':
                proposal=dict(p,physical_support_island=[lo,hi],
                    available_final_events_in_island=[e for e in usable if e['role']=='final' and lo<=e.get('start_seconds',e.get('start',0.)) and e.get('end_seconds',e.get('end',0.))<=hi],
                    status='source_supported_acquisition_opportunity',correction_qualified=False)
                break
        if proposal:proposals.append(proposal)
        else:waiting.append(dict(physical_support_island=[lo,hi],status='insufficient_independent_events_in_island'))
    return dict(methodology=VERSION,opportunities=sorted(proposals,key=lambda p:(p['end_seconds'],p['start_seconds'])),
        waiting=waiting,roles_unchanged=True,minimum_per_split=2,independent_seconds=3.,
        thresholds_relaxed=False,validation_outcomes_accessed=False,correction_qualified=False)
