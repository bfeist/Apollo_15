# Stage 05 detection census (check-tone-free tape motion)

Implemented November 15, 2026 from the
[implementation guide](09_2026-11-15_deflutter_detection_guide.md), which keeps the design
rationale and the research evidence. This page is the operating guide.
[Next steps](12_2026-11-15_deflutter_next_steps.md) lists what is still to be run.

## What it does

Saved v5 shared maps cover a median 9.7 % of tape time because v5 validated a
carrier only against check beeps on the target channel. The census instead
looks for **one tape motion that independent channels agree on**:

1. Read each canonical original once per 10-minute block (8 s halo), all
   channels together. Canonical time = WAV time + `start_seconds`.
2. Subtract the exact, stationary 60·n Hz digitization lines (16 s block least
   squares, parabolic-refined frequencies) so the weaker, fluttered 1971 hum
   recorded on tape at 60·ρ·n Hz becomes trackable.
3. Find broad on-tape humps on a log-frequency axis (never the 2150–2850 Hz
   beep band, never exact 60·n below 1 kHz), always add the strongest peaks in
   58.5–59.95 and 116–119.9 Hz, and track each with wide-capture FFT band
   demodulation (`cap = 0.06·f + 20` Hz) into a 2–30 Hz fractional speed error.
   Carriers up to 1150 Hz use 2.667 kHz audio; 1150–3900 Hz use full rate.
4. In 64 s windows every 42 s (14 s valid-region overlap), build the agreement
   graph (r > 0.6, different channel groups, frequencies > 2.5 % apart), pick
   the agreeing cluster with the **largest** common FM (the post-tape comb at
   ~1020/1140/1260 Hz carries only ~0.15 %), form the weighted consensus and
   validate it leave-one-channel-out. Among clusters within 25 % of that FM,
   the one on the most independent channel groups wins: an equal-FM cluster
   can carry the _inverted_ motion (mirror products), and the window records
   `sign_conflict`.
5. Gate each window: ≥3 agreeing channel groups; ≥2 frequency families or a
   hum cluster with ≥5; cluster FM ≥ 0.8 % and consensus 0.5–4 %; channels whose
   speech-band audio correlates > 0.95, or whose files are byte-identical,
   count once; at most 1 % of samples beyond the 4.9 % bound (brief real
   peaks are clipped like `flutter_tracking.bound`). Failed windows bypass.
6. Audit only: held-out check beeps (+curve, −curve, 1.5 s shift) on passing
   windows plus a deterministic 10 % random sample, and a dewarp-concentration
   witness for lines above 200 Hz in the random sample. Audits never gate;
   `evidence.json` lists windows whose beeps got worse (`adverse_windows`)
   and counts `sign_conflict_windows` for inspection.
7. Join passing windows once per tape by raised-cosine overlap-add into
   `consensus.f64` (`<f8`, speed/support at 200 Hz from tape second 0). Gaps
   are never extrapolated; coverage is the union of supported tape time.

The census is evidence, not authorization. The archive analysis and
qualification in [13_2026-11-17_deflutter_qualification.md](13_2026-11-17_deflutter_qualification.md)
decide which windows apply; stage 05a imports those decisions with
`--qualification-root` at priority 2, below saved regional (0) and
listening-approved v5 continuous (1) evidence, and compiles the single shared
inverse map as before. `--raw-census-root` imports this raw stream for
research only. Stage 05b
applies it unchanged to stage-04 audio of every channel, preserving offsets.

Code: `scripts/05_deflutter/flutter_census.py` (numerical core),
`flutter_detection.py` (run identity, jobs, cluster, publication, joining),
`census_source_stage.py` (sequential SSD staging) and `05_detect_flutter.py` (CLI). Research references remain in
`research/flutter_census/`.

## Folders

The census lives under `F:/Apollo_15_30-Track/`. Analysis, planning and rendered
audio use their existing roots under `D:/Apollo_15_30-Track/`.

