"""Compare frozen and interval-specific canonical references on independent bursts."""
from canonical_bespoke import *
from audio_processing import detect,denoise

def phase_map(x,sr,hz,band):
    t=np.arange(len(x))/sr
    z=signal.resample_poly(x*np.exp(-2j*np.pi*hz*t),1,sr//200)
    z=signal.sosfiltfilt(signal.butter(6,min(80,max(40,hz*.05)),fs=200,output='sos'),z)
    f=hz+np.gradient(np.unwrap(np.angle(z)))*200/(2*np.pi)
    centre=np.median(f[400:-400]);raw=np.clip((f-centre)/centre,-.06,.06)
    e=signal.sosfiltfilt(signal.butter(3,band,fs=200,btype='bandpass',output='sos'),raw)
    support=ndimage.gaussian_filter1d((abs(z)>np.median(abs(z))*.3).astype(float),20)
    tt=np.arange(len(e))/200
    return np.clip(e,-.04,.04)*support*np.clip(np.minimum(tt,tt[-1]-tt)/2,0,1)

def sparse_evaluate(x,sr,e,bursts):
    pos=positions(e,sr,len(x));result=[]
    for b in bursts:
        lo=max(0,int((b['start']-.15)*sr));hi=min(len(x),int((b['end']+.15)*sr))
        requested=pos[lo:hi];a=max(0,int(requested.min())-sr//5);z=min(len(x),int(requested.max())+sr//5)
        y=resample_at(x[a:z],requested-a)
        _,fb=burst_track(x[lo:hi],sr);_,fa=burst_track(y,sr)
        t=lo/sr+np.arange(len(fb))/1000;mt=np.interp(t*sr,np.arange(len(pos)),pos)/sr
        kb=(t>b['start']+.05)&(t<b['end']-.05);ka=(mt>b['start']+.05)&(mt<b['end']-.05)
        if min(kb.sum(),ka.sum())<60:continue
        before=float(np.std(fb[kb]));after=float(np.std(fa[ka]))
        result.append(dict(b,before_rms_hz=before,after_rms_hz=after,reduction_percent=100*(1-after/max(before,1e-12))))
    return result

def refs(first,tape):
    out=[]
    if first['candidates']:out.append(first['candidates'][0])
    non=[c for c in first['candidates'] if not c['near_mains_family'] and c['carrier_hz']>250 and all(c['channel']!=d['channel'] for d in out)]
    if non:out.append(non[0])
    historical={'A15_T510073':(17,404.),'A15_T510075':(13,406.),'A15_T510076':(11,414.5)}
    if tape in historical:
        ch,hz=historical[tape]
        if not any(c['channel']==ch and c['carrier_hz']==hz for c in out):out.append(dict(channel=ch,carrier_hz=hz))
    return [dict(channel=c['channel'],carrier_hz=c['carrier_hz'],scope='frozen_first_window') for c in out]

def unique_events(events,refch):
    eligible=sorted([b for b in events if b['channel']!=refch],key=lambda b:b['start']);clusters=[]
    for b in eligible:
        # Conservative across interleaved-head/feed delays as well as exact copies.
        # This can merge distinct rapid beeps; it avoids treating delayed replicas as votes.
        if clusters and b['start']-clusters[-1][0]['start']<=2.5:clusters[-1].append(b)
        else:clusters.append([b])
    return [dict(max(g,key=lambda b:b['amplitude_over_floor']),replica_channels=[b['channel'] for b in g]) for g in clusters]

def run(tape):
    dest=OUT/(tape['tape']+'_validation.json')
    if dest.exists():return
    survey=json.loads((OUT/(tape['tape']+'_survey.json')).read_text());frozen=refs(survey['windows'][0],tape['tape']);bych={c['channel']:c for c in tape['channels']}
    result=dict(tape=tape['tape'],frozen_references=frozen,windows=[],selection='Frozen references selected by reference-only first-window ranking; local alternative selected on first half per interval. No target-outcome gate.')
    for wi,w in enumerate(survey['windows']):
        choices=frozen.copy()
        if w['candidates']:
            c=w['candidates'][0]
            if not any(c['channel']==d['channel'] and c['carrier_hz']==d['carrier_hz'] for d in choices):choices.append(dict(channel=c['channel'],carrier_hz=c['carrier_hz'],scope='interval_specific'))
        data={};sources=[];results=[]
        def audio(ch):
            if ch not in data:
                x,sr,m=get(bych[ch],tape,w['start'],w['duration']);data[ch]=(x,sr);sources.append(m)
            return data[ch]
        for ref in choices:
            events=unique_events(w['events'],ref['channel']);x,sr=audio(ref['channel'])
            _,accepted=measure(x,sr,ref,ref['channel'])
            maps={'accepted_local_sinusoid':accepted,'phase_4_10':phase_map(x,sr,ref['carrier_hz'],[4,10]),'phase_2_15':phase_map(x,sr,ref['carrier_hz'],[2,15]),'accepted_half_gain':accepted*.5}
            for method,e in maps.items():
                measurements=[]
                for ch in sorted({b['channel'] for b in events}):
                    target,sr=audio(ch);bs=[b for b in events if b['channel']==ch]
                    main=sparse_evaluate(target,sr,e,bs)
                    controls={str(s):sparse_evaluate(target,sr,np.roll(e,round(s*200)),bs) for s in [.071,5.]}
                    for j,m in enumerate(main):
                        m.update(role='calibration' if wi==0 and m['end']<30 else 'heldout' if wi>0 or m['start']>32 else 'boundary_excluded',same_head=bych[ch]['head']==bych[ref['channel']]['head'],controls={s:cs[j]['reduction_percent'] for s,cs in controls.items()})
                    measurements+=main
                results.append(dict(reference=ref,method=method,events=measurements,speed_rms_percent=float(np.std(e[400:-400])*100)))
        result['windows'].append(dict(start=w['start'],sources=sources,results=results,event_detections=len(w['events'])))
        dest.write_text(json.dumps(result,indent=2))
        print('VALIDATE',tape['tape'],w['start'],'dedup events',max((len(r['events']) for r in results),default=0),'variants',len(results),flush=True)

def check_sparse():
    sr=8000;t=np.arange(20*sr)/sr;tau=t-.02/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
    x=np.sin(2*np.pi*2500*tau);e=.02*np.sin(2*np.pi*6.5*np.arange(4000)/200);bs=[dict(start=8.,end=8.25)]
    full=evaluate(x,sr,e,bs)[0];sparse=sparse_evaluate(x,sr,e,bs)[0]
    assert abs(full['after_rms_hz']-sparse['after_rms_hz'])<.001
    checks=[]
    for rate in [3.,6.5,12.]:
        truth=.02*np.sin(2*np.pi*rate*np.arange(4000)/200)
        warped=t-.02/(2*np.pi*rate)*np.cos(2*np.pi*rate*t)
        ref=np.sin(2*np.pi*777*warped);target=np.sin(2*np.pi*2500*warped)
        for band in ([4,10],[2,15]):
            curve=phase_map(ref,sr,777,band)
            corr=float(np.corrcoef(curve[400:-400],truth[400:-400])[0,1])
            if rate==6.5:assert corr>.98
            ev=sparse_evaluate(target,sr,curve,bs)[0]
            checks.append(dict(rate=rate,band=band,correlation=corr,reduction_percent=ev['reduction_percent']))
    stationary=phase_map(np.sin(2*np.pi*777*t),sr,777,[2,15])
    assert np.std(stationary[400:-400])<1e-6
    (OUT/'method_tests.json').write_text(json.dumps(dict(sparse_full_difference_hz=abs(full['after_rms_hz']-sparse['after_rms_hz']),synthetic=checks),indent=2))

if __name__=='__main__':
    check_sparse()
    for tape in json.loads((OUT/'inventory.json').read_text()):
        if tape['status']=='verified_complete_against_local_delivery':run(tape)
