# Shared tape-map processing

Stage 05 uses one correction at each canonical tape time. Tone evidence from
any of the 30 simultaneous channels can support that correction on every output
channel. Output-channel tones and legacy per-target eligibility never gate
application. Existing channel offsets are retained.

## Planning and rendering

`05a_plan_deflutter.py` imports completed saved all-channel plans and their
hash-bound assessments from `deflutter_planning_v5_allchannel/<tape>/`.
It also searches completed regional stores and their `audits/*/plan.json`
bundles alongside that root; additional
regional plans can be supplied with repeated `--regional-plan` arguments.
The importer validates originals, offsets, saved details, and map hashes.
Unfinished plans and `validate_transfer` proposals are not promoted.

Selection prefers eligible regional evidence, ranked by support then reference
quality and key; otherwise it uses eligible continuous evidence ranked by
reference quality and key. Selection uses each curve's saved support on the
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

Stage 05b requires this native shared plan and completed **v2** denoised audio.
It reads the WAV filename from `denoised.json`, verifies source and cleanup
bindings and audio hashes, and streams inverse resampling. It retains the v2
gain, frame count, and sample rate. It does not denoise again or independently
normalize or anchor a channel. It rejects a map that reaches unavailable
samples rather than changing it for that channel. New destinations prevent
overwriting previous results.

Compile one tape, then render one channel when ready:

```powershell
uv run python scripts/05_deflutter/05a_plan_deflutter.py --input D:/Apollo_15_30-Track/orig_concatenated/A15_T510076
uv run python scripts/pipeline.py run --stages 05b --tape A15_T510076 --channel 20 --plain
```

Map output defaults to `deflutter_planning_shared`; audio output defaults to
`defluttered_shared`. Repeating a compatible completed map build reuses it.
Changed evidence, implementation, or scope requires a new destination.
Interrupted compilation is not resumable: keep its partial directory and choose
a fresh output. Saved evidence is never rewritten. Use `pipeline.py plan` to
preview commands without compiling or rendering.

The former v5 command alias, review desk, sampling/approval machinery, and
bounded shared-map adapter have been deleted. Stage 05 has one planning command
and one renderer. Historical numerical readers remain where saved evidence
needs them; the removed implementation is recoverable from Git commit
`9e888c5` and the saved research snapshots. Old production approvals cannot
authorize this workflow. Previous WAVs, maps, and approval records are unchanged.

## Listening in Audition

```powershell
uv run python scripts/05_deflutter/render_channel_review.py --tape A15_T510076 --channel 20 --output reports/full_channel_review
```

This writes matched mono float WAVs: `A.wav` is the completed denoised source;
`B.wav` adds the shared tape map. Both use identical interval, duration, cleanup,
sample rate, PCM representation, and gain. Float WAVs retain resampling overshoot
without clipping or an unmatched level adjustment; peaks are recorded. No page
or server is created. `--start` and `--duration` use the same full-scope map and
complete denoised source with source halos, never reanchoring an excerpt.
Earlier listening files and approvals are untouched. Crosstalk subtraction,
when prepared, belongs before tape-map application; this refactor does not
introduce or claim crosstalk cleanup.

## Visual tone sightings

Open **original channels** for the tone search: denoising can remove useful
reference tones. The new denoised files are the source for correction/listening.
Keep the Audition display in local WAV time and report:

| Tape | Channel | Start–end in WAV | Approximate frequency | Observation |
| --- | --- | --- | --- | --- |
| T510076 | CHxx | hh:mm:ss–hh:mm:ss | Hz | Wavy line, short burst, dropout, etc. |

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
