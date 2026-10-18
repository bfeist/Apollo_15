"""Small matched-gain listening set; isolated research outputs only."""
from survey import *
from audio_processing import detect,denoise
OUT=ROOT/'reports/irig_free_research_20261016'
DEST=OUT/'listening';DEST.mkdir(exist_ok=True)
rows=json.loads((OUT/'focused_validation.json').read_text());records=[];cards=[]
for tape,start,ch,method in [('A15_T510073',650,13,'frozen_nonmains'),('A15_T510088',200,13,'fixed_accepted'),('A15_T510088',500,15,'fixed_accepted'),('A15_T510092',200,15,'fixed_accepted')]:
    r=next(r for r in rows if r['tape']==tape and r['start']==start);bych={m['channel']:m for m in r['sources']}
    c=r['frozen_nonmains'] if method=='frozen_nonmains' else dict(channel=14,carrier_hz=631.5)
    ref,sr,_=read(Path(bych[c['channel']]['path']),start,50);_,e=measure(ref,sr,c,c['channel'])
    x,sr,meta=read(Path(bych[ch]['path']),start,50);clean=x.copy();passes=[]
    for _ in range(6):
        lines=detect(clean,sr)
        if not lines:break
        clean=remove(clean,sr,lines);passes.append(lines)
    clean,profile=denoise(clean,sr,reduction_cap_db=6);after=resample_at(clean,positions(e,sr,len(clean)))
    # One gain over both versions, determined jointly. Trim estimator/filter endpoints.
    b=clean[5*sr:40*sr];a=after[5*sr:40*sr];gain=.85/max(np.max(abs(b)),np.max(abs(a)),1e-12)
    offset=max(range(24),key=lambda k:float(np.sum(b[k*sr:(k+12)*sr]**2)))
    name=f'{tape}_{start}_CH{ch}_{method}_AB.wav'
    audio=np.r_[b[offset*sr:(offset+12)*sr]*gain,np.zeros(sr),a[offset*sr:(offset+12)*sr]*gain]
    sf.write(DEST/name,audio,sr,subtype='PCM_24');check,fs=sf.read(DEST/name)
    assert fs==sr and len(check)==25*sr and np.isfinite(check).all() and np.max(abs(check))<.851
    record=dict(tape=tape,source=meta,start=start,channel=ch,method=method,reference=c,passes=passes,
        noise_profile_seconds=profile,gain=gain,clip_start_source_seconds=start+5+offset,
        file=name,frames=len(check),peak=float(np.max(abs(check))))
    # Compare numerical validation on equally cleaned target; this is not a listening judgment.
    if ch==r['validation_channel']:record['cleaned_event_metrics']=evaluate(clean,sr,e,r['results']['fixed_accepted'])
    records.append(record)
    warning='Failure example: correction worsened the raw target measurement.' if tape.endswith('092') else 'Exploratory example; listening acceptance has not been established.'
    cards.append(f'<section><h2>{tape}, CH{ch}, {start+5+offset} seconds</h2><p>{method}. {warning}</p><audio controls src="{name}"></audio></section>')
(DEST/'manifest.json').write_text(json.dumps(records,indent=2))
(DEST/'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>IRIG-free research listening</title><style>body{font:18px system-ui;max-width:900px;margin:40px auto;padding:20px;background:#15202b;color:#eee}section{padding:20px;border:1px solid #607080;margin:20px 0}audio{width:100%}</style><h1>IRIG-free research listening</h1><p>Each file: 12 seconds before, 1 second silence, the same passage after. Both versions have identical tonal and broadband cleanup and one shared gain. All are experimental, including an intentional failure example. Originals and accepted reports remain unchanged.</p>'+''.join(cards),encoding='utf-8')
print('Verified',len(records),'matched A/B files',flush=True)
