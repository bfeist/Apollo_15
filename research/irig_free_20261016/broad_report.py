"""Reconcile finite discovery artifacts, verify provenance, and append coverage report."""
import hashlib,json,re
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'reports/irig_free_research_20261016'
OUT=BASE/'broad_discovery'
manifest=json.loads((BASE/'broad_manifest.json').read_text())
config=json.loads((OUT/'config.json').read_text())
assert hashlib.sha256((BASE/'broad_manifest.json').read_bytes()).hexdigest()==config['manifest_sha256']
results=[json.loads((OUT/(t['tape']+'.json')).read_text()) for t in manifest['tapes']]
summaries=[];sources={};fallbacks=[];ranked=[];skips=[]
for r in results:
    windows=[w for c in r['channels'] for w in c['windows']]
    summaries.append(dict(tape=r['tape'],channels=len(r['channels']),channels_analyzed=sum(any(w['status'] in ['analyzed_audio','reused_audio_analysis'] for w in c['windows']) for c in r['channels']),new_windows=sum(w['status']=='analyzed_audio' for w in windows),reused_windows=sum(w['status']=='reused_audio_analysis' for w in windows),skipped_windows=sum(w['status']=='skipped_unavailable_or_incomplete' for w in windows),missing_expected_channels=r['missing_expected_channels'],elapsed_seconds=r['elapsed_seconds'],top_candidates=r['shortlist'][:3]))
    for c in r['channels']:
        for w in c['windows']:
            if w.get('sampling','').startswith('fallback'):fallbacks.append(dict(tape=r['tape'],channel=c['channel'],start=w['start'],requested_start=w['requested_start'],sampling=w['sampling']))
            if w['status']=='skipped_unavailable_or_incomplete':skips.append(dict(tape=r['tape'],channel=c['channel'],window=w))
            for s in w.get('sources',[]):sources[s['path']]=s
            for cand in w.get('candidates',[]):
                if cand.get('status')=='unvalidated_coherent_candidate':ranked.append(dict(cand,tape=r['tape']))
ranked.sort(key=lambda c:c['rank_score'],reverse=True)
opportunities=[];seen=set()
for c in ranked:
    if c['tape'] not in seen:opportunities.append(c);seen.add(c['tape'])
stability=[]
for p,s in sources.items():
    try:
        st=Path(p).stat();ok=st.st_size==s['bytes'] and st.st_mtime_ns==s['mtime_ns']
        stability.append(dict(path=p,unchanged_size_mtime=ok))
    except OSError as e:stability.append(dict(path=p,unchanged_size_mtime=False,error=str(e)))
