# Shared tape-level defluttering

## Governing rule

The project owner reaffirmed on November 3, 2026 that each tape's 30 channels
were recorded simultaneously and must share a single tape-motion correction.
A correction supported by any channel is applicable to all channels at the
same canonical tape time. Reference discovery and evidence collection may be
per channel; correction selection, coverage, joining, and application are at
tape level. Absence of check tones on the output channel is not a bypass rule.

Preserve recorded channel offsets when mapping canonical time to local samples.
Build and anchor one shared map for the intended scope, then crop or apply that
map to channels. Never independently anchor each channel or short listening
excerpt. Regions without usable evidence anywhere on the tape remain bypassed.
This shared-map requirement does not turn arbitrary interference into a timing
reference or erase the recorded scientific limitations of individual methods.

## Existing data and the November 3 correction

The first CH20 reviews incorrectly used only CH20's target eligibility. Preserve
their audio and approvals as historical comparisons; do not repeat that selection
method. In the 13,200-15,000 second review, the old regional plan provides eleven
minutes through CH20 alone and sixteen minutes through the union of all targets.
The completed v5 all-channel plan contains additional continuous spans.

`scripts/05_deflutter/tape_shared_plan.py` adapts saved regional and continuous plans into a
shared review plan. It does not redo carrier discovery, calibration, or waveform
fitting. It verifies the source plans, maps, and source identities and records
the source of every choice. All output-channel entries receive the identical
selection, speed curve, support, and bypass decision for each interval.

The frozen selection order for C.wav is:

1. Retain the regional choices from the hash-bound, listening-approved B review
   wherever those choices still have a saved eligible evidence channel.
2. Otherwise use a saved regional candidate eligible on any channel. Prefer
   greater saved reference support, then reference quality rank, then the key
   in lexical order. Target identity and the count of agreeing channels do not
   rank candidates; copied feeds are not independent votes.
3. Fill remaining gaps with existing v5 `use_reference` spans. A candidate must
   have a saved local assessment without explicit worsening/failure on at least
   one supporting target. Missing reserved events remain a limitation, not a new
   output-channel veto. Prefer reference rank, then the key in lexical order.
4. Otherwise bypass. Do not promote `validate_transfer`, rejected choices, or
   untracked candidates merely to increase coverage.

This order is frozen before rendering C or judging its output. Resolve competing
curves by the declared priority; never average incompatible curves. Existing
fade/blend rules join the selected curves before one integration and inverse
resampling. Numerical integrity checks remain required. C is a new listening
trial; B's approval does not automatically approve C's added corrections.

The adapter produces a bounded shared map for this 30-minute comparison, not
an already qualified full-tape production plan. Older planner and production
entry points still carry per-target application decisions; migrate those to
shared tape plans before whole-tape rendering. Saved surveys remain reusable.

## Delivery

Deliver WAVs directly for Audition using `uv` commands. C uses the same denoised
input, 30-minute coordinates, sample rate, PCM format, and fixed gain as A/B.
Keep A/B unchanged and save C's map, selected sources, checks, and file hashes.
