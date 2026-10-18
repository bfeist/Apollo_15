"""Summarize the frozen study and render measured fresh candidates, never production."""
from expanded_followup import *
from html import escape
import hashlib
from datetime import datetime, timezone

def reductions(events, margin='0.04'):
    return [100*(1-e['margins'][margin]['after']['rms_hz']/max(e['margins'][margin]['before']['rms_hz'],1e-12)) for e in events]

def acceptable(o):
    ev=o['events']
    if not ev:return False
    for margin in ['0.025','0.04','0.055']:
        r=reductions(ev,margin)
        if min(r)<-10 or np.median(r)<20:return False
        for control in o['controls'].values():
            if np.median(r)<=np.median(reductions(control,margin)):return False
    return True

def full_render_check(clean, warped, sr, e, events):
    pos=positions(e,sr,len(clean));results=[]
    for b in events:
        lo=int((b['start']-.15)*sr);hi=int((b['end']+.15)*sr)
        _,freq=burst_track(warped[lo:hi],sr)
        t=lo/sr+np.arange(len(freq))/1000;mt=np.interp(t*sr,np.arange(len(pos)),pos)/sr
        measured={}
        for margin in [.025,.04,.055]:
            keep=(mt>b['start']+margin)&(mt<b['end']-margin)
            actual=float(np.std(freq[keep]));expected=b['margins'][str(margin)]['after']['rms_hz']
            if abs(actual-expected)>.15:raise ValueError('Full rendering differs from sparse evaluator')
            measured[str(margin)]=dict(rendered_rms_hz=actual,sparse_rms_hz=expected)
        results.append(dict(start=b['start'],checks=measured))
    return results

def render(tape,w,o):
    folder=DEST/tape['tape'];by={c['channel']:c for c in tape['channels']}
    x,sr,source=get(by[o['channel']],tape,w['start'],30);clean,cm=cleaned(x,sr)
    assert cm==o['cleanup'],'Cleanup provenance changed'
    e=np.load(folder/f"fresh_{w['start']:.2f}.npz")['curve'];warped=resample_at(clean,positions(e,sr,len(clean)))
    check=full_render_check(clean,warped,sr,e,o['events'])
    offset=max(2,min(16,o['events'][0]['start']-6));lo=round(offset*sr);hi=lo+12*sr
    a=clean[lo:hi];b=warped[lo:hi];gain=.8/max(np.max(abs(a)),np.max(abs(b)),1e-12)
    name=f"{tape['tape']}_{w['start']:.2f}_CH{o['channel']}";files={}
    for label,y in [('before',a),('after',b),('before_then_after',np.concatenate([a,np.zeros(sr),b]))]:
        p=DEST/(name+'_'+label+'.wav');sf.write(p,y*gain,sr,subtype='FLOAT')
        readback,rate=sf.read(p);assert rate==sr and len(readback)==len(y) and np.isfinite(readback).all() and np.max(abs(readback))<=.80001
        files[label]=dict(file=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),frames=len(readback),peak=float(np.max(abs(readback))))
    return dict(tape=tape['tape'],channel=o['channel'],window_start=w['start'],excerpt_start=w['start']+offset,duration=12,reference=w['reference'],mode=w['mode'],tracking=w['tracking'],source=source,cleanup=cm,shared_gain=gain,metrics=stats(o['events']),events=o['events'],controls=o['controls'],render_checks=check,files=files,listening_approved=False,note='Selected after fresh measurements; listening sample is exploratory, not an independent blinded trial.')