| Folder                                                        | Contents                                                                                         |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `deflutter_detection_census/<run-id>/config.json`             | Immutable settings, implementation hashes, input root, environment                               |
| `.../<run-id>/<tape>/inventory.json`                          | Source identity/offsets, duplicate-file audit, block plan                                        |
| `.../<run-id>/<tape>/blocks/<block>/`                         | Verified `result.json` + float32 `curves.npy` (core/read bounds, hashes, timings)                |
| `.../<run-id>/<tape>/consensus.f64`, `evidence.json`          | Joined stream and the hash-bound record of every window, block and audit                         |
| `.../<run-id>/cluster/`                                       | `status.json`, `logs/`, `inbox/`, `acks/`, `stop_requested`, lock                                |
| `.../<run-id>/benchmarks/`                                    | Isolated worker-count trials, comparisons, `selection.json`                                      |
| `deflutter_analysis/<run-id>/<analysis-id>/`                  | Archive analysis and qualification decisions ([guide](13_2026-11-17_deflutter_qualification.md)) |
| `deflutter_planning_qualified/<run-id>/<analysis-id>/<tape>/` | Stage-05a shared `plan.json` and map from qualified decisions                                    |
| `defluttered_qualified/<run-id>/<analysis-id>/<tape>/CHnn/`   | Stage-05b audio from that plan                                                                   |

A run ID (`census-<16 hex>`) is the hash of the detector settings, the
implementation files' bytes, the canonical-originals root and an optional
`--tag`. Reopening a run with different settings or code is refused. Each
tape's originals and offsets are bound when first scheduled; a later change is
refused. Changed settings, code or inputs therefore need a new run ID. Earlier
v5 bundles, shared plans and approval-bound audio are never written.

## Commands

All from `F:\_repos\Apollo_15`. Print the run ID for the current code:

```powershell
uv run python scripts/run.py 05_detect_flutter.py run-id
```

Detect one tape, or the archive, on Proprius and Genus (provisional 6 + 6
workers until a benchmark selection exists):

```powershell
uv run python scripts/run.py 05_detect_flutter.py run --tape A15_T510106
uv run python scripts/run.py 05_detect_flutter.py run --all-tapes
uv run python scripts/run.py 05_detect_flutter.py run --tape A15_T510106 --local-workers 2 --genus-workers 0
```

In an interactive terminal, `run` displays a live dashboard showing the tape
inventory check, sequential copy progress, completed blocks and tapes, worker
assignments, channel progress, throughput, ETA and failures. Use `run --plain`
for periodic text output. The all-tapes command audits each delivery and skips
a tape whose channel files are all duplicates.

Repeating the same command resumes: published blocks are verified and reused,
an uploaded inbox result is published without recomputation, and a tape is
joined as soon as all its blocks are published. A completed tape is verified
and skipped. `status` prints `cluster/status.json` (per-host workers, active
tapes, heartbeat ages, unique tape-hours per wall-hour); `stop` (or Ctrl+C in
the coordinator window) drains to committed blocks. A second Ctrl+C abandons
running blocks; nothing partial is ever published.

```powershell
uv run python scripts/run.py 05_detect_flutter.py status
uv run python scripts/run.py 05_detect_flutter.py stop
```

Through the pipeline (one job per tape selection; the detection root is the
run folder for stage 05; stage 05a takes `--flutter-qualification-root`, see
[13_2026-11-17_deflutter_qualification.md](13_2026-11-17_deflutter_qualification.md)):

```powershell
$run = 'F:/Apollo_15_30-Track/deflutter_detection_census/<run-id>'
uv run python scripts/pipeline.py run --stages 05 --tape A15_T510106 --flutter-detection-root $run --plain
uv run python scripts/pipeline.py run --stages 05 --all-tapes --flutter-detection-root $run --flutter-detect-genus-workers 6 --plain
```

