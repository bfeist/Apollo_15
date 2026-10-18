"""Wrong-time controls for exploratory coherent pairs; never promotes a pair."""
from survey import *
OUT=ROOT/'reports/irig_free_research_20261016'

rows=json.loads((OUT/'summary.json').read_text());validation=json.loads((OUT/'validation.json').read_text());out=[]
for v in validation:
    if not v['results'].get('frozen_pair_diagnostic'):continue
    r=next(r for r in rows if r['tape']==v['tape'] and r['start']==v['start'])
    refs=v['frozen_pair']['references'];bych={m['channel']:m for m in r['sources']};es=[]
    for c in refs:
        x,sr,_=read(Path(bych[c['channel']]['path']),r['start'],r['duration']);_,e=measure(x,sr,c,c['channel']);es.append(e)
    e=np.mean(es,axis=0);x,sr,_=read(Path(bych[v['validation_channel']]['path']),r['start'],r['duration'])
    qs=v['results']['frozen_pair_diagnostic'];result={}
    for shift in [.071,.137,1.,5.]:result[str(shift)]=evaluate(x,sr,np.roll(e,round(shift*200)),qs)
    record=dict(tape=r['tape'],start=r['start'],references=refs,
        both_near_60hz_family=all(abs(c['carrier_hz']/60-round(c['carrier_hz']/60))*60<5 for c in refs),
        controls=result)
    out.append(record)
    print(r['tape'],r['start'],{k:round(float(np.median([q['reduction_percent'] for q in b])),1) for k,b in result.items()},flush=True)
(OUT/'pair_controls.json').write_text(json.dumps(out,indent=2))
