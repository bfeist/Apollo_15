# Version 5: continuous all-channel deflutter planning

The new methodology is `continuous-allchannel-v5`. It is a research planner,
with reproducible bounded audio evaluation. Batch planning creates plans and
assessments; it does not render corrected WAVs. The stopped v4 plan and earlier `continuous-carrier-v5`
experiments remain historical artifacts. Iterations remain version 5.

The [October 30 investigation](deflutter_research_findings.md) found that
response-based numerical gains do not authenticate tape transport correction.
No experimental method qualified for production. The operating instructions
below describe the preserved v5 planner; they do not supersede those findings.

## Operation and compatibility

Run all tapes with four independent planner processes (set the worker count to
1 for sequential processing):

```powershell
uv run python scripts/pipeline.py run --stages 05a --all-tapes --flutter-planner v5 --flutter-plan-workers 4 --plain
```

The batch uses sorted `A15_T*` directories under `--concatenated` and existing
stage-03 files under `--denoised-root`. The default v5 output root is
`D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel`; override it with
`--planning-root`. A run record and per-tape logs are saved under `runs/` (or
`--output`). Failures are recorded, other tapes continue, and the batch exits
nonzero if any tape fails. Add `--flutter-plan-stop-on-error` to stop starting
new tapes after a failure; already running tapes finish. Replace `run` with
`plan` to preview commands.

For a restart, repeat the command with `--flutter-plan-resume`. Existing tape
destinations are passed to the v5 planner for compatibility checking and
checkpoint resumption; tapes without a destination start fresh. Survey coverage
and candidate details are committed after each complete monitor window using
`survey_checkpoint.json` and `survey_windows.jsonl`. At most the current
uncommitted window is repeated after interruption. Completed plans are checked
again and their matching assessments are reused. Add
`--exclude-tape A15_T510073` (repeat for other tapes) to leave an active or
deferred tape out of an all-tape batch. The legacy planner
remains the default unless `--flutter-planner v5` is specified.

Before a planned reboot, stop the runner with Ctrl+C and wait for its workers
to exit. After reboot, resume with any positive worker count:

```powershell
uv run python scripts/pipeline.py run --stages 05a --all-tapes --flutter-planner v5 --flutter-plan-resume --flutter-plan-workers 4 --plain
```

The October 28 transition also saved live candidate details for tapes 510100
and 510101 and imported them into this checkpoint format. Older interrupted
surveys with neither a checkpoint nor a live capture still need a fresh
destination. The survey storage wrapper calls the unchanged scientific engine;
existing planning implementation signatures remain compatible. Committed
stream prefixes are verified before uncommitted planning tails are removed.

Assessment also isolates a duration-anchoring speed-bound failure to its
channel. The saved report marks that channel `invalid_map`, labels its proposed
spans `rejected_invalid_map`, and recommends leaving it uncorrected. Other
channels finish assessment. The failed map is retained for diagnosis, with no
clipping, changed limit, or refitting on reserved measurements. Reports bind
the separate assessment implementation hash; the frozen planning engine keeps
existing survey and planning checkpoints compatible. Tape 510080's CH22 is
the first recorded instance of this rejection.

For a single bounded trial:

```powershell
uv run python scripts/05a_plan_deflutter_v5.py --input D:/Apollo_15_30-Track/orig_concatenated/A15_T510073 --start 10920 --duration 240 --output reports/my_fresh_v5_trial
```

The default destination root is
`D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel`. The bundle contains
`survey_manifest.json`, per-channel-second `survey.jsonl`, saved maps under
`plan/`, and `joined_assessment.json` after completion. For saved-map review,
pass the nested `plan/plan.json`, not the survey bundle. Existing stage-03
denoised files are reused; no denoising rerun is needed. `--denoised-root` names
the parent of the tape directories. `--original-only` disables supplementary
denoised discovery.

