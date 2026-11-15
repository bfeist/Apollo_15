# Implementation guide: check-tone-free tape-motion detection

Status: research-qualified design (2026-11-15), **not yet implemented in
stage 05**. This document tells an implementing model what to build, why, how to
verify it, and what not to repeat. The evidence is in
`reports/flutter_census_20261115/REPORT.md` (local, Git-ignored), which
links three worker reports. The research code is in `research/flutter_census/`.

Read first: `AGENTS.md` (one shared tape-level correction per canonical time),
[shared_deflutter.md](shared_deflutter.md), [tape_level_deflutter.md](tape_level_deflutter.md).
Everything below follows those rules.

## 1. The problem in one paragraph

Saved shared-map coverage is low (median 9.7 % of tape time, 0–35 %), but the
recordings are not short of flutter evidence. In the saved v5 plans, 92–100 % of
channel-time is bypassed for one reason: *"Insufficient independent
calibration/validation events"*. Correction is accepted only when ~2.5 kHz
check/Quindar beeps on a target confirm it. Meanwhile, after the
digitization hum is removed, the 1971 mains hum recorded on the tape carries the
full ~2 % tape flutter on 10–20 channels at almost every time.

A blind detector that never looks at beeps found agreeing on-tape carriers in
**88 %** of 228 sampled windows across 38 tapes (95 % under a relaxed rule).
On check beeps it was never shown, it cut measured beep FM by about 75 %:

- 22 fresh windows on 12 low-coverage tapes, 658 beeps;
- median 50.9 → 12.8 Hz;
- wrong-sign control 94.6 Hz; 1.5 s time-shift control 59.9 Hz.

This is the same relative improvement as the listening-approved v5 map in
T510083 13920–14100.

## 2. Physical facts the implementation must respect

1. **Flutter size and shape.** 1.5–2.2 % RMS fractional speed error. The dominant
   rate is 6.45–6.84 Hz, with a strong 13.2 Hz harmonic. Correction bounds of
   ±4.9 % are appropriate.
2. **On-tape vs post-tape.** Every tone recorded *on the tape* is FM-smeared by
   ±3 % of its frequency (±30 Hz at 1 kHz). A narrow, sharp line is post-tape
   (digitization chain) and carries **no** flutter. Do not rank carriers by
   spectral sharpness; that is the main bias of the current
   `flutter_tracking.discover`.
3. **Speed ratio ρ.** On-tape content is shifted by ρ = playback/record speed,
   about 0.983–0.995 and slowly varying (T510077 Quindar at 2442/2487 Hz,
   ρ ≈ 0.986). Historical mains hum appears at 60·ρ·n:
   - typically 58.6–59.9 Hz and 116–119.5 Hz;
   - also n = 3, 4, 5, 7, 9, 11 (≈177, 236, 295, 413, 531, 649 Hz).

   The "119 Hz / 118.5 Hz" frequent centres in the saved inventories are this
   hum.
4. **Digitization hum.** Exact 60·n lines are stationary, 20–60 dB stronger than
   the on-tape hum, and only 0.2–2 Hz away from it. They must be **subtracted
   before tracking**: a capture wide enough for the flutter sidebands always
   contains them. Block least squares over 16 s, per exact harmonic, removes
   them without touching the FM-spread on-tape line (blind partners 28 → 86 on
   T510077 19715).
5. **Post-tape common comb.** Lines near 1020/1140/1260/1740 Hz (odd 60 Hz
   multiples +0.2–0.4 Hz) appear on all channels with only 0.11–0.22 % FM. They
   agree perfectly across channels and will win a naive agreement vote.
   Choose the agreeing cluster with the **largest** common FM. Reject clusters
   whose median FM is below 0.8 % unless beeps confirm them.
6. **Timing.** All 30 channels share the flutter at **zero lag**:
   - every reliable channel lies within ±0.7 ms (σ 0.1–0.3 ms);
   - the lag is stable over hours;
   - there is no ~1.3 s, odd/even or head group.

   Map timing must be accurate to ≈1 ms; a 5 ms error leaves 21 % residual at
   6.6 Hz. Use sub-sample (parabolic) peak estimates wherever a lag is
   estimated. Do not add per-channel lags. Recorded channel offsets
   (`start_seconds`) still apply as today.
