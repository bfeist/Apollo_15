"""Append generic-scanner findings, render one bounded A/B, retain prior studies."""
from generic_scan import *
from audio_processing import detect,denoise
import hashlib
BASE=ROOT/'reports/irig_free_research_20261016';OUT=BASE/'generic_trial'
rows=json.loads((OUT/'results.json').read_text());coverage=json.loads((OUT/'coverage.json').read_text())
snapshot=json.loads((OUT/'manifest_snapshot.json').read_text());current=json.loads((BASE/'generic_manifest_current.json').read_text())
compact=[]
for r in rows:
    events=sorted([(q,v,i) for v in r['validation'] for i,q in enumerate(v['events'])],key=lambda x:x[0]['start']);clusters=[]
    for e in events:
        if clusters and e[0]['start']-clusters[-1][0][0]['start']<=.1:clusters[-1].append(e)
        else:clusters.append([e])
    unique=[max(g,key=lambda x:x[0]['amplitude_over_floor']) for g in clusters]
    c=r['selected'];metrics={}
    if unique:
        metrics['baseline_hz']=float(np.median([q['before_rms_hz'] for q,v,i in unique]));metrics['corrected_hz']=float(np.median([q['after_rms_hz'] for q,v,i in unique]))
        for shift in ['0.071','5.0']:metrics['shift_'+shift+'_hz']=float(np.median([v['controls'][shift][i]['after_rms_hz'] for q,v,i in unique]))
    compact.append(dict(tape=r['tape'],start=r['start'],sampled_channels=sum(c['status']=='sampled' for c in r['coverage']),
        selected_channel=c['channel'] if c else None,carrier_hz=c['carrier_hz'] if c else None,status=r['status'],metrics=metrics,target_summary=r.get('target_summary'),peer=c.get('best_peer') if c else None))
(OUT/'summary.json').write_text(json.dumps(compact,indent=2))

# One modest listening artifact from the supported new interval; no source writes.
chosen=next((r for r in rows if r['status']=='locally_supported_research_candidate'),None)
if chosen:
    tape=next(t for t in snapshot['tapes'] if t['tape']==chosen['tape']);c=chosen['selected'];chmap={c['channel']:c for c in tape['channels']}
    x,sr,refmeta=stream_read(chmap[c['channel']],chosen['start'],chosen['duration']);_,e=measure(x,sr,c,c['channel'])
    # CH15 is a speech review target selected by channel role, not outcome maximization.
    target=15 if 15 in chmap else chosen['validation'][0]['channel']
    audio,sr,targetmeta=stream_read(chmap[target],chosen['start'],chosen['duration']);clean=audio.copy();passes=[]
    for _ in range(6):
        lines=detect(clean,sr)
        if not lines:break
        clean=remove(clean,sr,lines);passes.append(lines)
    clean,profile=denoise(clean,sr,reduction_cap_db=6);after=resample_at(clean,positions(e,sr,len(clean)))
    # Fixed held-out speech interval, not selected to optimize timing score.
    b=clean[30*sr:42*sr];a=after[30*sr:42*sr];gain=.85/max(np.max(abs(b)),np.max(abs(a)),1e-12)
    sound=np.r_[b*gain,np.zeros(sr),a*gain];path=OUT/'T510088_discovered_reference_CH15_AB.wav'
    sf.write(path,sound,sr,subtype='PCM_24');check,fs=sf.read(path)
    assert fs==sr and len(check)==25*sr and np.isfinite(check).all() and np.max(abs(check))<.851
    metadata=dict(reference=refmeta,target=targetmeta,selection=c,source_stream_clip_start=chosen['start']+30,
        passes=passes,profile_seconds=profile,shared_gain=gain,frames=len(check),peak=float(np.max(abs(check))),
        listening_status='not auditioned; no user acceptance')
    (OUT/'listening.json').write_text(json.dumps(metadata,indent=2))

