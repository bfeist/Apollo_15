# Regional deflutter v2 runbook

The implementation preserves stage 03 and the old T510083 plan/render. New defaults use `carrier_survey_v2`, `deflutter_planning_v2`, `defluttered_v2`, `quindar_post_deflutter_analysis_v2`, and `defluttered_retimed_v2` beneath `D:/Apollo_15_30-Track`.

This implements the workflow in [DEFLUTTER_PIPELINE_REVIEW.md](../DEFLUTTER_PIPELINE_REVIEW.md). The initial numeric limits are **development limits**, not thresholds already qualified across Apollo 15. Carrier sensitivity, cross-tape pilots, fresh evaluation, and speech listening must be accepted before the batch. Tools never manufacture acceptance from a completed plan.

## 1. Qualify and benchmark the carrier survey

Install the optional dependency used by the in-place legacy IRIG decoder:

```powershell
uv sync --locked --extra irig
uv run --extra irig python scripts/qualify_carrier_survey.py --positive 'O:/30-track-audio/Apollo_11_30-Track/concatenated_wav_files/original/T648/A11_T648_HR1U_CH1.wav' --legacy-repo F:/_repos/Apollo_Audio_Processing --output reports/t648_qualification.json
uv run python scripts/pipeline.py run --stages 04 --tape A15_T510083 --carrier-max-windows 30 --plain
```

The qualification command reads one 30-second known-positive excerpt, injects two-second islands at off-grid positions across attenuation/noise/speed conditions, and checks pure-tone, AM-only, hum, noise, and speech-like controls. A tone or AM signal may be a **carrier candidate**; it must never become an authenticated clock solely from screening. Add real speech backgrounds and inspect the measured sensitivity before accepting qualification. No audio outputs are written.

Screening uses overlapping two-second windows at one-second hops over 700–1300 Hz, with an explicit minimum detectable island length of two seconds. Negative records describe this bounded search. Features are saved in immutable block files, with a resumable per-source index and per-tape index. CH01/IRIG names affect priority only. Every WAV, including an unlabelled recording, is included. Unlabelled recordings remain reference-only.

Inspect each `survey.json` performance record before launching the full survey. It reports source seconds, wall/CPU time, process OS read bytes (including cache hits), and a remaining screening estimate. Detailed tracking and decoding cost is separate. Resume by omitting the benchmark budget:

```powershell
uv run python scripts/pipeline.py run --stages 04 --all-tapes --plain
```

Carrier caches record candidate islands. The planner independently establishes `carrier_supported` using local phase/support measurements. Clipping, competing peaks, unsupported gaps and reacquisition edges exclude support. IRIG's low AM bits use a lower phase-amplitude threshold than generic references; decoding is not required for correction eligibility.

## 2. Decode original samples independently

```powershell
uv run --extra irig python scripts/irig_clock.py --survey D:/Apollo_15_30-Track/carrier_survey_v2/A15_T510083 --legacy-repo F:/_repos/Apollo_Audio_Processing --output D:/Apollo_15_30-Track/irig_clock_v2/A15_T510083
```

Only candidates plus context are decoded, in bounded overlapping chunks. Raw, 400 Hz high-pass, and 650–1350 Hz band-pass trials use both polarities. All decoded fields, validity, original sample positions, runs, repetitions, discontinuities and imported-code hashes are saved. Cache identity is independent of denoising and carrier tracking maps. A decoder/setting change requires a new decoder destination but does not invalidate carrier features.

Decodes remain `decoded_provisional`. Authentication requires a reviewed record identifying a decoder cache SHA-256 and one excerpt/attempt/run, for example:

```json
{
  "decoder_cache": {"path": "decode_000.json", "sha256": "EXACT_FILE_HASH"},
  "excerpt_index": 0,
  "attempt_index": 2,
  "run_index": 0,
  "authentication": {
    "status": "accepted",
    "recorded_clock_basis": "GMT",
    "reviewer": "REVIEWER",
    "evidence": "Evidence establishing the recorded clock basis and format"
  }
}
```

