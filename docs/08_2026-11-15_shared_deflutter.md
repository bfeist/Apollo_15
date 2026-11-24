# Shared tape-map processing

Stage 05 uses one correction at each canonical tape time. Tone evidence from
any of the 30 simultaneous channels can support that correction on every output
channel. Output-channel tones and legacy per-target eligibility never gate
application. Existing channel offsets are retained.

## Capture alignment and selective recovery

Stage 02 measures constant differences between the WAV clocks after concatenation.
Multiple separated parts of the original tape must support a shift. Shared content
and independent tone motion help distinguish a capture offset from an ordinary
repeat. Conflicting or changing offsets hold the tape for review. The primary
search spans ±16 seconds. Receipts bind the measurements to the original files
and detection method. Stage 04 uses the signed start difference when seeking a
donor. Stage 05 uses those same coordinates to trace, qualify and apply one
physical tape map across all channels. Original WAV placement and gain are preserved.

From the repository root on Proprius, preview or run just the affected tapes.

```powershell
uv run python -u scripts/run.py recover_capture_alignment.py plan
uv run python -u scripts/run.py recover_capture_alignment.py run
```

The November 19 audit found 33 staggered channels on seven tapes. Replacing
their tape maps affects 210 output channels. The other 31 eligible tapes keep
their saved plans. Recovery reuses verified aligned traces, stage-03 WAVs,
target-only stage-04 scans, and qualification on the unaffected tapes. It
checks the combined held-out results before audio publication. Corrected
stage-05 outputs are staged under `D:/Apollo_15_30-Track/stage_evidence/archive/capture_alignment_recovery/aligned-*/defluttered_shared/`.
For an overnight run, use `uv run python -u scripts/run.py recover_capture_alignment.py run --finish-archive`.
It verifies each corrected tape's WAVs against the new plan and stage-04 release,
moves the previous tape directory to `aligned-*/previous_defluttered_shared/`,
and publishes the corrected tape in `D:/Apollo_15_30-Track/defluttered_shared/`.
An editor holding a WAV open can block the tape directory rename on Windows.
The coordinator keeps the corrected WAVs staged, processes the other tapes,
and retries publication at the end. Close the old WAVs in the editor if the
console reports a blocked rename. File Explorer can also hold open channel
directories when browsing the archive. Close that Explorer window if the
folder stays locked after the WAVs close.
It also completes outstanding channels on the unaffected tapes. The same command
resumes from verified results. For a fresh archive, run
`uv run python scripts/pipeline.py run --stages 02 --all-tapes` after ingest.

## Archive rendering

The previous archive release used the stage-04 `repeat-v2` release and 38
whole-tape plans for `census-0a7f22d8282355c1/analysis-1d46441629e82576`.
The census excluded the duplicate-feed tape T510096. The 1,139 scheduled
channels retain their frame counts, 8 kHz sample rate, acquisition offsets and
existing gain. Workers write 32-bit float WAVs.
Each output carries the original D: map and stage-04 evidence paths in its
`review.json`.

One transfer thread stages inputs on F: and publishes verified results back to
`D:/Apollo_15_30-Track/defluttered_shared/<tape>/CHnn/`. It services one D:
operation at a time while workers render independently. Workers share a queue
across two staged tapes. As each worker finishes a channel it can start a
channel on the next ready tape while slower channels finish on the first.
Publication starts only after the whole tape finishes. Genus uses its native
SSD for processing and returns completed channels through F:. The coordinator
releases its tape scratch after verification on D:. An existing stage-04 mirror
on F: can supply a hash-matched input and stays available after publication.

The console shows each worker, its current channel and progress, transfer
activity, throughput, render ETA and archive ETA. Archive ETA starts after the
first newly published channel. Ctrl+C drains current channels. Run the same
command to resume from verified F: results and completed D: channels.
The `check` subcommand compares both workers on a 30-second excerpt in scratch.

## Governing rule

The project owner reaffirmed on November 3, 2026 that each tape's 30 channels
were recorded simultaneously and must share a single tape-motion correction.
Reference discovery and evidence collection may be per channel; selection,
coverage, joining and application are at tape level. Build and anchor one map
for the intended scope, then crop or apply it to channels. Never anchor a
channel or a short listening excerpt independently, never average incompatible
curves, and never promote `validate_transfer`, rejected or untracked
candidates merely to increase coverage. Regions without usable evidence on any
channel remain bypassed. The first CH20 reviews gated on CH20 alone; their
audio and approvals are kept as historical comparisons only.

## Planning and rendering

`05a_plan_deflutter.py` imports completed saved all-channel plans and their
hash-bound assessments from `stage_evidence/flutter/carrier_tones/<tape>/`.
Existing maps retain the original source paths and hashes. The archive loader
resolves those sources to the retained files.
It also searches completed regional stores and their `audits/*/plan.json`
bundles under the original tape archive's data root; additional
regional plans can be supplied with repeated `--regional-plan` arguments.
With `--qualification-root deflutter_analysis/<run-id>/<analysis-id>/` it also
imports the tape's [qualified census decisions](13_2026-11-17_deflutter_qualification.md)
from the [detection census](10_2026-11-15_deflutter_detection.md); unresolved-contradiction
intervals then bypass every source. `--raw-census-root` imports unqualified
detector candidates for research only. A census-only tape is accepted when no
v5 bundle exists.
The importer validates originals, offsets, saved details, and map hashes.
Unfinished plans and `validate_transfer` proposals are not promoted.

