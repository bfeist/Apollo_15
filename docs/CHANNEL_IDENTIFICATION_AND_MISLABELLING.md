# Channel identification and suspected mislabelling

Investigation plan, 2026-10-18. **No channel reassignment or physical fault has been established by this document.** This work should follow the current processing run; it does not authorize changing that run, launching transcription, or renaming recordings.

## What is reported, observed, and still hypothetical

Ben reports that the digitization team did not check tape-head alignment. His proposed explanations are that original CH1, possibly CH2, were missed, that original CH3 appears under delivered label CH1, and that tape/head displacement may vary during digitization. Those are **unconfirmed hypotheses**, not a verified two-channel offset or a diagnosis of mechanical movement. The kind of alignment allegedly unchecked also needs clarification.

Keep these quantities separate:

- **Delivered channel label:** the filename and acquisition channel number.
- **Physical recorded track:** the stripe on the original tape; identification requires recording/playback documentation or other independent evidence.
- **Communication content/role:** the loop, feed, controller activity, or mixture actually audible during an interval.
- **Timing/head offset:** relative content timing; it does not itself identify physical track position.
- **Azimuth alignment:** head-gap orientation; distinct from lateral positioning across tracks. Neither should be inferred solely from a transcript role change.

Lateral displacement need not produce neat channel swaps. A transition could instead involve amplitude loss, mixed adjacent tracks, crosstalk, intermittent missing content, or duplication. Changing communication routing can also change content without any physical tape movement. Relabelling cannot reconstruct a track that was never captured.

## Repository facts and reference hierarchy

The current inventory parser in [flutter_planning.py](../scripts/flutter_planning.py) extracts `_CH<number>_`, optional `HR...` head labels, and filename start/end ranges. It checks file structure, not semantic channel identity. Its timeline is explicitly not authenticated GET or proof of head alignment. It currently classifies filenames containing `IRIG` as timing-only: a wrong label can therefore affect downstream inclusion, not just display text. Record suspected false IRIG classifications for a later explicit eligibility review; do not change an active job or silently omit those channels from the identification pilot.

The dated [frozen research inventory](../reports/bespoke_expanded_20261017/frozen_inventory.json) records, for example, T510073 filenames labelled `CH1_IRIG`, `CH2_FD_OPS`, `CH3_MD`, and `CH20_FLT_DO`. These are **delivered claims**, not an authoritative roster. Historical inventory paths may predate the move to `D:/Apollo_15_30-Track/orig_concatenated`; resolve current manifests rather than copying stale paths. No authoritative mission-wide physical-track/role roster was found in the inspected configuration files.

Build a versioned expected-role reference table before automated assignment. Prefer tape-specific track sheets, reel/box photographs, recording diagrams, digitizer wiring/channel maps and transfer notes. Record source, applicable tape/date/side/head, uncertainty, and contradictions. Use independently identified recordings and documented controller/loop conventions as supporting references. Do not expand ambiguous abbreviations into supposedly certain roles. Filename labels should be a weak prior, and hidden during blind evaluation.

## Evidence ledger and competing explanations

For each observation retain an evidence ID, source file identity, timebase and interval, excerpt hash, raw/processed version, measurement or quoted transcript, reviewer/model version, and alternative interpretations. Separate observation, hypothesis, and decision. A missing decoded timecode signal is not proof that its physical track was omitted.

| Explanation to test | Evidence that would help distinguish it |
|---|---|
| Fixed filename/acquisition-channel offset | Repeated independent role anchors support the same offset across several channels and early/middle/late intervals; transfer notes corroborate wiring/order. |
| Time-varying lateral displacement | Coordinated changes in neighboring physical tracks, reciprocal level/crosstalk changes, and consistent change times across unrelated content. Physical adjacency must first be established. |
| Azimuth or frequency-response problem | Changes in high-frequency response/phase or intelligibility, without necessarily changing role identity; specialist review and transfer records are needed. |
| Historical routing, patched loops, or controller handover | Content/speaker changes consistent with operations, without corresponding cross-track leakage/level changes. |
| Head-group timing or independent processing-map mismatch | Similar content matches after a stable or varying lag; evidence may explain timing rather than labels. |
| Duplicate/missing capture or fragment assembly issue | Near-identical waveforms across channels, discontinuities at source-fragment boundaries, inconsistent manifests, or missing expected content. |
| ASR or cleanup artifact | Alleged role changes disappear on raw listening or are unsupported by audible speech. |

A hypothesis may explain only one tape or interval. Record counterexamples rather than forcing a mission-wide explanation.

## Cheap content-first identification after WhisperX

