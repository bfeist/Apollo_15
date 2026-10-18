"""Additional detected-event windows: frozen references and negative controls."""
from survey import *
OUT=ROOT/'reports/irig_free_research_20261016'

def main():
    rows=json.loads((OUT/'summary.json').read_text());sources={r['tape']:r for r in rows if r.get('start')==80}
    events=json.loads((OUT/'additional_event_search.json').read_text());out=[]
    for tape in events:
        base=sources[tape['tape']];frozen=None
        for ev in tape['events']:
            # Replicated beep on another channel is evaluated below, not a second window.
            if any(r['tape']==tape['tape'] and r['start']==ev['start'] for r in out):continue
            start=ev['start'];target_ch=ev['channel'];data={};prov=[]
            for m in base['sources']:
                x,sr,meta=read(Path(m['path']),start,50);data[m['channel']]=x;prov.append(dict(meta,channel=m['channel']))
            tt,fixed,stats,_=estimate(data[14],sr)
            info=dict(tape=tape['tape'],start=start,validation_channel=target_ch,sources=prov,fixed_tracking=stats,results={})
            maps={'fixed_accepted':fixed}
            if frozen is None:
                # Search non-mains candidates without using target beeps or their outcomes.
                bank=[]
                for ch,x in data.items():
                    if ch==target_ch:continue
                    conditioning=[dict(hz=float(h),width_hz=2) for h in range(60,1801,60)]
                    y=remove(x,sr,conditioning)
                    for c in candidates(y,sr):
                        if c['occupancy']<.7 or c['contrast_db']<10 or abs(c['carrier_hz']/60-round(c['carrier_hz']/60))*60<5:continue
                        try:
                            meta,e=measure(x,sr,c,ch)
                            if .2<meta['tracking']['rms_percent']<3 and meta['tracking']['support_gate_mean']>.8:bank.append(meta)
                        except ValueError:pass
                bank.sort(key=lambda c:c['spectral_score_db'],reverse=True)
                frozen=bank[0] if bank else False
                info['discovery_bank']=bank
            info['frozen_nonmains']=frozen
            if frozen:
                meta,e=measure(data[frozen['channel']],sr,frozen,frozen['channel'])
                maps['frozen_nonmains']=e;info['nonmains_tracking']=meta
            x=data[target_ch];qs=ev['bursts']
            for name,e in maps.items():info['results'][name]=evaluate(x,sr,e,qs)
            for name,e in maps.items():
                for shift in [.071,.137,1.,5.]:
                    label='fixed' if name=='fixed_accepted' else name
                    info['results'][f'{label}_shift_{shift}s']=evaluate(x,sr,np.roll(e,round(shift*200)),qs)
            info['replication']={ch:evaluate(v,sr,fixed,beeps(v,sr)) for ch,v in data.items() if ch not in [14,target_ch] and beeps(v,sr)}
            out.append(info);(OUT/'focused_validation.json').write_text(json.dumps(out,indent=2))
            print(tape['tape'],start,'CH',target_ch,'new reference',None if not frozen else (frozen['channel'],frozen['carrier_hz']),
                {k:round(float(np.median([q['reduction_percent'] for q in v])),1) if v else None for k,v in info['results'].items()},flush=True)

if __name__=='__main__':main()