7. **Data defect.** **A15_T510096**'s 30 channel files are byte-identical, and so
   are its `orig_delivery` fragments. It is effectively one channel. Do not count
   its "channels" as independent witnesses. Flag it for re-delivery.
   `research/flutter_census/e0_duplicate_audit.py` is a cheap guard; run it on
   every tape before planning.

## 3. Algorithm to implement

Process each tape independently. All 30 channels are read together per block;
canonical time = WAV time + `start_seconds`. Output is one shared-map candidate
stream per tape.

### Stage A — block preparation (per channel, per 10-minute block, +8 s halo)

1. Read the original WAV once (float32); never use denoised audio for detection.
2. `exact_mains`: Welch with 16 s segments. Take every harmonic n·60 Hz (n < 65)
   whose ±0.25 Hz peak exceeds the 2–6 Hz side median by >10 dB.
3. `remove_stationary`: for each listed harmonic and each 16 s sub-block, solve
   the 2×2 least squares for cos/sin and subtract. Batch all harmonics with a
   precomputed complex basis (`w1_fast.fast_stationary`, identical to
   `common.remove_stationary`).
4. Decimate 8 kHz → 2.667 kHz for carriers below 1.15 kHz (the fast path). Keep
   full-rate audio for higher carriers (stage C).

### Stage B — blind candidate tracks (no beeps, no target, no reference)

1. **Candidates.** Use a Welch PSD with 2 s segments on a log-frequency axis
   (0.25 % steps, 50–3900 Hz), Gaussian-smoothed (σ = 3 steps). Subtract a
   161-step median background. Take peaks with height and prominence ≥3 dB,
   at most 10 per channel.
   - Skip 2150–2850 Hz (beeps stay held out).
   - Skip exact 60·n ±0.5 Hz below 1 kHz.
   - Always add the strongest peaks in the hum bands 58.5–59.95 and
     116–119.9 Hz. `e4_blind_seed.candidates` usually finds them as humps;
     `w2_lag.py` shows forcing them explicitly, which production should do.
2. **Tracking.**
   - Complex demodulation at the candidate frequency, with a 6th-order
     Butterworth low-pass of `cap = clip(0.06·f + 20, 8, min(120, f − 20))` Hz.
     This is the DEV-chosen value, confirmed fresh; 0.04·f + 14 is slightly
     worse.
   - Output at 200 Hz. Take `e = (f_inst − f_centre)/f_centre`, bandpassed 2–30 Hz
     (3rd-order, zero-phase).
   - Support: amplitude above 0.3× median, frequency within 0.9·cap, eroded by
     10 samples.
   - The sign convention is the pipeline's (`flutter_tracking.trajectory`).
   - Production should use FFT band-isolation demodulation on the block rFFT
     (`w1_fast.fft_demod`; r 0.89–0.99 vs mixing at 59–987 Hz).
3. **Agreement graph** per 64 s analysis window (step ≤ 60 s; production can
   compute it on block curves directly).
   - Correlate every pair of tracks.
   - An edge requires r > 0.6, *different channels*, and frequencies more than
     2.5 % apart.
   - Build up to 4 clusters greedily: the seed is the highest-degree node; members
     are nodes with r > 0.6 to the seed.
   - Pick the cluster with the largest median FM among clusters with ≥3 members
     (guard for the post-tape comb).
4. **Consensus.**
   - Normalize each member by its LS gain against the seed (drop members with
     gain < 0.3).
   - Weight by r²/(1 − r²) with r ≤ 0.95.
   - Take the weighted mean, rescaled to the median RMS of the five
     best-correlated members (`e7_channel_loo.window`).
5. **Leave-one-channel-out validation.**
   - For every channel with members, predict its best member from members on
     *other* channels.
   - Record r and LS gain. A channel *agrees* when r ≥ 0.7 and gain is 0.6–1.6.
   - Consider replacing the gain gate with an errors-in-variables gain; the
     current gate fails when the seed is the loudest member.

### Stage C — gates, per window (all must hold)