1. **Freeze transcription provenance.** Record the exact processed input, source and map hashes, ASR/alignment/diarization versions and settings, language, and chunk offsets. Keep word/segment times, unaligned words, and available quality indicators. Diarization IDs are not named people or permanent identities across files. Forced alignment locates supplied words; it does not independently prove those words were spoken. The [WhisperX paper](https://arxiv.org/abs/2303.00747) describes VAD and alignment and the underlying transcription/timing failure modes.
2. **Retrieve evidence cheaply.** Index transcripts with lexical/BM25-style search for callsigns, self-identification, distinctive controller vocabulary, procedural exchanges and repeated response patterns. Build a small reviewed vocabulary with variants and ASR confusions. A call addressed to “CAPCOM” identifies an addressee, not necessarily the speaker or this channel's dominant loop. Generic mission words and shared air-ground chatter are weak evidence.
3. **Create anchor segments.** Human-confirm a few audible, high-specificity exchanges across different times and conditions. Retain exact words and timestamps, nearby dialogue, competing roles, and whether speech is dominant, background, or mixed. Silence/noise does not imply an absent role. Reject repeated boilerplate, implausible text and uncertain callsigns until checked against audio; ASR can hallucinate in noise.
4. **Optional small local model.** Give one local classifier only retrieved passages, the reviewed role definitions, and source/time citations. Ask for candidate roles, supporting quotations, counterevidence, and `unknown`/`multiple_roles` when appropriate. Do not send entire tapes of transcript, ask for physical-head diagnosis, or treat generated confidence as a probability. Cache results by transcript/model/prompt hash. Lexical retrieval remains useful if a model adds little.
5. **Independent validation.** Reserve manually labelled intervals not used for vocabulary design, prompts, assignment tuning, or anchor selection. Hide delivered labels and proposed offsets from the reviewer. Report precision of accepted assignments, coverage/abstentions, mixture errors, and change-boundary uncertainty—not just overall agreement. Sample disagreements and quiet/poor-quality intervals as well as easy examples.

Recurring speaker identity can strengthen an inference, but one controller can appear on multiple feeds, shifts change personnel, and weak/overlapping speech makes diarization unreliable. Speaker embeddings are optional supporting evidence, not role or physical-track ground truth.

NASA's [mission-transcripts index](https://www.nasa.gov/history/mission-transcripts-mercury-gemini-and-apollo/) includes the Apollo 15 technical air-ground transcript. Record its exact edition and timestamps and match distinctive exchanges. Matching a public transmission establishes shared content and potentially timing; controllers monitoring that transmission can make it appear on many channels. Air-ground transcripts do not cover every controller loop or constitute a physical-track roster. The [WhisperX project documentation](https://github.com/m-bain/whisperX#limitations) also identifies alignment, overlapping-speech and diarization limitations; its speaker IDs must not be treated as verified cross-file identities.

## Acoustic checks and time-varying assignment

Use short, simultaneous windows around content anchors and suspected changes. Compare raw and cleaned audio: cleanup can remove identifying tones, obscure faint leakage, or change spectral fingerprints. Never overwrite raw evidence with the transcription input.

Useful supporting measurements include speech envelopes, spectral fingerprints, recurring tone signatures, transient timing, and lagged cross-channel waveform correlation. Measure correlation at plausible lags across several separate events, not a single periodic tone. Shared air-ground feeds, electrical interference, and duplicated digitization can all correlate strongly. Compare foreground/background levels and distinctive local speech to distinguish common monitoring audio from an actual duplicate. Preserve measured lag as evidence, not an automatic head-offset correction.

Start with coarse temporal windows, then split around sustained changes in role evidence, speaker mixtures, cross-channel similarity, or level/spectral patterns. Require repeated evidence and permit a boundary interval rather than pretending every change has an exact timestamp. Mark low-quality windows unknown. Check whether changes coincide with source-fragment boundaries or processing/reference transitions before attributing them to tape motion.

Combine evidence in a **time-varying assignment model**, such as a constrained sequence model or min-cost flow over intervals. Prefer persistence but allow supported changes. Treat the proposed +2 relationship (original CH3 → delivered CH1) as a soft competing prior, alongside no shift and other explanations. Do not assume wraparound, numeric adjacency between interleaved heads, or one fixed permutation.

Allow missing expected tracks, duplicated captures, unmapped channels and multiple audible roles. One-to-one constraints are only appropriate for a supported physical-track interpretation; content roles may legitimately repeat across channels. If a physical topology is documented, test whether inferred neighboring-track relationships and simultaneous changes are consistent with it. A dominant-role label and a physical-track hypothesis should remain separate outputs. Do not let a global constraint force a confident answer in an evidence gap.

## Timebases: preserve the chain, not just a timestamp

### Concrete crosstalk signal investigation

Ben specifically proposes detecting adjacent-channel crosstalk from the signal. For a future bounded test, start with a distinctive utterance strong on one channel and a weaker apparently matching copy on another. Inspect simultaneous raw windows first; use cleaned versions as a comparison, not the sole evidence. Channel-specific nonlinear cleanup can alter the weak copy, and independently resampled files require their actual timing maps before comparison.

Estimate local delay with lagged cross-correlation, then measure band-limited correlation and magnitude-squared coherence across several speech bands and separate utterances. Keep amplitude/gain ratios and their frequency dependence, not only a normalized correlation score. Mask or separately analyze common hum and stable tones so they cannot dominate the match. Include donor-only, target-only and simultaneous-speech intervals; the direction of leakage cannot be established by symmetric correlation alone.

Build a time-indexed adjacency graph: nodes are immutable delivered channels; edges record candidate weak-copy relationships, lag, relative strength, coherence, evidence IDs and uncertainty. Compare edge changes with the independently documented physical head/track layout, not assumed filename-number adjacency. A content migration accompanied by reciprocal gain loss/increased neighboring leakage is more informative about displacement than a transcript change alone. Deliberate monitoring feeds, routing changes, duplicated captures and shared electrical interference remain competing explanations even when correlation is high.

Keep detection separate from subtraction. See [Crosstalk removal research](CROSSTALK_REMOVAL_RESEARCH.md) for a conservative reference-cancellation pilot, its failure cases and preservation checks. No crosstalk measurement or removal has been performed for these documents.

### Ben's reported 1.3-second timing discriminator

Ben's exact statement:

> For the crosstalk, we can take advantage of the 1.3 second offset between tracks. If two adjacent channels were having the same conversation historically, then, that conversation would have at 1.3 second offset between channels. If it’s cross talk, then the 1.3 second offset would be present in the cross talk. This goes the same for if many of the channels are listening to the space to ground communications in the background of their conversations. you’ll be able to tell if that’s cross talk by if that exact same timing of that communication is on an adjacent channel

Treat the reported approximately 1.3-second track/head offset as a valuable working prior, not a measured universal constant. The wording about the offset being present in crosstalk is ambiguous without a timebase. The working interpretation is to distinguish a weak copy at the donor's exact acquisition timing from genuine shared historical content at the calibrated head-group lag. Preserve the statement above separately from this interpretation.

Define pairwise lag as `L = target event acquisition time - donor event acquisition time`, after accounting for each file's acquisition start, but before historical content alignment. For genuine common historical content recorded on different offset head groups, expect the pair's calibrated lag `H`, potentially approximately +1.3 or -1.3 seconds. Playback/electrical leakage from the donor into another captured channel tends to follow donor acquisition timing, giving `L` near zero. If historical alignment then subtracts `H` from the target coordinate relative to the donor, genuine common content moves to lag zero and acquisition leakage moves to approximately `-H`. Do not mix these coordinate systems or infer the sign from channel numbers.

Build a pair-specific expected-lag table with tape and interval, immutable donor/target IDs, documented head groups/physical adjacency, file-start offsets, calibrated `H` with sign and uncertainty, calibration evidence independent of the suspected leak, and the exact timebase/map versions. Numeric neighbors need not use different heads; same-head pairs may have no useful timing separation. Do not assign every pair 1.3 seconds. Current [quindar_tape.py](../scripts/quindar_tape.py) explicitly searches zero, ±1.3 and ±13 seconds within a wider lag range, permits multiple local lag modes, and preserves head offsets. This supports testing competing lags; it does not independently verify Ben's reported offset for a particular pair.

For each candidate, compare lag hypotheses near zero and the calibrated positive/negative approximately 1.3-second offsets, retaining the wider search as a control against forcing a preferred peak. Use several independent utterances and speech bands, separately examining strong foreground speech and shared background space-to-ground communications. Record peak strength, uncertainty, relative level and coherence. Two supported peaks may indicate a mixture of historical shared content and acquisition leakage; retain both hypotheses rather than collapsing them to one offset. Add wrong-channel, time-shifted donor, donor-only and target-only controls; suppress common hum/periodic-tone dominance and check routing or fragment transitions.

Timing strengthens channel mapping and cancellation candidate selection but is not proof of crosstalk or its direction. Bleed recorded onto tape can follow historical timing, acquisition mechanisms can introduce other delays, and intentional routing or duplicated capture can produce near-zero copies. Misaligned files, uncertain head assignments, resampling and changing offsets can obscure the distinction. Preserve raw evidence, allow unknown/mixed outcomes, and never shift a flutter correction map merely to align shared dialogue.

### Mapping audio and transcript coordinates

| Coordinate | Required interpretation |
|---|---|
| Original file sample/frame | Stable primary location within that immutable canonical channel. |
| Canonical filename timeline | File-relative time plus its recorded start/origin offset; not automatically GET. |
| Denoised time | Verify frame count and start offset in its manifest; cleanup is normally duration-preserving here. |
| Defluttered time | Use the saved regional/continuous map and its direction. Equal total duration does not mean equal local content timestamps. |
| Retimed file/tape time | Use stage-05c input/output maps; account for shared-plan input/output start offsets. |
| WhisperX time | Relative to its exact input file or chunk; add chunk offsets before composing inverse maps. |
| External mission transcript time | A separate clock requiring documented content/time anchors and uncertainty. |

For current continuous rendering, `nominal_seconds.npy` maps original/cleaned sample-time positions to defluttered output time; invert it to locate original content from a defluttered timestamp. Stage 05c saves `timing_map.npz` with `input_seconds` and `output_seconds`. Compose these mappings in the correct order and preserve fractional-sample uncertainty. Check monotonicity, bounds and any excerpt origin; do not substitute a single duration ratio.

The current shared-tape implementation in [quindar_tape.py](../scripts/quindar_tape.py) records `input_start_seconds`, `output_start_seconds`, and preserves original head offsets. Freeze and reference the same shared map for simultaneous channel comparisons. Older single-channel retimed files, including the assessed T510083 CH20 example, need their own actual maps; do not assume they already use that shared plan. Record content-lag/head-offset hypotheses separately from playback-clock correction. Do not realign channels or shift their flutter curves merely to make dialogue match.

## Non-destructive mapping sidecars

Keep filenames and original channel IDs intact. Publish provisional annotations first; apply reviewed display labels to transcripts, search results and derivative exports. A sidecar should contain:

- Schema/version, tape ID, original delivered label/path, sample rate, immutable source hash and hash scope (container versus decoded samples).
- Half-open original-frame interval `[start_frame, end_frame)`, corresponding processed/transcript intervals, and exact map/transcript hashes.
- Candidate content roles, dominant/background/mixture interpretation, separate physical-track candidates, and missing/duplicate hypotheses.
- Decision status (`proposed`, `reviewed`, `rejected`, `unknown`, `multiple_roles`), evidence IDs, counterevidence and confidence category with an explicit rubric. Numeric probabilities only if calibrated on independent labels.
- Method/model/prompt version, reviewer/date, uncertainty at boundaries, previous revision and reason for change.

Reuse existing verified hashes where appropriate; a deferred full-file hash is explicitly pending, never invented. Do not use mutable path or timestamp alone as permanent identity. Permit alternative hypotheses and revisions without replacing original observations. Downstream consumers must distinguish the delivered label from the inferred one and retain links back to source samples.

## Bounded pilot and stop criteria

After processing, start with **one suspect tape and, if available, one independently documented comparison tape**. Inventory all delivered channels from existing manifests, including silent/IRIG-labelled channels. Use three separated 2-minute windows per tape: up to 180 channel-minutes per 30-channel tape, not full-mission transcription solely for identification. Reuse planned WhisperX output if already available. Add only a few short clips around candidate changes; record the additional budget before expanding.

Build a small human-reviewed anchor set; hold back at least one temporal window and additional difficult snippets for blind assessment. Run lexical retrieval first, then at most one small local-model pass on ambiguous retrieved passages. Validate suspected swaps with short simultaneous raw/cleaned acoustic comparisons. Export a ledger, proposed interval sidecars and a compact review sheet; no renames or automatic production routing changes.

Stop if evidence is insufficient: retain unknown/mixture outputs and list the missing reference material. Proceed to another tape only if accepted assignments remain accurate on the independent holdouts and the process saves reviewer time. A failed fixed-offset hypothesis is a useful result. Good role identification does not prove a physical cause or recover missing tracks.

## Questions the pilot should resolve

Which tape-specific track sheets and digitization wiring/head-layout records exist? What observations led to the original-CH3/delivered-CH1 suspicion, at which original timestamps? Are CH1/CH2 absent, weak, mixed, or captured under other labels? Do changes occur together across physically adjacent tracks, or follow content routing and speaker shifts? Which output timebase will WhisperX use? These are investigation questions, not prerequisites for preserving evidence or building a provisional index.

Documentation only: no transcription, audio scan, model execution, installation, tests, source modification, or interruption of denoising was performed to create this plan.
