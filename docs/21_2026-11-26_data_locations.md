# Where each stage's data is stored

Inventory of 2026-11-26. Large stage outputs are on the external data drives, laid out
by stage. The checkout holds code, small committed catalogs, and a few path-bound
evidence folders that saved records or hash-bound code name by exact path. Those
folders stay where they are and are listed here rather than moved. For stage order
and audio formats, see the [pipeline overview](00_pipeline_overview.md).

## 30-track pipeline (`scripts/30-track/`)

| Stage                | Stage data (outputs)                                                                       | Evidence the stage reads                                                                                                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 01 ingest            | `D:/Apollo_15_30-Track/orig_concatenated` (from `orig_delivery`)                           | -                                                                                                                                                                                     |
| 02 alignment         | `F:/Apollo_15_30-Track/deflutter_alignment`                                                | `D:/…/stage_evidence/archive/capture_alignment_recovery`                                                                                                                              |
| 03 denoise           | `D:/…/denoised`                                                                            | `D:/…/stage_evidence/denoise` (production records, frequency hints); repo `reports/denoise_v2_rollout_20261108`, `reports/denoise_v2_late_hum_20261108` (frozen runtime, path-bound) |
| 04 crosstalk         | `D:/…/crosstalk_removed` (junctions into `stage_evidence/archive`)                         | repo `reports/crosstalk_echo_Audition_20261108_v2` (named in about 7,500 saved records); repo `runs/04_archive_20261118` (its `cluster.log` is named in `execution.json`)             |
| 05 detect            | `F:/Apollo_15_30-Track/deflutter_detection_census`                                         | repo `reports/stage05_ssd_pool_20261118` (worker benchmarks named in `selection.json`); repo `reports/flutter_census_20261115` (qualification corpus)                                 |
| 05a plan, 05b render | `D:/…/deflutter_maps`, `D:/…/defluttered_shared`                                           | `D:/…/stage_evidence/flutter/carrier_tones`, `D:/…/stage_evidence/archive/deflutter_*`                                                                                                |
| 06 transcribe        | `D:/…/timing_transcripts`                                                                  | `D:/…/stage_evidence/timing_transcription` (sessions, hash cache)                                                                                                                     |
| 07a SG retime fit    | `D:/…/stage_evidence/retiming` (`sg_reference`, `<tape>/sg_anchors`, `sg_quindar`, `sg_fit`) | repo `catalog/sg_drift`, repo `research/readme_figures/inventory.json` (both hash-bound in `sg_inputs.py`) and the SG data below                                                    |
| 07b retime           | `D:/…/retimed/<tape>/CHnn/` (`retimed.wav`, `timed_transcript.tsv`, `transcript_times.tsv`) | the 07a plan                                                                                                                                                                          |
| 08 normalize         | `D:/…/normalized/<tape>/CHnn/normalized.wav`                                               | -                                                                                                                                                                                     |
| 09 IRIG              | `D:/…/irig_search`, `D:/…/carrier_survey_v2` (defaults; not run in this archive)           | -                                                                                                                                                                                     |

`D:/…/_to_delete` and `F:/Apollo_15_30-Track/_to_delete` hold retired editions
(see `D:/Apollo_15_30-Track/STAGE_RESULTS.txt`). `F:/Apollo_15_30-Track/work` is
resumable render and crosstalk scratch.

## SG pipeline (`scripts/sg-audio/`)

| Stage                          | Data                                                                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 01_sg-internetarchive          | input `F:/Apollo_15_data/raw/audio/Internet Archive`; transcripts `F:/Apollo_15_data/processing/sg/timing_transcripts`, evidence `…/sg/evidence`                                           |
| 01_sg-nara                     | input `F:/Apollo_15_data/raw/audio/NARA`; `F:/Apollo_15_data/processing/sg_nara/{irig,maps,align,respeed,retimed_transcripts}`                                                              |
| 02_sg-transcript               | committed `MISSION_DATA/` (`sg_reels.tsv`, `raw_manifest.tsv`, `review/` decisions); working `.local/mission_data/` (`sg/sg_anchors.tsv`, journals, scraped data)                          |
| 03_sg-internetarchive-respeed  | committed `catalog/sg_drift/` (drift maps, bound by checksum in respeed provenance); plots `reports/sg_drift/`; respeeded reels `F:/Apollo_15_data/processing/sg/respeed`                   |

## Other pipelines

- **Flight plan** (`scripts/flight_plan/`): committed `MISSION_DATA/flight_plan/`, `MISSION_DATA/indexes/`,
  `MISSION_DATA/A15_TOC.*`; working `.local/mission_data/{flight_plan,ocr,ai}`.
- **Video/DVD** (`scripts/video/dvds/`): input `D:/! Media Files/A15/Spacecraft Films`; transcripts
  `F:/Apollo_15_data/processing/dvd`.
- **Audio inventory** (`scripts/audio_inventory/`): `.local/mission_data/audio_inventory/`.
- **Channel ID** (`scripts/30-track/10_channelid/`): `D:/…/channel_id` (`evidence/`, `glm/`, `maps/`, `labels/`,
  `shared/` final maps, `irig_nara/` time-code decodes of the originals, `consistency/`).
- **07i time-code retime**: `D:/…/stage_evidence/retiming/irig/<tape>/` (`decode/`, `fit-<digest>/`).
- **Transcript times** (07b, [docs/24](24_2026-11-27_transcript_times.md)): `D:/…/stage_evidence/retiming/transcript_times/<tape>/` (`preview-<digest>/`, `retimed-<digest>/`).

## Research, archive and the 2026-11-26 cleanup

- `research/` (committed) is development work that led to the current stages. A few files
  are read by current stages and stay in place: `research/crosstalk_probe.py`,
  `research/readme_figures/inventory.json` and `research/benchmarks/` (accelerated-denoise
  qualification). The mission-timing pilot catalogs moved from `catalog/` to
  `research/mission_timing/catalog/`.
- `archive/` (not in Git) holds historical material. On 2026-11-26 the historical items from
  `reports/`, `runs/` and `sites/` (20,839 files, 8.2 GB) moved byte-identically to
  `archive/2026-11-26_reorg/<same relative path>`. `moves.json` lists every file with its
  sha256 (re-verified after the move), `README.json` summarizes the move and
  `reorg_data.py` is the script that did it. This includes the approved listening example
  `reports/channel_review_smoke_T510083_CH20`. To restore an item, move it back to its
  original path.
- Deleted on 2026-11-26, each listed in `archive/2026-11-26_reorg/deleted.json`:
  - 16.6 GB of `.partial` render scratch in `runs/05b_remaining_20261121/work`. All 120
    channels of that run were verified and published on D:.
  - Test and run logs in `runs/`.
  - Unused uv caches.
- `runs/` is the default parent of `pipeline.py` run folders. Only
  `runs/04_archive_20261118` there is evidence.
