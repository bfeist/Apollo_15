"""Append fixed-gate holdout findings, preserving original study text."""
import hashlib,json
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/irig_free_research_20261016/candidate_holdout'
rows=json.loads((OUT/'results.json').read_text());out=[]
for r in rows:
    if 'error' in r:continue
    events=[]
    for ch,result in r['results'].items():
        for i,q in enumerate(result['correct']):events.append(dict(channel=int(ch),index=i,event=q))
    events.sort(key=lambda x:x['event']['start']);clusters=[]
    for e in events:
        if clusters and e['event']['start']-clusters[-1][0]['event']['start']<=.1:clusters[-1].append(e)
        else:clusters.append([e])
    selected=[max(g,key=lambda e:e['event']['amplitude_over_floor']) for g in clusters]
    methods={}
    for name in ['correct','shift_0.071','shift_0.137','shift_1.0','shift_5.0']:
        qs=[r['results'][str(e['channel'])][name][e['index']] for e in selected]
        if not qs:continue
        methods[name]=dict(before_median_hz=float(np.median([q['before_rms_hz'] for q in qs])),
            after_median_hz=float(np.median([q['after_rms_hz'] for q in qs])),
            reduction_median_percent=float(np.median([q['reduction_percent'] for q in qs])),
            reduction_worst_percent=float(min(q['reduction_percent'] for q in qs)))
    best=min((k for k in methods if k!='correct'),key=lambda k:methods[k]['after_median_hz']) if methods else None
    out.append(dict(tape=r['tape'],fragment=int(r['group'].rsplit('-',1)[1]),group=r['group'],start=r['start'],label=r['label'],
        reference_only_pass=r['gates']['reference_only_pass'],signal_pass=r['gates']['signal_pass'],sensitivity_pass=r['gates']['sensitivity_pass'],
        signal_checks=r['gates']['signal_checks'],event_count=len(selected),replica_measurements=len(events),
        selected_events=[dict(channel=e['channel'],start=e['event']['start'],end=e['event']['end'],
            amplitude_over_floor=e['event']['amplitude_over_floor']) for e in selected],metrics=methods,best_shift_by_median_hz=best))
aggregates=[]
for tape in ['A15_T510073','A15_T510106']:
    selected=[r for r in out if r['tape']==tape and r['label']=='new_fragment'];events=[];shifted={k:[] for k in ['shift_0.071','shift_0.137','shift_1.0','shift_5.0']}
    for row in selected:
        raw=next(r for r in rows if r['tape']==tape and r['group']==row['group'] and r['start']==row['start'] and r['label']=='new_fragment')
        for event in row['selected_events']:
            res=raw['results'][str(event['channel'])];idx=next(i for i,q in enumerate(res['correct']) if q['start']==event['start'])
            events.append(res['correct'][idx]['reduction_percent'])
            for k in shifted:shifted[k].append(res[k][idx]['reduction_percent'])
    med=float(np.median(events)) if events else None;worst=min(events) if events else None
    best=max(float(np.median(v)) for v in shifted.values()) if events else None
    gates=dict(events=len(events)>=3,windows=sum(r['event_count']>0 for r in selected)>=2,
        median=med is not None and med>=20,worst=worst is not None and worst>=-10,
        shifted=med is not None and med>best)
    aggregates.append(dict(tape=tape,windows=len(selected),event_windows=sum(r['event_count']>0 for r in selected),
        unique_event_clusters=len(events),reference_gates_passed=sum(r['reference_only_pass'] for r in selected),
        median_reduction_percent=med,worst_reduction_percent=worst,best_shift_median_reduction_percent=best,
        outcome_gates=gates,passes=all(gates.values())))
(OUT/'summary.json').write_text(json.dumps(dict(windows=out,candidates=aggregates),indent=2))
print(json.dumps(aggregates,indent=2))
for r in out:
    if not r['event_count']:continue
    m=r['metrics'];c=m['correct'];b=m[r['best_shift_by_median_hz']]
    print(r['tape'],r['fragment'],r['start'],r['label'],'n',r['event_count'],'gate',r['reference_only_pass'],
        'Hz',round(c['before_median_hz'],2),round(c['after_median_hz'],2),round(b['after_median_hz'],2),r['best_shift_by_median_hz'],
        'reduction',round(c['reduction_median_percent'],1),'worst',round(c['reduction_worst_percent'],1))

