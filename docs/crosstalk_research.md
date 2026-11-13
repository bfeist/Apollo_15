# Apollo 15 crosstalk research

The bounded two-path method is now available as [official stage 04](crosstalk_stage.md),
after completed v2 denoising and before the shared de-flutter map. The numerical
model is shared with this research exporter. The original listening files remain
immutable; promoting the stage does not extend the demonstrated coverage below.

## Current Audition trial: bounded earlier-copy removal, November 8

The new comparison is in `reports/crosstalk_echo_Audition_20261108_v2/`.
There are separate `CH17/` and `CH20/` folders because the document named CH20,
while saved research also examined CH17 at the same timestamp. The owner was
unsure which channel was intended when asked in this session.

On November 8, the owner reviewed the delivered comparison and reported:
"This works quite well. it's still kind of audible but much better".
This is a positive listening result for the bounded trial, with residual
crosstalk still audible. The feedback did not identify a specific channel or
B/C version, so it does not establish a preference between those exports.
Preserve these WAVs as the listening reference for further refinement.
The feedback is recorded in the delivery folder's `listening_feedback.json`.

Each folder contains these matched, 30-second, mono 8 kHz / 24-bit WAVs:

| File | Treatment |
| --- | --- |
| `A.wav` | Completed denoised target, unchanged baseline |
| `B.wav` | Joint main/echo fit; subtract only the earlier copy using frozen transfer coefficients |
| `C.wav` | Joint main/echo fit with locally adapted coefficients and a withheld-block confidence screen |
| `removed_B.wav`, `removed_C.wav` | Exactly the respective subtraction, within PCM quantization |

**Listen at 14.1–15.48 seconds, then compare the stronger occurrence around
15.7–17 seconds.** The files cover canonical 13,555–13,585 seconds, corresponding
to 5:55–6:25 in the long review. The changed interval is 6:09.10–6:10.48 there.
The originally marked 6:07 (12 seconds into these files) remains unchanged:
its subtraction was not established reliably. This is a partial demonstration
on the clearest nearby repeated phrase, not a claim to have cleaned the whole
example or the exact marked instant. Every sample outside the stated application
interval is identical to A, including the stronger later occurrence.

Within-target matches establish the **earlier** occurrence as weaker in this
phrase. On CH17, matched windows near canonical 13,569.3–13,570.4 seconds put
the later occurrence about 12–14 dB above the earlier one. CH20 matches at
13,569.3, .5 and .7 put it about 18–20 dB above the earlier occurrence.
These levels compare occurrences within each target, not donor/target gain.
Record and reuse the earlier-removal direction for T510083 CH17 and CH20;
see `directions.json` and each channel's `review.json` in the delivery folder.
The bounded trial has positive listening feedback; channel/version-specific
acceptance and wider application remain unestablished. Do not flip the direction
in other windows.

CH19 is the completed denoised donor for both. Its alignment to the stronger
later target occurrence is about −1.369 seconds, whereas its alignment to the
earlier weaker copy is about +1.27 ms on CH17 and +0.04 ms on CH20. These are
different paths. In the notation below, `delta` is approximately −1.369 seconds
and `Delta` is approximately −1.37 seconds. Subtracting the −1.37-second donor
path would remove the stronger occurrence. The trial subtracts the near-zero
path only within the separately evidenced earlier-repeat interval.

`research/crosstalk_echo_review.py` tracks local fractional alignment and fits
separate 33-tap main and echo filters jointly in the 300–3,000 Hz prediction
band. That band limits the subtraction; A/B/C retain the same existing target
cleanup. The target is never retimed. B calibrates at excerpt 14.25–14.75 seconds
and freezes its transfer coefficients. C uses overlapping 300 ms fits, screened
on alternating withheld blocks with guards between the regressors. Both have
80 ms raised-cosine transitions. All exports use the original CH20 benchmark's
common gain of 1.3996783018483525, with no separate normalization or de-flutter.

| Target | B calibration residual change | B withheld tail, 14.8–15.4 s | C same tail |
| --- | ---: | ---: | ---: |
| CH17 | −11.76 dB | −7.11 dB | −8.08 dB |
| CH20 | −4.84 dB | −1.23 dB | −1.57 dB |