| gate | rule | why |
|---|---|---|
| agreement | ≥3 agreeing channels | independent witnesses of one motion |
| diversity | ≥2 frequency families (not the same Hz and not low-order harmonics of one f0 ≥ 80 Hz), **or** hum cluster with ≥5 agreeing channels | protects against a single source with its own FM |
| magnitude | cluster median FM ≥ 0.8 % and consensus FM in 0.5–4 % | rejects post-tape combs and noise |
| duplicates | channels whose raw audio correlates > 0.95 count once | T510096 and copied feeds |
| controls | where beeps exist: +curve median ratio < 0.8, −curve > 1, 1.5 s shift ≥ 0.9 | audit only, never a requirement |
| speed bound | \|e\| ≤ 4.9 % after joining | existing map preflight |

Windows that fail are **bypassed**; do not force correction into gaps.

Concentration contrast (`e3_consensus.concentration` under +e, −e, a 1.5 s shift,
and no dewarp):

- **Do** use it as a witness for lines above about 200 Hz.
- **Do not** use it for the hum: below about 200 Hz the FM index is too small
  for power to concentrate.
- **Never** validate a held-out line by narrow-capture residual tracking in a
  dewarped domain and then add the dewarp curve back. That is circular: a
  stationary line returned r = 1.0 in E3.

### Stage D — continuous stream and shared-map integration

1. **Join window consensuses.** Use overlap-add with raised-cosine weights on
   overlapping windows (overlap ≥ 14 s). Adjacent windows agree r ≈ 0.987.
   Support is 1 inside passing windows, and support edges fade by the existing
   0.5 s rule. Never extrapolate across failed windows.
2. **Write a stream** in the existing continuous format: `<f8`, frames × 2
   (speed, support), 200 Hz, `start_seconds`. Add a sha256-bound
   `evidence.json` per tape holding every window's clusters, per-channel
   leave-one-out r/gain, families, gates, controls and implementation hash.
3. **Import** as a new candidate kind in `scripts/05_deflutter/shared_tape.py`.
   - Add a sibling of `continuous_candidates` that returns candidates with
     `source_kind='blind_consensus'` and `curve.format='stream'`.
   - Recommended priority: below saved regional (0), equal to or below saved v5
     continuous (1), i.e. 2. Over its saved spans v5 has the same beep ratio and
     is listening-approved; elsewhere the blind stream fills the gaps.
   - `select_intervals`, `compile_map` (single integration, DC anchor, 5 % bound,
     monotonic check) and `shared_render.py` need no change.
4. **Coverage.** Report it as the union of supported tape time (already done by
   `compile_map`), never as channel-seconds.

### Output folders and stage-05 wiring (required implementation)

Use **`D:/Apollo_15_30-Track/deflutter_detection_census/`** for the data produced
by this detection phase. Keep detection evidence, compiled maps and rendered
audio in these separate, explicit destinations:

| root under `D:/Apollo_15_30-Track/` | contents |
|---|---|
| `deflutter_detection_census/<run-id>/` | immutable run configuration, implementation/environment/source bindings, worker benchmark reports and selected pool settings |
| `deflutter_detection_census/<run-id>/<tape>/blocks/<block-id>/` | verified per-block `result.json` and float32 `curves.npy`, including core/halo bounds and completion hashes |
| `deflutter_detection_census/<run-id>/<tape>/` | joined `consensus.f64` (`<f8`, speed/support pairs at 200 Hz) and completed `evidence.json` binding the stream and all block results |
| `deflutter_detection_census/<run-id>/cluster/` | job ownership, host progress/heartbeats, logs, isolated inbox, publication acknowledgements and graceful-stop state |
| `deflutter_planning_census/<run-id>/<tape>/` | native shared `plan.json`, selection, evidence bindings and compiled inverse map from stage 05a |
| `defluttered_census/<run-id>/<tape>/CHnn/` | stage-05b `deflutter.wav` and its provenance |

`<tape>` is the canonical folder name, e.g. `A15_T510077`. A run ID binds the
detector settings, code and input identities; resume reuses only verified,
compatible completed blocks. Changed settings or inputs require a new run ID.
Do not put production detection data in the research reports directory or
overwrite the saved v5/shared stores.

Wire the detector into stage 05 as follows; these are requirements to implement,
**not commands or options that already exist**:

1. Add a stage-05 detection coordinator/worker entry point under
   `scripts/05_deflutter/`. It reads canonical originals and schedules the same
   block jobs on Proprius and Genus (§5). Detection is an explicit preparation
   operation; planning and rendering never launch a hidden resurvey. Reuse saved
   survey/planning/map evidence first and schedule only missing census blocks.
2. Add `--detection-root` to `05a_plan_deflutter.py`, pointing at
   `deflutter_detection_census/<run-id>/`. Keep `--evidence-root` for saved v5
   evidence. Extend `shared_tape.build_tape_plan` to merge the new
   `blind_consensus` candidates with saved regional/continuous candidates before
   the existing selection and single map compilation. Validate canonical source
   identity, offsets, completion and hashes for every imported artifact. A
   requested missing, incomplete or stale census bundle is an error. Permit a
   completed census-only bundle when v5 evidence is unavailable; remove the
   current unconditional v5-bundle prerequisite for that case.
3. Add `--flutter-detection-root` to `scripts/pipeline.py` and forward it as
   `--detection-root` in both single-tape and batch stage-05a job construction.
   Each tape is detected and compiled once, regardless of requested output
   channels. Keep `--flutter-plan-workers` for tape-plan compilation; expose
   separate local/Genus worker counts for the detection pool.
4. For this rollout, select
   `deflutter_planning_census/<run-id>/` through the existing
   `--flutter-planning-root` and `defluttered_census/<run-id>/` through
   `--defluttered-root`. Forward the resulting shared `plan.json` to stage 05b
   via its existing `--flutter-plan` argument. Stage 05b applies that one inverse
   map to completed stage-04 crosstalk outputs derived from v2 denoised audio,
   preserving offsets and gain. Retain the explicit denoised-only comparison
   bypass. Detection on originals does not change this audio processing order.
5. Record the census run ID and evidence/stream hashes in the compiled plan's
   provenance and retain the map binding in rendered outputs. Extend the
   stage-05 operating documentation and pipeline command-plan tests so the new
   evidence is consumed end to end, rather than remaining a research artifact.

## 4. Accuracy work still open

The absolute 6 Hz / 5 cents goal is met by no method, including the approved v5
map. The best window reached 9.2 Hz. The confirmed median residual is 12.8 Hz
(≈0.5 % speed).

Treat every item below as DEV until confirmed on fresh windows. Declare the
confirmation set before seeing outcomes, as `confirm_windows.txt` was.

1. **Amplitude.**
   - Consensus members under-read: stationary residue and neighbours dilute
     wide-capture FM. W3 measured per-family gains of 0.07–0.48 against the v5
     map.
   - Choose a per-window scale α ∈ [0.9, 2.0] maximizing the mean concentration
     of ≥3 distinct-channel, non-beep census lines above 200 Hz under dewarp(α·e).
   - W3 found the same α minimized beep residual in 6/8 windows. Require an
     interior maximum and ≥3 families; otherwise keep α = 1.
2. **Autofocus refinement** (`e11_refine.py`).
   - Dewarp by e and keep lines whose concentration is ≥2.5× all controls.
   - Track their residual with a 5 Hz capture and update e by the weighted mean.
   - DEV result: 14.5 → 13.1 Hz. More lines are needed; include full-rate
     harmonics.
3. **Phase-domain estimation.** Estimate displacement from unwrapped carrier
   phase per harmonic and average displacements, instead of averaging
   differentiated frequency. This reduces noise gain. Untested.
4. **More families.** CH30-type buzz harmonics (~98.4 Hz series on T510077) and
   hum harmonics above 1 kHz on the full-rate path.
5. **Beeps as references.** Where beeps exist they are excellent short carriers.
   Using them *in* the estimate is allowed under the tape rule, but then they are
   not validation. Keep a held-out subset.
6. **Do not** narrow the band to 3–16 Hz, and do not add lags (both measured
   worse).

## 5. Performance and distribution

Measured in W1:

| item | current | new |
|---|---|---|
| v5 survey | 5.7–28 k s per tape, 81 process-h archive | — |
| e5 window evaluator | 36 s per 64 s × 30 ch (53 source-h per wall-h per core) | — |
| block prototype `w1_fast.py` | — | 160–426 source-h per wall-h per core (8–13×) |

