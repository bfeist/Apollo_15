# Pipeline overview

`D:` means `D:/Apollo_15_30-Track`, `F:` means `F:/Apollo_15_30-Track`, and `P:`
means `F:/Apollo_15_data/processing`. Run any script with
`uv run python scripts/run.py <script> ...`. Per-stage storage, including the
path-bound evidence kept in the checkout, is in [data locations](21_2026-11-26_data_locations.md).

## Run order

```
audio_inventory (catalogs, NARA downloads)
      │
      ▼
SG:   01_sg-internetarchive ──► 02_sg-transcript ──► 03_sg-internetarchive-respeed ──┐
      01_sg-nara (independent of IA) ───────────────────────────────────────────────┤ retimed SG set
                                  │                                                  │
                                  └──► flight_plan (documents)                       │
                                                                                     ▼
30-track: 01 ► 02 ► 03 ► 04 ► 05 ► 05a ► 05b ► 06 ──────────────────────────────► 07a ► 07b ► 08
                                                │                                  fit   apply  deliver
                                                └──► 07i (time-code tapes) ─────────────────┘
Independent: video/dvds (dvd_transcribe.py); 09_irig (standalone search); 10_channelid
```

**07a is the link between the pipelines.** It uses the retimed SG set as its
timing basis: the IA reels placed on GET by `catalog/sg_drift`, the NARA tapes
respeeded from IRIG-B, and the transcripts of both. 07a checks that set first
and stops, naming the missing inputs, if it is absent.

## 30-track (`scripts/30-track/`)

| Stage | Input → output | Stored in | Audio format |
| ----- | -------------- | --------- | ------------ |
| 01 ingest | delivered fragments → one WAV per track | `D:orig_concatenated` | 8 kHz, 16-bit PCM |
| 02 alignment | originals → capture offsets per WAV | `F:deflutter_alignment` | (reports) |
| 03 denoise ([guide](07_2026-11-15_denoise_cluster.md)) | originals → hum-subtracted tracks | `D:denoised` | 8 kHz, 32-bit float |
| 04 crosstalk ([guide](06_2026-11-13_crosstalk_stage.md)) | 03 tracks → weaker repeats removed | `D:crosstalk_removed` | 8 kHz, 32-bit float |
| 05, 05a ([census](10_2026-11-15_deflutter_detection.md), [qualification](13_2026-11-17_deflutter_qualification.md)) | originals → one tape map per tape | `F:deflutter_detection_census`, `D:deflutter_maps` | (maps) |
| 05b ([guide](08_2026-11-15_shared_deflutter.md)) | 04 tracks + map → defluttered tracks | `D:defluttered_shared` | 8 kHz, 32-bit float |
| 06 transcribe ([runbook](15_2026-11-22_transcription_runbook.md)) | 05b tracks → timing transcript | `D:timing_transcripts` | reads 05b; ASR at 16 kHz internally |
| 07a SG retime fit ([guide](20_2026-11-25_sg_retime_30track.md)) | 06 transcript + retimed SG set → one plan per tape | `D:stage_evidence/retiming` | (plans) |
| 07i time-code retime ([guide](22_2026-11-26_irig_timecode_retime.md)) | 05b time-code track (+ 07a anchors for the hybrid) → one plan per time-code tape | `D:stage_evidence/retiming/irig` | (plans) |
| 07b retime | 05b tracks + 06 transcript + 07i plan, else 07a plan → `retimed.wav`, `timed_transcript.tsv` | `D:retimed` | 8 kHz, 32-bit float |
| 08 normalize ([guide](05_2026-11-13_speech_normalization.md)) | 07b tracks → **delivery** `normalized.wav` (+ timed transcript) | `D:normalized` | **8 kHz, 16-bit PCM, TPDF dither** |
| 09 IRIG | originals CH01–CH04 → IRIG decodes | `D:irig_search` | (reports) |
| 10 channel ID | 06 transcripts + GLM + time code → delivered-to-true channel maps | `D:channel_id` | (maps, labels) |

The conversion to the delivery format happens only in **08**. It resamples any
input that isn't at 8 kHz, applies one constant gain with a -1 dBFS peak
ceiling, and dithers to 16 bits.

## Space-to-ground (`scripts/sg-audio/`)

| Stage | Input → output | Stored in | Audio format |
| ----- | -------------- | --------- | ------------ |
| 01_sg-internetarchive | IA reels → reel inventory, timing transcript | `P:sg/timing_transcripts` | input 48 kHz, 24-bit stereo |
| 01_sg-nara ([guide](19_2026-11-24_nara_flight_tapes.md)) | NARA MP3s → IRIG maps, respeed to GET, transcript | `P:sg_nara/{irig,maps,align,respeed,retimed_transcripts}` | MP3 44.1 kHz → 44.1 kHz, 24-bit |
| 02_sg-transcript ([guide](16_2026-11-23_mission_data_ingestion.md)) | journals, 1971 transcripts, IA transcript → mission transcripts, SG anchors | `MISSION_DATA/`, `.local/mission_data/` | (text) |
| 03_sg-internetarchive-respeed | 02 anchors → drift maps, respeeded reels | `catalog/sg_drift/`, `P:sg/respeed` | 48 kHz, 24-bit stereo |

## Documents, video and inventory

| Pipeline | Input → output | Stored in | Audio format |
| -------- | -------------- | --------- | ------------ |
| flight_plan ([guide](17_2026-11-23_flight_plan_ingestion.md)) | 02_sg-transcript snapshots, flight-plan PDFs → flight plan, TOC, indexes | `MISSION_DATA/{flight_plan,indexes}`, `MISSION_DATA/A15_TOC.*` | (documents) |
| video/dvds | Spacecraft Films MP4s → timing transcript | `P:dvd` | MP4 audio streams; ASR at 16 kHz |
| audio_inventory ([guide](18_2026-11-24_audio_inventory.md)) | JSC index, IA and NARA catalogs → which audio is held where; NARA downloads | `.local/mission_data/audio_inventory/` | (catalogs) |

All transcription entry points (`06_transcribe.py`, `sg_transcribe.py`,
`nara_retimed_transcribe.py`, `dvd_transcribe.py`) run the shared engine in
`scripts/00_common/` (`transcribe_cli`, `transcribe_run`, `mission_asr`,
`mission_align`). Each keeps its own store.

**Code layout rules.**
- Every script is registered in `scripts/layout.json`. `run.py` and `_bootstrap.py` put every
  registered folder on `sys.path`, so modules import by name.
- `scripts/30-track/_bootstrap.py` and `scripts/sg-audio/_bootstrap.py` forward direct runs.
- Many sources are hash-bound: the frozen denoise runtime (`03_denoise/runtime/`, see
  `source_manifest.json`), the `sg_*.py` edition code and modules that hash themselves.
  Move them byte-identically, and adapt saved paths in `script_relocation.py`,
  `stage_storage.py` or `archive_storage.py` instead of editing them.

Stage numbers changed on 2026-11-26: 06a became 06, 06c became 07a, 06b became
07b, 07 became 08 and 08_irig became 09_irig. `scripts/script_relocation.py`
(`RENAMED`) records every old → new script path.
