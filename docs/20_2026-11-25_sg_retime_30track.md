# Retiming the 30-track tapes against the space-to-ground set

**Status: implemented as stage 06c (2026-11-25); the archive run waits for 06a word alignment.**
The stage is `scripts/30-track/06_retime/06c_sg_retime.py`
([implementation](#implementation-stage-06c)). It reads the 06a transcripts with CTC-aligned word
times ([runbook](15_2026-11-22_transcription_runbook.md#word-times-forced-alignment)). The
earlier rescue passes, and every number measured on Whisper's own word times, are withdrawn. The
IA and NARA stores are aligned. The 30-track store is being aligned in place. Official editions
are built once that finishes. The plan below is the design record. Where the implementation
departs from it, [departures](#departures-from-the-plan) says why.

## Summary

Many 30-track channels carry the space-to-ground (SG) loop behind their own conversation. The SG
loop is the same audio that is on the SG reels, and those reels are now on GET:

- **NARA flight tapes** (608, 640–652, 654, 655) are respeeded from IRIG-B to within a millisecond
  of GET ([guide](19_2026-11-24_nara_flight_tapes.md)).
- **Internet Archive (IA) reels** (540–583-AAA) are respeeded from TEC/journal anchors, with a
  held-out error of 0.4–0.9 s on most reels
  ([guide](16_2026-11-23_mission_data_ingestion.md#space-to-ground-reels)).

Stage 06a has transcribed every 30-track channel (18,675 channel-hours, complete), and both SG sets
are transcribed with the same recognizer. Matching **ASR against ASR** of the same broadcast is
much tighter than matching ASR against the 1971 typed transcript. The prototypes show this is
feasible on every tested tape:

| Pilot tape (reference)      | Channels with ≥100 SG matches | Anchors after outlier rejection | Pooled residual, median / p90 | Audio refinement |
| --------------------------- | ----------------------------: | ------------------------------: | ----------------------------: | ---------------- |
| T510104 HR2L (NARA 646–649) |                      16 of 30 |                          24,793 |               0.14 s / 0.67 s | ±0.01 s          |
| T510093 HR1U (IA 569–574)   |                      26 of 30 |                         114,807 |                0.35 s / 2.4 s | ±0.01 s          |
| T510082 HR1U (IA 551–553)   |                      24 of 30 |                          41,868 |               0.07 s / 0.29 s | not run          |

The tape is treated as one unit, as in stage-05 flutter analysis. Anchors found on any channel
are pooled on canonical tape time, which already includes the stage-02 capture offsets. Each
tape gets one curve, and that curve is applied to all 30 channels
([details](#the-tape-is-one-unit)).

Five findings shape the plan:

1. **Text matching finds the tape-to-GET offset without a prior.** The filename clock with the hum
   speed is wrong by 3–13 minutes: −770 s on T510104, −220 s on T510093 and −175 s on T510082.
   A ±30 min vote window absorbs this error.
2. **Audio cross-correlation at text-located windows gives ~10 ms precision.** Use it against
   both NARA and IA.
3. **Quindar bursts on the 30-track match NARA bursts to 20 ms** once the text curve predicts them.
   They also give an independent speed witness, and NARA calibrates the tone generator.
4. **The 30-track speed changes within a reel by much more than the hum median suggests.** On
   T510104 the 1971/playback rate drifts by about 1 % across hours 11–15, and by 2.4 % within
   hours 15–16. The hum fundamental falls from 59.14 Hz to about 57.5 Hz over the same hours.
   A single tape rate or a 30-min smoother cannot follow this. The curve has to be local where
   the anchors allow.
5. **Every tape shows two stagger groups 1.33–1.39 s apart.** Each SG-carrying channel falls in
   one of them. This measures the main-utterance stagger that the catalog lists as `unknown` for
   every track ([timing plan](14_2026-11-22_mission_timing_plan.md#how-to-interpret-a-speech-timestamp)).

SG reference coverage of the 30-track (union of tape time, per AGENTS.md) is **71 %** of the
delivered tape-hours by GET span. The rest has no SG reel: GET 7–9, 15–25, 40–49 and the gaps
inside reels, plus 653-AAA and after 286 h. Inside the covered spans, speech density decides the
usable share: 19–50 % of tape time is within 120 s of an anchor, and 24–65 % within 600 s. The
Quindar and hum rate witnesses carry the curve between anchored intervals.

## Implementation (stage 06c)

### Commands

```
uv run python scripts/run.py 06c_sg_retime.py status --all-tapes
uv run python scripts/run.py 06c_sg_retime.py run --all-tapes          # reference, anchors, refine, quindar, fit
uv run python scripts/run.py 06c_sg_retime.py anchors --tape A15_T510104 # or any single step
```

Apply a final plan with the existing 06b, to a separate output root:

```
uv run python scripts/run.py 06b_retime.py --tape TAPE --all-channels \
    --quindar-plan D:/Apollo_15_30-Track/stage_evidence/retiming/sg_fit/<edition>/tapes/TAPE/plan.json \
    --retimed-root D:/Apollo_15_30-Track/retimed_sg
```

The plan is in the stage-06 shared-plan layout. `quindar_tape.load_shared_plan` and
`target_map` accept it unchanged. 06b slices the same map for every channel at its own canonical
origin. On T510100, CH26 (origin +7.06 s) starts at output +6.99 s, which is the origin times the
local rate.

| Module         | Role                                                                                                      |
| -------------- | --------------------------------------------------------------------------------------------------------- |
| `sg_inputs.py` | Store paths, session and alignment state, chunk checksums, word cache, channel-origin audit, prior, IA/NARA placement |
| `sg_match.py`  | SG reference stream, 4-gram pairs, block vote, curve-free stagger, tape events                            |
| `sg_curve.py`  | P-spline tape curve with recorder segments, IA offsets `b_k`, rate witnesses, hold-out, smoothing choice  |
| `sg_audio.py`  | GCC-PHAT refinement, Quindar scan, generator calibration, matching and rate witnesses                     |
| `sg_stage.py`  | Editions, provenance, artifacts, joint fit, plans                                                         |

### Artifacts

Everything is under `D:/Apollo_15_30-Track/stage_evidence/retiming/` (`--root` overrides it).
Each edition folder is named by the digest of all its inputs and is written once, through a
temporary folder and a rename. A changed input gives a new edition beside the old one.

| Path                                             | Content                                                                                                                                             |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sg_reference/<edition>/`                        | `reference.npz` (SG words on GET), `manifest.json` (sources, segments, chunk checksums, alignment state)                                           |
| `<tape>/sg_anchors/<edition>/`                   | `pairs.tsv.gz`, `blocks.tsv`, `stagger.tsv`, `events.tsv` (the one input to the fit), `curve.json`, `manifest.json`                                 |
| `<tape>/sg_anchors/<anchors>/audio/<edition>/`   | `windows.tsv`, `events.tsv` (audio anchors), `stagger.tsv` (audio-refined stagger)                                                                  |
| `<tape>/sg_anchors/<anchors>/quindar/<edition>/` | `bursts.tsv`, `witnesses.tsv` (rate), `check.tsv` (positions against NARA)                                                                          |
| `sg_quindar/calibration-<digest>.json`           | NARA bursts on IRIG time and the generator calibration                                                                                              |
| `sg_fit/<edition>/`                              | `coverage.tsv`, `ia_offsets.tsv`, `ia_segment_revision.json` (proposal, never applied) and `tapes/<tape>/plan.json`, `curve.json`, `timing_map.npz` |
| `_cache/`                                        | Word arrays per transcript digest, Quindar scans per exact file identity                                                                            |

Anchor editions bind the 06a identity and the transcript digest of every channel, as well as
its 05b review checksum, origin and deflutter-plan checksum. Reference editions bind every IA
and NARA transcript digest, the drift-map checksum and the NARA respeed record checksums. A
transcript state is the checksum over its chunks' row checksums
(`transcript_read.identity_state`) and its count of chunks aligned with the current aligner.

### Word times and finality

The stage reads published rows (`transcript_read.identity_rows`). The word `sf`/`ef` values are
the CTC-aligned times once a chunk is aligned. `wsf`/`wef` keep Whisper's times and are not used.

- **Final editions.** An edition is final only when two conditions hold. Every transcript it
  read is aligned with the current aligner (`mission_align.align_id`). No transcription session
  is running on its store: an in-place alignment renames a chunk aside for a moment, so a read
  during a session is not stable.
- **Provisional editions.** If either condition fails, the step refuses, unless
  `--allow-provisional` is given. The edition is then named `provisional-…`, and its plans carry
  `status: provisional`, which `load_shared_plan` rejects. The manifest lists the reasons
  (`provisional_reasons`).
- **Concurrent alignment.** Each read is checked against the chunk checksums taken before it,
  and it is read again if a chunk was aligned in between.
- **Text sigma.** A text event is weighted with sigma 0.15 s when both word times are aligned
  (the aligner's pilot onset error is 0.076 s median and 0.23 s p90), and 0.5 s otherwise.
- **Status.** `status` lists the running sessions per store. `status --scan` also counts aligned
  chunks per tape. It reads every receipt, which took 43 minutes for all tapes while the aligner
  was writing to the same disk.
- **State on 2026-11-25.**
  - IA and NARA are aligned, and the reference is final: 378,588 words, all aligned.
  - The 30-track alignment session `20261125_132823_e63e9f` has done T510073–T510094 and is
    continuing.
  - When it ends, run `run --all-tapes` once.

### Departures from the plan

1. **Block vote.**
   - _Offset and slope._ Each 20-minute block takes the best line, offset and slope (±12 %),
     not a flat 2-s mode. The rate changes too fast within a block for a flat mode: 2–5 % late
     in the reels of T510104, T510105 and T510107.
   - _Clusters._ A mode needs votes from at least two separate 30-s stretches of tape. One
     phrase heard on 24 channels produces hundreds of votes. With only two stretches, the slope
     is forced to zero, because any slope joins two points.
   - _Outliers._ The 5-block running median is replaced by a continuity test. A mode is dropped
     when its neighbours continue each other around it. These are replays and second loops, for
     example T510089 hour 4.2 at −263 s between −327 s and −324 s.
   - _Recorder stops._ A break between runs is a stop only if GET steps _forward_ and both sides
     are confirmed. A backward step cannot be a stop. At such a break the run with fewer votes is
     dropped.
2. **Stagger measured without the curve.**
   - _Method._ The plan proposed taking each channel's median residual to the pooled curve.
     That lets a per-interval stagger absorb tape motion that every channel shares. Instead,
     stagger comes from the same SG word heard on two channels (`t_c − t_j`), chained from the
     reference channel, which has the most pairs. It is measured per tape and per 30-minute
     interval.
   - _Fallback._ Channels without such overlap join the group their curve residual falls in
     (±0.3 s), or are excluded from positions.
   - _Mixed channels._ A channel is mixed if more than 25 % of its differences lie away from
     the median.
3. **Smoothing scored on IRIG time only.**
   - _The problem._ IA positions are on each reel's own clock. Scored against them, the hold-out
     chose the smallest penalty on T510093. The curve then followed the IA recorder's wow:
     0.3 % rms short-term rate, up to 1.9 %.
   - _The rule._ Held-out positions are scored against NARA only, on tapes with at least 200
     NARA events. IA-only tapes take the median choice of the NARA tapes (see 11).
   - _The grid._ It starts at λ = 1. Below that the curve follows word-timing noise between
     the 120-s knots.
4. **Rate bounds** (open decision 3). The hard bounds are 0.85–1.10 and 10 % per hour. The
   penalty is raised only beyond them. The plan's 0.94–1.06 and 4 % per hour are review flags
   in each plan (`review_flags.rate`). Late-reel slow-downs reach rate 0.91 on T510107. That
   matches the falling hum the plan describes on T510104.
5. **Frame of IA evidence.** A drift-map GET is `Φ + b_k`. Audio windows and Quindar matches
   against IA are therefore predicted in that frame, and they keep their `b_k`. The first
   version did not do this, and its audio lags on T510093 showed the absorbed `b_k`: median
   −2.13 s, against +0.006 s after the fix.
6. **Plan placement.** A plan stores the speed map 06b renders: output = ∫rate. It also stores
   the GET placement as `GET = output + get_equals_output_plus_s` per recorder segment, the same
   convention as the IA and NARA respeeds.
7. **Channel origins.** The 05b review's own planning folder has moved on some tapes, for
   example T510082. The bound plan is then read from `deflutter_maps/<tape>/plan.json`, and only
   if its checksum is the one the review names. The audit of all 1,139 channels refuses none. It
   finds the same seven offset tapes as the table below.
8. **Hum witness.** Not implemented. The census has no hum time series on the 05b axis, and
   stage 03 removed the hum from the 05b audio. Quindar is the only rate witness.
9. **Stagger groups from well-measured intervals.** With aligned times, one channel interval
   scatters by about 0.01 s. Groups are therefore tight clusters: a gap of more than 0.15 s
   separates them, and only intervals with a MAD of at most 0.15 s count. A group must hold at
   least 5 % of those intervals, and at least three. The plan's loose clustering formed spurious
   groups from single noisy intervals (−1.96 s and +1.20 s on T510089), and the capture-offset
   flag then matched them.
10. **Runner-up block line.** Each block also keeps the best line of its remaining pairs. If the
    best line continues neither neighbour and the runner-up does, the block takes the
    runner-up. On T510089 hour 4.2, a second loop outvotes the true line: 4,050 of 7,650 pairs at
    −265 s, between neighbours at −327 s. Dropping that block, as in departure 1, lost its true
    votes. Its slope also let it fake a recorder stop.
11. **Smoothing for IA-only tapes.** IA-only tapes take the median λ of the NARA tapes only when
    at least three NARA tapes are in the fit. With fewer, they use the default (λ = 10⁴). With a
    single NARA tape (T510073), λ = 10⁶ made the IA tapes worse than their own anchor fit.

### Results with aligned word times (scratch root, provisional)

These are the five tapes that were already aligned on 2026-11-25. They are provisional, because
the 30-track alignment session was still running. Reference: all IA and NARA reels, aligned.

| Tape (reference)    | Kept pairs | Events | Stagger groups (s) | Anchor residual median (s) | Held-out median / p95 (s) | Audio anchors |
| ------------------- | ---------: | -----: | ------------------ | -------------------------: | ------------------------- | ------------: |
| T510073 (NARA + IA) |     35,934 |  8,593 | −1.38, 0.00        |                      0.075 | 0.177 / 1.6               |           219 |
| T510082 (IA)        |     67,122 |  6,252 | −1.38, 0.00        |                      0.033 | 0.130 / 0.86              |           118 |
| T510088 (IA)        |    137,489 | 13,472 | 0.00, +1.38        |                      0.049 | 0.122 / 2.0               |           284 |
| T510089 (IA)        |    130,601 | 17,391 | 0.00, +1.38        |                      0.062 | 0.239 / 2.9               |           326 |
| T510093 (IA)        |    266,108 | 35,943 | 0.00, +1.39        |                      0.221 | 0.96 / 5.4                |           558 |

- **Pairs and stagger.**
  - *Pairs.* There are 24–47 % more kept pairs than with Whisper's times, and every pair is
    aligned on both sides.
  - *Precision.* The stagger of one channel interval now scatters by about 0.01 s. Before, the
    scatter was 0.05–0.2 s.
  - *Groups.* The groups sit at 1.38 ± 0.01 s on every tape.
  - *Flags.* No capture-offset flag remains. The T510089 CH22–CH24 and CH27 flags from the
    Whisper-timed run are withdrawn.
- **IA is now the limit.** On IA tapes the anchor residuals (0.03–0.22 s) and held-out errors
  (median 0.12–0.96 s) are much larger than the word-time error. They come from the IA reels'
  own clocks: recorder wow that a polynomial drift map cannot follow, and the whole-second
  printed GETs the map is fitted to.
- **Drift map refitted (2026-11-25).** `catalog/sg_drift` (`sg-drift-v2`) was refitted from the
  aligned IA transcripts
  ([mission data, step 5](16_2026-11-23_mission_data_ingestion.md#space-to-ground-reels)). The
  numbers above are with the refit, which left this pilot equal or better:
  - T510073: residual 0.094 → 0.075 s, held-out p95 2.0 → 1.6 s.
  - T510093: held-out p95 5.7 → 5.4 s.
  - T510082 and T510089: unchanged.
  - T510088: held-out median 0.147 → 0.122 s, p95 1.9 → 2.0 s.

  A first refit gave 561-AAA a three-anchor segment 54 s *earlier* than its neighbour. That
  raised T510089's held-out p95 to 7.3 s, and through the curve it raised false capture-offset
  flags. The drift fit now drops such replay islands. The capture-offset flag is now
  curve-free: it compares each channel's timing of the same SG words with channels of known
  stagger.
- **Not yet measured with aligned times.** These wait for the 30-track alignment to finish:
  - the NARA tapes (T510095 onwards);
  - Quindar matching;
  - archive coverage;
  - the IA segment offsets proposed by the joint fit (`ia_segment_revision.json`).

  The generator calibration (key 2526.7 Hz, unkey 2477.6 Hz, from NARA bursts on IRIG time) does
  not depend on word times.

### Review flags

- **Late-reel slow-downs.** The Whisper-timed run showed the rate falling below 0.94 near the
  ends of T510105 and T510107. Plans flag this in `review_flags.rate` (open decision 3).
- **Open decisions 1–4.** These remain open. The plan's reference group is the group of the
  channel with the most pairs. The plan records the other groups as metadata
  (`get_placement.stagger_groups_s`) and applies no shift per channel.

## Data the stage uses (all existing, none re-computed)

| Input              | Location                                                                                     | Time domain                                                |
| ------------------ | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| 30-track ASR (06a) | `D:/Apollo_15_30-Track/timing_transcripts/asr-c1198b48a6d3/sources/tape/<tape>/CHnn/`        | canonical post-05b tape s = `canonical_origin_s + sf/rate` |
| 30-track audio     | `D:/Apollo_15_30-Track/defluttered_shared/<tape>/CHnn/deflutter.wav` + `review.json`         | local 05b s; add `start_seconds` once                      |
| IA SG ASR          | `F:/Apollo_15_data/processing/sg/timing_transcripts/asr-c1198b48a6d3/sources/sg/<reel>/`     | reel source s                                              |
| IA GET map         | `catalog/sg_drift/sg_drift_maps.json`                                                        | `GET = t + D(t) + c_segment`                               |
| NARA ASR (mix)     | `F:/Apollo_15_data/processing/sg_nara/retimed_transcripts/asr-08ce474a85b7/sources/sg_nara/` | respeed output s                                           |
| NARA GET           | `F:/Apollo_15_data/processing/sg_nara/respeed/255-PAOa-<tape>/<tape>.json` `segments`        | `GET = u + get_equals_output_plus_s`                       |
| Coarse prior       | filename start stamp and per-tape hum (`research/readme_figures/inventory.json`)             | `GET ≈ start − launch + t·hum/60` (minutes off)            |
| Quindar detector   | `quindar_speed.detect` / `quality_events` (stage 06)                                         | any                                                        |

Select the IA transcript identity by `content_sha256` against the drift map's `sha256`.
`md_sg.reel_identity` fails on 553-AAA (two identities, neither matching by size and mtime). This is
fixed by checksum selection.

## The tape is one unit

The 30 channels of a tape are one moving tape. This stage follows the same rule as stage-05
flutter analysis (`05_analyze_flutter`, [shared map guide](08_2026-11-15_shared_deflutter.md)).
It finds one tape-motion correction at each canonical tape time, and applies it to every channel.

- **One time axis.** Every observation is placed on canonical post-05b tape seconds before it is
  used. For a transcript word, that is `canonical_origin_s + sf / rate` from the 06a `source.json`.
  For audio, it is the 05b `review.json` `start_seconds` plus local seconds. These origins already
  contain the stage-02 capture offsets (55 channels on 7 tapes, 0.98–11.87 s; see
  [channel offsets](#channel-offsets-from-earlier-stages)) and the shared 05b map. Add the origin exactly once. Never re-measure or re-fit capture offsets in this stage.
- **Search every channel, pool the evidence per tape.** Text pairs, audio windows and Quindar
  matches are found on whichever channels carry the SG loop. They then join one tape-level
  evidence set. An anchor found on CH18 constrains the curve for CH01–CH30 at that tape time.
- **No channel is gated on its own evidence.** A channel with no SG loop, no Quindar or no speech
  (IRIG, quiet loops) still gets the tape's correction wherever any channel supports it. Channels
  are not fitted, duration-anchored or bypassed one by one.
- **One curve, one application.** The fit gives one `Φ_tape` per tape interval. It is joined once
  and applied by 06b as the same inverse map to every channel. Each channel keeps its existing
  canonical offset, so every channel moves together.
- **Coverage is the union.** A tape second counts as supported if an anchor on any channel
  supports it. Coverage is not a sum of channel-seconds, and not one channel's share.
- **Duplicates count once.** The same SG utterance or Quindar burst heard on many channels is one
  physical event. Collapse it to one vote per event before fitting, as `quindar_tape` does, so that
  heavily monitored passages do not outweigh the rest. Their agreement across channels is kept as
  that event's quality measure.
- **Stagger is a placement, not tape motion.** The 1.33–1.39 s groups (step 3) say where the SG
  copy sits on a channel relative to the tape reference. They play the same role as the stage-02
  offset when an anchor is transferred to the reference time: `t_ref = t − stagger`. They are
  measured from that channel's own SG matches against the pooled curve. They never create a
  channel-specific map.
  - A channel with too few matches to measure its stagger contributes an anchor only if the anchor
    falls clearly in one group (within ±0.3 s of 0 or of the group delay).
  - Otherwise the anchor is kept as evidence but excluded from positions.
- **Reuse.** Reuse saved evidence from every channel: 06a transcripts, Quindar surveys, census hum
  and stage-04 timing records. Completed artifacts stay immutable.

### Channel offsets from earlier stages

Stage 02 measures the constant start offset of each original WAV on its tape's clock. These are
the digitizer capture offsets, `delayed_frames` in the capture-alignment report. Stage 05 applies
them through `tape_coordinates.aligned_inventory`:
`start = acquisition_start − delayed_frames / rate`, then rebased so that the earliest corrected
start is 0. The result is each channel's canonical origin.

The origin is recorded in four places, and this stage must check that they agree before using any
channel:

| Record                                                       | Field                                                              |
| ------------------------------------------------------------ | ------------------------------------------------------------------ |
| Stage-02 report (bound in `deflutter_maps/<tape>/plan.json`) | `alignment.path`, `sha256`, `action`, `origin_shift_seconds`       |
| `defluttered_shared/<tape>/CHnn/review.json`                 | `start_seconds` (canonical) and `source.acquisition_start_seconds` |
| 06a `source.json`                                            | `identity.canonical_origin_s`, `identity.review_sha256`            |
| This stage's anchor records                                  | origin used, and the review and plan checksums                     |

**Audit (2026-11-25), all 1,139 published 05b channels.** Every channel has a 06a transcript on its
current 05b review (`review_sha256` matches). Every 06a origin equals the review's
`start_seconds`. 55 channels on 7 tapes have nonzero origins. All acquisition starts are 0, so the
whole offset comes from stage 02. The other 31 tapes are all zero.

| Tape    | Channels and canonical origin (s)                          | Stage-02 action, origin shift |
| ------- | ---------------------------------------------------------- | ----------------------------- |
| T510073 | CH01–20, CH25–30: +0.9785 (CH21–24 are the earliest, at 0) | retrace, −0.9785 s            |
| T510088 | CH17–19, 27, 29: +11.86675; CH25, 26, 28, 30: +11.86825    | retrace                       |
| T510089 | CH30: +3.706125                                            | retrace                       |
| T510099 | CH25–30: +8.731125                                         | retrace                       |
| T510100 | CH25–30: +7.06075                                          | retrace                       |
| T510109 | CH25–30: +6.55475                                          | retrace                       |
| T510111 | CH07: +6.799375                                            | retrace                       |

Rules for this stage:

- **Use the canonical origin for every observation.**
  - Words: `canonical_origin_s + sf / rate`.
  - Audio: `review.start_seconds + local seconds`.
  - Quindar: the survey's canonical `tape_seconds`.

  Refuse a channel whose 06a identity, review and plan alignment disagree. Do not fall back to
  local WAV time or to the acquisition start.

- **Write anchors in canonical tape seconds.** Each anchor keeps its source channel and local
  time too. The 06b curve is then a function of canonical time and applies to each channel at its
  own origin, as the 05b map does. 06b keeps the existing offsets and does not re-align channels.
- **Keep capture offsets separate from stagger.** Capture offsets are digitizer start differences
  of 1–12 s, already removed. Stagger is the 1.33–1.39 s placement of the SG copy measured
  after that. A residual near a capture-offset value (for example 7.06 s on T510100 CH25–30)
  means a channel was placed on local time by mistake. Flag it; never fit it as stagger.
- **Track stale editions.** If stage 02 is re-run for a tape (selective recovery in the
  [shared map guide](08_2026-11-15_shared_deflutter.md#capture-alignment-and-selective-recovery)),
  its new 05b reviews give new 06a identities and new origins. Anchors bound to the old review
  checksum become stale and are rebuilt for that tape only.

**Check on an offset tape (T510100 against NARA 640–643, throwaway prototype).**

- **Canonical origins.** CH26–29 (origin +7.06 s) fall into the same two stagger groups as the
  zero-origin channels: −0.27/−0.20 s and +1.16/+1.19 s, against −0.19…−0.14 s and
  +1.14…+1.21 s.
- **Local times.** The same channels would sit 6.7–8.2 s off.
- **Independence.** The offset channels split across both stagger groups, so capture offsets and
  stagger are independent.
- **Pilot tapes.** T510082, T510093 and T510104 have zero offsets, so the pilots above did not
  exercise this path.

The prototypes already worked this way. Words and audio were placed on canonical tape seconds
through the recorded origins, and all channels were pooled into one fit per tape. The per-channel
stagger was solved inside that pooled fit.

## Method

### 1. SG reference stream (once per mission)

- Place every SG ASR word on GET. IA words go through their reel segment's map; words outside a
  placed segment are dropped, as are 567/568-AAA (rate only). NARA words go through their tape's
  segment constant. 653-AAA has no timecode and is excluded.
- Keep the source, reel/tape seconds and word probability, and drop words with hard flags
  (`collapsed_timing`, `long`, …, as in `md_sg.HARD_FLAGS`).
- Where IA and NARA overlap (GET 189–199, 608 against 540), keep both as separate observations.
  The overlap calibrates IA against IRIG (step 5).

### 2. Text anchors, searched on every channel of the tape

Tokenize as `md_sg` does (`md_common.tokens` + `md_reconcile.comparable`).

- **Grams.** Consecutive 4-token grams of the channel ASR whose words fall within 6 s. Drop grams
  repeated more than 6 times on the channel, grams with a single distinct token, and
  `thank you thank you`.
- **Distinctiveness.** Keep a gram if it occurs at most 3 times in the SG stream within the tape's
  GET span ±1.5 h.
- **Vote.** Each channel gram gives a pair `(tape t, SG GET g)` with `d = g − prior(t)` inside
  ±1,800 s. Per 20-minute block, take the mode of `d` (2 s bins, 3-bin smoothing, at least 6
  votes). Drop block modes more than 20 s from the 5-block running median. Keep pairs within 6 s
  of the interpolated mode curve.
- **Prototype result.** 83–94 % of a carrying channel's pairs land on its mode. The rest are
  common phrases, replays and repeated readouts.

### 3. Per-channel stagger and the pooled tape curve

- **Pooling.** All channels' pairs, on canonical tape seconds, form the tape's single evidence set.
  Stagger only transfers each pair to the reference time (see [the tape is one unit](#the-tape-is-one-unit)).
- **Iteration.** Iterate: pool all channels, fit a robust smooth of `d` against `t`, and take each
  channel's median residual as its stagger. The reference channel is the one with the most pairs.
- **Prototype result.** Stagger clusters at 0 and −1.33…−1.39 s on all three tapes. The
  per-channel |r| median is 0.09–0.15 s on NARA and 0.1–0.5 s on IA.
- **Mixed channels.** A few channels sit between the groups (T510093 CH07 −0.66, CH22 −1.04,
  T510104 CH19 −0.78). They carry both copies, or the copy changes along the tape (T510104 CH18
  correlates at 0 s near hour 15 and at −1.35 s elsewhere). Fit stagger per channel per interval.
  Do not fit it per tape.
- **Echo direction.** The stagger is the main-utterance placement. Stage 04's echo direction is a
  separate record (AGENTS.md) and is not changed here.

### 4. Audio refinement (precision step)

- **Window.** At each text-anchored window (40 s), resample the 30-track channel onto the GET axis
  using the local text offset and rate. Band-pass both signals to 300–1,800 Hz and apply Tukey
  tapers. Correlate against the SG reference (NARA mix, or the IA reel mapped through its drift
  map) with GCC-PHAT over ±5 s.
- **Prototype result.**
  - Detection is clean where SG speech is present: the peak is 10–42× the median, against about
    6× in silence.
  - Lags reproduce the text stagger to within 10–20 ms: 0.009/0.011 and −1.340/−1.342 s on
    T510104; 0.007/0.007 and −1.344/−1.345 s on T510093.
  - At T510104 hour 15.1 the text curve was 0.13–0.18 s off, and the correlation corrected it.
- **Tape-wide result.** A window accepted on any channel is a tape anchor at its canonical time.
  Refining it on further channels of the same group adds agreement, not extra votes.
- **Acceptance.** Accept a window only if the peak/median ratio is at least 9 and the second peak,
  more than 0.2 s away, is weaker by a set margin. Repeat across the channels of one stagger group
  to vote down crosstalk copies.
- **Result per window.** A tape-time/GET pair with about 10 ms scatter. For IA this is relative to
  the IA reel's clock and inherits that reel's map error. Step 5 resolves that error.

### 5. One curve per tape, fitted jointly with the SG set

The IA maps are only as good as the printed GETs (residual steps of 3–4 s at TEC page boundaries,
segment constants from few anchors). The 30-track is continuous over 17 h and crosses several IA
reels and segments. The two sets therefore constrain each other:

- **Model.**
  `GET = Φ_tape(t_ref) + b_k`, where `Φ_tape` is the smooth, monotone tape curve and `b_k` is a
  nuisance offset for each IA segment `k`.
  - `b_k` is 0 for NARA segments, which are IRIG and held fixed.
  - The IA prior on `b_k` is its `cv_mae_s`.
  - `t_ref` is the tape second in the reference stagger group, so `t_ref = t − stagger(channel, interval)`.
- **Network.** Overlapping tapes share `b_k`: HR1 and HR2 run simultaneously, and each recorder's
  upper and lower reels overlap by about 30 minutes. NARA and IA overlap at GET 189–199 and in
  608/540. A tape crossing IA reels 571→572 then measures their relative constant. The prototype saw
  +0.5 s on T510093 at the 572/573 reels against 0.0 s on 571, a step the smooth tape curve cannot
  make by itself.
- **Smoothness.**
  - Fit `Φ` as a penalized spline in rate (λ chosen by withheld 30-minute blocks, like
    `nara_tape_map` and `quindar_retime.curve_model`).
  - Bound the rate to physical values. The prototype needs up to about 2.5 % change within an hour
    at T510104 hours 15–16.
  - A 30-minute Gaussian smoother is too stiff there: one held-out block is off by 28 s.
- **Rate witnesses between anchors.**
  - **Quindar.** Use the stage-06 Quindar pitch ratio (from `quindar_survey`/`quindar_tape`) as a
    rate observation. Calibrate the tone generator from NARA bursts on IRIG time, not from the
    nominal values (step 6).
  - **Hum.** The 1971 hum fundamental from the stage-05 census is a second rate witness, available
    on nearly every track. It is noisy (the prototype's single 300 s FFTs disagreed by up to 1 Hz
    between harmonics), so give it a low weight.
  - **Gaps.** Across gaps with neither witness, hold or ramp the rate as `nara_tape_map` does. Show
    the extrapolated span.
- **Recorder stops.** A step in `d` larger than the rate bounds allow is a recorder stop or splice.
  Start a new segment there instead of bending the curve.
- **Validation.**
  - Hold out whole 30-minute blocks of anchors and report the held-out median and p95 in seconds.
  - Report NARA-anchored and IA-anchored blocks separately.
  - Use the Quindar bursts matched to NARA as an independent check of positions. They never enter
    the position fit.

### 6. Quindar matching to SG (independent check and calibration)

- **Matching.** Detect bursts on the 05b channels and on the SG audio with the stage-06 detector.
  Predict each 30-track burst's GET from the curve and pair it with an SG burst within the
  tolerance and with the same tone class (key/unkey, after removing the 30-track speed).
- **NARA prototype (T510104, 8 channels).** 204 matches, 84 % within 0.25 s. The group-0
  channels have a residual of +0.005, +0.004, −0.002 and +0.011 s, with a MAD of 14–21 ms. The
  −1.35 s group channels scatter (MAD 0.6–1.1 s). Their bursts are partly bleed from the
  neighbouring stack at group-0 time, which is consistent with the stage-04 crosstalk geometry.
  Use Quindar position checks on the stagger group in which the burst occurs, not on the channel's
  speech group.
- **IA prototype (T510093).** Matching is weak: 32 pairs, MAD 0.4 s. Key and unkey bursts 1.5–2.5 s
  apart are easily mispaired before the curve is refined by step 4. Re-run after step 4.
- **Generator calibration.** The NARA bursts on IRIG time give key 2524.5–2527.2 Hz and unkey
  2476.5–2478.1 Hz on the well-populated tapes (645, 646, 649). Spec is 2525/2475 Hz. Replace the
  nominal in `quindar_speed` fits with these measured values and their spread. This is about the
  0.1 % bias that `archive/2026-11-26_reorg/reports/sg_quindar/README.md` could not resolve.

## Where it plugs into stage 06

_As built:_ the step is `06c_sg_retime.py`, the joint fitter exists, and the plans go to 06b
through `--quindar-plan`. The clock-anchor increment below was therefore not needed. A tape
without a curve (T510111, T510112) gets no SG plan and is bypassed.

- **New evidence step `06c`**, next to the Quindar survey. It writes per-tape
  JSON/TSV under `D:/Apollo_15_30-Track/stage_evidence/retiming/<tape>/sg_anchors/`:
  - text pairs, accepted anchors and per-channel/interval stagger, with each anchor's source
    channel, its canonical tape second and the origin used (stage-02/05b review checksum)
  - one tape-level event table, with duplicates across channels collapsed, which is the only
    input to the fit
  - audio-refined windows and Quindar matches
  - all bound to the 06a settings ID, transcript identity checksums, 05b review checksums, the
    drift map checksum and the NARA respeed record checksums.

  Reuse completed artifacts, and never rewrite earlier ones.

- **Joint fit.** One fit across all tapes, because the IA nuisance offsets are shared. It writes
  one frozen plan per tape in the existing `shared_plan` layout. Each plan holds `Φ_tape`, its
  support intervals, its held-out errors and the `b_k` used. The IA segment corrections are
  written separately, as a proposed revision of `catalog/sg_drift`, for review. They never
  silently replace the accepted map.
- **06b application.** `06b_retime.py` applies the same inverse map to every channel and keeps the
  existing offsets (AGENTS.md shared-tape rule). This includes channels that contributed no anchor.
  It bypasses only where no channel supports the curve, and reports coverage as the union of
  supported tape time.
- **Clock anchors.** The `--clock-anchors` input of `build_shared_plan` already accepts
  authenticated anchors carried through the 05b map. The SG anchors can enter through that path
  first, before the joint fitter exists, as a lower-cost first increment.

## Cost

All steps run on saved transcripts and audio.

| Step             | Prototype time                                                                         | Archive projection          |
| ---------------- | -------------------------------------------------------------------------------------- | --------------------------- |
| Text matching    | 30–60 s per tape (first load of 30 channel transcripts; a cache then makes it instant) | under 1 h                   |
| Pooled fit       | about 35 s per tape                                                                    | under 30 min                |
| Quindar scan     | 25 s for 2 channels × 17 h; 37 s for 5 NARA tapes                                      | about 2 h for every channel |
| Audio refinement | about 1 s per window per channel                                                       | hours at 1 window/min/group |

## Open decisions for the owner

1. **Reference stagger group.** One tape map must define GET for one head-stack group. Applying it
   to all channels leaves the other group 1.33–1.39 s off GET. AGENTS.md forbids per-channel maps.
   Should a constant per-channel shift by the measured stagger be allowed as a separate,
   documented step? Or should it stay metadata, as the timing plan already describes for speech
   timestamps? Which group is "on time" is a convention unless the head geometry is established.
   The Quindar/bleed pattern suggests the groups are the two head stacks.
2. **IA map feedback.** Should the joint fit be allowed to propose revised IA segment constants
   (`b_k`) back to `catalog/sg_drift` as a reviewed revision?
3. **Within-reel speed changes.** The size of the change on T510104 hours 11–16 (up to 2.5 % in an
   hour) should be confirmed by listening or by the census hum before rate bounds are fixed. It
   looks physical, because the hum fundamental follows it.
4. **Uncovered spans.** GET 7–9, 15–25, 40–49 h, 653-AAA and after 286 h have no SG reel. There the
   curve relies on Quindar and hum alone, as stage 06 does today. NARA 609–639 are not digitized.
   Ordering them would close most of the IA gaps with IRIG-timed audio
   ([inventory](18_2026-11-24_audio_inventory.md)).

## Prototype record (not kept)

The prototype scripts ran under `%TEMP%/sg30` with `uv run --project F:/_repos/Apollo_15` and only
read the saved artifacts above. They were deleted afterwards. The steps were:

1. SG span coverage per tape
2. 4-gram text pairs per channel
3. pooled fit with stagger and held-out blocks
4. Quindar scans of 05b channels, NARA mixes and IA reels
5. Quindar matching and pitch comparison
6. 300 s hum FFTs
7. GCC-PHAT refinement

Further detail on the numbers quoted above:

- **T510104.** The offset to the prior was −780 s in hours 0–1, −768 to −786 s in hours 8–11,
  −914 to −971 s in hours 15–16 and −1,104 s at hour 16. The 30-minute-smoother hold-out gave a
  median block bias of 0.50 s, p90 3.2 s and a maximum of 28 s, from the stiff smoother in the
  rate-change hours. The NARA-matched Quindar pitch ratio was 0.9891 against the local text rate
  of about 0.9885.
- **T510093.** Anchors cover hours 2–14. 567/568-AAA give no GET for hours 0–2. Hold-out median
  block bias 0.69 s, p90 3.6 s. The offset steps down by 30–40 s over hours 11–14, another
  late-reel rate change.
- **T510082.** Anchors cover hours 1–8 (IA 551–553). Hold-out median block bias 0.29 s. The rate is
  2,500 ppm faster than the hum prior.
