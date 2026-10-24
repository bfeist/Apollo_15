# Crosstalk removal: feasibility and bounded research plan

2026-10-18. **No removable crosstalk has yet been confirmed.** This is a proposal, not an implemented processor or a recommendation to redo the current multi-day denoise batch. Original recordings, production code and accepted outputs remain unchanged.

Related: [Channel identification and suspected mislabelling](CHANNEL_IDENTIFICATION_AND_MISLABELLING.md). Establish evidence for a leakage relationship before attempting cancellation. Ben's possible head-position/track-offset explanation remains unconfirmed; identifying similar speech does not establish its physical cause.

## When subtraction could help

If one captured channel contains a relatively clean source and another contains a weaker, predictably filtered/delayed copy, it may be possible to estimate that leakage and subtract part of it. A conceptual local model is:

`target[n] = wanted[n] + sum_k h_t[k] * donor[n-k] + other_noise[n]`

Here `h_t` includes gain, frequency-dependent response and delay, and may change slowly with time. A single gain subtraction is only a restricted special case. Fractional-sample delay and small clock drift matter because phase mismatch can leave residuals or reinforce interference. This model is a hypothesis to test, not an assertion that the tape is a linear stationary mixture.

Reference-based adaptive cancellation relies on a reference correlated with the interference, without significant wanted signal contamination. The [MathWorks adaptive-noise-cancellation example](https://www.mathworks.com/help/dsp/ug/adaptive-noise-cancellation-using-rls-adaptive-filtering.html) illustrates estimating a transfer path from a separate reference. It does not establish suitability for Apollo recordings or require using MATLAB.

First distinguish physical neighboring-track bleed from deliberately shared/routed conversations. The same transmission may legitimately belong in both channels. Removing it merely because it correlates would alter the historical communication record. A clean-looking donor may itself contain target speech or reciprocal leakage; simple subtraction could then erase the target. A source never captured cannot be reconstructed by relabelling or cancellation. Recovering a faint surviving copy elsewhere, if possible, is a separate claim requiring its own evidence.

## Establish the pair and its signal domain

Use the identification ledger and time-indexed adjacency graph to choose one strong donor/weak-copy pair. Do not assume numerical channel adjacency is physical adjacency. Record several matching utterances, relative levels, local lags, speech-band coherence and their variation. Exclude common mains tones from speech-copy evidence. Check alternate explanations: monitoring feeds, source routing, shared microphones, repeated content, and duplicated digitization.

Work in a documented common acquisition-time coordinate. Original files can retain different starts/head offsets; shared timing provenance does not guarantee simultaneous dialogue. Keep measured content lags distinct from transport correction. Independently defluttered or retimed files require inverse/composed saved maps before pairwise estimation. A frozen shared tape map helps preserve a common clock, but does not remove physical head/content offsets.

Estimate on raw data first, with copies conditioned identically only where justified. Denoising can remove faint leakage and independently change phase, amplitude or speech detail. A filter estimated on raw channels cannot simply be applied to independently cleaned/retimed channels. Estimate and apply in the same compatible domain, or explicitly transform and validate the model. A shared nonlinear time warp also changes the effective delay/filter response; do not assume cancellation and resampling commute.

A plausible future branch is aligned raw-pair cancellation followed by normal cleanup and timing correction, while original reference tracks remain untouched for flutter analysis. Another is cancellation in a verified common processed domain. Compare these only if the small raw-domain pilot succeeds; neither branch should replace current production on speculation.

### Use the reported head offset to select cancellation candidates

Ben reports an approximately 1.3-second track/head offset and proposes using the exact timing of matching foreground conversations and background space-to-ground communications to distinguish leakage from legitimate shared content. His exact statement and the ambiguity in its timing wording are preserved in [the identification plan](CHANNEL_IDENTIFICATION_AND_MISLABELLING.md#bens-reported-13-second-timing-discriminator). Treat the offset as a valuable working prior requiring pair-specific calibration, not an established lag for every adjacent channel.

Use `L = target event acquisition time - donor event acquisition time`, correcting file-start origins but retaining historical head offsets. Genuine common historical content on different offset head groups should follow the calibrated pair lag `H`; playback/electrical leakage into another captured channel tends to follow the donor's acquisition timing near `L = 0`. After historical alignment subtracts `H` from the target coordinate relative to the donor, common historical content is near zero and that acquisition leakage is near `-H`. State which coordinate system every measurement uses. Do not infer the sign, head group or physical adjacency from channel numbering. Same-head pairs may offer no timing discriminator.

Before fitting a cancellation filter, require a pair-specific expected-lag table: tape/interval, donor and target identities, head-layout evidence, file-start offsets, independently calibrated signed `H` and uncertainty, and timebase/map versions. Compare hypotheses around zero and calibrated ±1.3-second offsets across several independent utterances and speech bands, retaining a wider lag search as a control. The existing [quindar_tape.py](scripts/quindar_tape.py) considers zero, ±1.3 and ±13 seconds and retains original head offsets; its search range is not proof of a physical offset. Analyze foreground speech and common background space-to-ground audio separately, keeping gain ratios, coherence and lag-peak uncertainty.

Dual peaks can reflect both legitimate head-delayed content and a donor-timed weak copy. Preserve that mixture interpretation: a cancellation candidate must isolate the proposed leakage path without subtracting the legitimate shared feed. Include wrong-channel and time-shifted donor controls, donor-only/target-only passages, overlap and routing-transition checks. Fit and apply only in the same documented timing domain; transfer through independently defluttered or retimed coordinates requires the actual saved maps and fresh validation.

This discriminator strengthens pair selection, not authorization to subtract. Recorded-on-tape bleed can follow historical timing; acquisition paths can have nonzero delays; intentional routing and duplicate capture can also match donor timing. Uncertain head assignments, drift, changing offsets and weak/periodic signals can make the hypotheses indistinguishable. Do not force a 1.3-second model or cancel an ambiguous shared transmission. Retain bypass/unknown outcomes and all target-preservation tests below.

## Conservative estimator candidates

Start with robust, regularized least-squares FIR estimation on a few **donor-active, target-quiet** intervals confirmed by listening. “Quiet” must mean no wanted target speech, not just low RMS or a VAD decision. Fit delay, gain and a short frequency-dependent path with bounded coefficients/filter energy. Choose filter length and regularization using separate validation intervals; long filters can fit unrelated speech and noise.

Freeze that fit and evaluate different passages, especially simultaneous independent donor and target speech. For gradual changes, compare local frozen fits first. A later constrained Wiener/FIR or normalized least-mean-squares (NLMS) approach could update slowly, with adaptation disabled during target speech, uncertain overlap, dropouts or abrupt routing changes. Limit step size and coefficient changes; fading to bypass is preferable to adapting through ambiguous evidence. These are candidate techniques, not verified settings.

For several donors, use a sparse adjacency-supported multichannel model with regularization. Correlated references and intentional shared feeds can make the solution non-identifiable. Bidirectional bleed requires a joint model rather than successive blind subtraction. Direct mixing-matrix inversion is only defensible when the estimated model is sufficiently well-conditioned and stable on independent passages; otherwise it can amplify noise and estimation errors. Record conditioning and uncertainty, and abstain where the source mixtures cannot be separated reliably.

Apply partial cancellation first, with a predeclared conservative attenuation/subtraction-strength cap and an output-energy guard. For example, compare bypass and a half-strength predicted-leakage subtraction before considering full strength; this is a trial setting, not an established safe value. Cancellation strength is not itself a guarantee of target preservation. Unsupported bands/intervals stay unchanged, with short validated ramps at entry/exit to avoid clicks. Never automatically normalize each comparison independently.

## Validation must protect the target

| Check | What it establishes or detects |
|---|---|
| Known synthetic mixtures of independently selected real excerpts | Recovery/error against a known target; include frequency-dependent leakage, fractional delay, changing gain, correlated sources and reciprocal bleed. Digital mixtures do not prove the historical mixing model. |
| Donor-only held-out passages | Whether a frozen transfer filter predicts leakage outside its training data. |
| Target-only held-out passages | Unwanted attenuation, new artifacts or overfitting when the donor should not drive subtraction. |
| Simultaneous independent speech | Whether leakage reduction preserves target words, consonants and timing under overlap. |
| Wrong donor, shifted donor and no-leakage controls | Whether apparent improvement is generic suppression rather than correct cancellation. |
| Transitions, weak speech, dropouts and routing changes | Failure of a stationary fit, unstable adaptation and boundary artifacts. |

Measure donor-related residual energy/coherence alongside target-preservation error, spectral change, transient/peak behavior and boundary continuity. On known mixtures, report target reconstruction error and target distortion, not just interference reduction. On historical mixtures the clean target is unknown: lower correlation, less energy, or a better ASR transcript alone cannot prove preservation.

Listen at matched gain to **before, after and the removed estimate**. Wanted target words in the removed estimate are grounds to reject the setting. Use blinded order on held-out snippets, keep unsuccessful examples, and record reviewer disagreements. Separate training, parameter selection and final blind tests; do not select only good-sounding outputs. Define acceptance thresholds from a small independently labelled pilot before expanding; no numerical “safe” threshold is established here.

## Role of AI and blind source separation

Transcripts and a small local text model can retrieve matching utterances, identify possible overlap and organize evidence with timestamps. They cannot establish the mixing path or certify that subtraction preserves speech. ASR can hallucinate on weak/noisy signals; confirm crucial words by listening. Do not synthesize or replace historical speech.

ICA or other blind separation is exploratory: independence, stationarity and sufficient independent observed mixtures may be violated by common mission chatter, routing and reciprocal bleed. Permutation/scale ambiguities also need explicit handling. Generative speech enhancement may invent or suppress detail and is unsuitable as an unqualified archival restoration master. Any such comparison belongs in separately labelled derivatives with raw originals preserved; it is not the first pilot.

## Small future pilot and decision

After alignment and pair identification, select **one pair and at most six 20–30-second paired excerpts**: two donor-active/target-quiet fitting examples, one development example, and three untouched tests spanning target-only speech, overlap and a later/changed condition. If those conditions cannot be verified, stop rather than manufacture a “clean” training set. Add a small set of known synthetic mixtures from the same bounded reads; no mission-wide scan or model calls are needed.

Use one local CPU worker initially and existing libraries where suitable. Save a separate report folder containing the frozen input/mapping hashes, source-frame intervals, delay/filter coefficients, training masks, candidate settings, conditioning, model domain, metrics and a few matched before/after/removed clips. Include the identification sidecar version and explicit failure/bypass intervals. Never overwrite the donor, target, timing references, current denoised intermediates or approved examples.

Proceed only if a frozen model reduces leakage in independent tests without audible target loss, unstable gain, or misleading assignment of deliberate shared content. If the fit fails, document whether alignment, contaminated references, time variation or insufficient evidence prevented inference. Even successful local removal does not establish a whole-tape method. Do not redo the multi-day batch until a demonstrated benefit and a compatible processing order justify a separate, reversible derivative workflow.

Documentation only: no removal, audio scanning, model execution, installation or tests were performed to create this plan.
