# Iterative deflutter research runbook for Dot

Version: 2026-10-30. Repository: `F:\_repos\Apollo_15`.

## 1. Assignment

Lead a reproducible, iterative investigation of timing references across the Apollo 15 tapes. Build a general method that discovers useful underlying tones, measures their timing motion, and establishes where correction helps. Do not build a detector around one successful passage, frequency, circuit, or channel label.

Do the research and implementation autonomously within the boundaries below. Run bounded experiments, inspect evidence, identify the limiting stage, and choose the next experiment. Do not stop after a plan, one attractive example, or a failed candidate. Preserve failures and explain uncertainty. This document authorizes research work and experimental review artifacts; it does not authorize a production restoration run or a claim of human listening approval.

The intended outcome is a tested methodology and a defensible per-tape recommendation. Higher apparent correction coverage alone is not success.

### User requirements that override earlier interpretations

- Digitization introduced extremely loud, unfluttered ground hum. That contamination must not be mistaken for historical timing information or crowd other candidates out of discovery.
- **Keep original-audio discovery and tracking. A reference does not have to survive denoising.** Cleanup can remove useful historical tones. Search originals and existing denoised audio independently, combine candidates with provenance, and test the extraction path actually used.
- Denoised audio is a complementary discovery/measurement source, not an eligibility requirement. A denoised-only discovery can be tracked on originals if independently qualified there.
- Search for underlying residual tones, including persistent phone-line background noise during silence, speech, and changes in line use. Do not restrict discovery to dial tones, signaling events, known telephone frequencies, or speech-free intervals.
- GOSS CONF is an example, **not a special case to implement**. No channel name may determine which frequencies are searched or which candidates are eligible.
- A narrow spectral peak, shared feed, or correlation does not establish transport motion. An audible broadband noise floor may contain no usable frequency reference.
- Discover throughout the available recordings. Support may change and references may switch; unsupported regions remain uncorrected.

## 2. Starting evidence and repository map

Read these before changing the scientific engine:

| Resource | Purpose |
| --- | --- |
| `reports/deflutter_discovery_audit_20261030/assessment.txt` | Detailed audit, corrected to preserve original-only references |
| `reports/deflutter_discovery_audit_20261030/evidence.json` | Source-bound discovery replay, survey/budget counts, proposed-carrier distribution |
| `reports/deflutter_discovery_audit_20261030/cleaned_evidence.json` | Short-window diagnostic and cleanup-log evidence; extra peaks are not qualified carriers |
| `docs/deflutter_v5.md` | Current v5 operation and checkpoint behavior |
| `docs/continuous_reference_pipeline.md` | Earlier architecture and limitations; distinguish historical claims from current evidence |
| `scripts/05_deflutter/flutter_tracking.py` | Spectral discovery, tracking, check-event detection |
| `scripts/05_deflutter/v5_allchannel.py`, `scripts/05_deflutter/v5_resume.py` | All-channel survey, candidate provider, durable survey storage |
| `scripts/05_deflutter/continuous_acquisition.py` | Event roles, dependence handling, acquisition horizons |
| `scripts/05_deflutter/continuous_planning.py`, `scripts/05_deflutter/flutter_planning.py` | Candidate screening, shortlist, reference lifetimes, calibration/validation |
| `scripts/05_deflutter/continuous_tracking.py`, `scripts/05_deflutter/continuous_maps.py` | Persistent motion extraction and joined maps |
| `scripts/05_deflutter/continuous_quality.py`, `scripts/05_deflutter/flutter_policy.py` | Actual eligibility gates, controls, hashes, production contract |
| `scripts/05_deflutter/v5_safe_assessment.py` | Exact-map evaluation and explicit rejection of invalid maps |
| `scripts/05_deflutter/audio_processing.py`, `scripts/05_deflutter/channel_review.py` | Cleanup, cached denoised audio, rendering, source identity |
| `tests/test_v5_*.py`, `tests/test_continuous_*.py` | Existing scientific/storage regression coverage |

Canonical data roots:

```text
D:\Apollo_15_30-Track\orig_concatenated\A15_T*
D:\Apollo_15_30-Track\denoised\A15_T*\CHnn\denoised.wav
D:\Apollo_15_30-Track\denoised\A15_T*\CHnn\denoised.json
D:\Apollo_15_30-Track\denoised\A15_T*\CHnn\cleanup.jsonl
D:\Apollo_15_30-Track\deflutter_planning_v5_allchannel\A15_T*
```

The completed v5 batch has 39 tapes. This is a starting observation to verify, not a hard-coded future inventory size. All completed assessments already contain reserved-event outcomes. Do not confuse those historical reservations with new untouched evidence.

