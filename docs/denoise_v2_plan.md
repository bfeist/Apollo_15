# Denoise v2 findings and rollout

V2.16 is the current candidate. The [all-tapes runbook](denoise_v2_all_tapes_runbook.md)
covers the 39-tape inventory, full-channel canary, listening gate, and guarded
move of the existing output to `denoised_v1`. New production output uses
`denoised`.

## Current listening and measurements

The original complaint was speech ringing near 09:28 in the denoised-only
T510083 CH20 review. The owner preferred the local 05 hum-subtraction trial
and accepted attenuation of speech energy where it overlaps established hum.
Preserve the comm hiss, original timing, and the exact 05 reference and
provenance. The v2.13 spots received positive feedback limited to those files.
The longer v2.13 comparison exposed a separate hum recurrence at 25:30–26:20.

In the recurrence, the original 180 Hz line stayed steady while the 3180 Hz
phase reference lost confidence. The earlier correction weakened, and
wrong-phase estimates reinforced 420, 540 and 660 Hz in a speech gap. V2.16
independently qualifies persistent low residuals from the original and applies
a compact projection after blending neighboring anchor estimates. It checks
for repeated reinforcement and still gates the correction at genuine source
dropouts. The compact projection has ±50 ms support. Earlier adaptive stages
have their own finite responses.

- The [v2.16 listening guide](../reports/denoise_v2_late_hum_20261108/LISTEN.txt)
  covers the 30-minute CH20 review at canonical seconds 13,200–15,000, the
  focused late-hum excerpt, preferred 05, and three other tape spots.
  Comparison WAVs have matched intervals and gains.
- The 180 Hz line fell about 46 dB from the original during 25:35–26:03. In
  the 25:50–25:53 gap, the 420, 540 and 660 Hz lines fell about 33, 31 and 31 dB.
  The 180 Hz residual still measured about 12 dB above the neighboring floor
  in that gap.
- The full 30-minute render reduced quiet-grid energy by 26.00 dB and
  changed quiet off-grid energy by −0.510 dB. The two-minute excerpt matches
  the full render sample for sample. The preferred-05 excerpt is identical
  to v2.13, and its real burst refit measures −81.19 dBFS in the neighboring
  speech band.
- The [latest spectral audits](../reports/denoise_v2_low_hum_audit_20261108/)
  retain both the short and full v2.16 measurements. The full-channel
  T510083 CH20 canary still requires listening acceptance before cutover.

## Processing contract

Fit and subtract from original samples. Use source-grid anchors, retain
bounded neighboring-model context, and preserve offsets and length. Confirm
candidate lines using independent observations. Apply constant peak gain after
subtraction with a −3 dBFS program peak target and a +12 dB boost cap. Exclude
qualifying endpoint impulses from gain measurement while preserving those
samples in the float WAV.

A full scan of the 30 previous v1 outputs for T510083 measured a CH26 peak
of −29.62 dBFS. The owner heard no program content in a spot check, so its
peak is excluded from the gain-policy comparison. CH01 is timing-only.
The largest computed gain among the remaining 28 channels is +9.38 dB.
The [local scan](../reports/denoise_v2_rollout_20261108/v1_gain_scan_T510083.json)
records the measured peaks and endpoint checks for every channel.
Keep sustained absence distinct from missing or masked evidence. Apply the
qualified compact bank once after baseline blending and taper the end of
its coverage over up to six seconds. Record unsupported intervals and
residual flags for review. Process all numbered channels, including CH01.
Use the unchanged originals for timing evidence. Denoising precedes
crosstalk subtraction, followed by the shared tape
timing map.

## Local evidence

| Location | Contents |
| --- | --- |
| `reports/denoise_v2_late_hum_20261108/` | Current v2.16 review WAVs, decisions, regression excerpts and provenance |
| `reports/denoise_v2_low_hum_audit_20261108/` | Current residual measurements and verification tools |
| `reports/denoise_v2_rollout_20261108/` | Frozen inventory and inventory generator |
| `reports/denoise_diagnostic_20261108/` and `reports/denoise_v2_reference_05_20261108/` | Original preferred 05 and its frozen reproduction package |
| `reports/denoise_v2_original_review_20261108/`, `reports/denoise_v2_continuous_bank_20261108/` and `reports/denoise_v213_preflight_20261108/` | Earlier bound listening records and exact comparison inputs retained at their original paths |
| `archive/denoise_v2_superseded_20261108/` | Superseded local reports, the prior plan, and the verified move manifest |

For historical replay, restore any needed archived directory to its original
`reports/` path using the archive manifest. Some historical scripts and
manifests use those paths directly. All local reports and audio remain
outside version control.
