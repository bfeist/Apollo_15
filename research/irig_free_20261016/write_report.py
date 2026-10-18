"""Build the research notebook from retained measurements; no audio processing."""
import hashlib,json,platform,sys,time
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports/irig_free_research_20261016'
def load(name):return json.loads((OUT/name).read_text())
rows=load('summary.json');validation=load('validation.json');focused=load('focused_validation.json');search=load('additional_event_search.json');synth=load('synthetic.json');controls=load('pair_controls.json')
def metric(q):
    if not q:return '—'
    v=[x['reduction_percent'] for x in q]
    return f'{np.median(v):.1f}% / {min(v):.1f}%'
def best_control(results,prefix):
    values=[np.median([q['reduction_percent'] for q in v]) for k,v in results.items() if k.startswith(prefix) and v]
    return f'{max(values):.1f}%' if values else '—'
sources={m['path']:m for r in rows if 'sources' in r for m in r['sources']}
fresh=[]
for p,m in sources.items():
    path=Path(p);s=path.stat()
    fresh.append(dict(path=p,unchanged_size_mtime=(s.st_size==m['bytes'] and s.st_mtime_ns==m['mtime_ns']),
        local_wavs_in_channel_folder=len(list(path.parent.glob('*.wav')))))
state=dict(completed_utc=datetime.now(timezone.utc).isoformat(),python=sys.version,platform=platform.platform(),
    source_recheck=fresh,code_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in list((ROOT/'research/irig_free_20261016').glob('*.py'))+[ROOT/'scripts/flutter.py',ROOT/'scripts/audio_processing.py',ROOT/'scripts/05b_deflutter.py']})