Audit findings to test, not work around with exceptions:

- Discovery is repeated throughout each tape, but uses a temporal median, at least 50% occupancy, 30 Hz peak spacing, and eight hypotheses per channel/source version.
- In three fully audited surveys, 79% of original candidate instances were exact mains harmonics rejected after consuming candidate slots.
- All 229 detailed acquisition calls in that sample exhausted their eight-reference budgets. Cheap screening is not the same as full candidate qualification.
- Same-channel references are excluded from direct qualification. The v5 transfer-group alternative is disabled.
- Qualification depends heavily on detected short check-tone events. Missing such events does not establish missing carrier tones.
- Cleanup removes persistent non-mains features too. The current engine tracks motion on originals even when discovery used cleaned samples; that capability is valid to retain.
- Lifting the cap produced more hypotheses in originals but none in 36 sampled cleaned contexts. Shortening the search window produced many extra cleaned peaks, most with poor preliminary tracking. Neither cap increases nor shorter windows alone are an adequate methodology.
- Across all saved proposals, about 84.5% of proposed channel time uses centers within 5 Hz of a 60 Hz harmonic. This warrants investigation. **Five Hz proximity is not proof of mains origin and is not an approved universal exclusion width.**

## 3. Working boundaries and autonomy

1. Inspect applicable repository instructions, `git status`, active processes, and existing experiment state first. The repository may contain substantial pre-existing uncommitted work. Do not stash, reset, overwrite, or commit that work as your own.
2. Preserve canonical originals, existing denoised audio, cleanup logs, and every completed planning bundle. Do not re-denoise the archive or change cleanup as a shortcut to finding tones.
3. Start new methods in separate research modules and fresh output directories. Snapshot the baseline implementation and its hashes before modifying shared modules. A new scientific implementation needs a new methodology/version identity; do not disguise it as a compatible v5 resume.
4. You may write research code, meaningful tests, manifests, local reports, diagnostic plots, and bounded experimental audio. You may continue through failed experiments without asking the user which parameter to try next.
5. Do not launch whole-archive correction rendering, publish artifacts externally, mark production accepted, or invent listening approval. Put perceptual questions in a review queue and continue independent research while they await a listener.
6. Keep final evidence isolated. Do not open sealed outcomes to decide what implementation to build. If a tool would expose them automatically, use or build a discovery-only adapter first.
7. Do not relax validation limits to increase coverage. A potentially useful alternative acceptance contract is a separate research hypothesis, not permission to relabel current failures.
8. Do not wait indefinitely on an unchanged process. Inspect logs, committed checkpoints, CPU/I/O, and errors. Stop/restart only your own bounded jobs when needed, preserving committed state and failure evidence.

## 4. Persistent experiment state

Use a new campaign root, for example:

```text
F:\_repos\Apollo_15\reports\deflutter_iterative_20261030\
    campaign.json
    inventory.json
    split_manifest.json
    exposure_ledger.jsonl
    experiment_ledger.jsonl
    candidate_catalog.jsonl
    state.json
    NEXT.md
    baseline\
    iterations\I001\
        experiment.json
        source_manifest.json
        implementation\
        discovery\
        tracking\
        calibration\
        validation\
        maps\
        review\
        metrics.json
        decision.md
```