text='''

## Follow-up: frozen 404/406 Hz candidates on new fragments

**Outcome: neither candidate qualifies for unattended use.** The 404 Hz candidate produces useful reductions in several new passages but also materially worsens other candidate events, including events in intervals that pass the reference-only confidence gates. The 406 Hz candidate improves two new events, but both are in one interval; that is insufficient independent coverage to accept or definitively reject its physical usefulness. Missing independent validation is the specific remaining blocker for that candidate. No production change is justified.

This appendix preserves the original study above. It uses [a frozen plan](research/irig_free_20261016/holdout_plan.json) written before processing the new fragments. The unchanged original report's SHA-256 is retained in `candidate_holdout/original_report.sha256`. The follow-up code is separate; do not run the original `write_report.py` over this appendix without preserving it.

### Fixed design and coverage

T510073 uses CH17 at 404 Hz; T510106 uses CH12 at 406 Hz. The same accepted estimator and nearby-hum policy are used without fitting frequency, phase, lag or gain to target events. The estimator remeasures local phase/rate from the reference; fixed parameters do not mean extrapolating an old timing map.

For each tape, choose stable matching groups at the quarter, three-quarter and last available index, excluding original fragment 17. This selects fragments **8, 25 and 33** on both tapes. Test source seconds **80, 650 and 1250**, 50 seconds each: **18 new windows / 54 target-channel excerpts** in total, plus the reference excerpts. Targets are CH7/12/15 for T510073 and CH13/15/20 for T510106. All windows, including no-event windows and gate failures, are retained. The same gates are separately applied to the known T510088 success, T510092/T510106 failures and candidate discovery intervals.

Each reference/target read passes the original stable-file checks and records an excerpt hash. [Selected exact paths](reports/irig_free_research_20261016/candidate_holdout/selected_groups.json); [complete results, gates, controls and source hashes](reports/irig_free_research_20261016/candidate_holdout/results.json); [deduplicated summary](reports/irig_free_research_20261016/candidate_holdout/summary.json). The `*_prevalidation.json` records save reference gate results before target outcomes are evaluated.

The original 404/406 Hz names are search centers, not identified physical oscillators. Their discovery tracking centers were approximately 412 and 418 Hz respectively, and their capture bands overlap the 420 Hz interference region. Thus even “not near an exact mains multiple” at the configured center does not prove an independent recorded tone. No oscillator identity is assigned here.

### Gates fixed before the holdout run

- Signal gate: at least 70% spectral frames above 10 dB local contrast, mean support at least 0.8, estimated RMS speed 0.1–3%, and no more than 1% of interior timing samples at the 4% speed bound.
- Sensitivity gate: repeat reference estimation with capture center shifted by −5 and +5 Hz; both estimated curves must correlate at least 0.9 with the original and retain 0.8–1.25 times its RMS. This is a diagnostic robustness check, not a replacement estimator chosen by target scores.
- Independent outcome gate: at least three nonduplicated candidate events in at least two new windows; median reduction at least 20%; worst event no more than 10% worse; aggregate median improvement must exceed each of the four shifted-map control medians. These are provisional research criteria, not calibrated confidence probabilities.

Cross-channel events starting within 100 ms are conservatively grouped. For the same event, retain the channel with the largest *original* amplitude-to-floor ratio, without looking at correction outcome. All replica measurements remain available. This reduces duplicate votes but cannot prove independent oscillators; a recurring signaling sequence can still be one physical source. No event is discarded because it worsens.

### Candidate results across new fragments

| Candidate | New windows / event-bearing | Unique event clusters | Reference gates passed | Median reduction | Worst reduction | Best shifted median reduction | Decision |
|---|---:|---:|---:|---:|---:|---:|---|
'''
for a in aggregates:
    decision='Reject unattended use: adverse held-out events' if a['tape'].endswith('073') else 'Unvalidated: only one event-bearing window'
    text+=f"| {a['tape']} | {a['windows']} / {a['event_windows']} | {a['unique_event_clusters']} | {a['reference_gates_passed']}/{a['windows']} | {a['median_reduction_percent']:.1f}% | {a['worst_reduction_percent']:.1f}% | {a['best_shift_median_reduction_percent']:.1f}% | {decision} |\n"
