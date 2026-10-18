"""Bounded search for independent target events, never selected on correction outcomes."""
from canonical_bespoke import *
from bespoke_validate import unique_events

for tape in json.loads((OUT/'inventory.json').read_text()):
    if tape['status']!='verified_complete_against_local_delivery':continue
    dest=OUT/(tape['tape']+'_event_search.json')
    if dest.exists():continue
    survey=json.loads((OUT/(tape['tape']+'_survey.json')).read_text())
    counts=[len(unique_events(w['events'],-1)) for w in survey['windows']]
    audit=dict(tape=tape['tape'],initial_unique_event_counts=counts,scans=[],selected_starts=[],purpose='Find target events only; no reference correction scores used')
    if sum(counts)>=8 and sum(n>0 for n in counts)>=3:
        audit['status']='initial_event_coverage_sufficient_for_bounded_comparison';dest.write_text(json.dumps(audit,indent=2));continue
    bych={c['channel']:c for c in tape['channels']}
    for start in [round(float(f)*(tape['duration']-60),2) for f in np.linspace(.08,.92,14)]:
        if any(abs(start-w['start'])<120 for w in survey['windows']):continue
        events=[];sources=[]
        for ch in [12,15,20]:
            x,sr,m=get(bych[ch],tape,start,60);sources.append(m);events.extend(dict(b,channel=ch,head=bych[ch]['head']) for b in beeps(x,sr))
        unique=unique_events(events,-1);audit['scans'].append(dict(start=start,sources=sources,events=events,unique_event_count=len(unique)))
        if len(unique)>=2:audit['selected_starts'].append(start)
        dest.write_text(json.dumps(audit,indent=2))
        if len(audit['selected_starts'])>=2:break
    audit['status']='bounded_search_complete';dest.write_text(json.dumps(audit,indent=2))
    print('EVENT SEARCH',tape['tape'],'scans',len(audit['scans']),'selected',audit['selected_starts'],flush=True)
    if audit['selected_starts']:survey_tape(tape,audit['selected_starts'])
