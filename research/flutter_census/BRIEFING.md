# Flutter census briefing (2026-11-15) — for research workers

Read `F:/_repos/Apollo_15/AGENTS.md` first (tape-level shared correction rules).
Do NOT modify anything under `scripts/`, `D:/Apollo_15_30-Track/` (read only),
existing `reports/*` directories, or approval-bound audio. Write only inside
the output directory you are given and, if needed, new files in
`research/flutter_census/` with a unique prefix. Use `uv run python ...`.
Do not start servers or web pages. Be economical with CPU: the machine has
32 logical cores; another sweep may be running, use at most 8 processes.

## Data

- Originals: `D:/Apollo_15_30-Track/orig_concatenated/<tape>/*CHnn_*.wav`,
  mono 8 kHz PCM16, ~17 h, 30 channels per tape, simultaneous tracks.
- Canonical time = WAV time + inventory `start_seconds` (all zero on the tapes
  checked). `research/flutter_census/common.py: inventory(), read()`.
- Saved v5 all-channel plans: `D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel/<tape>/plan/plan.json`
  (`regions[0].targets[*].reference_spans`, action `use_reference` = saved
  correction). survey.jsonl / survey_manifest.json are large (0.2–0.4 GB).

## Why coverage is low (established today)

Saved v5 per-tape union coverage is 0–35 % (median ~9.7 %). In the plans,
92–100 % of channel-time is `leave_uncorrected` with reason
"Insufficient independent calibration/validation events": the planner
validates a reference only against ~2.5 kHz Quindar/check beeps on the target
channel. No beeps -> no correction, regardless of carriers.

## Key physical findings so far

1. Flutter is large: ~2.2 % RMS speed error, dominant ~6.6 Hz with a 13.2 Hz
   harmonic (T510077 19715 s; CH30 buzz harmonic at ~987 Hz tracked with
   1005.5 Hz / 90.5 Hz capture).
2. Therefore every tone that was recorded ON the tape is FM-smeared by ±3 %
   of its frequency (±30 Hz at 1 kHz). Narrow, sharp spectral lines are
   post-tape (digitization) and carry no flutter. The production discovery
   (`scripts/05_deflutter/flutter_tracking.py: discover`, 0.5 Hz bins, peak
   picking with ±15 Hz contrast) is biased toward exactly those useless lines.
3. Historical (1971) mains hum IS on the tape, shifted by the tape speed
   offset (~1.2 % slow on T510077): fluttered lines near 59.3–59.8 Hz and
   118.3–119 Hz on many channels, next to exact 60/120 Hz digitization lines.
   After block least-squares subtraction of the exact stationary digitization
   lines (`common.remove_stationary(x, common.exact_mains(x))`) these become
   trackable and agree across channels.
4. Dewarping any channel by a correct speed curve makes on-tape lines sharp.
   Wrong-sign dewarp smears them. This +e/−e (and time-shift) concentration
   contrast is a check-tone-free validation witness (`e3_consensus.concentration`).
5. PITFALL: tracking a held-out line's *residual* in a dewarped domain with a
   narrow capture and adding back the dewarp curve is circular (a stationary
   line then "agrees" with r = 1). Use wide-capture tracking on original audio,
   or the concentration controls, for independent validation.

## Tools (research/flutter_census)

- `common.py`: read, demod (complex demodulation, 1 kHz decimation, 200 Hz
  output), frac_dev (2–30 Hz fractional deviation + support), dewarp,
  find_lines, exact_mains, remove_stationary, corr, lagcorr.
- `e1_line_census.py`: narrow-line census vs a reference (shows the bias).
- `e2_sharpen.py`: +e/−e dewarp census of on-tape lines.
- `e3_consensus.py`: census_lines, families (same-source grouping), concentration.
- `e4_blind_seed.py`: blind seed (no reference, no beeps): log-frequency hump
  candidates, wide-capture tracks, cross-channel agreement clustering.
- `e5_window_eval.py`: blind seed -> census -> families -> leave-one-family-out
  validation. Writes `<tape>_<t0>.json` and `<tape>_<t0>.npy` (consensus
  fractional speed error at 200 Hz, pipeline sign convention: error =
  (f_inst − f0)/f0, same as `flutter_tracking.trajectory`).
- Pipeline check-tone tools: `scripts/05_deflutter/flutter_tracking.py`
  (`beeps`, `evaluate_events`) and `scripts/05_deflutter/flutter.py`
  (`positions`, `resample_at`). Import by adding `scripts/05_deflutter` to
  sys.path.

## Output expectation

Write a concise `REPORT.md` in your output directory: what you ran (exact
commands), numbers, what worked/failed, caveats, and concrete implementation
recommendations. Save machine-readable results as JSON next to it.
