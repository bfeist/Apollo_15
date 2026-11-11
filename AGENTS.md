# Apollo 15 working rules

## Tape-level flutter correction

These recordings are simultaneous 30-track tapes. The project owner's required
model is one shared tape-motion correction at each canonical tape time.

- Search and reuse saved evidence from every available channel on the tape.
- A usable correction established on one channel is available to every channel
  on that tape at the same canonical time. Missing tones or check events on a
  particular output channel must not independently disable that correction.
- Select one tape-level curve per interval, join it once, and apply the same
  inverse timing map to all output channels, preserving their existing offsets.
  Do not fit or duration-anchor a separate map for each output channel.
- Bypass where no usable tape-level correction is available. Partial improvement
  is acceptable; retain smooth transitions and do not force correction into gaps.
- Count coverage as the union of eligible tape-time intervals across channels,
  not a sum of channel-seconds or one output channel's eligibility percentage.
- Reuse completed survey, planning, and map artifacts before considering new
  analysis. Keep previous results and approval-bound audio immutable.
- Legacy per-target plan decisions describe evidence gathered on that target;
  they are not the application gate for the shared tape map. Use the shared-map
  adapter for new reviews; legacy rendering entry points still need migration.

See `docs/tape_level_deflutter.md` for selection and provenance details.

## Delivery and commands

Crosstalk research uses completed denoised channels as both target and donor.
Determine the actual signed interchannel delay from matching signals, including
local variation and fractional-sample alignment, before fitting subtraction.
The approximately 1.3-second offset is a search lead, not a fixed delay to assume.
Crosstalk subtraction precedes application of the shared de-flutter map.
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