Fresh runs refuse occupied destinations. `--resume` continues a compatible
survey or planner checkpoint. Sources, settings,
implementation, policy, and survey hashes must match. The decoded excerpts
actually read from original and denoised audio have saved SHA-256 identities;
file metadata and the stage-03 source signature establish alignment. Full
denoised-file decoded hashes are not recomputed for bounded research.
Survey checkpoint offsets and hashes are checked before uncommitted file tails
are discarded. Coverage, candidates, and the cursor commit together.
`--max-windows` limits planning *after surveying the entire requested interval*.
Always specify `--duration` for a bounded experiment.

The legacy `05a_plan_deflutter.py` remains separate. Batch mode does not
silently adopt the new planner or resume v4 as v5. The existing production
acceptance contract rejects this new methodology until it is separately qualified.

## Evidence survey

Every available numbered source, including IRIG, is read in every requested
window independently of target check-tone counts, qualified donors, or previous
correction success. Timestamped unassigned recordings are also examined and
identified separately. Missing channel numbers, unavailable time, short context,
read errors, and unprocessed intervals are explicit. A failed inventory records
an error manifest and stops; it does not claim successful coverage. Unassigned
sources are surveyed but cannot yet serve as numbered correction donors.

Default core windows are 60 seconds, with four seconds of context on both sides
where available; short boundary windows expand to at least ten seconds when
the source permits. Each fractional or whole second gets an exact interval record;
the spectral and motion analysis uses the surrounding context, not independent
one-second snippets. Candidate support means the estimator has usable samples;
it does not mean correction is accurate. The independent coverage auditor checks
that the records partition each requested channel timeline without gaps or
overlap. That audit is distinct from scientific validation of the extraction.

Discovery searches original audio and, when present and aligned, already-denoised
audio. Their candidate lists are combined with provenance recorded. Tracking and
target-event measurements use original samples. Cleanup can reveal weak spectral
features, but also removes useful predictors: in the development CH03 passage,
the 117.5 Hz bin was suppressed by roughly 101–134 dB. A candidate found only in
denoised audio cannot qualify without motion evidence in the original.

The project owner identified exact 60 Hz harmonics as digitization noise, not
historical tape content. Candidate centers within 0.5 Hz of that comb are
excluded. This is one discovery-bin tolerance, not a physical boundary between
tape content and interference. Nearby carriers remain explicitly ambiguous and
require predictive tests. The denoiser's much wider notch bands are not used as
a blanket exclusion, since they remove nearby moving components too.

Discovery is bounded to eight spectral hypotheses per source version per channel
per context by default. Candidate cap and expensive-reference budget are explicit;
a budget failure does not establish that the recording has no timing information.
The survey examines every available interval, but cannot claim to detect every
possible useful signal. Physical adjacency and authentic head offsets remain
unknown. Channel numbering and filename alignment are not evidence of shared
playback motion.

The delivered T510073 channel-assignment photograph confirms CH01 GMT IRIG B,
console/feed names and several left/right position pairs. It describes routing,
not head-gap geometry. The reel label and digitization note also distinguish
the historical start/stop labels from the longer digitized duration. See
`reports/research_v5_allchannel_20261027/evidence/media_labels.md` for direct
transcriptions and provenance.

## Selection, monitoring, and independent evaluation

An immutable C,C,V,V,F cycle assigns detected events to calibration, validation,
and reserved final roles before predictive scoring. V5 collects all channels'
events before assigning roles. Detections within 0.5 seconds are conservatively
grouped as potentially shared events and receive the same role across channels,
even if a detector missed earlier copies. The global role index and recent
group anchors are checkpointed. Late detections bridging conflicting frozen
roles are excluded and counted. Each channel still requires separate events at
least three seconds apart. Coincidence does not prove common origin, and copies
delayed beyond the grouping tolerance remain an unresolved risk. Margins of one
event are not separate observations, and channel copies are never pooled as
independent votes. Survey features are unlabeled predictors;
the target response band, 2150–2850 Hz, is excluded from reference discovery.
Only completed survey contexts can supply candidates to a choice. Reference
motion must still be measured during the passage being corrected.