`--flutter-detect-local-workers` (0–16), `--flutter-detect-genus-workers`,
`--flutter-detect-genus-cpus` and `--flutter-detect-tapes` set the detection
pool; `--flutter-plan-workers` still sets concurrent tape-plan compilations.
Pipeline Ctrl+C stops the stage process tree immediately; use the direct
command or `stop` for a graceful drain. Planning and rendering never start
detection or analysis.

Research-only planning from the raw census candidates (never labelled
qualified; production plans use `--qualification-root`):

```powershell
uv run python scripts/05_deflutter/05a_plan_deflutter.py --input D:/Apollo_15_30-Track/orig_concatenated/A15_T510083 --raw-census-root $run --output D:/Apollo_15_30-Track/deflutter_planning_census_raw/<run-id>/A15_T510083
```

The plan's `census` block records `kind = raw_candidates`, the run ID,
config/evidence/stream hashes, coverage, beep-audit summary and duplicate
audit; `map.census_selected_seconds` reports how much tape time the census
supplied.
`deflutter_readiness.py --detection-root $run` reports census completion.

## Proprius and Genus

Proprius owns one queue and assigns each tape/block once. Workers are
persistent processes reading one job per line and returning JSON events. Each
job covers ten minutes of canonical tape time plus boundary context. A worker
extracts the tracks from that interval of each channel, then compares them
across channels. Several workers can read different intervals of the same WAV.

- Proprius workers run below normal priority with one numerical thread each.
- Genus: `ssh bfeist@192.168.0.3`, `exec env OMP_NUM_THREADS=1 … taskset -c
<mask> /home/bfeist/.local/bin/uv run --no-project --python
/home/bfeist/.local/share/apollo15-venv/bin/python
/mnt/f/_repos/Apollo_15/scripts/run.py 05_detect_flutter.py worker …`.
  Genus reads staged WAVs through `/mnt/f/tempF` and accesses the archive through `/mnt/d`.
- Every worker reports its code hashes, numpy/scipy/soundfile/libsndfile
  versions, thread limits and (Genus) CPU affinity at startup, including
  replacements; any mismatch stops scheduling. Genus's pinned environment
  matched Proprius (Python 3.13.11, numpy 2.5.4, scipy 1.18.1, soundfile 0.14.0,
  libsndfile 1.2.2) and passed the startup check over SSH on November 15.
- A normal run stages one active tape under `F:/tempF/apollo15-census-inputs/`.
  The coordinator copies and SHA-256 verifies **one channel WAV at a time**.
  A common copy lock serializes source staging across concurrent sessions.
  Both hosts read intervals of the same staged files using the OS file cache.
  The coordinator drains the tape's blocks before reclaiming the files. The
  inventory and results retain the source identities and offsets from D.
- Use `--input-mode direct` (pipeline `--flutter-detect-input-mode direct`)
  for reads from the originals. Explicit multi-tape concurrency also selects
  direct reads. Sparse block benchmarks default to direct reads. Use
  `--input-mode ssd --tapes 1` to benchmark the production storage path.
- Proprius stages block results under `F:/tempF/apollo15-census/` and Genus
  uses `/home/bfeist/.local/share/apollo15-census/` on its main SSD. Each
  worker copies a completed result into a private archive upload directory,
  verifies its bytes and renames it into the inbox. Transient storage errors
  retry with the local result retained.
- Proprius verifies job, source, settings and code bindings, file set, shape,
  finite bounded values and SHA-256. It renames the verified inbox directory
  into `blocks/` on the archive volume and writes an acknowledgement. Each
  worker then reclaims its acknowledged local result.

## Worker benchmarks

```powershell
uv run python scripts/run.py 05_detect_flutter.py benchmark --trial p6-g0-a --blocks @blocks.txt --tape A15_T510077 --tape A15_T510106 --local-workers 6 --genus-workers 0
uv run python scripts/run.py 05_detect_flutter.py compare-trials p6-g0-a g6-a
uv run python scripts/run.py 05_detect_flutter.py select-pool --local-workers 6 --genus-workers 8 --genus-cpus 0,2,4,6,8,10,12,14 --tapes 4 --trial p6-g8-a --trial p6-g8-b
```

