# Numbered pipeline folders

The `scripts/` root contains only the pipeline entry point, the command
dispatcher, its shared path adapter and the script index. Processing code is
grouped by step:

| Folder | Contents |
| --- | --- |
| `scripts/00_common/` | Shared process I/O utilities |
| `scripts/01_ingest/` | Concatenation and original-tape archiving |
| `scripts/03_denoise/` | Denoise commands, Windows/Genus coordination and housekeeping |
| `scripts/04_crosstalk/` | Evidence-bounded two-path repeat subtraction before shared de-flutter |
| `scripts/05_deflutter/` | Stage 05a planning, stage 05b application, shared audio processing and review evidence |
| `scripts/06_retime/` | Quindar surveys and shared tape-speed retiming |
| `scripts/07_normalize/` | Conservative speech-level measurement, constant gain to target and separate listening WAVs |
| `scripts/08_irig/` | Standalone IRIG search; its pipeline integration remains undecided |

Run the current denoise console from the repository root:

```powershell
uv run python -u scripts/03_denoise/denoise_v2_console.py
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
uv run python scripts/run.py carrier_survey.py --help
uv run python scripts/run.py quindar_survey.py --help
uv run python scripts/run.py irig_search.py --help
```

## Frozen denoise runtime

`scripts/03_denoise/runtime/` preserves the reviewed source files and accelerator
qualification script byte-for-byte. Its internal `scripts/` and `research/`
paths retain the structure expected by the existing qualification descriptors.
`source_manifest.json` records their original identities. Do not edit these
files or invoke them directly; use the console or dispatcher.

The path adapter changes only where external review/inventory artifacts are
found and how child commands are located. It does not alter settings, saved
run IDs, code-hash checks, checkpoint files or completed audio. Windows workers
and SSH workers initialize the same layout before loading the frozen runtime.

The identical normalization module beside the deflutter engine preserves its
existing source-fingerprint calculation. The shared-map importer verifies
saved evidence by its recorded artifact hashes, independently of the current
legacy engine fingerprint. The removed review/approval implementation is
recoverable from Git commit `9e888c5` and saved research snapshots. Existing
manifests, recorded source paths, audio, and snapshots are not rewritten.

New scripts must be listed in `scripts/layout.json`. Tests and research tools
initialize `scripts/_bootstrap.py` before importing shared processing modules.

Stage 06 was formerly 05c. `pipeline.py --stages 05c` and
`scripts/run.py 05c_retime.py` remain command aliases. Saved audio, plans and
historical report paths are not renamed. See [speech normalization](speech_normalization.md)
for stage 07 commands and its measurement limitations.