These are total residual-energy changes in the prediction band, **not measured
perceptual echo attenuation**. B's alignment still uses the evaluation audio;
C's coefficients also adapt there. The tail is part of the same phrase, not a
fresh-event qualification. Earlier exploration found poor fixed-filter transfer
to another phrase around excerpt 22 seconds, so that phrase bypasses subtraction.
Overlapping wanted speech in the changed interval still needs Audition review.

Verification checks equal lengths, common gain, clipping, baseline correspondence,
the A-minus-B/C subtraction identities, and bit-identical bypass. Synthetic
tests exercise both repeat directions with changing separation and overlapping
speech, fractional alignment with both polarities, and legitimate shared speech
at a nonzero interchannel offset with no added echo; the last control bypasses.
Run these tests and reproduce into a fresh directory:

```powershell
uv run python -m unittest tests.test_crosstalk_echo_review tests.test_crosstalk_probe
uv run python research/crosstalk_echo_review.py D:/Apollo_15_30-Track/denoised/A15_T510083 reports/new_echo_review
```

The earlier `reports/crosstalk_echo_attempt_20261108/` experiments and first
`reports/crosstalk_echo_Audition_20261108/` exports are retained. Use the `_v2`
delivery above, which additionally checks whether the echo path improves on a
separately fitted main-only model. Existing long-review A/B/C and benchmark WAVs
were not changed.

## Required model and prior findings

The owner corrected the first experiment on November 8. Use completed denoised
channels as both the target and donor for crosstalk research. The working order
is denoising → crosstalk subtraction → shared tape-level de-flutter.

First determine the actual relative offset from matching signals in the denoised
channels. The approximate 1.3-second spacing is a search lead. Measure its sign,
fractional-sample position, variation with tape time and uncertainty before
fitting subtraction. The failed original-source trials below do not answer
whether this approach works on the requested denoised sources.

The owner clarified that adjacent channels may intentionally contain the same
monitored source. This is compatible with using a neighbor as an echo reference.
The objective is to remove the additional roughly 1.3-second repeat within the
target while preserving its main utterance. A different conversation on the donor
is not required.

The owner further clarified that staggered tracks can put the unwanted copy
roughly 1.3 seconds before or after the main utterance. Generally the weaker
occurrence is the removal target. Compare levels of the matched utterance within
the target channel, accounting for overlapping voices and noise. Raw levels
across differently amplified donor and target channels do not settle this choice.
Use the relative level as a guide and retain ambiguity where the two occurrences
cannot be separated reliably.

The owner specifies that earlier-versus-later removal is fixed for each channel
on a given tape. Establish that direction from clear examples and save it keyed
by tape and target channel, with the supporting intervals. Reuse it throughout
the tape. Local tracking adjusts the delay magnitude within the established
direction. A weak or conflicting local match is grounds for bypass or review.
It does not reassign the direction. Relative copy level helps establish the
direction initially and assess later matches without selecting a new side in
each window.

There are two separate timing relationships to estimate. Let `delta(t)` align
the donor with the target's stronger main utterance, and let `Delta(t)` be the
signed time of the weaker copy relative to that main utterance. A positive value
places the weaker copy later, and a negative value places it earlier. The sign
is fixed for the tape/channel pair once established.
The main predictor reads the donor
at `t + delta(t)`. The echo predictor reads it at
`t + delta(t) - Delta(t)`. Both can vary locally. The measured interchannel lag
must be attributed to one of these paths before it is used for subtraction.

For the 6:07 benchmark, fit the main and echo paths together, with separate gains
and short transfer filters. Calibrate on words whose stronger and weaker copies
can be distinguished. Preserve the main-path prediction in the target and
subtract the weaker-copy prediction. Check the stronger occurrence, the weaker
copy and overlapping speech separately. Test both earlier and later weaker
copies, with changing separation. A control containing legitimate shared
audio at different track offsets, with no added echo, must bypass subtraction.
The previous coincident-feed synthetic control does not cover this case.

`research/crosstalk_offsets.py` reads completed stage-03 manifests, checks zero
source offset and WAV dimensions, and measures neighboring channel pairs in
two-second windows. It searches both signs from 0.8 to 1.9 seconds and separately
checks near-zero copies. Three frequency bands check the broadband estimate.
It records competing peaks and fractional peak estimates. These analysis filters
operate on the completed denoised audio. The full-file audio hashes in manifests
are retained as provenance, without claiming a new full-file hash verification.

## Preserve channel stagger assignments for later historical alignment