(OUT/'provenance.json').write_text(json.dumps(state,indent=2))
text='''# IRIG-free flutter research — 2026-10-16

## Conclusion

**There is a workable local fallback, but not yet a defensible unattended replacement for IRIG-B across these tapes.** The accepted T510088 CH14 example reproduces and beats the tested wrong-time controls. Its success does not transfer uniformly to later passages or other tapes. The next useful development is per-tape, per-interval reference qualification with an explicit “no trustworthy correction” outcome, not automatic application of the 631.5 Hz preset everywhere.

This investigation leaves all production scripts, accepted references and source audio untouched. It introduces isolated research code and reports only. No download was stopped or changed; no full-tape output, commit or push was made.

## Principal findings

1. **Accepted example reproduced:** T510088 fragment 14, source seconds 80–130, CH14 reference to CH12, two candidate Quindar bursts: about **71% median reduction in raw-audio pitch RMS**. The previously accepted cleaned-audio result was 69.5%/70.1%; the new calculation intentionally uses the original target and automatically detected event boundaries. These are the same historical events, so this is calibration, not independent generalization.
2. **Later T510088 passages are much weaker:** the unchanged preset gives 12.7% on one event in seconds 200–250 and 19.2% median on two events in seconds 500–550. The latter includes approximately 4.6% and 33.8% reductions. A five-second-shifted map does better on the latter median. These outcomes do not justify applying one model across the entire tape.
3. **Blind cross-tape transfer can damage the target:** fixed CH14/631.5 Hz correction increases pitch variation by 16.9% on T510092 at 200 seconds and 73.3% on T510106 at 1550 seconds. A correction curve being finite, smooth and bounded is not evidence that it represents the wanted speed error.
4. **New candidate near 400 Hz:** reference-only search chooses CH17 around 404 Hz on T510073. It reduces one candidate event's RMS by 50.3% in its discovery interval and 34.7% 450 seconds later with the frequency/channel frozen. However, a five-second-shifted map reduces the second event by 62.1%. It remains tentative. T510106 CH12 around 406 Hz gives 32.9% on one event, but has no later event holdout in this bounded search. Neither candidate is adopted.
5. **Spectral strength alone fails as a selector:** the brightest persistent band is frequently near-stationary 180 Hz hum. It can carry little of the target's wobble. Conversely, different harmonics of the same interference source can be highly correlated. They are not independent physical timing references.
6. **The accepted algorithm has a limited band/model:** synthetic 2.5%-amplitude speed errors at 4, 6.5 and 9 Hz leave 2.6%, 6.8% and 13.0% of independent-target pitch RMS. At 1, 3, 12 and 16 Hz it removes little or nothing. A 5–10 Hz changing modulation rate leaves 58.2%. This measures this implementation and settings, not the limits of all tone methods.

## Scope, data integrity and compute bounds

The user reported approximately 80% download completion. This is not a remote inventory audit. Ten tapes were selected across the available tape-number range, including earlier failures, the accepted tape, later tapes and the visibly incomplete T510112. Each uses one matching fragment group and two prespecified 50-second windows (80 and 800 seconds into the fragment). Except for T510088 fragment 14, the group is the middle available stable matching group, not a random mission sample. Channel numbers change roles between tapes; filenames are authoritative.

Candidate channels: 2, 7, 12, 13, 14, 15, 17, 20, where available. Matching timestamp suffixes, sample rates and frame counts are required. These checks do not prove interleaved-head alignment or channel identity. No lag was fitted to force cross-channel agreement.

Sources must be mono PCM WAVs at supported rates, at least ten minutes old, with declared PCM data fitting within physical size and a readable final frame. Size and mtime must remain unchanged around each excerpt read. Every analyzed excerpt records its exact path, start, duration, header information and float64 sample SHA-256. This demonstrates locally readable stable fragments, not source authenticity or completion against Box. No full-source hashes were calculated, to avoid unnecessary disk reads.

Numerical libraries use one worker thread per process, with at most two bounded research processes running together. No full-corpus audio decoding, large audio copies or dependency downloads were performed. Initial survey headers are checked once per eligible channel file; actual audio reads seek directly to the selected windows. Float arrays are bounded to 50-second excerpts; resampling uses the existing accepted implementation. Extra audio scans stop after two event-bearing windows per tape, with at most nine 50-second offsets on each of CH12/CH15. Limited repeated reads support independent checks and listening renders.

'''
text+=f"The initial survey contains **{len(rows)} windows, {sum(len(r.get('sources',[])) for r in rows)} channel-excerpts, and {len(sources)} distinct source WAVs**. Additional event search inspected **{sum(len(r['scans']) for r in search)} channel-windows**; all were bounded excerpts, not full files. At completion, **{sum(r['unchanged_size_mtime'] for r in fresh)}/{len(fresh)}** surveyed source files retained the same size and mtime. Exact completion time and code hashes are in [provenance.json](reports/irig_free_research_20261016/provenance.json).\n\n"
text+='''### Surveyed fragment groups

All paths begin `D:/Apollo15_delivery/A15_T…/A15-CH…/`. The fragment suffix below plus the per-channel full path in each `result.json` identifies the inputs unambiguously. Starts are seconds into that fragment, not mission elapsed time.

| Tape | Matching fragment timestamp suffix | Channels present in sample | Stable matching groups available |
|---|---|---|---:|
'''
for r in rows:
    if r.get('start')==80:text+=f"| {r['tape']} | `{r['group']}` | {', '.join(str(m['channel']) for m in r['sources'])} | {r['stable_matching_groups']} |\n"
