# Spur Crater timing evidence

The PAO reel, the 05b render of T510092 CH23, and DVD Disc 04 Title 02 Part 01 carry the same Spur exchange. Scott’s TV call and Irwin’s rover readout are identifiable in order, with Houston’s “Go ahead, Jim” and Scott’s crater call between them. The readout numbers continue in the same order on all three recordings. The identity comes from the ordered calls, the distinctive numerical sequence, and corresponding acoustic patterns. [Original transcript, printed page 816](https://apollojournals.org/alsj/a15/AS15_TEC.PDF#page=787)

| Printed GET | Landmark | PAO elapsed | T510092 CH23 canonical 05b | DVD Part 01 audio presentation |
| --- | --- | ---: | ---: | ---: |
| 145:26:36 | First voiced sound of Scott’s TV call | 105.76 s | 60399.22 s | 1622.41 s |
| 145:26:46 | First voiced sound of Irwin’s shadow-device and rover readout call | 112.87 s | 60406.54 s | 1629.61 s |
| | Spacing | 7.11 s | 7.32 s | 7.20 s |

The two printed stamps are 10 seconds apart. The mismatch with the approximately 7.2-second recorded interval is evidence of uncertainty in relating individual printed seconds to a speech onset. The cause is unresolved. Keep the nearby calls together when assigning fit and held-out data. [Visual picks and source bindings](spur_exchange.tsv)

The first voiced sound was picked separately on each source’s spectrogram after identifying the passage. The images show only source-local time. The pick starts and the corresponding bounds measure the visible speech pattern, which can lag a weak initial consonant. The transcript clock’s accuracy and the delay between the sources’ feeds remain unresolved. The CH23 review records a canonical file start of zero, so the local elapsed seconds are also canonical post-05b seconds. Its speech-stagger adjustment remains unresolved.

The full-reel Faster Whisper pass skipped the TV line and returned only the first heading number from the readout. Its 105.55-second tag for “Go ahead, Jim” precedes the visually observed PAO onset by 1.87 seconds. Parakeet separately recognized the TV phrase and the full numbered sequence on PAO, CH23, and DVD during bounded passes with continuous input. Its readings vary in the opening words and mangle the crater name. The DVD identification first came from the matching speech patterns and call order. For the TV phrase, normalized spectral-feature correlation reached 0.803 on CH23 and 0.945 on DVD, with local alternative peaks of 0.304 and 0.318. For the readout opening the corresponding peaks were 0.825 and 0.888, with alternatives of 0.365 and 0.370. Those scores compare each passage in a six-second search window. Timing in the table comes from visual review. [Visual picks and source bindings](spur_exchange.tsv)

## Reproduce the evidence

The run folder `archive/2026-11-26_reorg/runs/mission_timing_pilot/spur_review/` contains the six source spectrograms, crop settings, and rendering script. The `readout_early_numbers.png` and `readout_late_numbers.png` panels show the two halves of the continuous numbered readout. `visual_crops.csv` lists each crop origin and transform. `short_phrase_matches.tsv` retains the query and search windows behind the similarity scores. `plot_spur_review.py` reads the exact sources listed in the catalog TSV and calls the shared spectral feature routines. The full-reel baseline uses `asr/pao_572aaa_large_v3/segments.jsonl` in the pilot run. The independent decoder files are linked by path in the TSV. The stage-05b review adjacent to the WAV has SHA-256 `a6d392df6fc3039f88c7062575f86c3fdae0dd8fee7122951f917e1eab28b1b7`. Its tape map has SHA-256 `04c9684ec281d7ae532b1eb6c911591acdfa1b45b2432af6682edbe3f21e36d8`.

```powershell
uv run --with matplotlib python research/mission_timing/plot_spur_review.py
```

`observed_local_minus_pao_s` is the source’s local pick minus the PAO pick for the same event. The sign refers to these clock readings. `approximate_native_frame` gives a zero-based frame locator for the WAVs. The DVD uses audio presentation time, so its native frame field is empty. Recognition times describe the decoder’s tagged opening where available. A blank recognizer field means that no corresponding opening tag was recorded. The original printed stamp and scan locator join through `event_id`. [Scan-reviewed transcript rows](pao_572aaa_transcript_reviews.tsv)