def main():
    inv=json.loads((DEST/'frozen_inventory.json').read_text());rows=[];candidates=[];sources={};all_events=0;discovery_count=0;holdout_count=0;partial_count=0
    def collect(obj):
        if isinstance(obj,dict):
            if 'excerpt_float64_sha256' in obj:sources[(obj['path'],obj['start_seconds'],obj['duration_seconds'])]=obj
            for v in obj.values():collect(v)
        elif isinstance(obj,list):
            for v in obj:collect(v)
    for t in inv:
        folder=DEST/t['tape'];row=dict(tape=t['tape'],inventory_status=t['status'])
        if t['status']!='eligible_canonical_snapshot':row['reason']=t['reasons'];rows.append(row);continue
        s=json.loads((folder/'survey.json').read_text());v=json.loads((folder/'validation.json').read_text());f=json.loads((folder/'followup.json').read_text());p=json.loads((folder/'partial_analysis.json').read_text())
        for obj in [s,v,f,p]:collect(obj)
        discovery_count+=len(s['windows']);holdout_count+=len(v['search']);partial_count+=len(p)
        ev=[e for w in v['windows'] for e in w['events']];all_events+=len(ev)
        fixed=[dict(start=w['start'],channel=o['channel'],method=o['method'],**stats(o['events'])) for w in v['windows'] for o in w['outcomes']]
        fresh=[dict(start=w['start'],channel=o['channel'],qualifies=acceptable(o),**stats(o['events'])) for w in f.get('confirmation',[]) for o in w['outcomes']]
        row.update(discovery_windows=len(s['windows']),channels=len(t['channels']),holdout_search_windows=len(v['search']),holdout_event_detections=len(ev),fixed_method_results=fixed,followup_status=f['status'],selected_reference=f.get('selected',{}),fresh=fresh,partial_gates=[w['consensus'] for w in p])
        # Avoid duplicating voluminous calibration events in the summary.
        if row['selected_reference']:row['selected_reference']={k:z for k,z in row['selected_reference'].items() if k!='events'}
        passing=[(w,o) for w in f.get('confirmation',[]) for o in w['outcomes'] if acceptable(o)]
        if passing:
            w,o=max(passing,key=lambda pair:pair[1]['median_reduction']);candidates.append((t,w,o))
        rows.append(row)
    candidates.sort(key=lambda z:z[2]['median_reduction'],reverse=True)
    rendered=[render(*c) for c in candidates[:6]]
    changed=[]
    for t in inv:
        if t['status']!='eligible_canonical_snapshot':continue
        for c in t['channels']:
            st=Path(c['path']).stat()
            if (st.st_size,st.st_mtime_ns)!=(c['bytes'],c['mtime_ns']):changed.append(c['path'])
    if changed:raise ValueError(f'Canonical sources changed: {changed}')
    summary=dict(created_utc=datetime.now(timezone.utc).isoformat(),eligible_tapes=sum(t['status']=='eligible_canonical_snapshot' for t in inv),discovery_windows=discovery_count,discovery_channel_windows=discovery_count*30,holdout_search_windows=holdout_count,holdout_event_detections=all_events,partial_windows=partial_count,unique_excerpt_reads=len(sources),unique_source_files=len({k[0] for k in sources}),passing_fresh_tapes=len(candidates),listening_examples=len(rendered),canonical_sources_unchanged=True,rows=rows)
    (DEST/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False));(DEST/'sources.json').write_text(json.dumps(list(sources.values()),indent=2,allow_nan=False));(DEST/'listening.json').write_text(json.dumps(rendered,indent=2,allow_nan=False))
    lines=['# Expanded per-tape flutter study','',f"Completed {summary['eligible_tapes']} eligible tapes, {discovery_count} all-channel discovery windows ({discovery_count*30} channel excerpts), {holdout_count} held-out search windows, and {partial_count} separate partial-analysis windows.",'',f"{len(sources)} distinct source/start/duration reads across {summary['unique_source_files']} canonical files. The {all_events} held-out channel-event detections are not independent physical beeps: communication feeds can be duplicated. Measurements across methods and margins are repeated evaluations, not extra trials.",'','## Per-tape results','','| Tape | Fixed holdout detections | Follow-up | Fresh median / worst reduction | Partial gate passes |','|---|---:|---|---|---:|']
    for r in rows:
        if r['inventory_status']!='eligible_canonical_snapshot':lines.append(f"| {r['tape']} | — | Deferred: recent source at frozen snapshot | — | — |");continue
        fresh=r['fresh'];values=[f"CH{x['channel']} @ {x['start']:.2f}s: {x['median_reduction']:.1f}% / {x['worst_reduction']:.1f}%" for x in fresh]
        lines.append(f"| {r['tape']} | {r['holdout_event_detections']} | {r['followup_status']} | {'; '.join(values) or 'No fresh correction evidence'} | {sum(g['qualified'] for g in r['partial_gates'])}/2 |")
    lines += ['','## Interpretation and limits','','Fresh candidates are measured local improvements, **not listening-approved corrections**. Calibration selected among up to four carrier choices and three methods; fresh windows used the frozen choice. Rendering selection then used fresh results, so the listening set is intentionally favorable and not an unbiased success-rate estimate. See every failure in per-tape validation/followup JSON.','', 'A listening candidate requires median RMS reduction at least 20%, worst event no worse than -10%, and better median performance than wrong-sign and 1-second-shift controls at all three edge exclusions (25/40/55 ms). These are exploratory reporting criteria, not certified safety bounds. Periodic shift controls can accidentally preserve phase; passing them does not prove transport provenance.','', 'Raw reference audio remains separate from speech cleanup. Targets use iterative full-band notches followed by the existing denoiser capped at 6 dB. Broad/narrow estimators search modulation rates from 2–20 Hz and apply signed 2–30 Hz motion; the legacy local-fit comparator retains its historical 4–10 Hz search. No rate was forced to 6.5 Hz. Spectral peers may share interference/harmonics/feeds and are not automatically independent witnesses.','', 'Tone-free branch: phase-increment partial tracking excludes known line bands and long continuous ridges, aggregates short partial trajectories, and tests multi-channel agreement. Activity/background labels are automatic proxies, not listened-to speaker/noise labels. Synthetic recovery is weak; internal gate passage alone would not establish valid correction. See partial_synthetic.json and per-tape partial_analysis.json. No external audio model, paid service or uploaded audio was used.','', 'Coverage is three 30-second discovery intervals per eligible tape, limited held-out CH12/15/20 searches, and two partial-analysis windows. Sources match frozen size/mtime and have excerpt hashes. The inventory checked markers, headers and readable physical ends; only four tapes also match the earlier delivery-fragment audit. Neither remote delivery completeness nor full-tape/all-voice transfer is established. T510094 stays deferred under the original snapshot; later arrivals were not chased.','', 'The original approved T510088 and two T510077 examples remain unchanged. Production scripts and parameters were not changed. Absolute speed, mission-time anchors, head offsets, reference switching and full-tape continuity remain unresolved.','', '## Reproduce','','Run the isolated scripts in order with `.venv/Scripts/python.exe -B`: `research/irig_free_20261016/bespoke_expanded.py`, `expanded_partials.py`, `expanded_followup.py`, then `expanded_report.py`. Existing per-tape checkpoints are reused. Do not replace the frozen inventory or run concurrent writers against this report folder.','', '[Listening page](index.html) · [Summary](summary.json) · [Source manifest](sources.json) · [Listening provenance](listening.json)']
    lines += ['', '## Synthetic and artifact checks', '', 'The reproducible `expanded_synthetic.py` adds 12 cases: broad/narrow capture at 3, 6.5 and 11 Hz, a drifting rate, amplitude-only and stationary controls. Broad capture recovered signed gains 0.953–0.995 with near-zero fundamental phase error; corrected check-tone RMS was 0.60–2.38 Hz in the warped cases. Wrong-sign correction was worse, and negative controls stayed below 0.00001 fractional-speed RMS. This establishes the synthetic measurement/rendering chain, not real-tape transfer. [Synthetic results](synthetic_verified.json).', '', 'Run `expanded_synthetic.py` before reporting and `expanded_verify.py` afterward. The latter checks complete frozen coverage, strict finite JSON, disjoint fresh windows, frozen selected methods, approved audio hashes, new audio hashes and listening-page links. [Verification](verification.json). Full-render event RMS must agree with the sparse evaluator within 0.15 Hz at every margin.']
    (DEST/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    html=['<!doctype html><meta charset="utf-8"><title>Expanded Apollo 15 flutter review</title><style>body{font:17px system-ui;max-width:1000px;margin:40px auto;background:#101820;color:#eee;padding:20px}a{color:#8bd5ff}article{border:1px solid #567;padding:20px;margin:20px 0}audio{width:100%}</style><h1>Expanded per-tape flutter review</h1><p>New experimental candidates. Not yet listening-approved. Same cleanup and gain before/after. Selection favors measured fresh improvements; full results retain failures.</p><p><a href="report.md">Detailed report</a> · <a href="summary.json">All tape results</a> · <a href="../cyclic_flutter_20261017/index.html">Previously approved T510077 benchmark page</a> · <a href="../accepted_flutter/index.html">Approved T510088 benchmark</a></p>']
    for r in rendered:
        html.append(f"<article><h2>{r['tape']} · CH{r['channel']}</h2><p>Canonical start {r['excerpt_start']:.3f}s; reference CH{r['reference']['channel']} at {r['reference']['carrier_hz']:.1f} Hz; {r['mode']}. Full 30-second window event median reduction {r['metrics']['median_reduction']:.1f}%, worst {r['metrics']['worst_reduction']:.1f}%. Figures describe the measured check tones, not guaranteed speech improvement.</p>")
        for label in ['before','after','before_then_after']:html.append(f"<p>{label.replace('_',' ')} (joined version: 12s / 1s silence / 12s)</p><audio controls preload='none' src='{escape(r['files'][label]['file'])}'></audio>")
        html.append('</article>')
    if not rendered:html.append('<p>No fresh candidates passed the reporting criteria. No positive listening examples were manufactured.</p>')
    (DEST/'index.html').write_text('\n'.join(html),encoding='utf-8');print(json.dumps({k:v for k,v in summary.items() if k!='rows'},indent=2))

def plot():
    import sys
    sys.path.append(str(ROOT/'reports/nested_amplitude_20261017/plot_deps'))
    os.environ['MPLCONFIGDIR']=str(DEST/'plot_config')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    s=json.loads((DEST/'summary.json').read_text());labels=[];med=[];worst=[];colors=[]
    for row in s['rows']:
        for r in row.get('fresh',[]):
            labels.append(f"{row['tape'][4:]} CH{r['channel']} @{r['start']:.0f}s");med.append(r['median_reduction']);worst.append(r['worst_reduction']);colors.append('#357b9b' if r['qualifies'] else '#b7644b')
    fig,ax=plt.subplots(figsize=(11,10));yy=np.arange(len(labels));ax.barh(yy,med,color=colors,label='Median event reduction');ax.scatter(worst,yy,color='black',marker='|',s=100,label='Worst event');ax.set_yticks(yy,labels,fontsize=8);ax.invert_yaxis();ax.axvline(0,color='gray',lw=1);ax.set_xlabel('Reduction in check-tone RMS frequency motion (%)');ax.set_title('All fresh confirmation results — duplicated feeds are not independent events');ax.legend(loc='lower right');ax.grid(axis='x',alpha=.2);fig.tight_layout();fig.savefig(DEST/'fresh_results.png',dpi=160);plt.close(fig)
    p=DEST/'index.html';p.write_text(p.read_text(encoding='utf-8')+'<h2>All fresh measurements, including failures</h2><img style="max-width:100%" src="fresh_results.png" alt="Median and worst check-tone improvement for all fresh outcomes">',encoding='utf-8')

if __name__=='__main__':main();plot()