Large numerical/audio outputs may use a fresh `D:\Apollo_15_30-Track\deflutter_research\<campaign>\I001\` directory. Record resolved absolute paths in the manifest. Never place new artifacts inside a baseline bundle. Check free space before rendering; do not make full audio copies merely to freeze metadata.

`reports/` is ignored by Git. Preserve research source and the runbook separately from generated outputs, and state which deliverables are untracked/ignored. Do not change ignore rules merely to include large data.

Required records:

| Record | Minimum contents |
| --- | --- |
| `campaign.json` | Objective, requirements, source roots, baseline identity, seeds, default compute limits, evaluation policy |
| `split_manifest.json` | Passage coordinates, source/head/feed metadata, roles, dependence groups, guard regions, selection seed, prior exposure status |
| `exposure_ledger.jsonl` | Who/what accessed which discovery features, labels, target outcomes, or listening excerpts; when; purpose; resulting evidence restrictions |
| `experiment.json` | One principal hypothesis, planned comparison, code/config hashes, passages, budgets, metrics, advance/reject conditions, expected failure signature |
| `source_manifest.json` | Tape/channel/file, sample rate, frames, offsets, file identity, decoded excerpt hashes, source version, denoised/cleanup binding |
| `experiment_ledger.jsonl` | Every completed/failed/interrupted attempt, parent iteration, outcome, artifact paths; append rather than replace failures |
| `candidate_catalog.jsonl` | Every retained hypothesis and its successive status; frequency/time/support/source provenance and reasons for rejection/deferral |
| `state.json`, `NEXT.md` | Completed steps, running jobs, committed cursors, current champion, unresolved issue, exact next command/action |

Each candidate needs a stable ID based on source identity and time-frequency lineage, not a display name or a winning score. Preserve merges/splits and multiple extraction variants. Store measurements per interval; do not let the first detection's stale score represent all later detections.

On a new Dot session: read this runbook, `campaign.json`, `state.json`, `NEXT.md`, and the last three decisions; verify source/code identities; inspect any running jobs; resume the next unfinished compatible step. Do not restart the investigation from an attractive earlier example.

## 5. Evidence separation across tapes and time

### 5.1 Make the split before optimizing

Build an inventory from current metadata. Record missing channels, timing-only sources, unassigned recordings, source durations, head labels, and overlapping mission intervals. Labels can support later reporting and stratification; never pass circuit names to the detector.

Use a declared seed such as `apollo15-deflutter-20261030-v1`. Store the selection algorithm as code and its resulting manifest. Do not reshuffle after seeing results.

Use four roles:

| Role | Use |
| --- | --- |
| Development | Repeated inspection, parameter fitting, failure analysis, ablations |
| Promotion validation | A bounded gate for a frozen challenger; once inspected, it is consumed for that generation |
| Reserved audit | Evaluation after a method freeze; no selection or refitting from its outcomes |
| Retrospective baseline | Previously inspected results; useful paired comparisons, explicitly not untouched evidence |

Assign roles to **mission-time blocks shared across all channels and overlapping tape recordings**, not independently to channel excerpts. Default to 30-minute blocks, with a guard of at least 300 seconds plus the longest filter/context dependency on either side. Increase the guard if an acquisition or tracker uses longer history. Record excluded guard regions in coverage accounting. Reset or separately initialize state where required so hidden context does not cross a role boundary.

Stratify the block selection across early/middle/late mission time, recording heads, tape positions, active/quiet speech, source quality, and available source versions. Begin with metadata-based selections; activity/quality labels used for later balancing must come from development data or a frozen outcome-independent procedure. Balance reserved blocks without examining correction outcomes.

Reel boundaries and head labels do not establish independence. Copies of a feed, simultaneous recordings, channel pairs, repeated check events, overlapping captures, and neighboring margins may be dependent. Apply the same role to potentially shared response events across channels/reels. Treat uncertain dependencies conservatively and keep effective independent counts separate from raw counts.

### 5.2 Handle existing exposure honestly

The full v5 batch has already evaluated events across all 39 tapes. Its passages cannot become untouched again because they receive new names or a new split. The initial discovery audit also exposed its selected windows.

- Register the existing plans/assessments and prior experiments as potentially exposed. Audit known exposure before describing any region as fresh.
- Existing outcomes can support paired retrospective comparisons and development. Freeze code/config before running even these comparisons.
- New synthetic draws and genuinely uninspected evidence may support new tests, but new labels or a new detector on already inspected audio do not automatically create independence.
- A reserved audit on previously exposed material is a **prospectively frozen retrospective audit**, with that limitation stated prominently.
- If no genuinely untouched real material remains, finish the strongest honest retrospective, synthetic, structural, and listening evidence available. Report the qualification limitation; do not manufacture an independent final claim. Request new/blinded material only if that is the remaining obstacle to the claimed level of qualification.
- After a failed promotion gate, quarantine its outcomes from the next gate. The next challenger needs another predeclared unused validation block. If gates are exhausted, continue development but stop promotion claims until the evaluation protocol is revised explicitly.

### 5.3 Keep discovery and response evidence distinct

You may inventory the entire recorded spectrum. Explicitly mark DC/edge limits, digitization contamination, and bands reserved for response evaluation. Full-band coverage must not mean using the same response samples as both predictor and validator.

Keep the current 2150–2850 Hz response-band exclusion for correction predictors unless a separately designed independent validation route justifies a candidate there. Record such spectral hypotheses as `response_band_withheld`, not as absent tones. Keep immutable calibration/validation/reserved roles; do not assign roles after seeing a candidate's results. Do not pool copies or three margins of one event as independent observations.

## 6. The experiment ladder

Finish each stage's exit check before scaling the next. This is a sequence of research questions, not a mandate to implement every suggested estimator.

### Stage 0 — Verify the baseline and build measurement tools

1. Inventory sources and inspect the completed run record, saved surveys, plans, and assessments. Verify representative source/code/assessment hashes.
2. Snapshot the scientific baseline, configuration, tests, and known limitations. Baseline comparisons must use that snapshot, not an imported module that changes under them.
3. Reproduce a small source-bound sample from the October 30 audit. Confirm that capped frequency lists match and that missing candidates are not explained by changed audio or alignment.
4. Implement the campaign records and a discovery-only runner before experiments on reserved material. Ensure metrics can distinguish detector truncation, tracker failure, insufficient independent evidence, rejected map, and unprocessed work.
5. Benchmark one 180-second passage across all available channels, originals and cached denoised audio. Record wall time, peak memory, disk throughput, and artifact size. Use these measurements for batch estimates.

Exit: baseline identity is fixed; replay is reproducible; sources align; the discovery-only path cannot invoke target final evaluation inadvertently; costs are measured.

### Stage 1 — General tone discovery independent of check beeps

Build a time-frequency catalog throughout each requested passage before deciding whether the target contains check events. Include all channels, timing sources, and explicitly identified unassigned sources; discovery does not require a previously successful donor.

Compare a small declared family of detectors, rather than immediately combining everything:

| Challenger component | Question |
| --- | --- |
| Contamination-aware peak extraction before allocation | Does excluding known digitization hum first recover other features without inventing motion? |
| Several FFT/window resolutions, for example 0.5, 2, and 8 seconds | Can both narrow persistent lines and moving/interrupted features be represented? These are starting trial settings, not accepted constants. |
| Local background/prominence estimates and time-frequency ridges | Can weak features remain visible during speech without requiring a one-minute temporal median? |
| Adaptive separation based on linewidth and neighboring energy | Can close tones be distinguished without the current fixed 30 Hz suppression? |
| Separate original/cleaned inventories merged by time-frequency support | Which discoveries are complementary, suppressed by cleanup, or contaminated? |

Keep rejected/withheld counts and explicit rejection reasons. Do not reserve slots for mains-family peaks. Do not replace a hard cap with an unbounded memory load: stream the catalog and process expensive candidates in resumable batches. A budget limits completed analysis, not the existence of hypotheses. Any pruning must have a documented, tested rule and accounted-for discarded candidates.

Track line support during silence and speech, changing amplitude, gradual center-frequency drift, interruptions, and reacquisition. Do not fit a fixed frequency learned on another passage and call that a full-spectrum search. Do not treat every speech harmonic or denoiser artifact as a carrier.

Exit: a replayable inventory, completeness accounting, measured detector recall/false-positive behavior, and a demonstrated benefit over baseline on the declared development set. More peaks alone do not satisfy the exit check.

### Stage 2 — Test whether each feature supports motion measurement

For each retained hypothesis, evaluate complex phase continuity, center/linewidth drift, amplitude support, capture-band competitors, phase slips, clipping, and neighborhood interference. Determine an explicit usable interval; never extrapolate through a loss.

Preserve original-sample tracking. Test cleaned-sample tracking when it may reduce contamination, recording cleanup attenuation and phase effects. Original-only candidates remain eligible. A denoised discovery may use an original extractor. Freeze the selected source/extractor path before validation.

Mains protection must address two different failure modes:

1. Loud stationary digitization lines consume discovery slots or suppress weaker neighbors.
2. A nearby line enters the candidate's demodulation capture and controls the apparent phase/frequency.

Mask known digitization contamination before candidate allocation. Test neighboring capture leakage and use synthetic loud-unfluttered-hum controls. Record how mask widths were measured; do not apply a blanket +/-5 Hz rule simply because the audit used it diagnostically. A modulation sideband is not automatically another independent carrier. A nearly stationary trace is not positive timing evidence, and nonzero measured modulation alone is not proof of historical flutter.

Explore carrier-specific capture widths and reasonable motion bands using development data. Avoid imposing the two current motion bands as universal truth, but do not expand them without testing phase/amplitude fidelity, support, and negative controls. Timing-only signals such as IRIG may require a separate estimator; survey them without pretending an unsuitable narrow-carrier model has decoded their timing.

Exit: tracking accuracy on known-motion controls, explicit support intervals on real data, and contamination failures distinguishable from genuine motion hypotheses. A support score alone does not qualify correction.

### Stage 3 — Qualify correction, including same-channel carriers

Remove the architectural requirement that a direct reference must come from another channel. A carrier on the target can be eligible if independent response evidence demonstrates benefit. Prevent leakage: another extraction of the same carrier, its sidebands, shared feed copies, or margins of the same event are not independent validators.

For cross-channel transfer, test target-specific prediction. Do not infer shared tape motion from channel adjacency, filename alignment, circuit labels, or waveform correlation. Any estimated delay, gain, or mapping parameter belongs to calibration and must be frozen before validation.

Use check beeps where they provide independent response evidence. For channels without them, keep discovery running and label promising candidates `tracked_unvalidated`. Research another validator only as a separate hypothesis with its own controls. Do not turn agreement of two dependent tones into invented ground truth.

Run expensive calibration in declared batches across the candidate queue. Measure how the best calibration result changes as budgets increase, such as 8, 16, and 32 references, until the declared candidate set has been evaluated or remaining work is explicitly deferred. Proxy losses prioritize computation; they are not final rendered qualification. Select the extraction/source/model on calibration only, freeze one choice per target/interval, then validate it once.

If a frozen choice fails validation, preserve that failure. Use fresh evidence for another attempt. Do not cycle through candidates against the same validation outcomes or select a correction on reserved final results.

Exit: independent predictive improvement with unchanged controls; source/extractor/capture/model identity recorded; all failed or untested candidates accounted for. Keep partial improvement separate from full restoration.

### Stage 4 — Test lifetime, switching, and the exact joined map

Increase passages from 180 seconds to 10–20 minutes, then test one complete development tape. Require passing bounded tests before this increase.

Exercise acquisition after silence, voice activity changes, competing tones, fades, support loss, long gaps, deterioration, and reference changes. Test sparse and dense check-event regions. A working reference should not restart at each I/O boundary; a failed reference must not continue across unsupported audio.

Evaluate the exact final map after support fades, integration, switching, and duration anchoring. Check finiteness, strictly increasing positions, endpoint behavior, the existing speed bound, gap accounting, and chunk/resume invariance. An invalid map stays a failure; never clip, refit, or replace it silently after seeing final measurements.

Measure independent audio events near transitions where available. Structural checks alone do not prove audible or scientific correctness. Review physical support and independent validation support separately. Preserve identity through unsupported intervals and assess no-harm behavior there.

Exit: no invalid maps or silent support bridges in the predeclared promotion set; dense transition evidence where available; unresolved evidence gaps reported. Passing a structural test is not a listening approval.

### Stage 5 — Expand across tapes and freeze an archive recommendation

Expand by deterministic batches that include different mission times, heads, tape positions, and signal/activity conditions. Revisit neither only the easy tapes nor only failed examples. Discovery should eventually cover every available source/time interval, including explicit missing/unavailable/error cells.

Do not run the full expensive candidate search across the archive after every local change. Reuse source-bound feature caches only when their defining code/settings are unchanged. Keep new scientific output separate from historical v5 bundles.

After the method is frozen, run the predeclared reserved audit, with exposure limitations attached. Produce per-tape recommendations distinguishing locally supported correction, promising but unvalidated carriers, known failure, and unfinished search. A full-archive run can finish with low validated coverage; report what has been tested instead of weakening evidence requirements.

## 7. Default test matrix and metrics

### 7.1 Controlled tests

Create seeded synthetic tests with ground truth. Randomize frequencies throughout the allowed band; do not test only 59/118 Hz, 1 kHz, or a known successful carrier. Reserve different random draws for development, promotion, and final synthetic audit.

Include at least these conditions:

| Axis | Required cases |
| --- | --- |
| Carrier type | Pure, broadened, drifting, multiple nearby lines; continuous and interrupted |
| Context | Quiet background, active speech, changing speech activity, several unrelated channels |
| Motion | Zero, multiple nonidentical known timewarps, changing rate/amplitude, motion outside the present two trial bands |
| Amplitude | Strong/weak carriers, amplitude-only modulation, fades, dropouts, clipping |
| Contamination | Much louder unwarped 60 Hz harmonic comb; close valid carriers beside it; multiple capture competitors |
| Dependence | Same-channel independent carrier/response; duplicated feeds; same-frequency copies; wrong motion on an adjacent channel |
| Processing | Original, existing cleanup effects, cleaned-only feature visibility, feature suppression by cleanup |
| Boundaries | Short/fractional tails, missing data, filter settling, context boundaries, reference changes, interrupted resume |

For injection into real recordings, label it an injection test: the real background has unknown pre-existing motion. Keep the synthetic foreground/response truth separately measurable and preserve an uninjected comparison. Inject historical carriers before the known warp and digitization hum after it; do not accidentally warp the contamination. Ensure the warp convention is explicit and tested. Use pure synthetic cases to establish ground truth that real backgrounds cannot provide.

Default engineering promotion targets below are provisional research targets, **not new production acceptance thresholds**. Freeze the exact domain, amplitudes, tolerances, confidence intervals, and sample counts before running the gate:

- Easy synthetic discovery tier: at least 95% carrier-interval recall for isolated carriers at least 12 dB above the declared local background, supported for at least ten seconds, outside contaminated/withheld bands. Score frequency/time matching with predeclared tolerances tied to resolution and true linewidth. Report precision and support error too.
- Harder tiers: report complete recall/precision curves versus level, duration, drift, spacing, speech activity, and contamination strength. Do not hide hard cases in an aggregate or lower their difficulty after a failure.
- Easy known-motion tracking tier: absolute correlation at least 0.9 **with correct sign**, and motion amplitude gain between 0.8 and 1.2, after predeclared settling/support exclusions. Report phase/timing error as well; correlation alone can reward a tiny or wrongly scaled estimate.
- Negative controls: zero accepted correction in the declared pure unwarped/AM-only/hum-only controls. Report the count and statistical limits; zero observations does not prove a universally zero error rate.
- Legacy and new tests: no broken source/checkpoint invariants, no invalid map promotion, and no unreported negative-control failures. Add tests for meaningful scientific failure modes, not tests that simply mirror implementation.

If a target proves unrealistic, retain the failed experiment and document why. Revise the target only as a new protocol generation before new evidence is evaluated; never relabel an existing failure as a pass.

### 7.2 Preserve the current correction evidence contract

Read actual constants/functions and save their hashes at campaign start; documentation may lag code. As of this runbook, current planning eligibility uses median event residual ratio <=0.5 and every margin <=0.8, with meaningful baseline, independent events, continuous support, and negative controls. The additional restoration label requires residual <=6 Hz and <=5 cents. Baseline is at least 1 Hz; support is at least 0.95; current minimum is two calibration and two validation events separated by at least three seconds. Three margins on one event do not satisfy event independence.

Preserve identity/no-motion, wrong-sign, amplitude-derived, and wrong-time controls. The current control margin is 0.10, with at least two losing wrong-time controls from the frozen shifts. Record actual predicates, not only a boolean. Do not substitute `flutter_policy.numeric_pass` for `classify_numeric_quality` blindly: their ratio thresholds and classification roles differ.

The joined assessment requires at least two sufficiently separated reserved events fully inside a proposed span for event-supported improvement time. Counts of those span seconds do not establish restoration at every instant. Existing numerical contracts are research evidence, not automatic production or perceptual approval.

### 7.3 Report a funnel, not just coverage

For each tape, channel, source version, and declared frequency/time region, record:

```text
requested -> available -> analyzed -> spectral hypotheses -> trackable intervals
          -> calibration attempted -> validated -> exact-map supported
          -> listening reviewed -> production accepted
