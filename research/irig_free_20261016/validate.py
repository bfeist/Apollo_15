"""Synthetic bandwidth controls and frozen-reference, held-out excerpt validation."""
from survey import *

OUT=ROOT/'reports/irig_free_research_20261016'

def synthetic_checks():
    sr=8000;t=np.arange(16*sr)/sr;rng=np.random.default_rng(20261016);out=[]
    for mode,hz in [('rate',1),('rate',3),('rate',4),('rate',6.5),('rate',9),('rate',12),('rate',16),('dropout',6.5),('stationary_reference',6.5),('varying_rate',6.5)]:
        phase=2*np.pi*(hz*t if mode!='varying_rate' else 5*t+5/32*t*t)
        truth=.025*np.sin(phase)
        from scipy.integrate import cumulative_trapezoid
        tau=t+cumulative_trapezoid(truth,dx=1/sr,initial=0)
        ref=np.sin(2*np.pi*631.5*(t if mode=='stationary_reference' else tau))
        if mode=='dropout':ref[(t>6)&(t<10)]=0
        ref+=2*np.sin(2*np.pi*600*t)+2*np.sin(2*np.pi*660*t)+.15*rng.normal(size=len(t))
        tt,e,stats,raw=estimate(ref,sr);keep=(tt>2)&(tt<14)
        target=np.sin(2*np.pi*2475*tau)
        _,before=burst_track(target,sr);_,after=burst_track(resample_at(target,positions(e,sr,len(t))),sr)
        ratio=float(np.std(after[2000:-2000])/np.std(before[2000:-2000]))
        corr=float(np.corrcoef(e[keep],np.interp(tt[keep],t,truth))[0,1])
        out.append(dict(mode=mode,injected_rate_hz=hz,tracking=stats,correlation=corr,
            rendered_remaining_rms_ratio=ratio,dropout_estimated_rms_percent=float(np.std(e[(tt>7)&(tt<9)])*100)))
    (OUT/'synthetic.json').write_text(json.dumps(out,indent=2));print('synthetic',[(r['mode'],r['injected_rate_hz'],round(r['rendered_remaining_rms_ratio'],3)) for r in out],flush=True)

def select_pair(row,curves):
    cs=[c for c in row['candidates'] if c.get('occupancy') is not None and c['occupancy']>=.7 and c['contrast_db']>=10 and .1<c['tracking']['rms_percent']<3]
    pairs=[]
    for i,c in enumerate(cs):
        for d in cs[i+1:]:
            ratio=max(c['carrier_hz'],d['carrier_hz'])/min(c['carrier_hz'],d['carrier_hz'])
            if c['channel']==d['channel'] or abs(c['carrier_hz']-d['carrier_hz'])<80 or abs(ratio-round(ratio))<.03:continue
            corr=float(np.corrcoef(curves[f"candidate_{c['id']}"][400:-400],curves[f"candidate_{d['id']}"][400:-400])[0,1])
            pairs.append((corr,c,d))
    if not pairs:return None
    best=max(pairs,key=lambda p:p[0])
    return dict(correlation=best[0],references=[best[1],best[2]],gate_pass=best[0]>=.6)

def validate():
    rows=json.loads((OUT/'summary.json').read_text());frozen={};out=[]
    for row in rows:
        if 'sources' not in row:continue
        tag=f"{row['tape']}_{int(row['start'])}";curves=np.load(OUT/tag/'curves.npz')
        if row['tape'] not in frozen:frozen[row['tape']]=select_pair(row,curves)
        pair=frozen[row['tape']]
        data={}
        for m in row['sources']:
            x,sr,meta=read(Path(m['path']),row['start'],row['duration'])
            if meta['excerpt_float64_sha256']!=m['excerpt_float64_sha256']:raise ValueError('source changed since survey')
            data[m['channel']]=x
        # Candidate tone events must be outside the fixed reference CH14.
        burstsets={ch:beeps(x,sr) for ch,x in data.items() if ch!=14}
        validation_ch=max(burstsets,key=lambda ch:len(burstsets[ch]))
        qs=burstsets[validation_ch]
        # Add CH12 replication as separate measurements, not independent event counts.
        maps={'fixed_accepted':curves[f"candidate_{row['selected_fixed']}"]}
        if row['selected_auto'] is not None:maps['spectral_auto']=curves[f"candidate_{row['selected_auto']}"]
        details=dict(tape=row['tape'],start=row['start'],group=row['group'],validation_channel=validation_ch,
            candidate_counts={ch:len(b) for ch,b in burstsets.items()},frozen_pair=pair,results={})
        if pair and pair['gate_pass']:
            es=[]
            for c in pair['references']:
                meta,e=measure(data[c['channel']],sr,c,c['channel']);es.append(e)
            corr=float(np.corrcoef(es[0][400:-400],es[1][400:-400])[0,1])
            details['frozen_pair_holdout_correlation']=corr
            maps['frozen_pair_diagnostic']=np.mean(es,axis=0)
            details['frozen_pair_touches_validation_channel']=validation_ch in [c['channel'] for c in pair['references']]
        x=data[validation_ch]
        for name,e in maps.items():
            details['results'][name]=evaluate(x,sr,e,qs)
        for shift in [.071,.137,1.,5.]:
            # Deliberately incorrect timing, no phase tuning to event data.
            e=np.roll(maps['fixed_accepted'],round(shift*200))
            details['results'][f'fixed_shift_{shift}s']=evaluate(x,sr,e,qs)
        # Duplicate event in another channel checks replication, not independent oscillator evidence.
        if validation_ch!=12:
            details['ch12_replication']=evaluate(data[12],sr,maps['fixed_accepted'],burstsets.get(12,[]))
        out.append(details);(OUT/'validation.json').write_text(json.dumps(out,indent=2))
        print(tag,'CH',validation_ch,'events',len(qs),'pair',pair['correlation'] if pair else None,
            {k:round(float(np.median([q['reduction_percent'] for q in v])),1) if v else None for k,v in details['results'].items()},flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--synthetic-only',action='store_true');a=ap.parse_args()
    synthetic_checks()
    if not a.synthetic_only:validate()