The owner requires this research to retain whether each channel on each tape
belongs to the **0-second group** or the **approximately +1.3-second group**.
A later de-staggering step will use these assignments to make all tracks play
at the same historical time. Preserve this information as a reusable research
output, even when a particular crosstalk subtraction is rejected or bypassed.

Maintain a machine-readable record keyed by tape and channel, covering every
channel and marking unresolved assignments as unknown. Each record must retain:

- The stagger group (`0`, `+~1.3 s`, or `unknown`) and the common reference
  channel or timing reference that defines the tape's zero group.
- The measured signed offset in seconds and fractional samples, its sample
  rate and original source-coordinate convention; retain local measurements
  and their supported intervals rather than rounding them to 1.3 seconds.
- Confidence or uncertainty, supporting matched main-utterance events and
  channels, source identities, and links to the saved measurement artifacts.
- The distinction between measured relative alignment and any established
  absolute historical-time anchor. A relative zero group alone does not set
  the historical epoch.

Use the sign convention `stagger = channel event time - reference event time`
for the same main utterance. A positive stagger means that event appears later
in the channel's source timeline. In those source coordinates, the later
alignment step reads the channel at `reference_time + stagger` to align it
with the zero group. Record the coordinate domain explicitly when carrying
these measurements through the shared de-flutter map.

Keep stagger membership separate from the fixed earlier/later echo-removal
direction, donor-to-main alignment, and main-to-echo separation. An echo match
or its removal direction alone does not establish a channel's stagger group.
Use main-utterance matches and a common tape reference to establish membership;
retain conflicting or insufficient evidence as unresolved. Reuse established
membership throughout the tape while preserving measured local offset variation.

This requirement preserves information for a later processing step. Current
crosstalk comparisons retain their original sample timelines, and shared
de-flutter continues to use one tape-level inverse map while preserving channel
offsets. De-staggering will explicitly use the saved assignments afterward;
it must not be implemented by fitting a separate flutter map per channel.

## Denoised offset measurements, November 8

The revised survey measured all 29 numbered neighboring pairs in two 60-second
intervals on each of T510077 and T510083, using two-second windows. T510077
starts were 19,400 and 19,715 seconds. T510083 starts were 14,295 and 14,475.
Repeated matches clustered around 1.372 seconds on T510077. T510083 yielded
fewer agreeing windows and more scattered estimates around 1.37–1.38 seconds.

A closer examination of denoised T510077 CH28 and CH29 used quarter-second
windows at 19,410–19,420 and 19,432–19,442 seconds. Forty of eighty windows
passed the exploratory checks. Their median waveform correlation was 0.864,
and their median delay was +1.372294 seconds. CH29 is read later than CH28.
The observed estimates ranged from +1.371453 to +1.373595 seconds, a spread of
2.142 ms, or about 17 samples at 8 kHz. These are local matching estimates.
The spread includes estimation error and possible physical variation, and the
windows can belong to the same event. This does not establish one exact offset
for the whole tape. The donor was a median 8.27 dB stronger in these windows.

The checks require broadband absolute correlation at least 0.15, a peak at
least 1.15 times the strongest competing peak more than 5 ms away, and agreement
within 2 ms in at least two of three separately searched bands. These thresholds
screen candidates. The 2 ms tolerance is too broad to certify cancellation
accuracy. Shorter windows produced consistent positive polarity in this pair,
where the earlier long-window peaks had changing signs.

The immediate experiment is to estimate a local fractional delay trajectory
from calibration portions of a denoised event, test it on intervening samples
and fresh events, then estimate the leaked amplitude and frequency response.
Compare constant and locally varying alignment at the same gain. Listen to the
removed signal and verify preserved target speech before expanding coverage.
Any interpolation across unsupported times needs independent validation.

Measurements and source provenance are in
`reports/crosstalk_denoised_offsets_20261108/`. Synthetic positive and negative
fractional delays were recovered within 0.15 sample in the two checked cases.
Correlation and lag conventions follow the SciPy implementation. [SciPy correlation documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html)

```powershell
uv run python research/crosstalk_offsets.py D:/Apollo_15_30-Track/denoised/A15_T510077 reports/new_offset_measurements.json --starts 19410 19432 --span 10 --duration .25 --channels 28 29
```

## What the offset can tell us

The owner listened to the CH28/CH29 example below and reported that it contains
no crosstalk. This example is rejected. Its correlation and energy reductions
do not demonstrate crosstalk removal. Future examples must establish the main
utterance and the unwanted repeat within the target before testing subtraction.
Shared main speech on the donor is compatible with this test.

