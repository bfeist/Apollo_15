"""Summarize canonical experiments without rewriting previous research or concurrent edits."""
from bespoke_validate import *
import html

def stats(events):
    if not events:return dict(events=0)
    return dict(events=len(events),median=float(np.median([e['reduction_percent'] for e in events])),worst=min(e['reduction_percent'] for e in events),baseline_hz=float(np.median([e['before_rms_hz'] for e in events])),corrected_hz=float(np.median([e['after_rms_hz'] for e in events])),controls={s:float(np.median([e['controls'][s] for e in events])) for s in ['0.071','5.0']})

def main():
    inventory=json.loads((OUT/'inventory.json').read_text());allresults=[];listening=[];cards=[]
    for tape in inventory:
        p=OUT/(tape['tape']+'_validation.json')
        if not p.exists():continue
        r=json.loads(p.read_text());groups={};successful=[]
        for w in r['windows']:
            for v in w['results']:
                ref=v['reference'];key=f"{ref['scope']} CH{ref['channel']} {ref['carrier_hz']:g}Hz {v['method']}"
                g=groups.setdefault(key,dict(reference=ref,method=v['method'],calibration=[],heldout=[],event_windows=[]))
                g['calibration'] += [e for e in v['events'] if e['role']=='calibration']
                held=[e for e in v['events'] if e['role']=='heldout'];g['heldout']+=held
                if held:g['event_windows'].append(w['start'])
                s=stats(held)
                if s['events']>=2 and s['median']>=20 and s['worst']>=-10 and s['median']>max(s['controls'].values()):successful.append((s['median'],w,v,s))
        summary=[]
        for key,g in groups.items():
            summary.append(dict(label=key,reference=g['reference'],method=g['method'],calibration=stats(g['calibration']),heldout=stats(g['heldout']),event_windows=g['event_windows'],same_head_heldout=stats([e for e in g['heldout'] if e['same_head']])))
        calibrated=[s for s in summary if s['reference']['scope']=='frozen_first_window' and s['calibration']['events']]
        chosen=max(calibrated,key=lambda s:s['calibration']['median']) if calibrated else None
        confirmation=[];cp=OUT/(tape['tape']+'_confirmation.json')
        if cp.exists():
            cr=json.loads(cp.read_text());cg={}
            for cw in cr['confirmation']:
                for cv in cw['results']:
                    ref=cv['reference'];key=f"CH{ref['channel']} {ref['carrier_hz']:g}Hz {cv['method']}"
                    g=cg.setdefault(key,dict(events=[],windows=[]));g['events']+=cv['events'];g['windows'].append(cw['start'])
                    cs=stats(cv['events'])
                    if cs['events']>=2 and cs['median']>=20 and cs['worst']>=-10 and cs['median']>max(cs['controls'].values()):successful.append((cs['median'],cw,cv,cs))
            confirmation=[dict(label=k,statistics=stats(g['events']),windows=g['windows']) for k,g in cg.items()]
        allresults.append(dict(tape=tape['tape'],windows=len(r['windows']),methods=summary,calibration_selected=chosen,successful_interval_count=len(set(w['start'] for _,w,v,s in successful)),confirmation=confirmation))
        bych={c['channel']:c for c in tape['channels']};used=set()
        for _,w,v,s in sorted(successful,reverse=True,key=lambda item:(item[1].get('kind')=='fresh_confirmation',item[0])):
            if w['start'] in used:continue
            if len(used)>=2:break
            used.add(w['start']);ref=v['reference'];rx,sr,rm=get(bych[ref['channel']],tape,w['start'],60)
            if v['method'].startswith('accepted'):
                _,e=measure(rx,sr,ref,ref['channel'])
                if v['method']=='accepted_half_gain':e*=.5
            else:e=phase_map(rx,sr,ref['carrier_hz'],[4,10] if v['method']=='phase_4_10' else [2,15])
            event=max([q for q in v['events'] if q['role'] in ['heldout','fresh_confirmation']],key=lambda q:q['amplitude_over_floor']);ch=event['channel'];x,sr,xm=get(bych[ch],tape,w['start'],60)
            cleaned=x.copy();passes=[]
            for i in range(6):
                lines=detect(cleaned,sr)
                if not lines:break
                passes.append(lines);cleaned=remove(cleaned,sr,lines)
            cleaned,profile=denoise(cleaned,sr,reduction_cap_db=6);corrected=resample_at(cleaned,positions(e,sr,len(x)))
            offset=max(3,min(45,event['start']-6));lo=round(offset*sr);hi=lo+12*sr;b=cleaned[lo:hi];a=corrected[lo:hi];gain=.85/max(np.max(abs(b)),np.max(abs(a)),1e-12)
            name=f"{tape['tape']}_{w['start']:.2f}_CH{ch}_{v['method']}_AB.wav";dest=OUT/name
            sf.write(dest,np.r_[b*gain,np.zeros(sr),a*gain],sr,subtype='PCM_24');check,rate=sf.read(dest)
            assert len(check)==25*sr and rate==sr and np.max(abs(check))<.851
            meta=dict(file=name,tape=tape['tape'],window_start=w['start'],clip_start=w['start']+offset,reference=rm,target=xm,method=v['method'],carrier_hz=ref['carrier_hz'],gain=gain,cleanup_passes=passes,noise_profile=profile,raw_heldout=s,selection='Retrospectively chosen successful interval; listening not yet accepted')
            listening.append(meta);cards.append(f'<section><h2>{tape["tape"]} · CH{ch} · {w["start"]+offset:.2f} s</h2><p>{html.escape(v["method"])}; reference CH{ref["channel"]} at {ref["carrier_hz"]} Hz. Raw held-out median reduction {s["median"]:.1f}%, worst {s["worst"]:.1f}%.</p><audio controls src="{name}"></audio></section>')
    (OUT/'summary.json').write_text(json.dumps(allresults,indent=2));(OUT/'listening.json').write_text(json.dumps(listening,indent=2))
    (OUT/'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Canonical per-tape flutter trials</title><style>body{font:18px system-ui;max-width:960px;margin:40px auto;padding:20px;background:#15202b;color:#eee}section{padding:20px;border:1px solid #607080;margin:20px 0}audio{width:100%}</style><h1>Per-tape canonical listening trials</h1><p>12 seconds before, one second silence, same passage after. Identical dehum/denoise and shared gain. Selected exploratory successes, not tape-wide validation. The numerical checks concern candidate signaling events; speech quality and event identity need listening review.</p>'+''.join(cards),encoding='utf-8')
    baseline=json.loads((OUT/'baseline.json').read_text());unchanged={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in baseline.items() if p!='IRIG_FREE_FLUTTER_RESEARCH.md'}
    sourcechecks=[]
    for t in inventory:
        for c in t['channels']:
            st=Path(c['path']).stat();sourcechecks.append(dict(path=c['path'],same_size_mtime=st.st_size==c['bytes'] and st.st_mtime_ns==c['mtime_ns']))
    (OUT/'verification.json').write_text(json.dumps(dict(unchanged_code_and_readme=unchanged,source_checks=sourcechecks),indent=2))
    lines=['','', '## Canonical bespoke per-tape experiments — 2026-10-17','','This investigation uses completed canonical channel WAVs, independently of the running concatenator. A completion marker, all 30 channels, no partial outputs, ten-minute minimum age, matching frame totals/formats against every delivered fragment, readable final frames, and first/middle/last fragment sample identity are required. These checks establish correspondence to the local delivery, not remote completeness or physical head alignment. Exact paths, ranges, sizes, timestamps and sample checks are in [inventory.json](reports/canonical_bespoke_20261017/inventory.json).','', 'Each tape uses four prespecified 60-second positions spanning 15–85% of its available duration, plus a historical candidate interval where applicable. Every channel is sampled. Up to four spectral candidates per channel are considered outside the signaling band. Reference selection is reference-only: best ranked carrier, a different-channel non-mains alternative, and historical reference candidates for T510073/075/076. These are frozen from the first interval; a locally selected alternative is also tested in later intervals. Candidate screening is not a mandatory rejection gate.','', 'Compared methods: accepted robust local sinusoid; direct phase tracking at 4–10 Hz; wider 2–15 Hz phase tracking; accepted correction at half amplitude. The wider tracker uses a broader capture where needed. All maps are bounded and duration-preserving. No target event tunes a phase, lag, or scale. Candidate signaling bursts on reference channels are excluded; detections within a conservative 2.5-second span are counted once to allow for delayed replicas and head offsets (this can also merge genuinely distinct rapid events) using the strongest original signal. Replicas and same-head status are retained. Repeated beeps are not proof of independent oscillators.','', 'The first interval’s first-half events are calibration only; its boundary events are excluded, and its second half and later intervals provide target holdouts. Selecting the best calibration method and judging later results is separate from retrospectively identifying a promising interval. All tested variants and wrong-time controls (71 ms and 5 s) remain in the JSON, including worsening and event-free intervals. The local alternatives are chosen from reference spectra, not target results.','', '[All experiment artifacts](reports/canonical_bespoke_20261017/) · [Per-method numerical summary](reports/canonical_bespoke_20261017/summary.json) · [Matched A/B listening page](reports/canonical_bespoke_20261017/index.html) · [Listening provenance](reports/canonical_bespoke_20261017/listening.json)','', '| Tape | Windows | Calibration-selected method | Held-out events / windows | Median / worst reduction | Best shifted median |','|---|---:|---|---:|---:|---:|']
    for r in allresults:
        c=r['calibration_selected']
        if c:
            s=c['heldout'];val=f"{s['median']:.1f}% / {s['worst']:.1f}%" if s['events'] else 'No events';ctrl=f"{max(s['controls'].values()):.1f}%" if s['events'] else '—'
            lines.append(f"| {r['tape']} | {r['windows']} | {c['label']} | {s['events']} / {len(c['event_windows'])} | {val} | {ctrl} |")
        else:lines.append(f"| {r['tape']} | {r['windows']} | No calibration event; no outcome-selected frozen method | — | See exploratory results | — |")
        lines+=[]
    lines+=['','### Per-tape exploratory findings','']
    for r in allresults:
        eligible=[s for s in r['methods'] if s['heldout']['events']]
        best=max(eligible,key=lambda s:s['heldout']['median']) if eligible else None
        if best:
            s=best['heldout'];lines.append(f"- **{r['tape']}**: retrospectively strongest aggregate `{best['label']}`: {s['events']} held-out event clusters in {len(best['event_windows'])} windows; median {s['median']:.1f}%, worst {s['worst']:.1f}%, best shifted-control median {max(s['controls'].values()):.1f}%. {r['successful_interval_count']} intervals met the exploratory listening-render criterion (at least two events, median ≥20%, worst ≥−10%, beating both shifted medians). This ranking is exploratory and requires fresh confirmation; it is not a whole-tape recommendation.")
        else:lines.append(f"- **{r['tape']}**: no qualifying independent target bursts in these sampled intervals. Reference candidates alone do not validate correction; preserve original timing pending event-bearing passages or another independent timing reference.")
    lines+=['','### Limits, preservation and reproduction','',f"Rendered {len(listening)} matched A/B clips (25 seconds each); no full-tape corrected copies. Clips are numerically checked, not auditioned or accepted. Tone identity, absolute timing, physical head offsets, faint-speech preservation and transitions between interval-specific maps remain unresolved. The source snapshot is finite; incomplete/recent tapes are explicitly deferred in the inventory. Synthetic checks compare sparse event resampling against the original evaluator and test 3/6.5/12 Hz injected motion plus a stationary-reference null. Results: [method_tests.json](reports/canonical_bespoke_20261017/method_tests.json).",'',f"Production code and README hashes unchanged from this investigation's starting baseline: **{all(unchanged.values())}**. Canonical size/mtime recheck all unchanged: **{all(s['same_size_mtime'] for s in sourcechecks)}**. Previous report content is preserved; only this named appendix is appended. Research files are isolated under `research/irig_free_20261016/canonical_bespoke.py`, `bespoke_validate.py`, `bespoke_report.py` and `reports/canonical_bespoke_20261017/`.",'','```powershell','.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\canonical_bespoke.py inventory','.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\canonical_bespoke.py survey','.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\bespoke_validate.py','.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\bespoke_report.py','```','','Survey/validation reuse existing per-tape results; retain the saved inventory to reproduce this snapshot. Use a separate output directory for a new study.','']
    lines+=['','### Frozen follow-up and fresh confirmation','', 'After the first comparison, follow-up reference/method choices were recorded in [confirmation_plan.json](reports/canonical_bespoke_20261017/confirmation_plan.json) before measuring their fresh target outcomes. Each tape searched at most 12 additional positions on CH12/15/20, excluding previously inspected positions, and selected at most two windows with two or more conservatively deduplicated events. All no-event/one-event searches are retained. The 979.5 Hz T510073 and 1000 Hz T510075 alternatives were also tested on previously inspected passages, explicitly labeled retrospective. Neither alternative receives a whole-tape endorsement.','', '| Tape | Frozen follow-up | Fresh events / windows | Baseline → corrected median Hz RMS | Median / worst reduction | Best shifted median reduction |','|---|---|---:|---:|---:|---:|']
    for r in allresults:
        for c in r['confirmation']:
            s=c['statistics'];lines.append(f"| {r['tape']} | {c['label']} | {s['events']} / {len(c['windows'])} | {s['baseline_hz']:.2f} → {s['corrected_hz']:.2f} | {s['median']:.1f}% / {s['worst']:.1f}% | {max(s['controls'].values()):.1f}% |")
    lines+=['','Practical best-case recommendations:','', '- **T510073:** use CH3 near 117.5 Hz with the accepted local sinusoid only in individually checked intervals. A fresh passage at 6782.31 s gives 44.4% median improvement (two events), but the earlier held-out set includes 65.6% worsening. The weaker 979.5 Hz alternative fails the fresh check and does not solve the interval dependence. Preserve unqualified intervals.','- **T510075:** CH30 near 1000 Hz with 2–15 Hz phase tracking is a strong local option at 28663.35 s (two fresh events, 90.3% median and 87.8% worst improvement). It still worsens an event by 56.9% in the retrospective set. CH2 near 118 Hz also helps a different passage. A reviewed interval map with abstention is justified as the next development; one reference applied everywhere is not.','- **T510076:** strongest case here for broader follow-up: CH30 near 1000 Hz or CH11 near 414.5 Hz with 2–15 Hz tracking. Both improve every measured fresh event across two passages; the CH30 result is 82.9% median, 79.5% worst. This complements nine earlier held-out events across two windows. These references are candidates for a checked tape-specific correction, not proof of every voice or unsampled interval.','- **T510077:** CH30 near 1005.5 Hz is preferable to the low-frequency hum reference. Both bandwidths improve all fresh events, but 4–10 Hz gives 41.1% median and 34.5% worst improvement, versus 20.6%/12.8% for 2–15 Hz. The narrower tracker also improved all 15 earlier events across three intervals. Start with 4–10 Hz for checked passages; broader bandwidth is not automatically better.','', 'The best methods differ by tape and sometimes by interval. All fresh positive methods beat the fixed wrong-time controls in aggregate. Evidence remains candidate-tone measurements, not authenticated IRIG ground truth or listener acceptance. Two events in one new passage are especially limited evidence for T510073/075.','', 'Follow-up reproduction: run `python -B research/irig_free_20261016/canonical_extra.py` after the initial survey, then `bespoke_validate.py`, `bespoke_confirm.py`, and `bespoke_report.py`. Existing per-tape files are reused; the saved input snapshots and experiment plans identify this run.','']
    survey_windows=sum(len(json.loads(p.read_text())['windows']) for p in OUT.glob('*_survey.json'))
    event_searches=sum(len(json.loads(p.read_text())['scans']) for p in OUT.glob('*_event_search.json'))
    confirmation_searches=sum(len(json.loads(p.read_text())['search']) for p in OUT.glob('*_confirmation.json'))
    confirmed_windows=sum(len(json.loads(p.read_text())['confirmation']) for p in OUT.glob('*_confirmation.json'))
    samples={}
    def collect(value):
        if isinstance(value,dict):
            if 'excerpt_float64_sha256' in value and 'path' in value:samples[(value['path'],value['start_seconds'],value['duration_seconds'])]=value
            for child in value.values():collect(child)
        elif isinstance(value,list):
            for child in value:collect(child)
    for pattern in ['*_survey.json','*_event_search.json','*_validation.json','*_confirmation.json']:
        for p in OUT.glob(pattern):collect(json.loads(p.read_text()))
    coverage=dict(all_channel_tape_windows=survey_windows,all_channel_excerpt_reads=survey_windows*30,event_search_positions=event_searches,confirmation_search_positions=confirmation_searches,fresh_confirmation_windows=confirmed_windows,unique_path_start_duration_samples=len(samples),sampled_canonical_files=len({k[0] for k in samples}),samples=list(samples.values()))
    (OUT/'coverage.json').write_text(json.dumps(coverage,indent=2))
    lines+=['',f"Coverage accounting: **{survey_windows} all-channel tape windows ({survey_windows*30} channel excerpts)**, {event_searches} three-channel event-search positions, and {confirmation_searches} three-channel fresh-confirmation search positions. {confirmed_windows} fresh event-bearing windows were evaluated. Across reused reads within this investigation there are {len(samples)} distinct path/start/duration samples from {coverage['sampled_canonical_files']} canonical files. Searches and later full-window analysis overlap; they are not independent trials. [Exact coverage](reports/canonical_bespoke_20261017/coverage.json).",'', 'At the closing metadata check, T510078 had 31 WAVs, no shared completion marker, and an additional unidentified `A15_T510078_HR1U__208-10-47-00_209-03-59-07.wav`, ending about 16 seconds later than its named channels. Its identity/range ambiguity was not resolved or modified. T510079 still had a partial output. Neither was included in these experiments. [Closing concatenation status](reports/canonical_bespoke_20261017/closing_status.json).','']
    report=ROOT/'IRIG_FREE_FLUTTER_RESEARCH.md';current=report.read_bytes();marker=b'\n\n## Canonical bespoke per-tape experiments'
    endmarker=b'<!-- END canonical bespoke 20261017 -->'
    section='\n'.join(lines).encode('utf-8')+endmarker+b'\n'
    if marker in current:
        begin=current.index(marker);end=current.find(endmarker,begin)
        if end<0:raise RuntimeError('Existing appendix lacks end marker; refuse ambiguous replacement')
        new=current[:begin]+section+current[end+len(endmarker):]
    else:new=current+section
    if report.read_bytes()!=current:raise RuntimeError('Report changed concurrently; reread before append')
    report.write_bytes(new)
    print('REPORT',len(allresults),'tapes',len(listening),'clips',flush=True)

if __name__=='__main__':main()
