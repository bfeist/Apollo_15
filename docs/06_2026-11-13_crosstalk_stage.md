# Stage 04: crosstalk removal

The processing order is **03 denoise → 04 crosstalk → 05b shared de-flutter →
06 optional Quindar retiming → 07 speech normalization**. Stage 05a builds the
shared map independently from saved original-tone evidence. Crosstalk is removed
before retiming so measured donor/main/echo delays retain their source coordinates.
Normalization follows subtraction because removing a repeat changes the level.

Stage 02 measures each original WAV's start on the tape clock. Stage 04 uses
the signed difference between target and donor starts to locate the donor,
including adjacent channels. It then measures the local fractional delay and
validates the weaker repeat within the target. The target-only repeat scan,
the tape's established removal direction, and the v2 denoised WAVs can be
reused. The [selective recovery command](08_2026-11-15_shared_deflutter.md#capture-alignment-and-selective-recovery)
rechecks saved donor candidates and publishes a separately bound release.

Stage 04 promotes the two-path method used in the November 8 Audition comparison.
The owner reported substantial improvement, with residual crosstalk audible.
Feedback did not distinguish B from C. The official default uses **B's frozen
transfer coefficients**, refitted on the current denoised calibration interval;
it retains fractional local delay tracking and the withheld-block main-only versus
main-plus-echo check. The numerical model is shared with the research exporter.

The renderer works from a bounded evidence catalog. The original listening
catalog establishes earlier-copy removal for T510083 CH17
and CH20, using CH19, only at canonical **13569.10–13570.48 seconds**. Other
intervals and channels pass through unchanged. Wider coverage requires additional
within-target repeat evidence. An interchannel match alone never authorizes removal.
See [the research record](03_2026-11-15_crosstalk_research.md) for scope and listening limitations.

## Archive discovery and processing

From the Windows checkout, run the archive coordinator with a named evidence
directory. Repeating the command resumes from verified worker receipts and
completed tape catalogs.

```powershell
uv run python -u scripts/30-track/04_crosstalk/repeat_release.py --run repeat-capture-aligned
```

This full discovery command is for a fresh release. To repair the November
archive, use the selective recovery command above. A pipeline render accepts
the new qualification directory via `--crosstalk-evidence-root` and resolves
the catalog for each tape and channel.

The coordinator inventories numbered channels with completed stage-03 manifests.
It copies one tape at a time from D to `F:/Apollo_15_30-Track/work/crosstalk`, verifies
the source and copy against the stage-03 WAV hash, and keeps up to three tapes
available on the SSD. The default six Windows slots include one for periodicity
qualification. Six Genus workers launched over SSH at `bfeist@192.168.0.4`
share the discovery queue with Windows. Workers take channels from any
available tape. Each channel can enter donor validation as
soon as its scan finishes. Larger candidate lists enter the validation queue
first. `--local`, `--genus` and `--tapes` select the host and tape limits.
Worker receipts include input and implementation hashes and the executing host.

During the November 18 `repeat-v2` run, remote reads of WAVs staged on F
became a bottleneck. The remaining discovery work uses
`cluster/native_cluster.py` to assign whole tapes to each host. Windows runs
two discovery workers on F. Genus copies its assigned tapes sequentially from
D to its own SSD and verifies each WAV against the stage-03 hash. Six Genus
workers read those local WAVs and return their scan arrays, validation results
and receipts. Windows verifies the results and seals the tape before Genus
reclaims its copy. This handoff resumes from the existing receipts and feeds
the same qualification and rendering process.

Discovery searches successive quarter-second windows of the target for another
occurrence 0.8–1.9 seconds later. The coarse speech-band search is followed by
native 8 kHz fractional lag measurement. Validation requires another nearby
window at a consistent delay, matching energy across multiple speech bands,
and a distinct completed donor. Both target occurrences are searched
independently over signed donor offsets. The paths must point to the same
donor occurrence and agree across multiple bands. Workers consider donors in
order of channel distance and retain the first qualifying reference. Channel
start offsets are included throughout.

Before publication, the qualification pass rejects measured paths outside the
renderer domain. It removes stationary 60 Hz harmonics from the analysis
excerpts, requires the target and donor matches to survive across speech bands,
and compares the paired occurrence with nearby shifted controls for periodic
signals. Each new interval needs two independently observed speech minutes on
the same tape within 30 minutes and 15 ms of the measured separation. The
minutes must be separate from the proposed event and from one another. A
timing-only target needs witnesses from two distinct ordinary target channels.
The renderer still uses the fractional path measured for the individual
target and donor. The qualification filter is used only for measurement.

Established tape/channel directions take precedence. For new directions,
the level of the donor-matched waveform in each target occurrence contributes
one median vote per minute. Each window also records the total level ratio
so the influence of overlapping speech can be inspected.
A vote requires 5 dB of separation. At least three voting minutes and 85%
agreement establish a direction. Ambiguous channels bypass. The catalog protects
the identified main occurrences and the original reviewed phrase. It can join
touching, consistent windows for the same donor into at most 0.75 seconds of
support. The renderer tracks fractional delays again and fits on the current
denoised audio. New events must also reduce the projection onto the paired
target occurrence on phrase samples held out of coefficient calibration.
These checks measure waveform agreement. They can miss faint repeats and
cannot establish perceptual preservation of all overlapping speech.

All selected tapes finish discovery and qualification before audio publication
begins. Each final channel catalog passes the official renderer's evidence
reader. Qualification keeps temporary hard links to verified SSD inputs where
available and releases them after each tape. During
rendering, Windows holds the source and verified staged WAVs read-only and
publishes each channel from a private attempt directory. The output retains
the original frame count, offset and gain. A fully bypassed channel must
match the stage-03 decoded-sample hash. When stage 03 supplies its verified
sample hash and peak, stage 04 can publish an independent byte-identical copy
after reading back and checking its full file hash.

- Detection, preliminary catalogs, worker receipts, and the final `complete.json`
  live under `D:/Apollo_15_30-Track/stage_evidence/archive/crosstalk_detection/<run>`.
- Final channel catalogs and the per-tape `qualified.json` decisions live under
  `D:/Apollo_15_30-Track/stage_evidence/archive/crosstalk_qualification/<run>`.
- Archive WAVs and stage-04 manifests live under
  `D:/Apollo_15_30-Track/crosstalk_removed/<tape>/CHnn`.
  Stage 05b uses this root by default for individual channels.
- The original standalone and pipeline stage-04 defaults continue to use the
  listening catalog described below.

## Commands

Preview a single channel and its downstream stages:

```powershell
uv run python scripts/pipeline.py plan --stages 04,05b,07 --tape A15_T510083 --channel 20 --plain
```

Once the target and donor v2 files are complete, run stage 04 on a channel or tape:

```powershell
uv run python scripts/pipeline.py run --stages 04 --tape A15_T510083 --channel 20 --plain
uv run python scripts/pipeline.py run --stages 04 --tape A15_T510083 --all-channels --plain
```

`--all-tapes` also works. Batch mode processes one stage at a time. Single-channel
mode can run `--stages 04,05b,07` (or add `06`) once the shared map is available.
V2 denoising retains its separate production console and frozen runtime.

Defaults:

| Setting                                     | Path                                                           |
| ------------------------------------------- | -------------------------------------------------------------- |
| Target and donor inputs, `--denoised-root`  | `D:/Apollo_15_30-Track/denoised`                               |
| Output, `--crosstalk-root`                  | `D:/Apollo_15_30-Track/crosstalk_removed/<tape>/CHnn`          |
| Saved research, `--crosstalk-evidence-root` | `reports/crosstalk_echo_Audition_20261108_v2` in this checkout |

The standalone command offers an input-validating preflight without writing:

```powershell
uv run python scripts/30-track/04_crosstalk/04_crosstalk.py --tape A15_T510083 --channel 20 --dry-run
```

The standalone evidence argument is `--evidence-root`, and `--output` selects
the exact channel destination. Pipeline `plan` only prints commands; standalone
`--dry-run` validates manifests, saved evidence, and WAV headers but does not hash
the full source audio. Rendering verifies the audio hashes too.

## Processing and outputs

Both target and donor must be completed **v2 denoised** files, tied to the canonical
original inventory. A donor is always read from stage 03, never from another
stage-04 output. Processing order across channels therefore cannot alter results.
The earlier/later direction comes from the saved tape/channel registry and cannot
flip locally. The stage rechecks the repeat within the current target, separately
tracks donor-to-main and donor-to-echo alignment, fits both paths together, and
subtracts only the echo prediction in the evidenced interval with 80 ms fades.
The saved echo polarity and delay search are taken from measurements inside
the supported phrase; weak held paths outside it cannot widen that search.

The within-target screen requires absolute correlation of at least 0.25 in the
calibration phrase, searching within 30 ms of the saved separation on the fixed
side. It records the relative level as a guide, not an automatic rejection rule.
This is followed by the original B withheld-block check: the joint model must
reduce residual energy relative to the main-only model by more than 3.4%, and
by more than 0.2% of target energy. These are confidence screens, not independent
proof of preserved overlapping speech or perceptual echo attenuation.

An absent donor or insufficient context causes an explicit interval bypass.
An absent evidence catalog, conflicting directions, overlapping application
intervals, invalid manifests, or changed source hashes is an error. A valid
catalog with no evidence for the channel produces a complete unchanged output.
Missing donors and bypass reasons are visible in the report; completing a donor
later requires rendering into a **fresh destination** to reconsider those intervals.

Each destination contains:

- `crosstalk.wav`: mono 8 kHz float WAV at the input gain, frame count and offset.
- `crosstalk.json`: completion status, source hashes, fixed direction, per-interval
  decisions, local timing, coefficients, changed duration, output verification and
  limitations. Peak excursions are retained in float, not normalized or clipped.
- `evidence.json`: the reused interval definitions and source/evidence bindings.
- `removed_NNNN.npy`: the bounded subtraction for each applied interval, for audit.

Every sample outside applied support is identical to the completed denoised input.
Whole tapes stream in blocks; only bounded contexts are loaded for fitting.
Interrupted outputs remain marked failed. Existing destinations, research WAVs,
and approval-bound audio are never overwritten. No listening page or server is made.

Stage 05b now consumes completed stage-04 output by default, validates its
provenance, and applies the **same shared tape inverse map**, retaining channel
offsets. For an intentional denoised-only comparison, pipeline stage 05b accepts
`--skip-crosstalk`; the standalone renderer accepts `--denoised-input` explicitly.
The Audition map-review exporter also defaults to stage 04 and accepts
`--skip-crosstalk`. Its A/B pair remains matched in gain, interval and cleanup.
Existing stage-05/06/07 products are not regenerated or relabeled automatically.

## Extending the saved evidence

Use a new evidence directory, preserving old deliveries. Its `directions.json`
maps `Tnnnnnn/CHnn` to records containing `remove: "earlier"` or `"later"` and
the supporting evidence. Its nested `review.json` records use the existing
`research/crosstalk_echo_review.py` schema: tape, target, donor, direction,
direction evidence, canonical excerpt start/duration (at most 60 seconds),
application/calibration intervals, measured main/echo tracks, and source signatures.
Calibration must lie inside application support; intervals must not overlap.
These must describe established repeats, not unreviewed correlation candidates.
The stage does not reuse historical coefficients or gain on v2 audio.

Optional `stagger.json` preserves a dictionary keyed by tape/channel with measured
main-utterance stagger assignments and their reference, coordinate domain and
evidence. Stage 04 carries these records forward unchanged and fills absent
channels with `unknown`. Echo direction never determines stagger membership, and
this stage does not de-stagger or retime audio.

## Verification

```powershell
uv run python -m unittest tests.test_crosstalk_stage tests.test_crosstalk_echo_review tests.test_crosstalk_probe tests.test_shared_tape
uv run python -m unittest discover -s tests -p test_pipeline.py
```

Tests cover both echo directions, fractional tracking, legitimate shared speech
without an added repeat, exact bypass, source/evidence changes, missing donors,
failed runs, pipeline ordering and shared-map preservation for offset channels.

A read-only check against the archived listening-trial inputs reproduced B's
withheld-tail prediction-band residual changes (CH17 approximately −7.11 dB;
CH20 approximately −1.23 dB), with exact bypass outside support. These are total
residual-energy changes, not perceptual echo attenuation. This historical check
does not qualify incomplete or newly denoised v2 inputs; each render rechecks
the repeat and fits on the actual completed v2 files.
