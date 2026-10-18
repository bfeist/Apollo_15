"""Frozen follow-up choices and fresh target-event confirmation; bounded and isolated."""
from bespoke_validate import *
PLAN={
 'A15_T510073':[(3,117.5,'accepted_local_sinusoid'),(17,979.5,'phase_2_15')],
 'A15_T510075':[(30,1000.,'phase_2_15'),(30,1000.,'accepted_local_sinusoid')],
 'A15_T510076':[(11,414.5,'phase_2_15'),(30,1000.,'phase_2_15')],
 'A15_T510077':[(30,1005.5,'phase_2_15'),(30,1005.5,'phase_4_10')]}

def validate_window(tape,start,choices,events=None,kind='fresh_confirmation'):
    bych={c['channel']:c for c in tape['channels']};data={};sources=[]
    def audio(ch):
        if ch not in data:
            x,sr,m=get(bych[ch],tape,start,60);data[ch]=(x,sr);sources.append(m)
        return data[ch]
    if events is None:
        events=[]
        for ch in [12,15,20]:
            x,sr=audio(ch);events+= [dict(b,channel=ch,head=bych[ch]['head']) for b in beeps(x,sr)]
    results=[]
    for ch,hz,method in choices:
        ref=dict(channel=ch,carrier_hz=hz,scope='frozen_followup');rx,sr=audio(ch)
        if method=='accepted_local_sinusoid':_,e=measure(rx,sr,ref,ch)
        else:e=phase_map(rx,sr,hz,[2,15] if method=='phase_2_15' else [4,10])
        unique=unique_events(events,ch);measurements=[]
        for targetch in sorted({b['channel'] for b in unique}):
            x,sr=audio(targetch);bs=[b for b in unique if b['channel']==targetch]
            actual=sparse_evaluate(x,sr,e,bs);controls={str(s):sparse_evaluate(x,sr,np.roll(e,round(s*200)),bs) for s in [.071,5.]}
            for i,b in enumerate(actual):b.update(role=kind,same_head=bych[ch]['head']==bych[targetch]['head'],controls={s:cs[i]['reduction_percent'] for s,cs in controls.items()})
            measurements+=actual
        results.append(dict(reference=ref,method=method,events=measurements))
    return dict(start=start,kind=kind,sources=sources,results=results)

if __name__=='__main__':
    planpath=OUT/'confirmation_plan.json'
    if not planpath.exists():planpath.write_text(json.dumps(dict(choices=PLAN,search_positions='12 evenly spaced fractions .11 through .89; exclude within 120 s of any previously surveyed or event-searched interval',max_selected_windows=2,selection='at least two deduplicated target events; no correction-outcome selection'),indent=2))
    for tape in json.loads((OUT/'inventory.json').read_text()):
        if tape['tape'] not in PLAN:continue
        dest=OUT/(tape['tape']+'_confirmation.json')
        if dest.exists():continue
        choices=PLAN[tape['tape']];survey=json.loads((OUT/(tape['tape']+'_survey.json')).read_text());old=[w['start'] for w in survey['windows']]
        searchpath=OUT/(tape['tape']+'_event_search.json')
        if searchpath.exists():old += [w['start'] for w in json.loads(searchpath.read_text())['scans']]
        result=dict(tape=tape['tape'],choices=choices,retrospective=[],search=[],confirmation=[])
        # Directly test the unexamined alternative on existing passages, explicitly retrospective.
        if tape['tape'] in ['A15_T510073','A15_T510075']:
            for w in survey['windows']:
                if w['events']:result['retrospective'].append(validate_window(tape,w['start'],choices[1:] if tape['tape'].endswith('073') else choices,w['events'],'retrospective_alternative'))
            dest.write_text(json.dumps(result,indent=2))
        bych={c['channel']:c for c in tape['channels']}
        for start in [round(float(f)*(tape['duration']-60),2) for f in np.linspace(.11,.89,12)]:
            if any(abs(start-s)<120 for s in old):continue
            events=[];sources=[]
            for ch in [12,15,20]:
                x,sr,m=get(bych[ch],tape,start,60);sources.append(m);events += [dict(b,channel=ch,head=bych[ch]['head']) for b in beeps(x,sr)]
            count=len(unique_events(events,-1));result['search'].append(dict(start=start,sources=sources,events=events,unique_event_count=count))
            if count>=2:
                r=validate_window(tape,start,choices,events);result['confirmation'].append(r)
                print('CONFIRM',tape['tape'],start,[(v['method'],v['reference']['channel'],len(v['events']),round(float(np.median([e['reduction_percent'] for e in v['events']])),1),round(min(e['reduction_percent'] for e in v['events']),1)) for v in r['results']],flush=True)
            dest.write_text(json.dumps(result,indent=2))
            if len(result['confirmation'])>=2:break
        result['status']='bounded_confirmation_complete';dest.write_text(json.dumps(result,indent=2))
        print('CONFIRM END',tape['tape'],'searches',len(result['search']),'event_windows',len(result['confirmation']),flush=True)
