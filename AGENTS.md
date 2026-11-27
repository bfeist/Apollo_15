# Apollo 15 working rules

## Tape-level flutter correction

These recordings are simultaneous 30-track tapes. The project owner's required
model is one shared tape-motion correction at each canonical tape time.

Stage 02 establishes where each original WAV starts on the shared tape clock.
Use its measured offsets for stage-04 donor matching and for stage-05 tracing,
planning, and application. Preserve the acquisition times in the source records.
For an existing archive, use the selective recovery command in the shared-map
guide to reuse completed work on tapes whose clocks still agree.

- Search and reuse saved evidence from every available channel on the tape.
- A usable correction established on one channel is available to every channel
  on that tape at the same canonical time. Missing tones or check events on a
  particular output channel must not independently disable that correction.
- Select one tape-level curve per interval, join it once, and apply the same
  inverse timing map to all output channels, preserving their existing offsets.
  Do not fit or duration-anchor a separate map for each output channel.
- Retiming (07) places the tape curve on the earlier head row, which carries the
  time code and is real time. At 07b each later-row channel is moved earlier by
  its measured stagger (about 1.38 s) through the same map, per channel and
  interval from the measured rows, never from delivered channel numbers. See
  `docs/23_2026-11-26_head_rows_and_real_time.md`.
- Bypass where no usable tape-level correction is available. Partial improvement
  is acceptable; retain smooth transitions and do not force correction into gaps.
- Count coverage as the union of eligible tape-time intervals across channels,
  not a sum of channel-seconds or one output channel's eligibility percentage.
- Reuse completed survey, planning, and map artifacts before considering new
  analysis. Keep previous results and approval-bound audio immutable.
- Legacy per-target plan decisions describe evidence gathered on that target;
  they are not the application gate for the shared tape map. Use the native
  shared tape map for new reviews. Active stage-05 commands require v2 denoised
  inputs; historical scientific modules are evidence readers only.

See `docs/08_2026-11-15_shared_deflutter.md` for selection and provenance details and
`docs/10_2026-11-15_deflutter_detection.md` for the check-tone-free detection census.

## Delivery and commands

Crosstalk research uses completed denoised channels as both target and donor.
Determine the actual signed interchannel delay from matching signals, including
local variation and fractional-sample alignment, before fitting subtraction.
The approximately 1.3-second offset is a search lead, not a fixed delay to assume.
Crosstalk subtraction precedes application of the shared de-flutter map.
Official stage 04 writes separate, provenance-bound crosstalk outputs from v2
denoised targets and donors. Stage 05b consumes these by default; denoised-only
comparisons require an explicit bypass. See `docs/06_2026-11-13_crosstalk_stage.md`.
Adjacent channels may intentionally record the same monitored source. Such a
channel remains a valid reference. Establish the extra delayed copy within the
target, preserve its main utterance, and subtract only the unwanted repeat.
The unwanted copy can be roughly 1.3 seconds earlier or later. Generally remove
the weaker occurrence within the target channel and preserve the stronger one.
Determine earlier-versus-later removal once for each channel on each tape and
keep that direction fixed for the entire tape. Track local changes in the delay
magnitude within that direction. Reuse the established direction in later tests.
Compare matched content levels within that channel, allowing for overlapping
speech. Treat relative level as a selection guide rather than an automatic gate.
Measure donor-to-main alignment separately from main-to-echo separation.
An interchannel match around 1.3 seconds alone does not identify an echo.

Use `uv run python ...`. Deliver matched WAV files for Adobe Audition, with
identical source interval, duration, cleanup, and gain. Do not create listening
web pages or start servers unless explicitly requested. Preserve A.wav and
B.wav when the user requests a new C.wav comparison.
