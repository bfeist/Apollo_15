# Deflutter pipeline review and rollout criteria

Implementation follow-up: the revised code and operational commands are documented in the [v2 runbook](docs/deflutter_v2.md). The findings below describe the pre-v2 review snapshot. The new survey, independent decoder, transfer controls, version gates, final-map checks and acceptance records now exist; mission-wide survey and pilot/listening acceptance remain required before production. Stage 03 and the existing T510083 artifacts remain preserved.

Reviewed 2026-10-24 against the local research, saved T510083 results, current working-tree implementation, and `F:/_repos/Apollo_Audio_Processing`.

**Recommendation: retain the regional architecture, but complete the carrier survey and transfer-validation work below before mission-wide planning/rendering.** Denoising can finish unchanged. This document defines the proposed process; it does not claim that the new stages or acceptance gates are implemented.

## What the existing results establish

The useful foundations are already present: original-audio reference analysis, reusable denoised speech, per-region reference/method selection, calibration followed by unused check events, negative controls, explicit bypasses, saved maps, checkpointing, and continuous integration across transitions. The successful T510083 listening result is evidence worth preserving.

The saved full T510083 plan contains 1,027 analyzed regions and 29 audio targets. Its 29,783 channel-region decisions comprise:

| Decision | Count | Meaning |
| --- | ---: | --- |
| `render_and_review` | 1,195 (4.01%) | Passed local numeric transfer checks; still requires speech review |
| `validate_transfer` | 28,488 (95.65%) | Proposed correction without sufficient separate calibration/holdout evidence |
| `leave_uncorrected` | 100 (0.34%) | Failed or unavailable evidence, including tiny boundary regions |

These percentages count decisions, not exact durations. Summing region durations for CH20 gives 168 minutes with numeric transfer passes, about 852 minutes needing transfer validation, and 6 minutes bypassed. Tiny edge regions explain differences between counts and durations.

The completed CH20 `review.json` explicitly records `experimental_transfer: true`, approximately 99.36% nonzero speed coverage, and 0.244 s maximum displacement. Thus its broad correction is not the behavior of the default conservative renderer. The favorable listening result does not automatically validate the experimental choices elsewhere.

Evidence locations:

- `D:/Apollo_15_30-Track/deflutter_planning/A15_T510083/plan.json`
- `D:/Apollo_15_30-Track/defluttered/A15_T510083/CH20/review.json`
- [Earlier whole-tape assessment](reports/whole_tape_assessment_20261017/assessment.json)
- [Bounded rendered-CH20 assessment](reports/bounded_CH20_assessment_20261018/assessment.json)
- [Research baseline](IRIG_FREE_FLUTTER_RESEARCH.md) and [expanded study](reports/bespoke_expanded_20261017/summary.json)

The expanded study sampled 16 eligible tapes and found fresh exploratory passes on eight. T510073 and T510091 failed calibration; several tapes lacked suitable sampled events; T510092 behaved differently between passages. These are useful pilot cases, not permanent tape-level exclusions. There are now 39 canonical tape folders locally, so the prior study is not mission-wide coverage.

## Separate carrier correction from clock decoding

Adopt the user's proposed separation:

1. **Carrier branch:** discover and track the IRIG carrier just as another reference tone, including when data bits cannot be decoded. Apply only locally supported relative flutter correction to qualified targets.
2. **Clock branch:** independently decode original timecode and retain original sample positions, decoded fields, validity, continuity, and uncertainty. Decoding failure does not reject an otherwise useful carrier. Carrier detection alone never establishes historical time.

A short carrier island can correct its own supported interval, including other channels with demonstrated shared error. It cannot supply the missing phase waveform elsewhere on the tape. A short authenticated decoded sequence can supply a local historical clock anchor. One anchor does not determine hours of drift; any extension through Quindar timing remains a model with uncertainty.

