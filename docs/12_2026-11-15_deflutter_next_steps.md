# Deflutter detection census: what to do next

Written November 15, 2026, right after the stage-05 detection census was built.
How the system works and its commands are in
[10_2026-11-15_deflutter_detection.md](10_2026-11-15_deflutter_detection.md); the design and research
evidence are in [09_2026-11-15_deflutter_detection_guide.md](09_2026-11-15_deflutter_detection_guide.md).

## November 17 direction: archive-scale qualification

The owner rejected continued hand-picked listening trials as an approach to
an archive this large. The six November 17 excerpts remain diagnostic evidence;
they are not a representative quality estimate or a checklist to extend across
channels. Do not require individual-channel or individual-window listening
approval before making progress.

The workflow is:

1. While denoising runs, prepare automated audit aggregation and a fixed QA
   sampling protocol. Do not benchmark against a busy denoise workload.
2. After denoising, benchmark the worker pool and run the census over every
   available channel of every eligible tape. Reuse verified results on resume.
3. Aggregate coverage, bypasses, cross-channel agreement, sign conflicts and
   held-out audit outcomes over the entire archive. Report tape-time unions,
   distributions and outlier counts, including unmeasured intervals. Repeated
   copies of a beep are not independent evidence.
4. Validate a general rule for handling contradictory evidence against the
   saved regression corpus before changing application gates. Do not hard-code
   a T510073 exception, flip signs per channel, or treat missing check beeps as
   rejection. The existing detector already gates consensus eligibility;
   adverse beep audits currently flag problems but do not veto application.
   Automated handling of those contradictions remains development work.
5. Apply one accepted shared map per tape to all its output channels. Unsupported
   intervals bypass with smooth transitions. Do not force coverage to 100%.
6. Use one bounded listening batch to check the resulting method. Freeze its
   selection before listening: stratified random intervals across tapes,
   coverage/quality bands, and speech-bearing channels, with a separately
   identified diagnostic stratum for flagged cases. Preserve the seed and
   selection manifest, count speech-free selections separately, and report
   failures as well as improvements. Do not select only promising examples.

The owner explicitly supports detecting the entire archive first, then
performing further analysis. Detection is evidence collection, not authorization
to apply every eligible curve. An unresolved scientific outlier must not block
that collection pass. The next engineering task is automated archive reporting
and follow-up analysis, not more manually selected clips. The concrete design
is in section 4 below. It does not itself change detector code or establish
that the current detector is sufficiently reliable.

## Where things stand

Done:

- `scripts/05_deflutter/05_detect_flutter.py` (stage 05) with the numerical
  core `flutter_census.py` and the run/cluster/publication layer
  `flutter_detection.py`. One Proprius coordinator, persistent Proprius and
  Genus workers, verified inbox publication, acknowledgements, resume and
  graceful stop.
- Stage 05a `--detection-root` (census-only or merged with saved regional/v5
  evidence, priority 2), census provenance in `plan.json`, pipeline stage `05`
  and `--flutter-detection-root` / `--flutter-detect-*` options, readiness
  reporting.
- Synthetic test coverage (`tests/test_census_*.py`) and a real-data
  regression of the production code on the saved research windows.
- The Genus SSH worker path was started once and passed its startup identity
  check: code hashes, numerical versions, single-thread limits and CPU mask.
- Removed: the v5 survey/planning engine, per-channel map evaluation, the
  narrow-peak `discover`, the October 30 campaign code and their tests and
  documents (all in Git history).

Not done, because the denoise rollout is running:

- the worker-count benchmark and its sustained mixed-host trial;
- any production census run on the archive (`deflutter_detection_census/` does
  not exist yet);
- census-based maps, stage-05b audio and listening comparisons.

Regression results with the production code:

| Check (guide section 6) | Requirement | Result |
| --- | --- | --- |
| Confirmation windows, beep median ratio | ≤ 0.26 | **0.181** (661 beeps, 50.9 → 9.8 Hz) |
| Confirmation windows, beeps made worse | ≤ 1 % | **0.3 %** |
| Wrong-sign / 1.5 s shift controls | worse than identity / no benefit | 100.4 Hz / 61.9 Hz vs 50.9 Hz |
| sweep3 windows eligible (228, 38 tapes) | ≥ 85 % | **211/228 = 92.5 %** (research strict rule 88.2 %) |
| v5 consistency at T510083 14034 | r ≥ 0.8 at lag 0; ratio ≤ v5 + 0.05 | r 0.97; 0.20 vs v5 0.28–0.32 |