```

Keep these statuses distinct:

```text
missing_source / unavailable / read_error / insufficient_context
not_searched / response_band_withheld / contaminated / budget_deferred
spectral_hypothesis / tracking_failed / tracked_unvalidated
calibration_failed / validation_failed / supported_partial / restoration_at_events
invalid_map / worsening / listening_pending / listening_rejected
```

Also report detector false positives per source-time, localization error, phase slips, support duration, untried queue size, independent event counts, median and worst residuals, worsening events, transition results, raw and dependence-adjusted sample counts, wall time, memory, bytes read/written, and failures/retries.

Compare baseline/challenger on the **same independent events and available source-time**, including bypasses and failures. A challenger cannot win by omitting difficult events or changing the denominator. Report both equal-tape macro summaries and source-time weighted totals so one long/easy tape cannot dominate. Keep worst slices visible and avoid treating copies as statistical replication.

## 8. Concrete iteration schedule

Use this order unless evidence identifies an earlier blocking defect. Record any reordering and its reason.

| Iteration | Principal question | Default bounded work | Decision |
| --- | --- | --- | --- |
| I000 | Can we reproduce and measure the baseline? | Existing audit sample plus one 180-second benchmark | Fix measurement/storage defects before science changes |
| I001 | Does pre-allocation hum exclusion recover valid candidates? | Controlled close-tone/hum tests, then six development passages across three tapes/all channels | Keep only if recall improves without contamination/control regression |
| I002 | Do multiple resolutions and ridges improve detection? | Same paired development set; one component at a time | Reject extra-peak inflation without improved precision/trackability |
| I003 | Does dual-source discovery improve the usable catalog? | Original-only, denoised-only, union; identical coordinates and matching rules | Preserve complementary valid detections; no denoised-survival requirement |
| I004 | Can same-channel references qualify safely? | Controlled independent carrier/response tests, then development channels with separate check events | Reject leakage/self-validation; preserve useful direct references |
| I005 | Is qualification compute the main remaining bottleneck? | Fixed candidates, increasing calibration budgets; no repeated validation | Measure budget saturation and unfinished work |
| I006 | Can motion extraction handle different carriers? | Capture/source/model ablations with frozen candidate catalog | Keep only independently supported motion fidelity improvements |
| I007 | Does the method generalize? | Additional predeclared batches across the archive | Run one consumed promotion gate per frozen generation |
| I008 | Does it remain safe through transitions and long runs? | 10–20 minute passages, then one development tape | Exact joined-map and resume tests; preserve all failures |
| I009 | What can we recommend for the archive? | Frozen method, reserved audit and full discovery inventory | Per-tape evidence and limits; no automatic production rendering |

The first six real passages should each be 180 seconds, two per selected development tape, across all available channels and source versions. Choose tapes/starts from the split manifest with the fixed seed. Do not choose them because the old assessment shows success. Include at least one quiet/low-activity passage after an outcome-independent activity classifier is frozen; keep sampling metadata explicit.

At each iteration:

1. Read the current decision/state; identify whether discovery, tracking, qualification, validation availability, map construction, or compute is limiting progress.
2. Write `experiment.json` **before running**. State one principal hypothesis, fixed data, baseline/challenger, acceptance conditions, budgets, and what would disprove it.
3. Make the smallest meaningful code/config change. Snapshot exact implementation and source identities. Run relevant tests, then the bounded paired experiment.
4. Inspect aggregate and worst-case evidence, failures, source-version differences, and a fixed random sample of apparent successes. Verify there is no leakage, altered denominator, or silently missing work.
5. Write `decision.md`: advance, reject, or inconclusive; give numerical evidence and a causal explanation supported by diagnostics. An inconclusive result needs a specific missing measurement, not another unbounded search.
6. Advance only a passing frozen challenger to a fresh promotion gate. A failed gate consumes its evidence. Keep the previous champion, and preserve the challenger/failure.
7. Update the ledger and `NEXT.md`, then execute the next justified bounded step. Report material progress, failures, or a real need for input; do not repeatedly ask the user to approve routine research choices.

After three inconclusive iterations on the same hypothesis, synthesize the failure mechanisms and change the hypothesis or measurement design. Do not keep increasing the same cap or threshold indefinitely. If promotion evidence is unavailable, continue independent development/diagnostics while clearly withholding promotion claims.

## 9. Compute, checkpoints, and practical commands

### 9.1 Resource policy

Start with one worker and one native numerical thread per worker. Benchmark two workers before increasing concurrency. Initial maximum is four workers; use fewer when disk throughput, memory, or other user workloads suffer. This is a default resource budget, not a scientific assumption.

Target individual jobs under 30 minutes until cost is characterized. Decompose larger work into resumable passage/channel batches. Check free space and predicted output size before expansion. Process one complete development tape before any whole-archive expensive planning. Expand archive discovery in small logged batches only after bounded gates pass.

Commit checkpoints atomically with cursor, code/settings/source identity, artifact offsets, and hashes. Resume only a compatible checkpoint. Source or scientific changes require a fresh generation. Never truncate a committed stream to make a checksum pass. Preserve interrupted/uncommitted artifacts until their status is understood.

Logs should show actual work: source-time completed, catalog/queue counts, current stage, budget used, and last committed checkpoint. A quiet log alone does not prove a stalled process.

### 9.2 Existing commands that may be used now

Run from `F:\_repos\Apollo_15`. Read current `--help` before use; stop if the CLI differs materially. Commands below are real existing entry points, not a proposed new framework.

Read-only reproduction of the development audit (writes only its separate audit reports):

```powershell
.venv/Scripts/python.exe research/v5_discovery_audit.py
.venv/Scripts/python.exe research/v5_cleaned_discovery_audit.py
```

Relevant regression suite:

```powershell
uv run --with pytest python -m pytest tests/test_v5_allchannel.py tests/test_v5_resume.py tests/test_v5_safe_assessment.py tests/test_continuous_acquisition.py tests/test_continuous_evidence.py tests/test_continuous_planning.py tests/test_continuous_checkpoint.py tests/test_continuous_quality.py -q
```

A bounded **development-only baseline** example, using a fresh destination:

```powershell
uv run python scripts/05_deflutter/05a_plan_deflutter_v5.py --input D:/Apollo_15_30-Track/orig_concatenated/A15_T510073 --start 10920 --duration 180 --denoised-root D:/Apollo_15_30-Track/denoised --output reports/deflutter_iterative_20261030/baseline/dev_T510073_10920_180
```

That passage is a known historical development example, not the campaign's unbiased cross-tape sample. This command automatically evaluates reserved events after planning; **do not use it on sealed promotion/audit passages**. It is the old baseline, not an implementation of this proposed methodology. Always specify `--duration` for a bounded run. `--max-windows` limits planning after the full requested survey; it does not bound initial survey cost. Reusing a compatible interrupted destination requires explicit `--resume`.

Coverage accounting for that example:

```powershell
uv run python research/v5_coverage_audit.py reports/deflutter_iterative_20261030/baseline/dev_T510073_10920_180 reports/deflutter_iterative_20261030/baseline/dev_T510073_10920_180/coverage_audit.json
```

Exact joined-map checks, which **consume saved reserved-event evidence**:

```powershell
uv run python research/v5_evaluate_joined.py reports/deflutter_iterative_20261030/baseline/dev_T510073_10920_180/plan/plan.json reports/deflutter_iterative_20261030/baseline/dev_T510073_10920_180/joined_checks.json
```

For new methods, build the discovery-only catalog runner, resumable calibration queue, evaluator, and report adapters as needed. Their commands do not exist yet; implement/test them and record their actual invocations. Do not invent flags or assume the old planner already provides same-channel qualification, ridge discovery, or cleaned-sample extraction.

### 9.3 Rendering and listening

Produce matched excerpts from the same existing denoised input, applying identity, frozen baseline map, and frozen challenger map. Use one common gain; no separate normalization or cleanup between alternatives. Record source/map/output hashes, exact coordinates, extraction version, and gain.

Evaluate the full intended map first, then crop excerpts. Do not recompute duration anchoring separately for a short listening excerpt and claim it demonstrates the saved full map. Include transitions, dropouts, sparse-evidence passages, apparent wins, failures, and a fixed random sample, not only the best examples.

`scripts/05_deflutter/render_channel_review.py` exists, but bounded rendering selects a bounded denoised cache path and can invoke cleanup when it is absent. Before using it, inspect its behavior and adapt a separate research renderer to crop the already-cached full-channel denoised audio without re-denoising. A dry run must confirm the exact source and saved map. Do not use experimental-transfer flags to silently apply failed or unsupported decisions.

Create blinded A/B labels with a stored mapping and randomized presentation. Queue short excerpts with concrete questions about pitch stability, warble, transients, speech clarity, and new artifacts. If no listener is available, mark `listening_pending` and continue other work. Numerical results and visual inspection are not human listening approval.

## 10. Required reports and completion conditions

Each iteration report should fit this structure:

```markdown
# I00N — hypothesis