The owner then identified a real delayed-echo example at 6:07 in
`reports/listening_long_20261103/`. The original notes assigned this to T510083 CH20 near canonical second
13,567. The new benchmark is `reports/crosstalk_known_echo_20261108/`, containing
30-second denoised CH19, CH20 and CH21 excerpts from 13,555–13,585 seconds
(5:55–6:25 in the long review), all at the long review's common gain. The marked
echo occurs about 12 seconds into these excerpts. CH20 was checked against the
same crop of the original long-review A.wav within PCM quantization.

An offline transcription check found the earlier rejected CH28 and CH29 both
carrying the principal P-47/average-G conversation, also present on other tracks.
For the owner's example, CH19 matches CH20 at both near-zero and approximately
−1.38-second lags. The lag sign means the donor is read earlier. Initial joint
fits of these two paths did not transfer consistently between speech bursts.
The owner identified the audible duplication as the benchmark and subsequently
clarified that the weaker copy can occur on either side of the main utterance.
The unresolved measurement is the donor alignment to that weaker copy separately
from its alignment to the stronger main utterance.
These files establish the audible benchmark and are unmodified reference
excerpts. Search records and transcripts are in
`reports/crosstalk_speech_search_20261108/`. ASR is a source-content aid and does
not establish the presence or absence of faint leakage by itself.

The first changing-delay listening example is now
`reports/crosstalk_listening_20261108/`, covering T510077 CH28 at
19,408–19,448 seconds with denoised CH29 as donor. `A_denoised.wav` is the
baseline, `B_tracked.wav` uses the local delay, and `C_fixed_delay.wav` uses
1.372294 seconds with the same transfer coefficients and subtraction envelope.
`removed.wav` isolates the subtraction and `donor_aligned.wav` provides the
aligned denoised donor in the 300–3,000 Hz prediction band. The donor reference
has that diagnostic band filter, while A/B/C share the target's existing cleanup.
All five have the same length and common gain of +31.36 dB, limited by the donor
reference peak. They are mono 8 kHz 24-bit WAVs for Audition.

`research/crosstalk_tracking.py` measures positive-polarity correlation in 60 ms
windows every 20 ms, around the previously measured delay. It rejects weak,
ambiguous and isolated matches, interpolates supported estimates at fractional
sample positions, and fades subtraction out across unsupported gaps longer than
100 ms. A fixed 17-tap transfer filter is calibrated in the first ten seconds.
The target stays on its existing sample timeline. Only the donor prediction is
aligned. Subtraction is fully active for 17.48 of the 40 seconds, with fades
around the supported portions.

After the first ten calibration seconds, analysis-band residual energy changes
by −3.69 dB for tracking and +2.35 dB for the fixed delay. Tracking still uses
both channels during that evaluation, so this is an adaptive listening example.
The subsequent owner review rejected its interpretation as crosstalk.
A synthetic changing-delay case recovered delay with 0.068 sample median error
and reduced leakage error against known clean truth by 20.35 dB. An unrelated
donor was rejected. WAV duration, gain, clipping, exact baseline correspondence
and the subtraction identity were verified. `review.json` and
`verification.json` preserve the measurements, input provenance and WAV hashes.

The roughly 1.3-second offset gives us a place to look for a copy of a neighbor's
signal. Search both signs. A copy may also occur near zero depending on where
the leakage entered the chain. A delayed shared console feed can look like
leakage, and periodic signals can produce peaks at many unrelated delays. For
example, the T510083 screen found strong near-zero and periodic delayed peaks
around CH01. The test needs an identified repeat within the target and a
repeatable transfer from the donor to that repeat before subtracting speech.