```powershell
uv run python scripts/irig_clock.py --authenticate reviewed_clock_request.json --output original_anchors.json
uv run python scripts/irig_clock.py --map-anchors original_anchors.json --review PATH/TO/review.json --output defluttered_anchors.json
```

At least four sequential valid frames are required. Historical year bits must be zero; they are not interpreted as a modern calendar year. Apollo 15 labels use 1971 and range zero **1971-07-26 13:34:00 GMT**. Year rollover is detected and requires explicit separate year authentication. Zero-phase filtering does not calibrate crossing bias; the declared diagnostic uncertainty is 10 ms, with map-grid uncertainty added when carrying anchors.

Mapping uses the actual saved stage-05b map for the **same original source**. A timing-only or unlabelled source is never assigned to a speech target to transport its clock. Cross-channel historical alignment requires separate demonstrated alignment; it is not inferred from shared flutter or filenames. One anchor cannot determine long-term drift.

## 3. Run bounded pilots and freeze the method

Full-tape planning requires acceptance; bounded pilots use the complete carrier survey without that prerequisite:

```powershell
uv run python scripts/pipeline.py run --stages 05a --tape A15_T510083 --flutter-plan-start 27090 --flutter-plan-duration 180 --flutter-plan-workers 2 --plain
```

Include T510083, T510077, T510073, T510092, T510076, a newly inventoried tape outside the earlier 16-tape study, and any discovered Apollo 15 timecode island. Include early/middle/late passages, head groups, faint/no-beep targets, dropouts, boundaries and failures. The command does not automatically choose or approve these passages.

The planner now:

- Includes independently surveyed timing-only and unlabelled carriers without adding speech targets.
- Tries wide acquisition before rejecting a narrow capture; chooses reference candidates by coverage of target calibration winners, with explicit untried candidates and budget exhaustion.
- Freezes calibration choices before holdouts. A failed holdout remains failed; changing the method requires fresh evaluation passages.
- Requires two separated time windows in each split for direct numeric support. Multiple beep margins are not counted as independent events; oscillator/feed provenance remains explicitly unknown unless separately established.
- Reports residual Hz/cents, out-of-band motion, support, saturation, sparse evidence, and low-baseline identity decisions. Harmonic seeds cover 4–10 Hz (three harmonics); signed methods cover 2–15 or 2–30 Hz. This is relative flutter correction, not all wow/flutter or absolute speed correction.
- Uses polarity, no-motion, amplitude-derived, and three non-wrapping wrong-time controls. At least two wrong-time shifts must lose; one can coincide with a periodic waveform without forcing all true periodic references to fail.
- Allows local transfer-group evidence only when a directly supported donor and a different target carrier show matching zero-lag playback motion in separate windows, with phase/gain/residual and amplitude-coupling checks. No content lag is fitted. Groups expire at region boundaries, reference changes and unsupported gaps. Missing evidence stays a proposal or bypass.

The initial limits are in `flutter_policy.py` and `flutter_groups.py`. Freeze their hashes and the implementation **before** fresh evaluation. Generate an unapproved record:

```powershell
uv run python scripts/deflutter_readiness.py --write-rollout-template configs/local_rollout_approval.json
```

The record must identify the reviewer/date and hash-bound JSON evidence for `carrier_qualification`, `cross_tape_pilots`, `fresh_holdouts`, and `speech_listening`. Each evidence report must explicitly record `"status": "accepted"` after review; the rollout record must also be accepted. Retain measurements, passage coordinates, failures and listening notes in those reports. An implementation or policy change invalidates acceptance and completed-plan reuse.

## 4. Full plans, continuous review and production