# Verify every actually sampled map remains the same in the updated hour-24 manifest.
equivalence=[]
for r in rows:
    t=next(t for t in current['tapes'] if t['tape']==r['tape']);bych={c['channel']:c for c in t['channels']}
    for ch in r['coverage']:
        if ch['status']!='sampled':continue
        segs=bych[ch['channel']]['segments']
        for p in ch['sources']:
            matches=[s for s in segs if s['path']==p['path'] and abs(s['start']+(p['start_seconds']-s.get('source_start',0))-p['stream_start'])<1e-6]
            if len(matches)!=1:raise ValueError('Updated mapping differs for sampled source')
            source=Path(p['path']);stat=source.stat()
            equivalence.append(dict(path=p['path'],mapping_unchanged=True,size_mtime_unchanged=stat.st_size==p['bytes'] and stat.st_mtime_ns==p['mtime_ns']))
(OUT/'mapping_recheck.json').write_text(json.dumps(equivalence,indent=2))
old_prov=json.loads((BASE/'provenance.json').read_text());prod={}
old_hashes={k.replace('\\','/'):v for k,v in old_prov['code_sha256'].items()}
for name in ['scripts/flutter.py','scripts/audio_processing.py','scripts/05b_deflutter.py']:
    prod[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==old_hashes[name]
assert all(prod.values())
(OUT/'code_provenance.json').write_text(json.dumps(dict(production_unchanged=prod,research_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'research/irig_free_20261016').glob('*.py')}),indent=2))