text+='''
Source paths/hashes and every candidate estimate: [summary.json](reports/irig_free_research_20261016/summary.json). Each `<tape>_<start>/selection.json` freezes selection before target-event measurement; `curves.npz` retains the estimated speed curves. The extra-search file includes the paths/hashes of scans that found no events, so negative coverage is reproducible.

## Methods and separation of discovery from validation

- **No correction:** original event pitch RMS about its own mean. This isolates relative wobble and does not measure absolute playback speed.
- **Fixed accepted method:** untouched CH14; configured 631.5 Hz carrier, nearby stationary hum removal, ±40 Hz capture, reference-only 4–10 Hz seed, robust local sinusoid fits, support gate, 4% speed limit and endpoint fade. Same duration-preserving inverse time warp is applied to target audio. No gain/lag is fit to target beeps.
- **Spectral auto baseline:** top three broad spectral peaks per available channel, persistence threshold and local contrast, choosing the strongest qualifying reference. Selection does not inspect target-beep improvement. It frequently chooses stationary mains interference.
- **Coherent-pair diagnostic:** select different channel/carrier pairs with sufficient motion and zero-lag correlation using the first 50-second window. Freeze those channels/frequencies at 800 seconds. Initial heuristic excluded integer carrier ratios, but did **not** exclude all rational ratios of a common 60 Hz family. Inspection found the evaluated selected pairs are both near mains harmonics. Therefore their results remain diagnostic; the gate is not a production acceptance rule. The `both_near_60hz_family` field in `pair_controls.json` explicitly records this weakness.
- **Non-mains candidate search:** condition reference copies with 2 Hz notches at multiples of 60 Hz, then search residual peaks. Reject centers within 5 Hz of a nominal harmonic; require persistence, contrast, estimated motion and support. Rank using reference spectra only, then freeze the chosen channel/frequency for the next event window. Estimates themselves still use the original reference. This is a heuristic: a 174 Hz candidate can still be displaced hum, and “non-mains” does not prove a historical oscillator.
- **Event detection:** a wide 2.5 kHz complex tracker selects 0.18–0.36-second bursts with stable amplitude, plausible frequency and low fine-scale roughness. Fifty milliseconds are removed at each edge. Before/after masks refer to the same source interval through the inverse map. Event type is not audited from transmission context. Accordingly, all new detections are called **candidate tone events**, not certified Quindars. Recurring sequences on T510073 CH7 and T510092 CH20 may be other signaling; counting them as many independent Quindar tests would exaggerate evidence.
- **Controls:** circular shifts of 71 ms, 137 ms, one second and five seconds applied to the estimated curve. These are deliberately wrong-time controls, not alternative correction tuning. A near-period shift can preserve genuine periodic flutter, so it is not always a null. Long shifts can also accidentally agree. Report all four rather than choosing only the worst one.
- **Independence:** different copies of the same communications beep are replication, not independent oscillators. Cross-channel noise coherence can reflect digitizer interference or a duplicated feed. No algorithm here proves the source tone was recorded with every controller voice.

### Initial-window event measurements

Entries are median / worst-event percentage reductions in RMS pitch variation; negative values mean worsening. Empty windows remain in the coverage table, rather than being silently treated as success. Event counts include repeated signaling events and are not counts of independent experiments.

| Tape / start | Validation channel | Events | Fixed accepted | Spectral auto | Frozen pair diagnostic |
|---|---:|---:|---:|---:|---:|
'''
for r in validation:
    q=r['results']['fixed_accepted']
    text+=f"| {r['tape']} / {r['start']:g} | {r['validation_channel']} | {len(q)} | {metric(q)} | {metric(r['results'].get('spectral_auto',[]))} | {metric(r['results'].get('frozen_pair_diagnostic',[]))} |\n"
text+='''
For T510073 at 80 seconds, the coherent-pair diagnostic improves the 19 candidate CH7 events by about 49% median. Half-period control degrades them, while the near-period control retains much of the improvement. This is encouraging local relative-motion evidence, but its reference pair (about 418/300 Hz) is near a common mains family and the events are a repeating signaling sequence of unconfirmed origin. It does not establish all-track restoration. On T510075 the same pair selection's discovery improvement largely disappears in the later interval; wrong-time controls can outperform it. This is precisely why frozen-window testing matters.

Complete events, before/after RMS in Hz, zero-lag correlations, replication and controls: [validation.json](reports/irig_free_research_20261016/validation.json), [pair_controls.json](reports/irig_free_research_20261016/pair_controls.json).

### Additional event-bearing windows

First row for each tape discovers its non-mains candidate; subsequent row holds channel/frequency fixed. The best shifted control column is intentionally reported to expose chance/periodic agreement, not to recommend shifting the map.

| Tape / start | Target | Events | Fixed accepted median / worst | Frozen non-mains median / worst | Best shifted non-mains median |
|---|---:|---:|---:|---:|---:|
'''
for r in focused:
    text+=f"| {r['tape']} / {r['start']} | CH{r['validation_channel']} | {len(r['results']['fixed_accepted'])} | {metric(r['results']['fixed_accepted'])} | {metric(r['results'].get('frozen_nonmains',[]))} | {best_control(r['results'],'frozen_nonmains_shift_')} |\n"