text+='''
### Metric table: correct-time and wrong-time maps

Values are medians of per-event RMS pitch variation in **Hz**, about each event's own mean. Lower is better. The control is the **lowest median-Hz result among all four predeclared wrong-time shifts**, so it is an intentionally demanding comparison rather than a selectively bad null. All four control values and per-event results are retained. Percent reductions are computed eventwise, so their median need not equal the ratio of the two median-Hz columns. For new fragments, replicated channel detections count once.

| Tape | Fragment / source start | Role | Events | Gate | Baseline Hz | Corrected Hz | Best wrong-time Hz (shift) | Worst event reduction |
|---|---|---|---:|---|---:|---:|---:|---:|
'''
for r in out:
    if not r['event_count']:continue
    c=r['metrics']['correct'];shift=r['best_shift_by_median_hz'];b=r['metrics'][shift]
    text+=f"| {r['tape']} | {r['fragment']} / {r['start']} s | {r['label']} | {r['event_count']} | {'pass' if r['reference_only_pass'] else 'fail'} | {c['before_median_hz']:.2f} | {c['after_median_hz']:.2f} | {b['after_median_hz']:.2f} ({shift.removeprefix('shift_')} s) | {c['reduction_worst_percent']:.1f}% |\n"
text+='''
### Do the gates separate the known failures?

The frozen reference-only gates **pass the known T510088 success and reject both known blind-transfer failures**. That is useful, but it is not sufficient: T510073 fragment 25 at 650 seconds and fragment 33 at 650 seconds pass while containing adverse events. The latter includes an approximately **26% worsening on CH12**; the former includes approximately **19% worsening on CH15**. The signal gate also rejects fragment 25 at 80 seconds, which contains both improvements and a roughly doubled-RMS event. Therefore a simple reference-quality pass cannot authorize correction, and changing the gate merely to retain favorable intervals would be unjustified.

The known failures have already been inspected in the earlier study, so their gate behavior is a calibration observation. The new fragments are the held-out test. The gate thresholds were not tuned after seeing them. No threshold search or statistical significance claim is made from this small sample.

**Actionable criteria:** keep reference-quality gates as necessary diagnostics, add independent target checks across more than one interval, record the worst event as well as the median, and abstain when checks disagree or no independent signal is present. Do not equate strong persistence, carrier perturbation robustness, or a positive median with shared tape-speed provenance. Applying the 404 Hz correction to all tracks/intervals fails this test. For 406 Hz, preserve it as a research candidate only: two new CH20 events in one window cannot establish transfer to all tracks or the rest of the tape.

Candidate tone identity remains unaudited from transmission context. The new 406 Hz evidence is on CH20 rather than its CH12 estimator, but there is no authenticated IRIG/independent oscillator ground truth. Speech listening and event identity/provenance are still needed. The bounded investigation stops here with a concrete adverse-result rejection for blanket 404 Hz use and an explicit independent-evidence blocker for 406 Hz; it does not wait for the full download.

### Reproduce this appendix

```powershell
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\candidate_holdout.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\summarize_holdout.py
```

`candidate_holdout.py` regenerates only the isolated follow-up directory. Fragment discovery can change after downloads; retained selected paths/hashes identify this run. `summarize_holdout.py` preserves the original report prefix and replaces only this named appendix when rerun. Source audio, accepted reports and production scripts remain untouched.
'''
report=ROOT/'IRIG_FREE_FLUTTER_RESEARCH.md';prior=report.read_bytes().decode('utf-8');marker='\n\n## Follow-up: frozen 404/406 Hz candidates on new fragments'
prefix=prior.split(marker)[0]
expected=(OUT/'original_report.sha256').read_text().strip()
assert hashlib.sha256(prefix.encode('utf-8')).hexdigest()==expected,'Original report changed; refusing to replace'
report.write_bytes((prefix+text).encode('utf-8'))
