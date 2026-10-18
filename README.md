# Apollo 15 30-track audio processing pipeline

This repository contains the processing pipeline for the **30-track audio recordings of the Apollo 15 mission**. It assembles delivered fragments into continuous channels, reduces tonal and broadband noise, corrects tape flutter, and retimes the resulting audio using Quindar tones.

The aim is to improve intelligibility while preserving the voices and character of the historical recordings. Original audio stays separate from processed outputs. Source audio, generated reports, listening samples, and processing runs are not distributed in this repository.

## Setup

Use Python 3.10 or newer and `uv`. Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`. Run commands from the repository root.

```powershell
uv sync --locked
uv run python scripts/pipeline.py --help
```

## Pipeline

| Stage | Script | Purpose |
| --- | --- | --- |
| 01 | `scripts/01_concatenate.py` | Verify fragment order and concatenate each channel. |
| 03 | `scripts/03_denoise.py` | Remove persistent tones and residual hum, then apply broadband reduction capped at 3 dB. |
| 05a | `scripts/05a_plan_deflutter.py` | Analyze original channels and save regional flutter correction plans. |
| 05b | `scripts/05b_deflutter.py` | Apply supported timing corrections to denoised audio, then bounded constant-gain normalization. |
| 05c | `scripts/05c_retime.py` | Apply a shared tape speed curve derived from Quindars in completed defluttered audio. |

Stage numbers reflect the project's development history. Tonal and broadband cleanup are now combined in stage 03. `scripts/01a_zip_originals.py` is a separate utility for archiving complete concatenated tapes as ZIP files.

The whole-channel order is **03 -> 05a -> 05b -> 05c** after concatenation. Stage 05a reads original channels because denoising can remove useful timing references. Stage 05b reads denoised intermediates and saved plans.

### Data locations

Defaults reflect the local processing workstation, under `D:/Apollo_15_30-Track`. Supply overrides for another machine.

| Folder | Contents | Pipeline override |
| --- | --- | --- |
| `orig_delivery/` | Delivered fragments | `--delivery` |
| `orig_concatenated/` | Canonical per-tape, per-channel originals | `--concatenated` |
| `denoised/` | Reusable stage-03 intermediates | `--denoised-root` |
| `deflutter_planning/` | Regional plans, evidence, and timing maps | `--flutter-planning-root` |
| `defluttered/` | Stage-05b audio | `--defluttered-root` |
| `quindar_post_deflutter_analysis/` | Quindar surveys and frozen shared plans | `--quindar-analysis-root` |
| `defulterred_retimed/` | Stage-05c audio | `--retimed-root` |

The spelling `defulterred_retimed` is the existing output directory name. Logs and manifests go under local `runs/<run-id>/` by default; `--output` changes that location. Standalone scripts may use different option names; consult their `--help`.

## Running the pipeline

Preview commands without starting processing:

```powershell
uv run python scripts/pipeline.py plan --stages 03 --all-tapes
```

Denoise every available channel on every canonical tape:

```powershell
uv run python -u scripts/pipeline.py run --stages 03 --all-tapes --plain
```

This includes channel 1 and IRIG-labeled channels. Full-mission denoising is a long-running batch job. `--denoise-workers` controls concurrency; the pipeline default is 14. Avoid launching a second job against outputs already being processed.

For one channel:

```powershell
uv run python scripts/pipeline.py run --stages 03 --tape A15_T510083 --channel 20 --plain
```

Completed intermediates are retained across runs and verified against source identity, cleanup code/settings, frame count, and decoded audio hashes. Interrupted cleanup restarts in temporary storage. Cleanup-code changes can trigger reprocessing, so keep the implementation and environment stable during a batch.

### Plan and apply flutter correction

Plan a tape from its canonical originals, then render one denoised channel:

```powershell
uv run python scripts/pipeline.py run --stages 05a --tape A15_T510083 --plain
uv run python scripts/pipeline.py run --stages 05b --tape A15_T510083 --channel 20 --plain
```

Planning checkpoints completed regions and reuses matching completed plans. Interrupted plans can resume; incompatible source data, settings, or evidence are rejected. `--flutter-plan-workers` controls planning concurrency.

Plans distinguish supported corrections, regions requiring transfer validation, and regions to leave uncorrected. Whole-channel rendering joins regional timing curves continuously. `--experimental-transfer` allows proposed corrections in regions requiring transfer validation for listening review; those results remain experimental. A local listening-approved example does not validate every channel or an entire tape.

### Retime a completed tape

Finish stage 05b for every expected audio channel of a tape before its Quindar survey. IRIG-only channels are excluded from this readiness check because stage 05b does not render them.

```powershell
uv run python scripts/quindar_survey.py --tape A15_T510083 --workers 8
uv run python scripts/05c_retime.py --tape A15_T510083 --plan-only
uv run python scripts/pipeline.py run --tape A15_T510083 --all-channels --stages 05c --plain
```

The survey measures completed **defluttered audio**, not original audio. Planning freezes one shared speed curve per tape, used by all channels, including those without usable tones. Retiming changes duration and pitch, preserves historical content offsets between heads, and records timing/provenance information. Sparse tone coverage, residual flutter, and unknown historical generator accuracy limit absolute calibration.

## Repository contents

- `scripts/`: pipeline stages, shared processing code, and review utilities. `noise_review.html` is the template loaded by `review_noise_reduction.py`, so it is required source.
- `configs/`: processing decisions and example configurations. Examples can reference local audio and reports; adapt those paths to your data.
- `tests/`: automated checks for processing, orchestration, and file handling.
- `research/irig_free_20261016/`: experimental Python tools and their holdout configuration. These are research source, not production stages; some require local reports, data, or additional plotting dependencies.

Git ignores generated `reports/`, `runs/`, historical experiment `archive/`, research snapshots, audio, numerical artifacts, and Python environments/caches. These files remain available locally. The historical `IRIG_FREE_FLUTTER_RESEARCH.md` report is also local-only; it contains experiment history and references to excluded artifacts.

## Validation

```powershell
uv run python -m unittest discover -s tests -v
```

Listening review remains necessary to assess speech preservation and correction quality. Numeric improvements alone do not establish that a method works across the mission.
