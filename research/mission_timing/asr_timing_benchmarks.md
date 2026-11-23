# Parakeet and Spur gate benchmark

The tables below use outputs from `runs/mission_timing_pilot/asr/`.

FP32 Parakeet and source-safe distil Whisper were tested on the 3-hour T510093 census and a 1,720-second set containing PAO, CH23 and other 05b channels, DVD, journal audio, and a 120-second CH30 empty control. Each gated test used the same 0.12 Silero windows. The Parakeet benchmark independently re-ran Silero to verify every saved window against the exact source. It sent each window as an independent continuous excerpt and batched by duration. It wrote token onsets back on the timebase of each source. The TDT output supplied no word ends.

| Engine and gate | Suite | Source read and resample | CPU VAD | Decode and emit | Total per trial | Source hours per processing hour |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Parakeet batch 16, 0.12 | Census | 0.28 / 0.30 s | 1.82 / 1.97 s | 21.07 / 52.14 s | 23.18 / 54.43 s | 278.3 median |
| Parakeet batch 8, 0.12 | Census | 0.30 s | 1.95 s | 22.82 s | 25.07 s | 430.7 |
| Distil Whisper batch 16, source-safe 0.12 | Census | 1.82 s preparation including VAD | | 21.31 s | 23.13 s | 466.9 |
| Parakeet batch 16, 0.12 | Matched | 1.45 / 1.40 s | 0.47 / 0.45 s | 4.61 / 6.90 s | 6.53 / 8.76 s | 224.9 median |
| Parakeet batch 8, 0.12 | Matched | 1.31 s | 0.42 s | 4.28 s | 6.01 s | 286.1 |
| Distil Whisper batch 16, source-safe 0.12 | Matched | 1.89 s preparation including VAD | | 3.27 s | 5.16 s | 333.5 |
| Parakeet batch 8, continuous 24 s clips | Matched | 1.76 s | 0 s | 5.57 s | 7.33 s | 234.5 |

These totals include each read, gate and recognition pass. Model loading and warm-up were excluded from throughput. Wall times with both setup and all trials were 97.39 s for batch 16 gated, 33.79 s for batch 8 gated and 10.22 s for batch 8 continuous. The two batch 16 decode times varied substantially. Even its faster census trial was close to the source-safe Whisper run. Triaged comparisons are in `parakeet_vad012_batch16`, `parakeet_vad012_batch8`, `parakeet_matched_ungated_batch8`, `census_T510093_safe_vad012`, and `matched_safe_vad012`. The Parakeet gated runs produced zero out-of-span token starts. Duration sorting limited input padding to 4.4% at batch 16 and 2.0% at batch 8 for the census.

In the matched set, 0.12 VAD passed only 6 of the 21 independently picked Spur onset times from the three editions. Of those cases, the gated Parakeet run delivered 5 recognizable opening tags and gated Whisper delivered 4. Continuous Parakeet located a recognizable version of all 21 openings in the ordered exchanges, though it misrecognized some words, including the crater name. Its onset tags tended to precede the first visually picked voiced sound. Median signed difference was −0.26 s and maximum absolute difference was 0.45 s. Continuous Parakeet emitted zero tokens for the 120-second CH30 control. These cases measure recognition and timing in the reviewed passages. The full census has no comparable human-labeled false-positive test. Per-pick results are in `parakeet_spur_onset_benchmark.tsv`.

## CPU VAD alternatives

The alternate gate used the exact same matched and census sources on four CPU workers. The boost path bandpasses 220–3200 Hz, computes a 1-second smoothly interpolated RMS gain from 20 ms frame energies, and boosts up to eightfold toward a 0.045 RMS target while leaving loud sections unboosted. The resulting sample-aligned gate input is separate from the source waveform used for inference. The padding experiment adds 1.1 s of context to each side of the existing 400 ms-padded .12 windows and measures the union within each source.

| Gate | Spur start / complete bound coverage | Matched retained | Census early / middle / late retained | Census overall retained | Census read, resample and VAD |
| --- | --- | ---: | --- | ---: | ---: |
| 0.12 baseline | 6/21 · 5/21 | 39.76% | 29.50% / 42.36% / 45.46% | 39.11% | 3.90 s |
| 0.06 | 11/21 · 8/21 | 43.89% | 30.11% / 45.63% / 46.78% | 40.84% | 3.92 s |
| 0.12, wider padding | 16/21 · 15/21 | 50.62% | 32.83% / 48.67% / 50.58% | 44.03% | 3.90 s |
| 0.12, voice-band RMS boost | 20/21 · 20/21 | 66.48% | 35.75% / 72.91% / 63.70% | 57.46% | 5.39 s |

The RMS boost captured both the TV call and readout opening on all three sources. Its sole missed point was Irwin’s earlier debris description on CH23. The wider-padding case missed all three readout openings and two TV openings. All four variants left the CH30 control empty. The source-census wall time of 5.39 s for the boost comprised 0.52 s probe, I/O and resampling plus 4.87 s for the transform and VAD. The entire CPU experiment took 16.21 s. These retention figures measure which source samples would reach the recognizer. `spur_vad_experiment/vad_experiment.json` stores the frame ranges and per-region results, and `spur_vad_experiment/spur_onset_inclusion.tsv` records inclusion per pick.

