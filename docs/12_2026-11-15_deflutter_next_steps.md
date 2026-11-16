# Deflutter detection census: what to do next

Written November 15, 2026, right after the stage-05 detection census was built.
How the system works and its commands are in
[10_2026-11-15_deflutter_detection.md](10_2026-11-15_deflutter_detection.md); the design and research
evidence are in [09_2026-11-15_deflutter_detection_guide.md](09_2026-11-15_deflutter_detection_guide.md).

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
better). Inspect that window before the archive run: either those beeps do not
follow this tape's motion, or the hum there carries inverted motion. Decide
whether adverse beep audits should ever veto a window (today they never gate;
`evidence.json` lists them as `adverse_windows`).

Outputs are in the local `reports/flutter_census_20261115/p1_confirm_v2/`,
`p1_sweep3/` with `p1_sweep3_rerun/` (the 42 windows affected by the
sign-conflict rule, merged in `p1_sweep3_merged_summary.json`) and `p2_v5/`. These are numerical audits on sampled windows, not
listening approval, and no method yet meets the 6 Hz / 5 cent goal.

## 1. Review and commit

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

1. Check Genus topology first (`ssh bfeist@192.168.0.3 lscpu -e`). It is a
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

```powershell
uv run python scripts/run.py 05_detect_flutter.py run --all-tapes --exclude-tape A15_T510096
uv run python scripts/run.py 05_detect_flutter.py status
```

A15_T510096 has 30 byte-identical channel files and is skipped automatically
anyway; ask for its re-delivery. Rerun the same command after any stop or
failure; it resumes from verified blocks. Failures stop scheduling and are
listed with their worker log under `cluster/logs/`. Afterwards check each
tape's `evidence.json` `coverage` and `beep_audit`, and look at tapes with low
coverage (expected: late parts of T510084, T510087, T510097 where the hum is
absent and only the post-tape comb remains).

## 5. Compile census maps

```powershell
$id = '<run-id>'
uv run python scripts/pipeline.py run --stages 05a --all-tapes --exclude-tape A15_T510096 --flutter-detection-root D:/Apollo_15_30-Track/deflutter_detection_census/$id --flutter-planning-root D:/Apollo_15_30-Track/deflutter_planning_census/$id --plain
```

Each plan reports `tape_time_coverage` and `census_selected_seconds`; compare
with the saved v5 union (median 9.7 %).

## 6. Listening comparisons (Audition WAVs, no pages or servers)

Render matched A/B WAVs through the shared map only, into new folders so the
existing A/B/C files and approvals stay untouched. Stage-04 output for the
channel must exist (or add `--skip-crosstalk` for an explicit denoised-only
comparison):

```powershell
uv run python scripts/05_deflutter/render_channel_review.py --tape A15_T510106 --channel 20 --start 20000 --duration 1800 --flutter-plan D:/Apollo_15_30-Track/deflutter_planning_census/$id/A15_T510106/plan.json --output reports/listening_census_<date>/T510106_CH20
uv run python scripts/05_deflutter/render_channel_review.py --tape A15_T510083 --channel 20 --start 13200 --duration 1800 --flutter-plan D:/Apollo_15_30-Track/deflutter_planning_census/$id/A15_T510083/plan.json --output reports/listening_census_<date>/T510083_CH20
```

T510106 (0.6 % saved v5 coverage) tests new coverage; the T510083 passage
overlaps the approved `reports/listening_long_20261103/C.wav` and shows
whether the census changes approved spans (v5 keeps priority there) and how
it fills the gaps. Record the owner's decision with the WAV and map hashes.

## 7. After listening approval

Run stage 04, then 05b into the census roots (batch mode takes one stage at a
time; a single `--channel` can run `04,05b` together):

```powershell
uv run python scripts/pipeline.py run --stages 04 --tape A15_T510106 --all-channels --plain
uv run python scripts/pipeline.py run --stages 05b --tape A15_T510106 --all-channels --flutter-planning-root D:/Apollo_15_30-Track/deflutter_planning_census/$id --defluttered-root D:/Apollo_15_30-Track/defluttered_census/$id --plain
```

Stage 06 retiming and stage 07 normalization then read `defluttered_census/$id`
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