On sweep3, 1,322 beeps in eligible or audited windows gave 50.6 → 10.2 Hz
(median ratio 0.201) but 2.0 % were made worse. Most of those are one window,
**A15_T510073 36002**: a single 98-member hum cluster that matches the research
curve (r 0.98) while the beeps on CH21/CH22 got worse (ratio 1.86, wrong sign
better). Carry that unresolved window into the archive-wide follow-up analysis;
it does not block detection. Possible explanations include the beeps not
following the inferred tape motion or inverted motion in the hum estimate;
neither is established. Decide how contradictory audits affect application
after the census (today they never gate; `evidence.json` lists them as
`adverse_windows`).

Outputs are in the local `reports/flutter_census_20261115/p1_confirm_v2/`,
`p1_sweep3/` with `p1_sweep3_rerun/` (the 42 windows affected by the
sign-conflict rule, merged in `p1_sweep3_merged_summary.json`) and `p2_v5/`. These are numerical audits on sampled windows, not
listening approval, and no method yet meets the 6 Hz / 5 cent goal.

## 1. Review and commit

### November 17 preparation and listening update

The detector implementation is committed (`3518338`). The owner moved the old
deflutter planning/output directories into `D:/Apollo_15_30-Track/_to_delete`;
the active output roots are a clean slate. Do not assume the historical v5
root in the commands below is still available. Retain moved evidence and
previous reviews unchanged. Benchmarks remain deferred until denoising ends.

Six 64-second spot checks were rendered from completed v2 denoised channels
and saved production-regression curves, without new detection or benchmarking:
`reports/deflutter_spotchecks_20261117/`. Each hypothesis uses a native map
compiled once over the full tape scope, with support only in the selected
saved windows. Crosstalk was explicitly bypassed. A is the denoised source,
B adds the normal correction, and C (samples 01–03) reverses its sign for
diagnosis. These are sparse research maps, not completed archive censuses.

Owner feedback, bound to delivered audio/map hashes in that folder's
`listening_feedback.json`:

| Sample | Tape / channel / canonical start | Listening result |
| --- | --- | --- |
| 01 | T510073 / CH21 / 36002 | No clear improvement; tentative preference for C |
| 02 | T510073 / CH22 / 36002 | C best |
| 03 | T510073 / CH19 / 36002 | B best |
| 04 | T510106 / CH20 / 6102 | B better |
| 05 | T510083 / CH20 / 15385 | B better |
| 06 | T510106 / CH20 / 25683 | No speech, only line noise; B seems to have less flutter |

Samples 04 and 05 support audible improvement in those excerpts. Sample 06
does not establish speech improvement. T510073 remains unresolved: the
normal/reversed preference differs across channels at the same tape time.
The saved beep audit also favored the reversed sign on CH21/CH22, although
its median improvement over identity was small (52.3 to 47.5 Hz, versus
96.2 Hz for the normal curve). Neither channel is listed as an agreeing
hum witness in this saved window. This does not prove the hum is inverted
or justify channel-specific corrections. Keep this interval out of production
acceptance pending checks of independent witnesses and their timing.
No detector gate, shared-map policy, or archive approval changed.

Review the diff, run the tests, and commit before any production run: the run
ID hashes the bytes of `flutter_census.py`, `flutter_detection.py`,
`flutter_tracking.py` and `flutter.py`. **Any later edit to those files
creates a new run ID**; completed blocks of an older run stay valid there but
are not reused by the new one.

```powershell
uv run python -m pytest tests -q
uv run python scripts/run.py 05_detect_flutter.py run-id
```

## 2. Wait for the denoise rollout

Benchmark and run the census only after denoise has finished or has been
stopped at checkpoints from its console. During this session a 10-minute
block spent 120 s reading 30 channels from D: (versus ~100 s computing)
because denoise was loading the same disk; benchmarks taken now would measure
the contention, not the detector.

## 3. Worker-count benchmark (required before the archive run)

Use one fixed block set for every trial. A representative starting list is in
`research/flutter_census/benchmark_blocks.txt`: dense hum tapes, beep-rich
confirmation tapes, comb-dominated late T510084/T510087/T510112 blocks and
low-coverage T510106/T510081. Keep it unchanged across trials; add the final
short block of one tape if you want the tail case.

1. Check Genus topology first (`ssh bfeist@192.168.0.4 lscpu -e`). It is a
   16-core/32-thread Ryzen 9 5900XT with two L3 groups; logical CPUs 0–15 are
   normally one thread per physical core, with cores 0–7 and 8–15 on separate
   L3 caches. Record the masks you use, for example 1: `0`; 2: `0,8`;
   4: `0,2,8,10`; 6: `0,2,4,8,10,12`; 8: `0,2,4,6,8,10,12,14`;
   12: `0,1,2,3,4,5,8,9,10,11,12,13`.
