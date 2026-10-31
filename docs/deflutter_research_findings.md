# Deflutter investigation: October 30, 2026

No challenger qualified for real correction. Every one of the 39 tapes has
zero supported production seconds in this campaign. Numerical tone-jitter
reduction remains useful research evidence, but does not establish transport
correction or improved speech quality.

This summary preserves the conclusions of the local, Git-ignored
`reports/deflutter_iterative_20261030/DECISION_SUMMARY.md` after iteration I054.
The detailed evidence, source snapshots, hashes, maps, failures and exposure
ledgers remain in that campaign directory. The
[runbook](deflutter_iterative_research_runbook.md) defines the protocol;
[research/DEFLUTTER_CAMPAIGN.md](../research/DEFLUTTER_CAMPAIGN.md) maps the code
and reproduction commands.

## Findings and limits

- Discovery covered 1,200 source/version excerpts across nine tapes: 88 source
  hours, or 0.22916% of available source/version time, with zero read errors.
  Original and cached-denoised discovery remain separate. This measures search
  coverage, not detector recall or restored time.
- I016 and I024 are unqualified numerical leads. I024's worst residual was
  14.264744 Hz / 9.977118 cents across three validation events and
  9.944281 Hz / 7.069397 cents across two local final families. Both exceed the
  6 Hz / 5 cent restoration goals. The final families were consumed once.
- I026 showed stationary electrical mixtures passing the old validation/final
  checks. A later audit found held-out amplitudes influencing measurement
  eligibility. I050 repaired that feature-selection leak using calibration-only
  amplitudes and masked held-out intervals with a 25 ms guard.
- The repair did not authenticate motion. In I051, two of 24 paired synthetic
  cases were stationary, unwarped examples that passed complete validation and
  final checks, including the absolute numerical targets. Event-local
  interference cannot be inferred from the off-event nuisance samples.
  Further tuning on these examples would not resolve that failure.
- Current recommendations comprise two unqualified numerical leads, two
  event-scarce sampled cases, five sampled validation failures and thirty tapes
  unsearched in this campaign. Local failures and scarce events do not establish
  archive-wide absence of useful timing references.

## Independent witness routes

| Route | Bounded result | Decision |
| --- | --- | --- |
| Coded time track, I049/I052/I053 | Ten fixed 12-second original excerpts across four tapes; 120 raw/projected decoder attempts; zero valid frames | Stop these frozen paths. Other intervals and decoder variants remain unexamined. |
| Broadband delayed copy, I054 | One fixed CH11/12 pair; held-out correlation -0.02051 and residual/identity energy ratio 1.04170 | Availability failed; no new transport map was constructed. |
| Recorded electrical donor cancellation | Seventeen metadata survivors, none certified independent of target content or copy leakage | Source unavailable under the declared requirement; no real waveform FIR fit was run. |

A new experiment needs a distinct positive source or provenance lead:
demonstrable coded modulation, an adequate broadband copy with documented
delay/transfer order, or an independently identified electrical donor with
protected-content provenance. Previously consumed or exposed families remain
exposed after any implementation repair.

## Output assessment and retained artifacts

Local NISQA processed twelve WAVs across four 12-second windows; its predictions
are uncalibrated for Apollo radio audio. Offline ASR processed six I016 clips
without a trusted word-error reference. Spectral inspection supports local
tone-jitter reduction only. No human or assistant auditory judgment, perceptual
speech benefit, source upload or production rendering was established.

The campaign ended with no active workers and 51 research tests passing.
The saved test log records that historical check; the current repository suite
also covers the earlier v5 planner, checkpoint recovery and pipeline behavior.
Shared scientific implementation and policy were unchanged by the iterative
campaign. The retained v5 planner changes predate that campaign.

Generated reports, audio, numerical artifacts and listening-site exports remain
Git-ignored. Preserve their local copies for evidence reproduction. Start with
`DECISION_SUMMARY.md`, `PER_TAPE_RECOMMENDATIONS.md`,
`evidence_limits_feature_sealing_I050.json`,
`evidence_limits_event_local_I051.json` and `report_consistency_I054.json`
inside the campaign directory. The code alone does not include the audio or
full experimental evidence.