Implementation rules:

- **Read once.** Use 10-minute blocks with an 8 s halo, all 30 channels. Seeks
  per window halve disk throughput (18 vs 40 MB/s). Aggregate D: throughput
  saturates near 45 MB/s; the census needs about 7 MB/s.
- **Vectorize.** Batch stationary removal (was 66 % of time). Use one Welch and
  one rFFT per channel per block, and FFT band demodulation per candidate. Use
  float32 storage, one thread per worker (`OMP_NUM_THREADS=1`). Numba is not
  needed.
- **Job unit.** A job is tape × 10-minute block × all channels: about 290 MB read
  and 15–45 core-seconds. There are about 3,978 blocks for 38 tapes. Stage C
  validation and audits run only on windows that pass screening, plus a 10 %
  random audit.
- **Cluster (required).** Use **Genus as a worker-pool source alongside this
  computer, Proprius**, following the coordinator/SSH-worker pattern in
  [denoise_cluster.md](denoise_cluster.md). One Proprius coordinator owns the
  queue and assigns each tape/block once across both machines; do not start an
  independent Genus batch. Reuse the denoise transport and operational patterns
  without changing its frozen algorithms, production queue or checkpoints.
  - Proprius: at most 6 Windows workers, below-normal priority, preserving
    developer headroom.
  - Genus: SSH `bfeist@192.168.0.3`, repository `/mnt/f/_repos/Apollo_15`,
    originals on `/mnt/d`. Invoke `uv run --no-project --python ...` with the
    host-local pinned numerical environment described in the denoise guide;
    never mutate the shared Windows `.venv`.
  - Treat 6 Proprius + 6 Genus workers and Genus CPU mask `0,2,4,8,10,12` as
    **benchmark starting points**, not proven optimal counts for detection.
    Apply one-thread numerical-library limits on both hosts and verify remote
    affinity/environment at startup, including replacement workers.
  - Genus stages results locally, then uploads to the run's isolated D: inbox.
    Proprius verifies job/source/code bindings, shape, finite values and sha256
    before atomic publication to the folders above. Reuse JSON-write and
    transient-storage retry patterns; acknowledge publication before remote
    cleanup. Identity, quality and persistent I/O failures remain visible errors.
  - Report per-host workers, progress, heartbeats and throughput. Graceful stop
    drains to committed block results. Resume verifies and reuses completed
    blocks; a failed upload alone must not trigger recomputation.
- **Worker-count optimization (required before the archive run).** Benchmark
  this detector on Proprius alone, Genus alone, and both together. Denoise's
  optimal counts do not establish this workload's optimum.
  - Use a fixed representative block set across several tapes, including dense
    and sparse carriers, full-rate processing, validation and the audit workload.
    Keep detector settings and jobs identical across configurations.
  - Sweep Proprius counts `1, 2, 4, 6` and Genus counts `1, 2, 4, 6, 8, 12`.
    Expand Genus affinity masks to cover the tested counts and record them.
    Test promising mixed pairs and the `6 + 6` baseline, varying the number of
    simultaneously active tapes as well as worker counts to expose shared D:
    read/write and network contention.
  - Repeat the best configurations at least three times in varied order, record
    warm/cold cache conditions and competing I/O, and include read, compute,
    upload, verification and publication time. Report unique canonical tape-hours
    completed per wall-hour (exclude halo and duplicate work), wall time,
    CPU/RAM, D:/network throughput, failures and Proprius responsiveness.
  - Verify equivalent gates, support and curves across hosts/pool sizes under
    declared numerical tolerances. Choose the fastest stable combined settings
    within the six-worker Proprius cap and resource limits; prefer fewer workers
    when rates are within measurement variability. Save raw trials, the selected
    local/Genus counts, affinities and active-tape limit under the run's
    `benchmarks/`, bind that selection in its configuration, and confirm it with
    a sustained mixed-host trial before production.
- **Projection.** Screening takes about 47 core-h, or about 3.9 h on 12 workers;
  validation adds 1–2 h. That is ≈5–6 wall-hours for the archive, vs about 3
  days for v5. This is an unverified projection; replace it with the measured
  combined-host end-to-end rate after worker optimization.