2. Run each trial into its own name. Every trial needs the tapes of the block
   set (`--tape` repeated, or `--all-tapes`):

   ```powershell
   $tapes = '--tape','A15_T510077','--tape','A15_T510083','--tape','A15_T510106','--tape','A15_T510098','--tape','A15_T510090','--tape','A15_T510102','--tape','A15_T510084','--tape','A15_T510087','--tape','A15_T510112','--tape','A15_T510081','--tape','A15_T510110'
   uv run python scripts/run.py 05_detect_flutter.py benchmark --trial p4-g0-r1 --blocks @research/flutter_census/benchmark_blocks.txt @tapes --local-workers 4 --genus-workers 0
   uv run python scripts/run.py 05_detect_flutter.py benchmark --trial p0-g8-r1 --blocks @research/flutter_census/benchmark_blocks.txt @tapes --local-workers 0 --genus-workers 8 --genus-cpus 0,2,4,6,8,10,12,14
   ```

   Sweep Proprius `1, 2, 4, 6` and Genus `1, 2, 4, 6, 8, 12`, then promising
   mixed pairs and the `6 + 6` baseline, varying `--tapes` (2–4) to expose
   shared D:/network contention. Twelve blocks finish quickly at high worker
   counts; enlarge the list (same list for all trials) if a trial lasts under
   about ten minutes.
3. Repeat the best configurations at least three times in varied order. Note
   warm or cold cache (first read after reboot or other heavy I/O is cold),
   competing I/O, and Proprius responsiveness. Each `report.json` has wall
   time, unique tape-hours per wall-hour and per-block read, compute, CPU and
   upload seconds. Watch CPU/RAM and network in Task Manager and `htop`.
4. Check that hosts and pool sizes give the same science:

   ```powershell
   uv run python scripts/run.py 05_detect_flutter.py compare-trials p4-g0-r1 p0-g8-r1
   ```

   It requires identical gates and curve differences ≤ 1e-5.
5. Bind the choice (fastest stable setting within the six-worker Proprius cap;
   prefer fewer workers when rates are within variability):

   ```powershell
   uv run python scripts/run.py 05_detect_flutter.py select-pool --local-workers 6 --genus-workers 8 --genus-cpus 0,2,4,6,8,10,12,14 --tapes 3 --trial <best> --trial <repeat>
   ```

6. Sustained mixed-host trial: run real tapes for about an hour with the
   selected defaults. Its blocks are ordinary results and are reused later:

   ```powershell
   uv run python scripts/run.py 05_detect_flutter.py run --tape A15_T510106 --tape A15_T510077 --tape A15_T510083 --run-seconds 3600
   ```

   Save a short summary of the trials, selection and sustained rate under the
   run's `benchmarks/`, and replace the unverified 5–6 h projection in the
   operating guide with the measured rate.

## 4. Archive run

### Pass 1: collect evidence over the whole archive

After the worker benchmark, run the existing detector over all canonical
originals and all their available channels. Denoised audio is for downstream
rendering, not tone discovery. Preserve the census settings and code for the
duration of this pass. Do not wait for a solution to each flagged window.

```powershell
uv run python scripts/run.py 05_detect_flutter.py run --all-tapes --exclude-tape A15_T510096
uv run python scripts/run.py 05_detect_flutter.py status
```

A15_T510096 has 30 byte-identical channel files and is skipped automatically
anyway; ask for its re-delivery. Rerun the same command after any stop or
failure; it resumes from verified blocks. Failures stop scheduling and are
listed with their worker log under `cluster/logs/`. Afterwards check each
tape's saved results through the automated aggregation described below.
Low coverage is expected in late parts of T510084, T510087 and T510097 where
the hum is absent and only the post-tape comb remains; it must still be
measured rather than assumed.

### Pass 2: inventory and assess all saved results

Build a separate, versioned analysis bundle from the immutable census. Every
scheduled tape/block must be accounted for. Missing, stale, failed or excluded
inputs are distinct from analyzed intervals with no usable correction.

Produce one archive summary, one row per tape, a window index, and a queue of
follow-up intervals. Include:

- analyzed, candidate-supported, qualified and bypassed tape-time unions;
  qualified coverage is unknown until qualification has run;
- channel-group and frequency-family support, gate failures, clipping,
  sign-conflict flags, and agreement between overlapping window curves;