original=json.loads((BASE/'provenance.json').read_text())
code={name:hashlib.sha256((ROOT/name.replace('\\','/')).read_bytes()).hexdigest()==digest for name,digest in original['code_sha256'].items()}
canonical=list(Path(manifest['canonical_root']).rglob('*.wav'))
totals={key:sum(s[key] for s in summaries) for key in ['channels','channels_analyzed','new_windows','reused_windows','skipped_windows']}
totals.update(tapes=len(results),missing_expected_channel_slots=sum(len(t['missing_expected_channels']) for t in manifest['tapes']),snapshot_files=sum(c['discovered_chunk_files']+c['discovered_canonical_files'] for t in manifest['tapes'] for c in t['channels']),fallback_windows=len(fallbacks),unique_sampled_source_files=len(sources),all_sampled_sources_same_size_mtime=all(s['unchanged_size_mtime'] for s in stability),production_hashes_match=all(code.values()),canonical_wav_count=len(canonical))
audit=dict(verified_utc=datetime.now(timezone.utc).isoformat(),snapshot_utc=manifest['created_utc'],totals=totals,source_stability=stability,production_hashes=code,fallbacks=fallbacks,skipped=skips,ranked_next_validation=opportunities,original_completion=json.loads((OUT/'completion.json').read_text()),provenance='Original scan launched earlier in this task in session 42997, recovered exit code 0. One bounded availability retry followed. No production rendering performed.')
(OUT/'summary.json').write_text(json.dumps(summaries,indent=2))
(OUT/'final_verification.json').write_text(json.dumps(audit,indent=2))
lines=['','', '## Broad all-channel snapshot discovery', '',f"Snapshot: **{manifest['created_utc']}**. Completed finite screening of **{len(results)} tapes and {totals['channels_analyzed']}/{totals['channels']} available channels**, drawn from {totals['snapshot_files']:,} WAV files. There are {totals['missing_expected_channel_slots']} absent channel slots relative to channels 1–30 per tape; these were not analyzed. Inventory is not whole-recording audio coverage.",'',f"Final coverage: **{totals['new_windows']} new 30-second channel-windows + {totals['reused_windows']} prior 50-second channel-windows**, two sampled intervals per available channel, {totals['skipped_windows']} unresolved sample attempts. {len(fallbacks)} windows used explicitly recorded fallback positions. These positions do not fill missing timestamps or establish full-channel completeness.",'','The initial pass skipped T510081 CH8 at 49,542.25 seconds: `requested interval not covered`. Its nearest available fallback duplicated the first sample. One availability-only retry chose a distinct interior point; the previous skipped record is retained. No retries were selected based on favorable signal outcomes.','', '[Per-channel windows, sources and candidates](reports/irig_free_research_20261016/broad_discovery/) · [Reconciled per-tape summary](reports/irig_free_research_20261016/broad_discovery/summary.json) · [Provenance, fallback positions and ranked opportunities](reports/irig_free_research_20261016/broad_discovery/final_verification.json) · [Retry audit](reports/irig_free_research_20261016/broad_discovery/availability_retry.json)','', '### Method and confidence','', 'New screening discovers up to two carriers per channel from the first half of a 30-second sample and measures 4–10 Hz fractional motion with a fast phase tracker. It compares different-frequency candidates on different channels at the same actual timestamp, ranking with discovery-half correlation and checking second-half correlation. Mains pairs and near-integer frequency ratios are excluded from peer evidence. High coherence still does not prove independent oscillators or shared tape transport error. The diagnostic curves are not accepted correction maps. No new target-event resampling validation or listening acceptance was performed by this broad pass. Prior reused results retain their distinct full-tracker method and validation status.','', '### Ranked next validation opportunities','']
for c in opportunities[:5]:
    p=c['best_peer'];lines.append(f"- {c['tape']} at {c['window_start']:.2f} s: CH{c['channel']} {c['carrier_hz']:.1f} Hz with CH{p['channel']} {p['carrier_hz']:.1f} Hz; discovery/second-half correlation {p['discovery_correlation']:.3f}/{p['holdout_correlation']:.3f}. Unvalidated coherent candidate; freeze settings, test unseen intervals and independent target events before use.")
lines+=['','The previously supported late T510088 CH18 116.5 Hz result remains a separate validation follow-up (seven event clusters, 51.3% median improvement); it was reused, not independently rediscovered or replicated here. The 404/406 Hz holdout failures/limited coverage in the earlier appendix still apply.','', '### Per-tape coverage','', '| Tape | Channels sampled / available | New / reused / skipped windows | Top new discovery candidate |','|---|---:|---:|---|']
for s in summaries:
    top=s['top_candidates'];candidate=f"CH{top[0]['channel']} {top[0]['carrier_hz']:.1f} Hz; {top[0]['status']}" if top else 'Prior results reused; see channel records'
    lines.append(f"| {s['tape']} | {s['channels_analyzed']}/{s['channels']} | {s['new_windows']}/{s['reused_windows']}/{s['skipped_windows']} | {candidate} |")
lines+=['','### Verification and remaining limitations','',f"Source recheck: {len(sources)} distinct sampled files checked for unchanged size and modification time; all unchanged: **{totals['all_sampled_sources_same_size_mtime']}**. Stored excerpt hashes preserve original read provenance; this recheck did not reread the entire audio corpus or prove same-size/same-time content identity. Original production-code hashes all match: **{totals['production_hashes_match']}**. Canonical WAVs found at verification: **{len(canonical)}**; no real canonical file was available for the requested test, and none was created.",'', 'The original full-pass completion records 188.36 seconds for its invocation, which reused an earlier two-tape pilot. This is a finite snapshot, not a monitor; later downloads are outside its coverage. Full-tape continuity, head alignment, absolute speed, reference switching, and perceptual quality remain unvalidated. Research scripts, JSON/NPZ reports and this Markdown appendix were created or updated during this work; source audio and production processing were not changed.','', 'Verification commands: `python -B -m unittest discover -s research/irig_free_20261016 -p "test_*scan.py" -v` and `python -B -m unittest discover -s research/irig_free_20261016 -p "test_broad_discovery.py" -v`. Test results are recorded in `broad_discovery/checks.json` after execution.','']
report=ROOT/'IRIG_FREE_FLUTTER_RESEARCH.md';before=report.read_bytes();prefix=before.split(b'\n\n## Broad all-channel snapshot discovery')[0]
report.write_bytes(prefix+'\n'.join(lines).encode())
print(json.dumps(totals,indent=2));print('Ranked opportunities:',[(c['tape'],c['channel'],c['carrier_hz']) for c in opportunities[:5]])
