# Numbered pipeline folders

The `scripts/` root contains only the pipeline entry point, the command
dispatcher, its shared path adapter and the script index. The 30-track tape
stages 01-08 are in `scripts/30-track/`. Other sources have their own folders:

| Folder                           | Contents                                                                                                                                                                    |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scripts/00_common/`             | Shared process I/O utilities, and the shared transcription engine (`audio_sources`, `mission_asr`, `mission_store`, `transcribe_run`, `transcribe_watch`, `transcribe_cli`) |
| `scripts/30-track/01_ingest/`    | Concatenation and original-tape archiving                                                                                                                                   |
| `scripts/30-track/02_alignment/` | Capture alignment: where each original WAV starts on the shared tape clock                                                                                                  |
| `scripts/30-track/03_denoise/`   | Denoise commands, Windows/Genus coordination and housekeeping                                                                                                               |
| `scripts/30-track/04_crosstalk/` | Evidence-bounded two-path repeat subtraction before shared de-flutter                                                                                                       |
| `scripts/30-track/05_deflutter/` | Stage 05 detection census, 05a planning, 05b application, shared audio processing and review evidence                                                                       |
| `scripts/30-track/06_retime/`    | 06a timing transcript of 05b channels, Quindar surveys and shared tape-speed retiming                                                                                       |
| `scripts/30-track/07_normalize/` | Conservative speech-level measurement, constant gain to target and separate listening WAVs                                                                                  |
| `scripts/30-track/08_irig/`      | Standalone IRIG search; its pipeline integration remains undecided                                                                                                          |
| `scripts/sg-audio/`              | Space-to-ground reels: timing transcript (`sg_transcribe.py`) and, next, the reel drift fit                                                                                 |
| `scripts/sg_nara/`               | NARA flight tapes: IRIG-B respeed and transcription                                                                                                                         |
| `scripts/audio_inventory/`       | Audio source inventories and catalogs                                                                                                                                       |
| `scripts/video/dvds/`            | DVD audio streams: timing transcript (`dvd_transcribe.py`)                                                                                                                  |
| `scripts/sg_transcript/`         | Mission transcripts, commentary and photos; runs first ([guide](16_2026-11-23_mission_data_ingestion.md))                                                                   |
| `scripts/flight_plan/`           | Flight plan, as-flown timeline, TOC and their checks; runs after `sg_transcript` ([guide](17_2026-11-23_flight_plan_ingestion.md))                                          |
| `web_publish` (not yet designed) | Packages the output of every stage for the web, including the AiRT2 master files and indexes                                                                                |

Run the current denoise console from the repository root:

```powershell
uv run python -u scripts/30-track/03_denoise/denoise_v2_console.py
```

The general pipeline keeps its existing entry point:

```powershell
uv run python scripts/pipeline.py plan --stages 05a --all-tapes
```

`scripts/run.py` accepts any registered script's basename or its path relative
to `scripts/`. It supplies the shared import paths and resolves external saved
evidence. Use it for the hash-bound runtime and engine utilities:

```powershell
uv run python scripts/run.py denoise_v2_rollout.py --help
uv run python scripts/run.py denoise_v2_accelerated.py --help
uv run python scripts/run.py qualify_denoise_numba.py --help
uv run python scripts/run.py 05_detect_flutter.py --help
uv run python scripts/run.py carrier_survey.py --help
uv run python scripts/run.py quindar_survey.py --help
uv run python scripts/run.py irig_search.py --help
```

## The 30-track folder move

On 2026-11-24 the stage folders 01-08 moved from `scripts/` to
`scripts/30-track/` with no byte changes. Saved implementation hashes still
match. `scripts/pipeline.py`, `scripts/run.py` and command names are unchanged.
`scripts/30-track/_bootstrap.py` forwards direct stage runs to the shared
adapter. `scripts/script_relocation.py` corrects the few hash-bound sources that
found the repository from their own folder depth (`repeat_qualification`,
`flutter_qualification`, `flutter_detection`). It also maps the former script
paths in saved stage-04 bindings to their current location. Saved evidence
is not rewritten. Genus checkouts need the same commit before they run workers.

## Frozen denoise runtime

`scripts/30-track/03_denoise/runtime/` preserves the reviewed reference source files
byte-for-byte. Its internal `scripts/` and `research/` paths retain the structure
expected by qualification descriptors. The separate accelerated modules and
qualification script may be promoted through a new qualified execution ID;
they are not part of the frozen run identity. `source_manifest.json` records
the installed source hashes. Never change a source bound by the frozen run's
`implementation_sha256`. Use the console or dispatcher to invoke runtime code.

The path adapter changes only where external review/inventory artifacts are
found and how child commands are located. It does not alter settings, saved
run IDs, code-hash checks, checkpoint files or completed audio. Windows workers
and SSH workers initialize the same layout before loading the frozen runtime.

The identical normalization module beside the deflutter engine preserves its
existing source-fingerprint calculation. The shared-map importer verifies
saved evidence by its recorded artifact hashes, independently of the current
legacy engine fingerprint. The removed review/approval implementation is
recoverable from Git commit `9e888c5` and saved research snapshots; the v5
survey/planning engine and the October 30 research campaign code were removed
on November 15 and remain in Git history. `carrier_survey.py` stays for the
IRIG search. Existing
manifests, recorded source paths, audio, and snapshots are not rewritten.

New scripts must be listed in `scripts/layout.json`. Tests and research tools
initialize `scripts/_bootstrap.py` before importing shared processing modules.

Stage 06 was formerly 05c. `pipeline.py --stages 05c` and
`scripts/run.py 05c_retime.py` remain command aliases. Saved audio, plans and
historical report paths are not renamed. See [speech normalization](05_2026-11-13_speech_normalization.md)
for stage 07 commands and its measurement limitations.