- distributions of held-out beep and concentration audits, their measured
  coverage, and identity/normal/reversed/time-shift control comparisons;
- uncertainty and audit availability, separately from measured failures.

Use the raw event records, not just the current aggregate beep counts. Collapse
the same physical event measured in overlapping windows and duplicate feeds
where correspondence is established; retain uncertain associations explicitly.
Report robustness by tape and interval rather than treating repeated beep
measurements as independent samples. Clip interval unions to the actual tape
duration and never add overlapping windows or channel-seconds as coverage.

Classify each interval as a candidate for qualification, needing further
analysis, or unsupported. These are analysis states, not render permissions.
Queue contradictions, near-threshold evidence, inconsistent overlap curves,
and supported/unsupported boundaries automatically. Add a reproducible random
sample of apparently good and unsupported intervals so systematic failures and
missed opportunities can be detected outside the flagged set.

### Pass 3: bounded follow-up analysis

Merge nearby queued intervals and process them with surrounding context, using
the existing original inventory and canonical offsets. Schedule each tape-time
region once for all channels. Save resumable jobs and results, with reasons and
hashes; do not repeatedly reread an entire tape for each output channel.

First reuse the saved per-window consensus curves, gates, leave-one-group-out
metrics and audit events. The current census does **not** retain every carrier
track or every competing cluster's curve. Where these are needed, reread only
the queued original regions and save the additional tracks and hypotheses in
the follow-up bundle. Never overwrite the initial census.

The follow-up worker should test:

- whether the selected motion is stable across overlapping estimates and
  independent groups/families, rather than supported mainly by one shared feed;
- whether competing signs or amplitudes are explained by carrier/mirror
  contamination, weak tracking, or an unreliable audit witness;
- whether usable independent tones above 200 Hz become more concentrated under
  correction than under identity and the controls; low-frequency hum
  concentration is not an independent validation method;
- whether audit-event timing and identity are sound. A time-shift or reversed
  control performing well is a diagnostic result, not permission to introduce
  a per-channel lag or sign;
- whether an apparently unsupported region contains additional usable
  evidence. Adjacent successful regions do not authorize filling a gap.

Prefer additional independent evidence over fitting a correction to whichever
beeps happen to be available. Any measurements used to select a new hypothesis
become development evidence and cannot also serve as its held-out validation.
Speech-free/noise-only excerpts are useful carrier diagnostics but do not
establish speech improvement.

Run one defined follow-up pass and summarize its yield and remaining uncertainty.
If a failure mode is widespread, investigate it as a class before rendering;
do not create a manual listening queue proportional to the archive. Local cases
still unresolved after that pass remain bypassed under the qualification policy.
Another analysis pass requires an explicit, testable hypothesis, not repeated
parameter searches until a favorable result appears.

### Qualification: decide what may be applied

Keep the decision policy separate from detection. Freeze thresholds, control
requirements and a deterministic development/validation split before evaluating
new policy variants. Split by tape where practical; keep neighboring windows
and copies of the same event in the same partition. The already inspected
November 15/17 examples are development/regression evidence, not a fresh
confirmation set. Report held-out degradation as well as median improvement
and coverage. Validate cross-channel transfer, gaps and join behavior.

Final decisions are **apply**, **bypass: unsupported**, or **bypass: unresolved
contradiction**, each with its evidence and policy version. A technical failure
or unfinished analysis remains incomplete, not silently converted to bypass.
Missing beeps alone do not prevent application of a qualified tape-level curve.
There is no per-output-channel eligibility gate and no global sign flip chosen
from a few listening preferences.

Resolve overlapping decisions on the canonical tape-time grid. If an unresolved
contradiction covers an interval, an overlapping passing window must not leak
correction back into it without an explicit qualified resolution. Select at most
one accepted hypothesis at each time, join once with smooth support fades, and
integrate/anchor once for the full tape. Apply that same inverse map to every
output channel while preserving offsets. Partial improvement is acceptable;
no requirement to achieve complete coverage or the unmet 6 Hz / 5 cent goal.

### Artifacts and implementation boundary

Proposed analysis location:
`deflutter_analysis/<census-run-id>/<analysis-id>/`. The analysis ID binds the
analysis code/settings, policy, sample seed and input manifest. Each artifact
binds its contributing census records, extra analysis, source inventory and
curve hashes. Store archive/tape summaries, the follow-up job list and results,
validation results, and tape-level qualification decisions separately.

