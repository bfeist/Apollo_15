# PAO 572-AAA transcript references

The timing edition is NASA’s *Apollo 15 Technical Air-to-Ground Voice Transcription*, MSC-04558, as scanned in `AS15_TEC.PDF`. The source SHA-256 is `c69e4881c7d49fa7b13046e345575a404790a304b55b7a5e6eb7553d3ef79849`. Its introduction explains the time, speaker, and text columns and marks separate simultaneous GOSS net 2 communications. [NASA transcript collections](https://apollojournals.org/alsj/a15/a15trans.html) · [Technical transcript introduction](https://apollojournals.org/alsj/a15/AS15_TEC.PDF#page=2)

The catalog contains 79 scan-verified EVA candidates in 66 exchange groups and two controls. A candidate records the printed evidence and may still need verification against source audio. The links go to the source PDF page. In the scan, the first anchor at GET 145:23:19 is on printed page 814 and the last at GET 148:36:08 is on printed page 920. The transcript’s own tape numbers identify its recording and transcription sections. [Scan-reviewed rows](pao_572aaa_transcript_reviews.tsv)

| Field | Reading |
| --- | --- |
| `event_id`, `timed_row` | Stable ID based on one-based PDF page and one-based timed row within that page. Times and speakers may recur. The row count includes visibly damaged timestamp rows captured by the index. |
| `exchange_group` | Calls from the same exchange share a group for matching and holdout. The grouping describes the pilot’s use of the recorded interaction. |
| `pdf_page`, `printed_page`, `transcript_tape` | PDF page is the scan locator. The others are the identifiers printed in that scan’s header. |
| `stamp_ocr`, `speaker_ocr` | Literal tokens from the embedded text layer, including recognition errors. |
| `printed_stamp`, `get`, `get_seconds` | Scan-reviewed original `DD HH MM SS`, hours since launch, and integer elapsed seconds. The conversion uses zero-based elapsed days. The table preserves printed anomalies. |
| `printed_speaker`, `direction`, `link` | Printed speaker code, up/downlink inferable from the speaker, and the command- or lunar-module link. CDR-EVA is Dave Scott and LMP-EVA is Jim Irwin. CC is CAPCOM. |
| `printed_text` | Scan reading with line wraps joined and spacing normalized. Typewritten spellings and interruptions are retained where legible. |
| `reference_status` | `scan_verified_candidate` is an EVA call. `scan_verified_control` marks either the separate CC–CM sequence or the visibly mistyped drilling stamp. |
| `timestamp_resolution_seconds`, `working_reference_uncertainty_seconds` | The original stamp has one-second resolution. The ordinary review gives one second as a starting working uncertainty for modeling. This is separate from acoustic pick uncertainty and does not establish how the print relates to utterance onset. |

Two findings show why the scan matters. On PDF page 849, the embedded text reads `06 03 22 41` although the scan prints `06 03 12 41`. On PDF page 857, the scan itself prints `06 02 29 02` in the middle of calls around `06 03 29`; the catalog preserves the printed value as a control. PDF page 873 also has printed page 904 where the text layer reads 902. [PDF page 849](https://apollojournals.org/alsj/a15/AS15_TEC.PDF#page=849) · [PDF page 857](https://apollojournals.org/alsj/a15/AS15_TEC.PDF#page=857) · [PDF page 873](https://apollojournals.org/alsj/a15/AS15_TEC.PDF#page=873)

## Rebuild the pilot search index

Run from the repository root with the verified source PDF in the pilot run’s source directory.

```powershell
uv run --with pymupdf==2.1 python research/mission_timing/index_technical_transcript.py `
  runs/mission_timing_pilot/transcript/source/AS15_TEC.pdf `
  runs/mission_timing_pilot/transcript/technical_rows.tsv
```

The extractor reads the embedded text layer on PDF pages 784–910. It groups words by y position, estimates the time columns on each page, and preserves left-to-right layout. Each timed row retains the original timestamp and speaker tokens, recognized text and normalized search words. Bounding boxes are PDF points with the origin at the top left. Standalone wrap text at a page start retains an untimed record. The index follows printed order, including the separately marked CC–CM sequences. `link_context=cc_lm` restricts comparisons to the lunar-module conversation in this pilot. Same-second entries and regressions are flagged. A shared whole-second stamp or interruption mark signals a possible overlap whose duration is unmeasured. The JSON sidecar gives the source hash and extraction parameters. The downloaded Internet Archive OCR was useful as a second reading of Scott’s Spur TV sentence. The generated index uses the NASA scan’s text layer. [Internet Archive OCR edition](https://archive.org/download/Apollo15/AS15_TEC_djvu.txt)
