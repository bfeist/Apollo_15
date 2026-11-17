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
   can carry the *inverted* motion (mirror products), and the window records
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
`05_detect_flutter.py` (CLI). Research references remain in
`research/flutter_census/`.

## Folders

| Root under `D:/Apollo_15_30-Track/` | Contents |
| --- | --- |
| `deflutter_detection_census/<run-id>/config.json` | Immutable settings, implementation hashes, input root, environment |
| `.../<run-id>/<tape>/inventory.json` | Source identity/offsets, duplicate-file audit, block plan |
| `.../<run-id>/<tape>/blocks/<block>/` | Verified `result.json` + float32 `curves.npy` (core/read bounds, hashes, timings) |
| `.../<run-id>/<tape>/consensus.f64`, `evidence.json` | Joined stream and the hash-bound record of every window, block and audit |
| `.../<run-id>/cluster/` | `status.json`, `logs/`, `inbox/`, `acks/`, `stop_requested`, lock |
| `.../<run-id>/benchmarks/` | Isolated worker-count trials, comparisons, `selection.json` |
| `deflutter_analysis/<run-id>/<analysis-id>/` | Archive analysis and qualification decisions ([guide](13_2026-11-17_deflutter_qualification.md)) |
| `deflutter_planning_qualified/<run-id>/<analysis-id>/<tape>/` | Stage-05a shared `plan.json` and map from qualified decisions |
| `defluttered_qualified/<run-id>/<analysis-id>/<tape>/CHnn/` | Stage-05b audio from that plan |

A run ID (`census-<16 hex>`) is the hash of the detector settings, the four
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
uv run python scripts/run.py 05_detect_flutter.py run --all-tapes --exclude-tape A15_T510096
uv run python scripts/run.py 05_detect_flutter.py run --tape A15_T510106 --local-workers 2 --genus-workers 0
```

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
$run = 'D:/Apollo_15_30-Track/deflutter_detection_census/<run-id>'
uv run python scripts/pipeline.py run --stages 05 --tape A15_T510106 --flutter-detection-root $run --plain
uv run python scripts/pipeline.py run --stages 05 --all-tapes --flutter-detection-root $run --flutter-detect-genus-workers 6 --plain
```

`--flutter-detect-local-workers` (0–6), `--flutter-detect-genus-workers`,
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
persistent processes reading one job per line and returning JSON events.

- Proprius: at most 6 workers, below-normal priority, one numerical thread.
- Genus: `ssh bfeist@192.168.0.3`, `exec env OMP_NUM_THREADS=1 … taskset -c
  <mask> /home/bfeist/.local/bin/uv run --no-project --python
  /home/bfeist/.local/share/apollo15-venv/bin/python
  /mnt/f/_repos/Apollo_15/scripts/run.py 05_detect_flutter.py worker …`.
  Originals are read through `/mnt/d`. The shared Windows `.venv` is never used.
- Every worker reports its code hashes, numpy/scipy/soundfile/libsndfile
  versions, thread limits and (Genus) CPU affinity at startup, including
  replacements; any mismatch stops scheduling. Genus's pinned environment
  matched Proprius (Python 3.13.11, numpy 2.5.4, scipy 1.18.1, soundfile 0.14.0,
  libsndfile 1.2.2) and passed the startup check over SSH on November 15.
- Genus writes to `/home/bfeist/.local/share/apollo15-census/<run-id>/…`,
  copies into a private `inbox/<tape>/<block>.upload`, verifies the bytes and
  renames it. Transient CIFS errors retry for ~15 minutes with the local result
  kept; identity, quality and persistent I/O errors stop the session.
- Proprius verifies job/source/settings/code bindings, file set, shape,
  finite bounded values and sha256 before copying into a `.partial` folder and
  renaming it into `blocks/`. It then writes `acks/`; Genus deletes its local
  copy only after the acknowledgement.

## Benchmark tools (not yet run)

```powershell
uv run python scripts/run.py 05_detect_flutter.py benchmark --trial p6-g0-a --blocks @blocks.txt --tape A15_T510077 --tape A15_T510106 --local-workers 6 --genus-workers 0
uv run python scripts/run.py 05_detect_flutter.py compare-trials p6-g0-a g6-a
uv run python scripts/run.py 05_detect_flutter.py select-pool --local-workers 6 --genus-workers 8 --genus-cpus 0,2,4,6,8,10,12,14 --tapes 4 --trial p6-g8-a --trial p6-g8-b
```

Trials publish into `benchmarks/<trial>/` only and report unique tape-hours
per wall-hour plus per-block read/compute/CPU/upload times. `compare-trials`
checks identical gates and curve differences ≤ 1e-5. `select-pool` writes
`benchmarks/selection.json`, which later `run` commands use as defaults.

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

| Set | Windows eligible | Beeps | Identity → curve (median Hz) | Wrong sign | 1.5 s shift | Median ratio | Worse |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| Research `confirm_cap20` | 20/22 (all 22 audited) | 658 | 50.9 → 12.8 | 94.6 | 59.9 | 0.241 | 0.5 % |
| Production, same confirmation windows | 22/22 | 661 | 50.9 → 9.8 | 100.4 | 61.9 | 0.181 | 0.3 % |

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

A real 10-minute block (T510106, 30 channels) took ~100 s of computation on
one core plus 120 s of reading while the denoise rollout was loading D:.
Worker counts and the archive rate are not yet measured.

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