```powershell
uv run --with faster-whisper==1.2.1 python research/mission_timing/benchmark_spur_vad.py `
  runs/mission_timing_pilot/asr/census_inputs.json `
  runs/mission_timing_pilot/asr/matched_inputs.json `
  catalog/spur_exchange.tsv `
  runs/mission_timing_pilot/asr/spur_vad_experiment
```

## Distil Whisper with the boosted gate

The same source-safe batch 16, beam 1 settings were used for all five runs. The boost changes the gate’s selected time ranges. The recognizer reads the original unboosted samples from those ranges. Actual wall includes input setup, model startup, decoding, output and provenance checks. It excludes the Python launcher. The pooled rate comes from source hours divided by the sum of the three census walls, with equal source duration from each tape.

| Source sample | Source hours | Retained by gate | Prepare and decode | Actual wall | Source hours per wall hour |
| --- | ---: | ---: | ---: | ---: | ---: |
| T510077 census | 3.0 | 34.20% | 22.142 s | 23.815 s | 453.5 |
| T510093 census | 3.0 | 57.46% | 30.844 s | 32.422 s | 333.1 |
| T510100 census | 3.0 | 9.69% | 8.250 s | 9.898 s | 1,091.2 |
| Pooled census | 9.0 | | 61.235 s | 66.134 s | 489.9 |
| Sustained busy T510093 | 7.5 | 68.33% | 90.832 s | 92.443 s | 292.1 |
| Matched PAO, 05b, DVD and journal | 0.478 | 66.48% | 7.628 s | 12.000 s | 143.3 |

The pooled census projects 39.17 hours for 19,191.88 channel-hours. The sustained busy sample projects 65.71 hours if the archive behaves like that sample. The three-tape pool is a pilot with equal tape weighting. It does not measure archive-wide activity, long-run throughput, remote storage, or recovery jobs. On the same sustained source intervals the unboosted 0.12 run took 58.46 seconds wall and retained 41.85% of source time.

### Audio retained, words recognized, and time recovered

The boosted gate kept 20 of 21 visual opening positions, including both the TV and rover calls on all three editions. The decoder returned identifiable text for all 21 to some degree, including 19 full or contextually recognizable openings and two partial openings. It recovered near-visual TV and readout start tags on PAO and CH23. Across nine usable first-word tags, median absolute error against the first voiced sound was 0.232 s and the maximum was 0.332 s. The crater-name tags remain contextual and the two partial transcriptions cannot time the beginning of the printed call.

Nine case openings received zero-duration tags. The worst raw discrepancy was −17.592 s on the DVD shadow response. The DVD readout wording was recovered in sequence, with its tag collapsed to 1617.452 s versus a visible onset of 1629.61 s. Across the entire matched output, 859 of 2,298 words had zero duration, compared with 285 of 1,621 in the earlier source-safe unboosted test. Retained audio and recovered words improve recall. The packed Whisper timing still requires validation or separate bounded alignment. The case-by-case record is `voice_gain_whisper_spur_onsets.tsv` in the ASR run directory.

### Reproduce the boosted runs

```powershell
$apolloAsrRuns = 'runs/mission_timing_pilot/asr'
$apolloCudnn = 'C:/Program Files/NVIDIA/CUDNN/v9.10/bin/12.9'
$env:PATH = $apolloCudnn + [IO.Path]::PathSeparator + $env:PATH
$apolloGainSuites = @(
  @{ Inputs = 'matched_inputs.json'; Output = 'matched_safe_vad_voice_gain' },
  @{ Inputs = 'census_inputs.json'; Output = 'census_T510093_safe_vad_voice_gain' },
  @{ Inputs = 'sustained_T510093_inputs.json'; Output = 'sustained_T510093_safe_vad_voice_gain' },
  @{ Inputs = 'census_T510077_inputs.json'; Output = 'census_T510077_safe_vad_voice_gain' },
  @{ Inputs = 'census_T510100_inputs.json'; Output = 'census_T510100_safe_vad_voice_gain' }
)
foreach ($apolloGainSuite in $apolloGainSuites) {
  uv run --with faster-whisper==1.2.1 python research/mission_timing/benchmark_asr.py `
    --inputs "$apolloAsrRuns/$($apolloGainSuite.Inputs)" `
    --output "$apolloAsrRuns/$($apolloGainSuite.Output)" `
    --model distil-large-v3.5 --batch-beam 16 1 --vad-threshold 0.12 `
    --vad-voice-band-rms --dll-dir $apolloCudnn
  if ($LASTEXITCODE -ne 0) { break }
}
```

Use fresh output directory names for a rerun. Each measured output directory contains `completion.json` with the wall time, `timing.json` with the model configuration and phase timings, and `sources.json` for source bindings.