The [local evidence desk](deflutter_review_app.md) adds `representative-listening-v1`, an explicit sampling-based alternative to individual channel listening approvals. Start with `python scripts/deflutter_review.py` (or add `--demo`). It prepares bounded audio from exact full-channel maps, retains numeric/transfer/listening distinctions, previews exact acceptance scope and writes records consumed by the existing readiness/production gates. Methodology can use review-only full-duration maps containing bounded pilots and explicit identity elsewhere; production still requires complete actual full-tape maps. Acceptance never starts rendering.

```powershell
uv run python scripts/pipeline.py run --stages 05a --all-tapes --rollout-approval configs/local_rollout_approval.json --plain
```

Plans report duration-weighted categories per channel, reference switches and search-budget exhaustion. Regional durations do not imply full support within each interval; inspect saved support spans. No clock success reruns denoising. No old completed plan is silently reused under v2.

Use `render_channel_review.py` for representative listening and continuous review. It preserves numeric/group/listening/production distinctions. Experimental proposals remain available only in this review tool. Exact joined maps are checked on reserved boundary events after fades, blending and duration anchoring. Failed or insufficient reserved evidence remains explicit. Full-channel production acceptance must refer to the full-channel map, because a locally anchored excerpt map is different.

```powershell
uv run python scripts/render_channel_review.py --tape A15_T510083 --channel 20 --output reports/v2_continuous_T510083_CH20
```

For a channel with a nonzero filename offset, pass its `--start` from the plan. Use existing prepared denoised intermediates. Whole-channel production stage 05b requires them and never redoes denoising. Bounded listening reviews can use their own aligned caches.

Create `production_acceptance.json` in each tape's planning folder (or under `--production-approval-root`):

```json
{
  "status": "accepted",
  "rollout_approval": "ABSOLUTE_PATH_TO_ROLLOUT_JSON",
  "plan_sha256": "EXACT_PLAN_HASH",
  "channels": [
    {
      "channel": 20,
      "speech_preserved": true,
      "transitions_accepted": true,
      "continuous_review": {"path": "ABSOLUTE_PATH_TO_REVIEW_JSON", "sha256": "EXACT_REVIEW_HASH"}
    }
  ]
}
```

Acceptance requires conservative matching full-channel review, successful final-map checks and unchanged map artifacts. Identity-only bypass channels can pass the numeric no-harm check; listening still requires review. Group-supported targets reserve the last half-second of each independent carrier window for final-map checks. Maps with failed or insufficient reserved evidence cannot be promoted by adding a listening checkbox. This is an evidence gap, not authorization to enable experimental transfer.

```powershell
uv run python scripts/deflutter_readiness.py --rollout-approval configs/local_rollout_approval.json --output reports/readiness.json
uv run python scripts/pipeline.py run --stages 05b --all-tapes --plain
```

Readiness exits 2 until prerequisites are met. Batch jobs continue after failures and preserve per-job logs. Existing outputs are never overwritten. Stage 05b cannot use global experimental transfer for production.

## 5. Post-deflutter clock constraints and slow retiming

Run the existing exhaustive post-deflutter Quindar survey. When authenticated same-source anchors have been carried through the applied flutter map, include them when freezing the tape model:

```powershell
uv run python scripts/quindar_survey.py --tape A15_T510083
uv run python scripts/05c_retime.py --tape A15_T510083 --plan-only --clock-anchors defluttered_anchors.json
```

The slow model uses supported clock-run intervals as independent constraints on Quindar-derived elapsed time. A conflict blocks freezing rather than forcing agreement. Single anchors provide no drift constraint; gaps and unverified cross-source timing remain unanchored. Clock annotations and applied-map hashes are validated on reuse. A changed map invalidates its clock annotations and downstream survey/plan provenance.

The shared-plan report measures differential displacement against the first available channel's actual 200 Hz map. Applying one slow curve does not assert synchronized channels or erase historical head/content offsets. The legacy full-speed IRIG deflutter command is never invoked, so its slow correction cannot be applied again in 05c.