text='''

## Generic scanner: all-channel discovery with canonical stream inputs

The latest requested direction is implemented in [generic_scan.py](research/irig_free_20261016/generic_scan.py). **It discovers carrier frequencies instead of looking for 631.5, 404 or 406 Hz.** The fixed-candidate experiments above remain evidence about validation and failure modes, not scanner defaults. The scanner enumerates all available channels per tape, samples common time windows across them, retains interval-specific candidate rankings and supports an explicit `no_trustworthy_reference` outcome.

This is an isolated research tool. It neither concatenates nor renders corrected production tracks, and it does not change the accepted pipeline. All source reads are bounded. The real-data trial samples every mapped available channel at each selected time, not just the historical eight-channel subset. The complete inventory covers all tapes; audio analysis below is a representative three-tape subset, not a claim that the whole delivery has been analyzed.

### Canonical input and fragment mapping

The canonical unit is **one logical channel stream per tape**, normally backed by one concatenated WAV. A unique canonical file takes precedence over chunks; the two representations are never combined. Multiple canonical files for the same channel are marked ambiguous instead of selecting one arbitrarily. A recent canonical file defers the channel rather than falling back to old fragments. A changed size/mtime, a truncated WAV, unsupported format, a physical-duration mismatch, or a missing mapped interval defers the requested read. The scanner reads only original audio.

For timestamp-free canonical filenames, `--canonical-common-start` is an explicit operator declaration that all canonical files start at the common tape origin. Without that declaration or timestamp mapping, those files remain unmapped. Matching lengths alone never establishes alignment. The scanner is tested with canonical WAVs, but there were **no real WAVs in `D:/Apollo15_concatenated` at inventory time**, so the real-data trial used fragment maps.

Current fragments are mapped by their complete day/hour/minute/second filename intervals, not by list position or fragment number. Gaps stay gaps; no time is compressed. Reads can cross adjacent fragments without writing a concatenated copy. Overlap or duplicate intervals block a channel. Source identity and actual physical frame count are checked when sampled. This map describes digitized file timing, not authenticated mission time or solved head offsets.

The inventory exposed ranges ending at hour 24, such as `207-23-35-00_207-24-05-00`. The mapper now normalizes hour 24 arithmetically to the following day; a test confirms the interval is 1800 seconds and that 207-24-05 equals 208-00-05. The first conservative inventory had deferred those segments. The later corrected inventory retains them, and every source interval actually read by the representative run was verified unchanged under the corrected mapping. Both snapshots are preserved; no result was relabeled as a rerun.

'''
def totals(m):return len(m['tapes']),sum(len(t['channels']) for t in m['tapes']),sum(c['discovered_chunk_files']+c['discovered_canonical_files'] for t in m['tapes'] for c in t['channels'])
ta,ca,fa=totals(snapshot);tb,cb,fb=totals(current)
text+=f"The scan snapshot contains **{ta} tapes, {ca} channels with files, and {fa:,} WAVs**. The refreshed manifest contains **{tb} tapes, {cb} channels and {fb:,} WAVs**, including {sum(s.get('normalized_hour24',False) for t in current['tapes'] for c in t['channels'] for s in c['segments'])} normalized hour-24 segments. This growth confirms that downloads were continuing during the study. Inventory is local only; it does not verify Box completeness. Expected channels 1–30 missing from the snapshot, mapped gaps, recent segments, unmapped/ambiguous files and unscanned channels are explicit in the outputs.\n\n"
text+='''[Corrected current inventory](reports/irig_free_research_20261016/generic_manifest_current.json), [frozen scan inventory](reports/irig_free_research_20261016/generic_trial/manifest_snapshot.json), [per-tape/per-channel analysis coverage](reports/irig_free_research_20261016/generic_trial/coverage.json).

### Discovery, ranking and validation

For each tape, a bounded number of evenly spaced common windows covers its mapped duration. Defaults are two 50-second windows per tape with a six-tape-window total budget. A budget limit is reported, never silently presented as full coverage. All channels in the selected tape are attempted at each window; incomplete channels produce explicit reasons. More windows can be requested without changing the algorithm, and new windows can choose different reference channels/frequencies.

Carrier peaks are discovered from **only the first half** of each window, across 100 Hz to the smaller of 3900 Hz and Nyquist minus 100 Hz. Broad spectral peaks, contrast, persistence, reference tracking support, modulation amplitude and shared fractional-frequency motion determine ranking. Three candidates per channel are retained by default, reserving slots for non-mains peaks so a harmonic comb cannot occupy all slots. There is no preferred tape-specific carrier. This top-K search is bounded, not exhaustive detection of every possible weak tone.

Tracking currently reuses the accepted 4–10 Hz local sinusoidal estimator with a configurable discovered carrier. Therefore generic *frequency discovery* does not remove that estimator's bandwidth/model limitations. Near-60-Hz-family and approximate integer-ratio checks flag likely shared interference/harmonics; these checks are heuristics, not proof of independent oscillators. The selected candidate is ranked by first-half evidence, then checked against second-half peer motion and candidate tone events on other channels. Target scores are never used to choose a different winning candidate retrospectively.

An interval is labeled `locally_supported_research_candidate` only when the preselected reference has at least 0.6 peer correlation in both halves, at least three deduplicated held-out event-time clusters, at least 20% median event improvement, no event more than 10% worse, and improvement exceeding the two predeclared shifted controls (71 ms and five seconds). Otherwise it says `no_trustworthy_reference`, with the candidate and failure evidence retained. This local numeric label is **not** production approval or proof of an independent historical oscillator. Different delayed copies of one transmission may survive time-based deduplication; event identity, shared speed history and head alignment remain review requirements.

### Representative end-to-end results

All times below are relative to the mapped tape origin, not seconds into one fragment. This run uses 15% and 85% positions in each tape's available mapped duration; it does not revisit only the accepted fragment. Exact source segments, hashes, discovered peaks, peer correlations and all validation events are in [results.json](reports/irig_free_research_20261016/generic_trial/results.json).

| Tape / mapped start | Channels sampled | Selected channel / discovered Hz | Held-out event clusters | Median / worst reduction | Outcome |
|---|---:|---|---:|---:|---|
'''
for r in compact:
    q=r.get('target_summary') or {};ev=q.get('unique_time_clusters',0)
    score=f"{q['deduplicated_median_reduction_percent']:.1f}% / {q['deduplicated_worst_reduction_percent']:.1f}%" if ev else 'not measured'
    text+=f"| {r['tape']} / {r['start']:.2f} s | {r['sampled_channels']} | CH{r['selected_channel']} / {r['carrier_hz']:.1f} | {ev} | {score} | {r['status']} |\n"