Trials publish into `benchmarks/<trial>/` only and report unique tape-hours
per wall-hour plus per-block read/compute/CPU/upload times. Staged trials
include the whole-tape copy and its verification in wall time. The report's
`source_staging` list gives bytes and seconds per tape. `compare-trials`
checks identical gates and curve differences ≤ 1e-5. `select-pool` writes
`benchmarks/selection.json`, which later `run` commands use as defaults.

### Shared SSD and combined worker pool, November 18

Run `census-b25e1e6b0cd15886` selects **8 Proprius + 8 Genus**, one staged
tape and Genus CPUs `0,2,4,6,8,10,12,14`. At startup the coordinator limits
Genus to four concurrent SSH handshakes and waits for every worker to report
its environment before copying the first tape.

Both hosts processed the same 48 consecutive blocks of A15_T510091, covering
eight hours. Each trial used the same already-staged WAVs on F, with its
coordinator logs and benchmark output on F. The host file caches stayed enabled.

| Workers on each host | Wall seconds after staging | Tape-hours per wall-hour |
| -------------------: | -------------------------: | -----------------------: |
|                    6 |                     212.44 |                   135.57 |
|                    8 |                     188.12 |                   153.09 |
|                   12 |                     194.01 |                   148.45 |

Eight workers per host increased throughput by 12.9% over six. At eight, F's
sampled average disk queue length had a 95th percentile of 0.054. Across all
three pools every eligibility decision matched and the largest absolute
speed-curve difference was 9.32 × 10⁻¹⁰ or less. Each count had one timed
run on this block set. Reports, comparisons and disk counter samples are in
`reports/stage05_ssd_pool_20261118/census-a4d7bb3535c7f9b8/benchmarks/`.

A separate archive-backed 12 + 12 trial copied and verified the tape's 30 WAVs
(14.31 GiB) from D to F serially in 57.5 seconds and published all 48 blocks
in 276.25 seconds total. Windows logged Disk 0 retries and storage-controller
resets during that trial. Disk 0 is a member of the D stripe. The table
isolates the worker comparison from those archive stalls and the one-time
source copy. Use the archive run's `benchmarks/selection.json` for the
selected settings and supporting report hashes.

### Detector optimization benchmarks

On November 18, six workers on each host separately processed the same
[12 benchmark blocks](../research/flutter_census/benchmark_blocks.txt) across
11 tapes (two tape-hours). The table compares the saved trials under
`census-b03f3ffbafded89c` with the optimized detector under
`census-409e426ba812e587`. Compute time excludes input reads. Throughput
includes startup, reads, and publication.

| Host     | Median compute seconds/block, before → after | Tape-hours/wall-hour, before → after |
| -------- | -------------------------------------------: | -----------------------------------: |
| Proprius |                                 100.5 → 46.6 |                          33.0 → 70.7 |
| Genus    |                                 106.0 → 35.2 |                          25.3 → 75.3 |

The workers use batched real FFTs for Welch estimates and reuse a 256-sample
sine/cosine basis for each group of stationary harmonics. Each beep-audit
window shares its four inverse maps across channels. Both hosts use the
existing single-threaded numerical stack. The trials revisited previously
read audio, and the earlier Genus trial included initial inventory setup.
Combined-host archive throughput is unmeasured.

In a separate single-worker Proprius replay of T510083 block 0023, detection
took 57.1 s before and 31.2 s after. Peak process working set fell from 564
to 490 MiB. A local Numba prototype fused frame detrending and windowing, and
accumulated power directly from the FFT output. Across the five Welch sizes
for one 616 s channel, the sum of median timings fell from 325 to 257 ms
(float64 input, five alternating repetitions, warm JIT). This prototype was
only timed in isolation. The deployed Genus census environment uses SciPy
and NumPy and has no Numba dependency.