Numbered neighbors are the starting hypothesis. The channel-assignment photo
describes console routing, and the physical track layout still needs evidence
for these reels. The Quindar code also considers longer offsets for identifying
duplicate events. Its delay search serves a different purpose. [Project provenance](deflutter_v5.md)
· [Quindar event matching](../scripts/06_retime/quindar_tape.py)
· [NASA playback-head history](https://www.nasa.gov/podcasts/houston-we-have-a-podcast/the-heroes-behind-the-heroes-part-2/)

## Research sequence

1. **Measure the offset on denoised audio.** Start with adjacent numbered tracks and run
   the same test against nonadjacent and coincident controls. Confirm the same
   main-to-echo separation and donor alignment in several excerpts. Include
   target-only and overlapping speech. Check shared-feed routing and periodic
   peaks when attributing matches to the main or echo path. Keep lag estimates
   in original sample coordinates.
2. **Fit and freeze the subtraction.** Compare a signed scalar gain with a
   short finite impulse response filter for the echo path, fitting the main path
   separately in the same model. Use separate events to check the fit with coefficients
   frozen. If the lag drifts, first establish its trajectory from independent
   donor events and freeze it before checking the target. A region with weak or
   ambiguous evidence bypasses subtraction, with short fades at usable edges.
3. **Review matched excerpts in Audition.** Compare baseline, scalar, and
   filtered subtraction on the same interval and at the same gain. Solo the
   removed signal and listen for wanted speech, especially faint syllables and
   simultaneous voices. Require less audible bleed and preserved target speech
   before moving from a diagnostic to a candidate. A smaller residual alone
   can also result from removing a shared feed.
4. **Apply the shared de-flutter map after subtraction.** Hold cleanup, gain,
   source interval and the saved tape map fixed when comparing the combined
   result. Check target speech, remaining leakage and transition fades. Expand
   to two donors after a single donor transfers across separate passages.

## Superseded original-source tests, November 8, 2026

The owner rejected this test design. Its audio is retained as a failed historical
experiment. Its fixed lags and original-source results do not qualify an offset
or subtraction model for the denoised channels.

`research/crosstalk_probe.py` screened 58 directed numbered-neighbor pairs in
ten 12-second windows on T510083 and six on T510077. T510083 covers a sampling
of the existing CH20 review interval. T510077 adds a second reel using regions
around prior research excerpts. The screen searched ±1.10–1.55 seconds and
within 25 ms of zero. It used a 320–3,000 Hz analysis band with suppressed
60 Hz harmonics. Periodic energy remained visible in the lag peaks.

Three selected pairs were fit with a scalar and a regularized 33-tap filter
around fixed signed lags. Zero-lag and displaced-lag filters used the same
calibration interval. Each comparison used two disjoint check windows with
no refit. Some check windows were visible during screening, so these are
transfer diagnostics. A final qualification must reserve untouched events.
All times below are seconds on the original WAV timeline. A positive lag reads
the donor later than the target. Residual changes are in the analysis band.

| Reel and subtraction | Lag | Fit start | Check starts | Filtered residual change |
| --- | ---: | ---: | --- | --- |
| T510083 CH20 from CH21 | −1.376875 s | 14,295 | 14,475 and 14,335 | +0.003 dB and +0.127 dB |
| T510083 CH21 from CH22 | +1.377250 s | 14,295 | 14,475 and 14,335 | +0.389 dB and +0.123 dB |
| T510077 CH28 from CH29 | +1.371125 s | 19,400 | 19,440 and 19,715 | +1.766 dB and −0.098 dB |

All six windows failed at least one filter check. The tests required a
held-out gain ratio of 0.65–1.5, positive transfer above 0.5 in at least two
of four bands, 0.03 dB total reduction, and a 0.02 dB margin over both controls.
On T510077 the scalar also had mixed results, raising residual energy by
1.257 dB at 19,440 and lowering it by 0.192 dB at 19,715. Several T510077 pairs
had peaks around 1.37 seconds, with signs and exact lags varying across windows.
The next test should trace individual donor events and rule out shared or
periodic sources before trying a locally varying lag. The injected-delay
regression recovered the known delay and reduced error against clean truth.
The coincident shared-feed regression rejected the delayed subtraction.

The two T510077 CH28 windows are exported as failed-fit listening diagnostics
under `reports/crosstalk_20261108/T510077_Audition_diagnostics/`. For each
window, `baseline`, `scalar` and `fir` share the same unretimed 12-second
interval, gain and source cleanup. `removed_fir` isolates what the filter took
out. All are mono 8 kHz, 24-bit WAVs. `trial.json` records coefficients and
per-window scores. The November 3 de-flutter review WAV hashes were verified
after this run.

The screen and trial CLI accept fresh destinations. For example, to reproduce
the T510077 listening diagnostics, run from the repository root.

```powershell
uv run python research/crosstalk_probe.py trial D:/Apollo_15_30-Track/orig_concatenated/A15_T510077 reports/crosstalk_repeat --pair 28 29 --fit-start 19400 --checks 19440 19715 --lag-samples=10969 --duration 12 --export-diagnostics
```