text+='''
The late T510088 candidate is **CH18 around 116.5 Hz**, selected without supplying that frequency. Its distinct-frequency peer candidate is CH29 around 970 Hz; discovery/held-out curve correlations are approximately 0.996/0.831. The seven deduplicated event clusters show approximately **51.3% median and 45.6% worst improvement**. Both wrong-time controls worsen the aggregate result. This is encouraging new-interval evidence for generic discovery, not confirmation of every controller voice. The same tape's earlier 118 Hz candidate has weaker shared-motion evidence and falls short of the fixed 20% target threshold, so it remains unapproved despite modest improvements. The other tapes lack qualifying held-out events in these windows and correctly remain unvalidated rather than being called successful based on correlation.

| Generic window | Baseline median Hz RMS | Corrected median Hz RMS | 71 ms shifted control | 5 s shifted control |
|---|---:|---:|---:|---:|
'''
for r in compact:
    m=r['metrics']
    if m:text+=f"| {r['tape']} / {r['start']:.2f} s | {m['baseline_hz']:.2f} | {m['corrected_hz']:.2f} | {m['shift_0.071_hz']:.2f} | {m['shift_5.0_hz']:.2f} |\n"
text+='''
[Matched CH15 listening example from the newly discovered late-T510088 reference](reports/irig_free_research_20261016/generic_trial/T510088_discovered_reference_CH15_AB.wav): 12 seconds cleaned before, one second silence, the same passage corrected, with one shared gain. The reference was measured before cleanup. This file is numerically verified but has **not** been auditioned or accepted by the user. [Parameters](reports/irig_free_research_20261016/generic_trial/listening.json).

### Engineering checks and remaining work

Six isolated tests pass: chunk-boundary reads equal canonical reads; gaps are not compressed; changed files are deferred; canonical files replace chunks without double counting and duplicate canonicals block; timestamp-free canonicals require the explicit common-start declaration; hour-24 normalization; and generic discovery of synthetic **777/1229 Hz** shared references with held-out target correction plus noise-only abstention (some assertions share a test). The planted carriers differ from every historical preset. Tests use temporary files inside the workspace and remove only their own temporary directories. The first test attempt encountered a sandbox temporary-directory path issue; specifying a workspace-local test directory resolved it without escalation.

All real sampled intervals were checked against the corrected mapping, and original production DSP hashes still match the earlier study. This demonstrates the generic research path on available fragment streams and synthetic canonical streams. It does not validate full-corpus coverage, RF64/other unsupported source formats, variable-speed models, true oscillator independence, listening quality, automatic reference transitions or production rendering. Source rates here were 8 kHz; candidate scanning below Nyquist cannot recover ultrasonic tape bias absent from the files.

Before integration, expand time sampling with explicit budgets, calibrate reference-selection/abstention against reviewed diverse passages, confirm absolute stream origins/head offsets for canonical files, and test overlapping-window transitions and real full-channel files once available. Keep source timeline maps separate from flutter maps. A new mapping snapshot is needed after downloads or concatenation; a changed file must be rechecked, not silently reused. No ongoing monitor, download change or full concatenation was launched.

### Commands

Build a fresh inventory from current delivery and canonical folders:

```powershell
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\generic_scan.py manifest --output reports\\generic_next_manifest.json
```

When timestamp-free canonical files are ready and their common start has actually been established, add `--canonical-common-start`. Canonical files then take precedence; chunks remain inventory context and are not counted twice. Do not use that flag merely because lengths match.

Reproduce the bounded representative scan in a **new** output folder:

```powershell
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\generic_scan.py scan --manifest reports\\irig_free_research_20261016\\generic_trial\\manifest_snapshot.json --output reports\\generic_recheck --tapes 510073,510088,510106 --windows 2 --max-tape-windows 6 --candidates 3
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\test_generic_scan.py
```

Omit `--tapes` to consider the entire inventory. Increase `--max-tape-windows` explicitly to analyze more tape/windows; defaults deliberately stop after six and record unscanned coverage. `--windows` changes the number of time samples per tape, and `--duration` accepts 30–120 seconds. The scanner refuses to overwrite an existing result set. `generic_report.py` appends/replaces only this named report section and generates the single bounded listening artifact; original study and frozen-candidate appendix remain intact.
'''
report=ROOT/'IRIG_FREE_FLUTTER_RESEARCH.md';previous=report.read_bytes();marker=b'\n\n## Generic scanner: all-channel discovery with canonical stream inputs';prefix=previous.split(marker)[0]
report.write_bytes(prefix+text.encode('utf-8'))
print(json.dumps(compact,indent=2))
print('Appended generic scanner report. Production hashes unchanged.',flush=True)