Decision: advance / reject / inconclusive
Compared implementations and hashes:
Data and exposure roles:
One intended change:
Metrics and worst slices:
Detection versus tracking versus correction findings:
Negative controls, dependencies, failures, and unfinished work:
Resource cost:
Evidence links:
Next bounded experiment and reason:
```

At campaign completion, deliver:

1. A reproducible general method and documented commands, with source/version identities and meaningful tests.
2. An archive coverage matrix showing analyzed, missing, errored, withheld, and unfinished intervals; no claim that file reads establish tone recall.
3. Discovery recall/precision and known-motion fidelity results, including hard cases and negative controls.
4. Paired per-tape comparisons against the frozen baseline on unchanged denominators, with independent-event and exposure qualifications.
5. A candidate/decision catalog distinguishing original-only, cleaned-only, and shared discoveries; same-channel versus transferred corrections; and budget-deferred work.
6. Exact joined-map results, transition/support/resume checks, and matched listening artifacts with real review status.
7. A recommendation for each tape: supported local correction, promising unvalidated evidence, known failure, or incomplete investigation. List the dominant remaining limitation for each.
8. A final `NEXT.md` describing any unfinished independent work and what specific evidence or user action is needed.

Research completion does not require claiming full restoration. Do not call the campaign complete merely because its compute budget ends. Leave accurate checkpoints and outstanding work. If genuinely new independent evidence or human listening is the only remaining requirement, state that precisely without blocking unrelated completed analysis.

## 11. Kickoff instruction to give Dot

> Read `F:\_repos\Apollo_15\docs\deflutter_iterative_research_runbook.md` and execute the research campaign it describes. Start by verifying repository instructions, existing work, source identities, prior exposure, and the frozen baseline. Create the campaign state and data split, then carry out I000 and proceed through justified bounded iterations. Preserve original and denoised discovery, exclude digitization contamination from candidate allocation, and pursue a general detector rather than a GOSS-specific solution. Keep detailed evidence and resume instructions. Do not overwrite completed plans/audio, reuse inspected evidence as untouched validation, relax thresholds for coverage, or launch production correction. Continue autonomous research and report material findings and actual blockers.
