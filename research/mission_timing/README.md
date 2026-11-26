# Reproduce the Apollo 15 mission-timing pilot

The pilot has 31 identifiable openings of scan-reviewed transmissions on the stereo PAO reel. A three-source comparison later found a 4.279 s repeat on PAO. Four picked calls precede it and 27 follow it. The tables carry local samples, source checksums, printed times and page locators. The PAO, DVD and tape observations of one call inherit the same printed time. [Post-repeat anchor table](catalog/pao_572aaa_after_repeat_predictions.csv) · [All observations and initial whole-reel comparison](catalog/pao_572aaa_anchor_predictions.csv) · [Mission timing plan and results](../../docs/14_2026-11-22_mission_timing_plan.md)

## Repeat the fit from the reviewed measurements

For transcription performance, the two workload scenarios for the higher-recall first pass projected to 39.2 and 65.7 hours on one 4090. [Measured RTX 4090 speed and speech timing](asr_timing_benchmarks.md)

Run from the repository root. The saved table is sufficient to rebuild the fit on a machine without the audio. The cutoff at 9,360 PAO seconds was chosen before fitting. All copies of an exchange share a fold. The reported final map refits the selected model to all qualified exchanges after scoring the reserved region.

```powershell
uv run --with matplotlib python research/mission_timing/fit_pao.py research/mission_timing/catalog/pao_572aaa_after_repeat_predictions.csv --out-dir runs/pao_refit --report-dir reports/pao_refit --source-id '572-AAA after repeat' --duration-seconds 11699.323708333333 --holdout-start-seconds 9360
```

## Rebuild the source-time picks

Use the NASA technical scan and the matching audio editions. The source identities, page indexing and GPU setup are documented separately. The plot renderer runs on CPU. This pilot used Torch and TorchAudio 2.8.0 and the English `WAV2VEC2_ASR_BASE_960H` weights. The CTC character emissions supply a proposed opening. The visual picks use a locally displayed speech onset with both GET and model boundaries hidden. Their crop locations were suggested by CTC, so this review measures timing on identifiable calls and carries a selection bias toward visible openings. [Scan and extractor](catalog/pao_572aaa_transcript_reviews.md) · [ASR commands and environment](ASR.md) · [TorchAudio acoustic model](https://docs.pytorch.org/audio/2.8/generated/torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H.html)

```powershell
$run = 'archive/2026-11-26_reorg/runs/mission_timing_pilot'
$src = 'research/mission_timing'
$pao = 'N:/Projects/Apollo/Mission Data/Apollo 15/Audio/Internet Archive/572-AAA.wav'
$rows = 'research/mission_timing/catalog/pao_572aaa_transcript_reviews.tsv'
$full = "$run/asr/pao_572aaa_large_v3/segments.jsonl"
$distil = "$run/asr/pao_572aaa_distil/segments.jsonl"
$torchPython = 'C:/Users/Feist/.pyenv/pyenv-win/versions/3.10.11/python.exe'
uv run python "$src/find_calls.py" $rows $full "$run/call_candidates.jsonl" --field printed_text
uv run --no-project --python $torchPython python "$src/refine_calls.py" $rows "$run/call_candidates.jsonl" $full $pao $run
uv run --with matplotlib python "$src/onset_review.py" "$run/acoustic_audit.jsonl" $pao "$run/onset_review"
uv run python "$src/reviewed_anchors.py" $run
uv run python "$src/find_calls.py" $rows $distil "$run/distil_candidates.jsonl" --field printed_text
uv run --no-project --python $torchPython python "$src/refine_calls.py" $rows "$run/distil_candidates.jsonl" $distil $pao "$run/distil_review"
uv run python "$src/score_boundaries.py" $run
uv run python "$src/transfer_calls.py" $run $pao 'D:/Apollo_15_30-Track/defluttered_shared' 'D:/! Media Files/A15/Spacecraft Films'
uv run --with matplotlib python "$src/fit_pao.py" "$run/pao_anchors.csv" --out-dir $run --duration-seconds 11699.323708333333 --holdout-start-seconds 9360
uv run --with matplotlib python "$src/cross_source_timing.py" $run $pao 'D:/Apollo_15_30-Track' 'D:/! Media Files/A15/Spacecraft Films' 'archive/2026-11-26_reorg/reports/mission_timing_pilot'
uv run python "$src/audit_dvd_edits.py" $pao 'D:/Apollo_15_30-Track/defluttered_shared/A15_T510093/CH18/deflutter.wav' 'D:/! Media Files/A15/Spacecraft Films' "$run/dvd_edit_audit"
uv run --with matplotlib python "$src/pao_edit_check.py" $run $pao 'archive/2026-11-26_reorg/reports/mission_timing_pilot/pao_after_repeat'
```

`find_calls.py` retrieves a printed opening across the reel by words alone and retains a competing non-overlapping hit. The CTC step audits the first several recognized words with a second recognizer, including an unconstrained reading. A wrong crop or wrong words can defeat forced alignment. `score_boundaries.py` reports the number of scorable openings, signed and absolute errors, and the largest miss. Each source time is measured in seconds from its own file. Store word starts and ends separately when projecting either endpoint to a later timing map.

The visual catalog records the reviewed crop origin, bracket and edition hash. Changes to that crop or edition trigger a new review. The stored quarter-second working pick uncertainty allows for weak initial consonants and choice of landmark as well as the visible bracket width. For an independent comparison of the opening Spur exchange, each of the three recordings has its own spectrogram picks. [Visual review table](catalog/pao_572aaa_onset_picks.csv) · [Spur exchange across recordings](catalog/spur_exchange.md)

`transfer_calls.py` tests the established PAO phrases against separate 05b and DVD sources. Each target search requires two adjacent query windows to agree on the onset, sufficient separation from competing peaks, and a comparison against an unrelated passage. The resulting rows retain their common transcript event, target sample, 05b origin and release hashes. Acoustic transfer measures the same landmark in another edition. Feed identity and stagger require their own evidence. The PAO fit and the channel checks operate on metadata and leave the published audio intact.