Across the 180 windows, every eligibility decision, rejection reason, agreeing
channel, and beep audit matched its host's previous trial. The maximum absolute
change in fractional speed was 3.73 × 10⁻⁹. One reported Genus consensus FM
value rounded to 1.6003% from 1.6004%. The run's `benchmarks/` folder holds
`p6-g0-worker-opt/report.json`, `p0-g6-worker-opt/report.json`,
`compare_previous_implementation.json`, and the cross-host curve comparison.

## Validation so far

Synthetic tests (`tests/test_census_detector.py`, `tests/test_census_stage.py`)
cover hum recovery only after stationary removal, stationary-line rejection
and concentration, comb-versus-motion cluster choice, duplicate channels,
overlap-add continuity without gap filling, beep controls, clipping, stream
round-trip through `shared_tape.read_curve`, Genus staging/acknowledgement,
inbox recovery, tamper and source refusal, census-only planning, v5 priority,
one map on offset channels including one without tones, and pipeline forwarding.

Real-data regression with the production code on one 64 s read per window
(`research/flutter_census/p1_production_regression.py`, outputs in the local
`reports/flutter_census_20261115/p1_*`):

| Set                                   |       Windows eligible | Beeps | Identity → curve (median Hz) | Wrong sign | 1.5 s shift | Median ratio | Worse |
| ------------------------------------- | ---------------------: | ----: | ---------------------------- | ---------: | ----------: | -----------: | ----: |
| Research `confirm_cap20`              | 20/22 (all 22 audited) |   658 | 50.9 → 12.8                  |       94.6 |        59.9 |        0.241 | 0.5 % |
| Production, same confirmation windows |                  22/22 |   661 | 50.9 → 9.8                   |      100.4 |        61.9 |        0.181 | 0.3 % |

The confirmation windows were fixed before the research outcome and were not
used for tuning; the sign-conflict rule left them unchanged. On the 228 sweep3
windows 211 (92.5 %) were eligible; see [next steps](12_2026-11-15_deflutter_next_steps.md)
for their beep audit and one adverse window. At the listening-approved T510083
14034 window the production consensus agrees with the saved v5 map at lag 0
(r 0.968/0.966 on CH15/CH17, gain v5/census 1.02/0.88) and its beep ratio
(0.20) is lower than v5's (0.28/0.32) on the same 4–5 beeps
(`p2_v5_consistency.py`). These are numerical audits, not listening approval.
Simultaneous beeps on several channels are one event, so counts overstate
independence. No method yet meets 6 Hz / 5 cents.

An earlier 10-minute T510106 block spent 120 s reading while denoising was
loading D:. Storage contention can dominate the archive rate even when the
same blocks are compute-bound in a warmed benchmark.

## Implementation choices beyond the guide

- Exact-mains frequencies are parabolic-refined; the 2.667 kHz Welch bin is
  0.0009 Hz off 60 Hz, which otherwise left a residual line stronger than the
  on-tape hum.
- The speed gate clips brief peaks to ±4.9 % and fails a window only when more
  than 1 % of its samples saturate. Real ~2 % RMS flutter peaks near 5–6 %.
- Cluster choice adds the sign-conflict rule above. Without it the sweep3
  regression picked an inverted 20–32-member cluster over an 87–110-member one
  of almost equal FM in two windows (all beeps worse, ratios 1.6–1.8).
- Consensus amplitude is the median FM of the five best leave-one-out
  channel groups. The errors-in-variables gain is recorded (`tls_gain`) but not
  used as a gate.
- Beep audits detect beeps on a tapered FFT-isolated 2.5 kHz baseband; the
  concentration witness uses fast-path lines (200–1150 Hz) in audit windows.
- The duplicate guard combines byte-identical file fingerprints with
  speech-band correlation of cleaned audio. Near-silent channels that carry
  the same post-tape content can be grouped, which only reduces votes.
- Amplitude calibration (α), autofocus refinement, phase-domain estimation and
  full-rate hum harmonics above 1 kHz (guide §4) are not implemented.