Changing only the analysis or qualification policy creates a new analysis ID
and reuses the unchanged census. Changing one of the four hashed detector files
still creates a new census run ID under the current implementation. Preserve
all old evidence and approval-bound deliveries in either case.

**Implemented November 17** (operating guide:
[13_2026-11-17_deflutter_qualification.md](13_2026-11-17_deflutter_qualification.md)):
the archive aggregator (`05_analyze_flutter.py assess`), the follow-up
scheduler/worker (`followup`), the frozen qualification policy and held-out
validation (`qualify`, `validate`, `regression`), the frozen QA selection
(`qa-batch`, `qa-render`) and the stage-05a import of qualification decisions
(`--qualification-root`, pipeline `--flutter-qualification-root`). The former
05a `--detection-root` is now `--raw-census-root`; it compiles raw detector
candidates for research and labels them `qualified = false`. Populate the
analysis from the whole census when denoising and the detection run finish.
On the saved regression corpus the contradiction rule flags only
A15_T510073 36002, without tape-specific code.

## 5. Compile census maps

Compile qualified maps into fresh destinations bound to both IDs.
Unresolved-contradiction intervals bypass every source, including saved
regional/v5 evidence. Historical evidence, if reused, must be located and
validated explicitly after the owner's directory move.

```powershell
$id = '<run-id>'
$a = "D:/Apollo_15_30-Track/deflutter_analysis/$id/<analysis-id>"
uv run python scripts/pipeline.py run --stages 05a --all-tapes --exclude-tape A15_T510096 --flutter-qualification-root $a --flutter-planning-root D:/Apollo_15_30-Track/deflutter_planning_qualified/$id/<analysis-id> --plain
```

Each plan reports `tape_time_coverage` and `census_selected_seconds`; compare
with the saved v5 union (median 9.7 %).

## 6. Listening comparisons (Audition WAVs, no pages or servers)

Use the fixed representative QA batch described above for method qualification.
The two commands below are diagnostic examples only; neither they nor the six
November 17 excerpts should become the archive acceptance set.

Render matched A/B WAVs through the shared map only, into new folders so the
existing A/B/C files and approvals stay untouched. Stage-04 output for the
channel must exist (or add `--skip-crosstalk` for an explicit denoised-only
comparison):

```powershell
uv run python scripts/05_deflutter/render_channel_review.py --tape A15_T510106 --channel 20 --start 20000 --duration 1800 --flutter-plan D:/Apollo_15_30-Track/deflutter_planning_qualified/$id/<analysis-id>/A15_T510106/plan.json --output reports/listening_census_<date>/T510106_CH20
uv run python scripts/05_deflutter/render_channel_review.py --tape A15_T510083 --channel 20 --start 13200 --duration 1800 --flutter-plan D:/Apollo_15_30-Track/deflutter_planning_qualified/$id/<analysis-id>/A15_T510083/plan.json --output reports/listening_census_<date>/T510083_CH20
```

T510106 (0.6 % saved v5 coverage) tests new coverage; the T510083 passage
overlaps the approved `reports/listening_long_20261103/C.wav` and shows
whether the census changes approved spans (v5 keeps priority there) and how
it fills the gaps. Record the owner's decision with the WAV and map hashes.

## 7. After method qualification and the representative listening check

Run stage 04, then 05b into the census roots (batch mode takes one stage at a
time; a single `--channel` can run `04,05b` together):

```powershell
uv run python scripts/pipeline.py run --stages 04 --tape A15_T510106 --all-channels --plain
uv run python scripts/pipeline.py run --stages 05b --tape A15_T510106 --all-channels --flutter-planning-root D:/Apollo_15_30-Track/deflutter_planning_qualified/$id/<analysis-id> --defluttered-root D:/Apollo_15_30-Track/defluttered_qualified/$id/<analysis-id> --plain
```

Stage 06 retiming and stage 07 normalization then read `defluttered_qualified/$id/<analysis-id>`
through `--defluttered-root`.

## 8. Accuracy work still open

Treat each as development work, declare a fresh confirmation set before seeing
outcomes, and give each accepted change a new run ID (guide section 4):

- amplitude calibration by concentration-chosen α with ≥3 families;
- autofocus refinement with more lines, including full-rate hum harmonics
  above 1 kHz and CH30-type buzz series;
- phase-domain displacement estimation;
- an errors-in-variables gain gate (`tls_gain` is already recorded);
- a duplicate guard that does not group near-silent channels sharing only
  post-tape content;
- detector speed (Welch, stationary removal and FFT demodulation dominate the
  ~100 s per block) once the benchmark shows whether CPU or D: limits the run.