text+='''
Exact additional source identifiers, event timestamps, references and controls: [focused_validation.json](reports/irig_free_research_20261016/focused_validation.json). Full extra scan coverage, including no-event windows: [additional_event_search.json](reports/irig_free_research_20261016/additional_event_search.json). Reference motion is remeasured in each window; “frozen” means estimator settings/channel/frequency, not extrapolating an old phase curve into the future.

## Synthetic bandwidth and negative controls

16-second 8 kHz recordings; a 631.5 Hz reference and independent 2475 Hz target share a known 2.5%-amplitude time warp, with strong stationary 600/660 Hz interference and seeded Gaussian noise on the reference. Target pitch is measured after actual resampling. Endpoints are excluded from the metric. Remaining RMS ratio 1 means no improvement, 0 means ideal removal. The varying-rate case sweeps modulation from 5 to 10 Hz; dropout removes the reference for four seconds while interference/noise remain. The stationary-reference case deliberately gives the reference none of the target's motion.

| Case | Injected modulation | Target remaining RMS / original | Estimated/truth correlation |
|---|---:|---:|---:|
'''
for r in synth:text+=f"| {r['mode']} | {r['injected_rate_hz']} Hz | {r['rendered_remaining_rms_ratio']:.3f} | {r['correlation']:.3f} |\n"
text+='''
The 200 Hz timing-curve sampling rate is not a 100 Hz usable tracking bandwidth. This estimator chooses a 4–10 Hz modulation seed and a local sinusoidal model; it cannot recover arbitrary slow wow, fast flutter, irregular motion or missing reference evidence. The synthetic stationary-reference case correctly leaves the target essentially unchanged, but real interference mixtures can still generate spurious tracked motion. [synthetic.json](reports/irig_free_research_20261016/synthetic.json)

## Listening review

[Open the four matched A/B examples](reports/irig_free_research_20261016/listening/index.html). Each contains 12 seconds before, one second silence, and the same 12 seconds after. Both versions have the same iterative tonal cleanup and mild broadband denoise before resampling, and one gain shared across the pair. The strongest pre-correction energy selects the listening window; this is not a speech classifier. Three tentative examples and a T510092 failure are included to make adverse effects reviewable. The retained WAVs were checked for finite samples, expected rate/duration and peaks below 0.851. I have not auditioned them or obtained listening acceptance. [Render parameters and verification](reports/irig_free_research_20261016/listening/manifest.json)

All main tables above use raw-target frequency measurements to avoid conflating a cleanup change with a timing result. The listening path cleans first. Its manifest additionally measures events on equally cleaned target audio where that target is the validation channel. No independent normalization obscures the before/after comparison.

## Practical recommendation and decision gates

1. Preserve the existing accepted stage and reference example. Treat its configuration as a local worked example, not a tape-independent preset.
2. Add reference qualification as a research stage before batch processing: retain multiple narrow and broadened candidates from original channels, distinguish nearly stationary mains from shared fractional motion, and keep provenance/confidence for every time interval.
3. Freeze channel/frequency/filter choices, then test several unseen intervals and independent signaling events across early/middle/late tape positions and both head groups. Require improvement of actual resampled targets without material worst-case degradation; inspect matched speech and removed-signal examples. Do not count duplicated feeds or harmonics as independent sources. The present small sample does not justify a calibrated numeric acceptance threshold.
4. Use short Quindar events as checks and possible slow-speed anchors once intro/outro identity and generator calibration are established. Do not extend one beep's phase over a tape. Unknown continuous carriers support relative flutter, not absolute mean speed, UTC or GET. Preserve duration unless a separate absolute anchor explicitly justifies changing it.
5. Where reference support disappears or validation fails, retain uncorrected audio and flag the interval. A smooth, bounded estimate is not sufficient to correct it. T510092 and blind T510106 transfer are concrete rejection examples.
6. Before full tapes: implement overlap/context handling, reference transitions, dropout/phase-slip detection, variable modulation-rate models, head alignment and bounded-memory processing, then test boundaries explicitly. These are future engineering tasks; this study did not change production behavior.

**Confidence:** high that fixed-preset transfer is unsafe and that the accepted algorithm's current band is limited; moderate that useful recorded tones can provide local relative correction beyond the original example; low that the newly found 404/406 Hz candidates or automatic selectors generalize across a complete tape. No evidence here establishes mission-wide restoration or a replacement for absolute IRIG timing.

## Primary-source context and alternative methods

- [Maziewski, Modulation Frequency Constraints on Wow and Flutter Determination, 2008](https://acoustics.ippt.pan.pl/index.php/aa/article/download/639/558/1008): contrasts hum, spectral-center and high-frequency bias tracking, with settings-dependent bandwidth limits. This motivated testing actual synthetic time warps instead of inferring bandwidth from curve sampling rate. It does not validate our implementation or tapes.
- [Howarth et al., US7933084B2](https://patents.google.com/patent/US7933084B2/en): describes deriving correction from stable recorded reference components, including oscillator/bias signals. All surveyed files are 8 kHz in this study; ultrasonic bias absent from the delivered band cannot be recovered by filtering. In-band unidentified lines must not be called bias without evidence. Capture of suitable wideband originals would be a separate option, not a tested solution here.
- [Celemony Capstan manual, pp. 7–14](https://assets.celemony.com/assets/1/f/2/Manual_Capstan_1.1_English.pdf): describes comparing polyphonic pitch motion and applying a common curve to multiple tracks, while noting difficulties with sparse/expressive material. This supports investigating shared content motion, but speech harmonics from one voice are not independent oscillators. The earlier project's speech-motion trial remains useful context, not a new generalization result in this study.
- [Apollo Lunar Surface Journal, Quindar tones](https://apollojournals.org/alsj/quindar.html): historical signaling context for interpreting candidate intro/outro events. Frequency proximity alone does not establish tone identity or absolute speed. The present detector deliberately does not label absolute-speed corrections.
- The parent research supplied the [JAES hum-tracking paper record](https://aes.org/publications/elibrary-page/?id=14160), emphasizing hum recorded with the program and negligible digitization-added hum. Direct retrieval timed out in this execution; no additional empirical claim depends on having retrieved it locally. The provenance distinction is also required by our stationary-reference controls and the other reference-tone sources above.

## Reproduction and retained evidence

Run from `F:/_repos/Apollo_15` using the existing environment; no install is required. To preserve this snapshot, copy research outputs aside before a full rerun. `survey.py` skips existing window directories; use a fresh `--output` for a clean survey, and adjust downstream `OUT` if doing so. The remaining scripts regenerate their own research JSON/report files, never production or delivery files.

```powershell
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\survey.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\validate.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\find_events.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\focus.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\pair_controls.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\render_review.py
.\\.venv\\Scripts\\python.exe -B research\\irig_free_20261016\\write_report.py
```

`find_events.py` resumes completed tape scans. The exact tested paths and hashes take precedence over rediscovery after more downloads arrive. No scheduled monitor was created; this is a completed bounded investigation, not a claim of ongoing coverage. Subsequent downloads and all unvisited channels/intervals remain untested. Old missing-IRIG surveys were read as context; this run did not exhaustively re-audit IRIG absence or assume it from channel filenames. Full source authenticity, head offsets, speech intelligibility preservation, absolute speed and perceptual improvement remain unverified.
'''
(ROOT/'IRIG_FREE_FLUTTER_RESEARCH.md').write_text(text,encoding='utf-8')
print('Wrote report',len(text),'characters; sources',len(sources),'unchanged',all(r['unchanged_size_mtime'] for r in fresh))