- **Coarse-to-fine.** Screen everything. Compute concentration, amplitude
  calibration and beep audits only where screening passes. Recompute full-rate
  harmonics only for blocks that ship.

## 6. Verification protocol for the implementation

1. **Unit tests** (new `tests/test_census_*.py`, synthetic):
   - a fluttered 59.1 Hz line next to a 40 dB stronger stationary 60 Hz line is
     tracked after removal (r > 0.9 to the truth), and not without removal;
   - a stationary line gives r ≈ 0 to the consensus, and its concentration drops
     under dewarp;
   - a common post-tape comb with 0.15 % FM loses cluster selection to a 2 %
     on-tape cluster;
   - duplicated channels are counted once;
   - zero-lag joins and the overlap-add are continuous;
   - wrong-sign and shift controls behave as measured;
   - the stream round-trips through `shared_tape.read_curve`.
2. **Regression on saved evidence.**
   - Rerun `sweep3` windows and the `confirm_windows.txt` set.
   - Require eligible windows ≥ 85 %, confirmation beep median ratio ≤ 0.26 and
     no more than 1 % of beeps worse. Record new numbers; never tune on the
     confirmation set.
3. **Consistency with v5.** Where saved v5 spans exist, consensus vs v5 should
   give r ≥ 0.8 at lag 0 (W3: 0.80–0.87). Beep ratios should not be worse than
   v5's by more than 0.05.
4. **Listening.**
   - Render matched A/B WAVs for Audition: identical interval, duration, cleanup
     and gain, through the shared map only (`render_channel_review.py`).
   - Start with a low-coverage tape (e.g. T510106 20000–21800, where 98 % of
     windows passed), then extend T510083 CH20 around the approved passage.
   - Preserve existing A.wav/B.wav and approvals. A new comparison is C.wav or a
     new folder.
5. **Immutability.** Never rewrite saved surveys, plans, maps or approval-bound
   audio. Use the exact detection, planning and audio roots in §3, each scoped
   by run ID; keep benchmark results and remote staging isolated from prior runs.
6. **Distributed operation and integration.** Test single ownership across both
   hosts, interrupted workers/uploads, graceful stop/resume, hash/source refusal,
   and reuse of completed blocks. Verify pipeline forwarding in single-tape and
   batch modes, census-only planning, priority of saved approved spans, and one
   shared map reaching stage 05b for offset channels including a channel with no
   local tone/beeps. Confirm stage-04 input remains the default, incomplete
   evidence cannot be published, and all three output roots stay isolated from
   previous products. Save the worker optimization and sustained-trial reports
   before beginning the archive run.

## 7. What not to repeat

- Gating correction on target-channel check beeps. They are an audit. Under the
  tape rule, evidence on any channel supports all channels.
- Discovering carriers by narrow spectral peaks, or allocating the candidate cap
  before mains exclusion.
- Tracking hum lines without first subtracting the exact stationary digitization
  lines.
- Circular residual validation (§3, stage C).
- Treating identical or copied channels as votes.
- Per-channel lags or separately anchored per-channel maps.

## 8. File map

| path | content |
|---|---|
| `research/flutter_census/BRIEFING.md` | worker briefing, findings summary |
| `research/flutter_census/common.py` | read, demod, frac_dev, dewarp, exact_mains, remove_stationary, lagcorr |
| `research/flutter_census/e4_blind_seed.py`, `e7_channel_loo.py` | blind candidates, tracks, clusters, LOO (reference implementation of stage B/C) |
| `research/flutter_census/w1_fast.py` | block-based fast prototype (reference for production speed) |
| `research/flutter_census/e3_consensus.py`, `e11_refine.py` | concentration witness, autofocus refinement |
| `research/flutter_census/e8_beep_check.py`, `e9_beep_windows.py`, `e13_find_beep_windows.py` | beep audit with controls; fresh beep-window selection |
| `research/flutter_census/e0_duplicate_audit.py` | duplicate-channel guard |
| `research/flutter_census/w2_*.py`, `w3_*.py` | lag study; check-tone validation and amplitude calibration |
| `reports/flutter_census_20261115/` | all outputs, `REPORT.md`, worker reports |