Calibration proxy scores allocate a finite expensive-reference budget. The final
choice is the smallest measured calibration residual among shortlisted variants,
without a spectral-strength preference between comparable losses. That one choice
is validated once against identity, wrong sign, amplitude-derived, and wrong-time
controls. Validation failures do not trigger retries on the same events. These
tests establish target-specific predictive evidence, not historical oscillator
provenance. Shared feeds, cross-talk, common oscillators, and overlapping capture
bands remain possible dependence. The old rank-selected transfer-group path is
disabled; no channel inherits a correction solely from another channel's success.

The existing relative-improvement gates remain: median validation ratio at most
0.5, every margin at most 0.8, meaningful baseline, independent temporal events,
continuous support, and controls. The desired absolute target, at most 6 Hz and
5 cents, is an additional restoration label. Useful partial reduction need not
meet that absolute target. No threshold was relaxed to raise coverage.

References are monitored, ended at support loss or predictive deterioration,
and reacquired on fresh evidence. Unsupported gaps stay uncorrected. Validated
references can switch; no ensemble fusion is claimed. Speech-only speed estimates
remain unidentifiable without additional stable evidence and are not used as
validated correction. Wider capture near 13,158 seconds recovers more samples,
but cannot by itself validate the post-loss motion.

Earlier estimator research tested convex two-reference blends, recentering,
temporal shifts, gain adjustments and alternative modulation bands. Blending
gave a modest development improvement without fresh qualification; its inputs
also need independence and support checks. V5 retains tested switching rather
than introducing an unqualified ensemble. That research remains under
`reports/research_continuous_20261026/estimator`.

The exact saved joined map is evaluated after support fades, switching, and
duration anchoring. Final results never refit a failed map. Each proposed span
needs at least two separated reserved events fully inside it for the report's
event-supported span count. Identity with no measurements is insufficient
evidence, not zero flutter. Results distinguish partial improvement, restoration
at measured events, worsening, no meaningful benefit, and insufficient evidence.
Structural transition checks do not substitute for independent audio measurements
or listening. Counts of span seconds describe the support around measured events;
they do not claim every instant was independently measured as improved.

## Reproduction and remaining work

The fresh research bundle is `reports/research_v5_allchannel_20261027`.
`evidence/` contains all-tape metadata inventories, bounded all-channel spectra,
motion comparisons, original/denoised comparisons, and reproducible findings.
`evaluation/manifest.json` declares passage roles. Superseded runs are retained
and labelled; the two first reserved passages were retired after an external
policy clarification, before any resulting outcomes were used for tuning.
A subsequent audit found that per-channel role cycles could put duplicated
responses into different roles across channels. Those evaluation results were
also retained but superseded. The synchronized-role implementation uses another
predeclared untouched final pair, evaluated once after its implementation freeze.
These changes address supplied provenance and evaluation isolation; no numerical
acceptance threshold was tuned on final outcomes. Frozen source files and hashes
are saved with each evaluation generation.

Useful commands:

```powershell
uv run --with pytest python -m pytest tests -q
uv run python research/v5_coverage_audit.py reports/my_fresh_v5_trial reports/my_fresh_v5_trial/coverage_audit.json
uv run python research/v5_evaluate_joined.py reports/my_fresh_v5_trial/plan/plan.json reports/my_fresh_v5_trial/joined_checks.json
```

Synthetic cases exercise rendered known-motion reduction, distinct adjacent
motion, a misleading strong common line, drops, reacquisition, switching,
support-faded joined maps, missing data, exact timeline partitions, and checkpoint
integrity. Real-audio results and the all-tape recommendation are recorded in
`reports/research_v5_allchannel_20261027/findings.md`. No listening judgment is
claimed; the preserved matched excerpts are for subsequent listening.

The remaining research priorities are independently attributable historical
carriers, useful evidence outside conventional check tones, IRIG-specific timing
extraction, detector recall/false-positive measurement, dense validation through
transitions, and economical surveys. Survey candidates are now journaled per
window alongside resumable coverage; the full manifest is assembled at the end.
Further work includes reducing the planning manifest's memory footprint.