Selection prefers eligible regional evidence, ranked by support then reference
quality and key; otherwise it uses eligible continuous evidence ranked by
reference quality and key; the qualified census stream (priority 2) fills the
remaining supported tape time. Selection uses each curve's saved support on the
common 200 Hz grid: a dropout exposes the next eligible reference, and bypass
is retained where none has usable support. Copied feeds do not cast independent
votes. A local
saved assessment must exist without explicit failure/worsening on at least one
witness. Missing reserved events remain a recorded limitation. The saved outcome
`full_restoration_at_measured_events` is included. This is numerical evidence for
a listening trial, not proof of physical tape-motion correction.

The compiler joins selected curves with smooth support/selection fades, removes
supported DC drift once, and integrates once over the tape scope. It stores a
single `tape_map.npy` with canonical time, nominal time, speed, and support.
`selection.json` and `evidence.json` retain choices, witnesses, and rejections.
Coverage is a union of tape-time intervals, with both eligible duration and
effective duration after support fades reported. Bypasses retain zero local
speed correction and may retain a constant displacement for continuity.

Stage 05b requires this native shared plan and completed [stage-04 crosstalk
output](06_2026-11-13_crosstalk_stage.md), derived from **v2** denoised audio. It verifies
`crosstalk.json`, source/evidence bindings and audio hashes, then streams inverse
resampling. An intentional denoised-only comparison requires an explicit bypass. It retains the v2
gain, frame count, and sample rate. It does not denoise again or independently
normalize or anchor a channel. It rejects a map that reaches unavailable
samples rather than changing it for that channel. New destinations prevent
overwriting previous results.

Compile one tape, then render one channel when ready:

```powershell
uv run python scripts/05_deflutter/05a_plan_deflutter.py --input D:/Apollo_15_30-Track/orig_concatenated/A15_T510076
uv run python scripts/pipeline.py run --stages 04,05b --tape A15_T510076 --channel 20 --plain
```

Map output defaults to `deflutter_planning_shared`; audio output defaults to
`defluttered_shared`. For census-based maps use the run-scoped
`deflutter_planning_census/<run-id>/` and `defluttered_census/<run-id>/` roots
(commands in [the detection guide](10_2026-11-15_deflutter_detection.md#commands)). Repeating a compatible completed map build reuses it.
Changed evidence, implementation, or scope requires a new destination.
Interrupted compilation is not resumable: keep its partial directory and choose
a fresh output. Saved evidence is never rewritten. Use `pipeline.py plan` to
preview commands without compiling or rendering.

The former v5 command alias, review desk, sampling/approval machinery, and
bounded shared-map adapter have been deleted, and on November 15 the v5
survey/planning engine itself (continuous planner, assessments, resume and
per-channel map evaluation) was removed in favour of the detection census.
Stage 05 has one detection command, one planning command and one renderer.
The saved-evidence readers remain; removed source is recoverable from Git
(commit `9e888c5` and the November 15 history) and the saved research snapshots. Old production approvals cannot
authorize this workflow. Previous WAVs, maps, and approval records are unchanged.

## Listening in Audition

```powershell
uv run python scripts/05_deflutter/render_channel_review.py --tape A15_T510076 --channel 20 --output reports/full_channel_review
```

This writes matched mono float WAVs: `A.wav` is the completed stage-04 source;
`B.wav` adds the shared tape map. Both use identical interval, duration, cleanup,
sample rate, PCM representation, and gain. Float WAVs retain resampling overshoot
without clipping or an unmatched level adjustment; peaks are recorded. No page
or server is created. `--start` and `--duration` use the same full-scope map and
complete stage-04 source with source halos, never reanchoring an excerpt.
Earlier listening files and approvals are untouched. The review exporter accepts
`--skip-crosstalk` to explicitly compare the denoised-only source with de-flutter.
Stage-04 bypass intervals remain unchanged before applying the common map.

## Visual tone sightings

Open **original channels** for the tone search: denoising can remove useful
reference tones. The new denoised files are the source for correction/listening.
Keep the Audition display in local WAV time and report:

| Tape    | Channel | Start–end in WAV  | Approximate frequency | Observation                           |
| ------- | ------- | ----------------- | --------------------- | ------------------------------------- |
| T510076 | CHxx    | hh:mm:ss–hh:mm:ss | Hz                    | Wavy line, short burst, dropout, etc. |

A waterfall screenshot can clarify a faint or complicated line. A visibly
wavy tone is a discovery lead; its origin, trackability, and saved validation
still determine whether it can anchor the common map. The former roughly 20%
estimate described saved eligible intervals, not how much of the tape contains
tones or how much is ultimately correctable.

Trace a sighting without new signal analysis (replace example coordinates):

```powershell
uv run python scripts/05_deflutter/inspect_tone.py --tape A15_T510076 --channel 15 --time 12345 --hz 1000 --local-time
```

The lookup reports saved discovery/tracking status and, if a shared plan exists,
eligible evidence, local rejected witnesses, and the selected tape correction.
`--local-time` converts channel WAV time using its canonical offset. Without it,
`--time` is canonical tape seconds. This distinguishes an undiscovered tone,
an untrackable candidate, and a usable reference that lost selection. The lookup
does not automatically accept a sighting or rerun discovery.