IRIG-B uses an amplitude-modulated 1 kHz carrier, with data at 100 bits/s. The low-amplitude portions of its legitimate code must not automatically be mistaken for signal dropouts. A nearby pure tone or interference line remains a generic reference candidate until pulse structure or decoding establishes IRIG identity. [NTP's IRIG decoder documentation](https://www.ntp.org/documentation/drivers/driver6/)

### Supplied Apollo 11 reference: bounded probe completed

Source: `O:/30-track-audio/Apollo_11_30-Track/concatenated_wav_files/original/T648/A11_T648_HR1U_CH1.wav` (mono, 8 kHz). Only three 30-second excerpts were examined; no audio was rendered or modified.

| Original start | Strongest spectral peak in 700-1300 Hz | Plausible decoded frames, raw | Plausible frames after 400 Hz high-pass |
| --- | ---: | ---: | ---: |
| 60 s | 967 Hz | 0 | 5 |
| 600 s | 961 Hz | 2 | 26 |
| 3,600 s | 995.5 Hz | 0 | 27 |

Counts above use normal polarity and the unmodified legacy decoder. Plausibility requires matching SBS/H:M:S, valid ranges, and zero in the historical year field. These checks are not full authentication. The peaks are spectral maxima, not estimates of the carrier's mean frequency or proof of a specific speed error. The later excerpts decode day 204, around 02:56 and 03:45, respectively. Polarity and band-pass alternatives, consecutive-frame runs, sample positions, imported-code hashes, and excerpt hashes are retained in the report.

Reproduce with a new output JSON path:

```powershell
F:\_repos\Apollo_Audio_Processing\.venv\Scripts\python.exe research\irig_carrier_probe.py --input 'O:\30-track-audio\Apollo_11_30-Track\concatenated_wav_files\original\T648\A11_T648_HR1U_CH1.wav' --legacy-repo F:\_repos\Apollo_Audio_Processing --output reports\pipeline_review_20261024\a11_t648_irig_probe_repeat.json
```

[Probe code](research/irig_carrier_probe.py) and [local evidence](reports/pipeline_review_20261024/a11_t648_irig_probe.json). This establishes a useful known-positive test source and demonstrates the need for preprocessing and a broad acquisition search. It does not validate Apollo 15 detection sensitivity, carrier-to-speech transfer, or corrected speech quality.

### Survey design

- Read canonical **originals**, never denoised channels. Scan CH01 and IRIG-labelled files first, but include every other channel and unlabelled recordings such as T510078's extra file. Do not assign an unlabelled file to a speech channel merely because timecode is found there.
- Cover the entire timeline with overlapping short screening windows. Sparse early/middle/late sampling is inadequate for rare timecode islands. Cache inexpensive carrier/envelope features; run detailed tracking and decoding only on candidate spans with context. Benchmark cost before scanning all files.
- Acquire across plausible speed-shifted frequencies around 1 kHz, then refine the carrier center and capture width. The existing generic detector requires at least 50% occupancy across its analysis window; a few-second island in a minute can fail that rule. IRIG screening needs separate short-window logic and an explicit minimum detectable island length.
- Record `not_scanned`, `screened_no_candidate`, `carrier_candidate`, `carrier_supported`, `decoded_provisional`, and `clock_anchor_validated` separately. Negative results describe the searched bandwidth, sensitivity, signal level, and duration, not proof of absolute absence.
- Retain original sample spans and reference identity. Detect signal loss, clipping, competing carriers, cycle slips, clock discontinuities, and reacquisition. Do not carry a reference across an unsupported gap.
- Inject attenuated/filtered/noisy copies of the known-positive reference into controlled backgrounds to measure detection sensitivity. Include pure 1 kHz tones, AM-only signals, hum, and speech as negative controls. Freeze thresholds before testing fresh Apollo 15 passages.

### Borrowing the existing IRIG implementation

Reuse the decoder and investigate `ToneFollower` from `Apollo_Audio_Processing`; do not invoke its full deflutter command as a drop-in production stage. Its carrier follower is already distinct from its decoder. The old orchestrator combines carrier motion with frame-based duration correction, infers integer frame intervals by rounding carrier-scaled elapsed samples, and can use carrier-only timing at its stop/tail. Sparse timecode requires stricter gap and continuity handling.

For stage 05b, derive a relative flutter component with declared bandwidth and the existing duration-preserving timing contract. Compare signed tracking against harmonic tracking; IRIG is not intrinsically a 4-10 Hz sinusoid. Decoded absolute timing and slow speed observations belong to a separate clock model. Do not apply the legacy full-speed correction and subsequently apply the same slow correction again in 05c.

Decode originals first, with documented filter and polarity trials. If a flattened temporary reference is needed to help decoding, preserve its inverse map and translate every decoded frame back to the original sample coordinate. Account for filter delay and polarity-dependent crossing bias; diagnostic zero-phase filtering does not authenticate submillisecond historical time.

The legacy mission table has no `a15` entry, and the decoder defaults to Apollo 11. Add an explicit Apollo 15 basis before generating mission labels. Treat historical year/control bits according to the actual format, rather than reading them as modern calendar fields: the 2004 revision added year information. [RCC IRIG 200-04, revision notes](https://dolben.org/docs/irig_200-04.pdf)

Apollo 15 range zero is 1971-07-26 13:34:00 GMT; distinguish it from the fractional-second liftoff instant when computing GET. [NASA mission summary](https://www.nasa.gov/wp-content/uploads/static/history/SP-4029/Apollo_15a_Summary.htm)

Require several sequential frames with valid structure/fields and plausible sample-time increments before promoting an anchor. Preserve shorter provisional discoveries for review. Handle rollover, repeated frames, and discontinuities explicitly. Verify the recorded clock basis before interpreting its day/time as GMT.

## Planner gaps to address before the batch

| Finding in current implementation | Required process/change |
| --- | --- |
| `flutter_planning.analyze_region` inventories but skips timing-only channels | Import supported carrier islands into candidate discovery, without making timecode channels speech targets. |
| Shortlist fills in target-channel order, up to four references | Use per-target winners or a fair coverage-aware shortlist. Record budget exhaustion; try additional candidates using calibration evidence before declaring search exhausted. |
| Eligibility first uses a relatively narrow capture, before wider alternatives are evaluated | Test plausible wider acquisition before rejecting a candidate solely on narrow-capture failure. Keep contamination controls. |
| Harmonic seed searches 4-10 Hz; signed variants cover 2-15/2-30 Hz | Report the modeled bandwidth and out-of-band residual motion. Do not claim correction of all wow/flutter. Broaden methods only with new validation. |
| One calibration event and one held-out event can qualify a whole minute | Label evidence strength. Add independent time windows and oscillator/feed grouping; multiple margins on one beep are not independent observations. |
| Transfer gates mainly use relative improvement | Predeclare absolute residual Hz/cents, worst-event worsening, support, and speech-preservation criteria using pilot data; freeze them before fresh holdouts. Low baseline flutter needs a no-op/no-harm decision. |
| Beep-free targets usually remain `validate_transfer` | Establish bounded groups sharing playback error using independently checked carrier phase/amplitude and target evidence. Transfer group membership must be demonstrated, expire at changes, and remain distinct from direct target validation. |
| Fixed 70 ms wrong-time control and wrapped `np.roll` | Use multiple predeclared non-wrapping shifts and polarity/AM/no-motion controls. Keep these independent of target-based lag fitting. |
| Reference support, clipping, and transition blending can alter the applied map | Report speed-limit saturation and check the final joined map on reserved events near transitions/dropouts, not only isolated candidate maps. |
| Renderer calls numeric-pass regions `validated` and can apply all proposals experimentally | Separate numeric pass, transfer-group evidence, listening approval, and production acceptance. Do not use global experimental mode to fill production coverage gaps. |
| Complete plans can be reused after implementation changes | Introduce a methodology/version compatibility gate. Preserve T510083 as a baseline, but use a new planning destination for the revised method; a normal rerun can otherwise reuse the old completed plan. |

The separation of content time and playback time is essential. Delayed copies of speech or Quindars can identify duplicated evidence; their content lag does not automatically justify shifting a flutter map. Test shared playback motion independently, retain head/content offsets, and do not optimize a periodic lag on held-out tones. Independent per-channel maps can also create differential displacement; measure that before claiming synchronized channels or using a shared retiming curve.

A failed frozen choice should remain a failed holdout. If further development uses that outcome, reserve fresh evaluation data rather than cycling alternatives against the same holdout until one passes.

## Staged rollout and stopping points

1. **Finish stage 03.** Preserve its code, settings, and intermediates. None of the proposed reference work requires denoising again.
2. **Build and qualify the carrier survey.** Use T648 as a known-positive fixture, add negative/dropout/speed-shift tests, then survey the originals. Save timecode discoveries even if no correction is accepted.
3. **Run bounded cross-tape pilots with revised planning.** Include T510083 as the regression baseline; T510077 for broader signed tracking; T510073 for crowded references; T510092 for changing transfer; a sampled no-event tape such as T510076; and a newly inventoried tape outside the 16-tape study. Include any Apollo 15 timecode island found. Cover early/middle/late passages, head groups, faint speech, no-beep targets, and difficult boundaries. These are diagnostics, not an exhaustive survey.
4. **Freeze the method and acceptance rules, then evaluate fresh passages.** Retain all failures. Compare matched-gain denoised and corrected speech, check every audible calibration tone in the review excerpts, and distinguish correction strength from intelligibility and new artifacts. Do not substitute a percentage improvement for listening.
5. **Plan all tapes only after pilot acceptance.** Produce a duration-weighted coverage report per tape/channel: direct numeric support, demonstrated group support, proposals, rejection, and no evidence; also reference switches, fades, saturation, maximum displacement, and search-budget exhaustion. Report actual read bytes, wall time, and CPU use to estimate the remaining run. A completed plan is not itself approval to render.
6. **Render representative continuous passages before full channels.** Use the final joined maps and existing denoised audio. Review transitions and failures; then render accepted channels/tapes in batches. Retain bypassed spans and reasons instead of forcing a correction everywhere.
7. **Survey post-deflutter Quindars and freeze slow retiming.** Carry decoded anchors through the actual stage-05b map into its output coordinate system. Extend the stage-05c clock model to use authenticated local anchors/intervals where available and Quindar constraints elsewhere, with explicit uncertainty/conflict handling. Keep a single supported shared playback curve where evidence warrants it; preserve historical offsets. Never silently force contradictory clocks together.

The carrier and decoder products should have independent caches and provenance. New decoder success should be able to update clock annotations without redoing denoise or carrier analysis. Any change to the applied flutter map invalidates downstream measurements tied to that output.

## What was verified during this review

- Read the research baseline, expanded results, full T510083 plan, CH20 render metadata, and relevant planning/tracking/rendering/retiming code.
- Read the older IRIG decoder, carrier follower, deflutter timing logic, and mission-time table.
- Ran all 8 `test_flutter_planning.py` tests and all 11 `test_channel_review.py` tests successfully. They establish implementation behavior on their fixtures, not mission-wide audio quality.
- Ran the bounded T648 probe and saved decoded evidence and code/excerpt hashes. Source size and modification time were unchanged; the whole source was not hashed.
- No Apollo 15 IRIG survey, new full-tape plan, or full audio render was started. The new production survey, transfer policy, and clock integration remain implementation work.
