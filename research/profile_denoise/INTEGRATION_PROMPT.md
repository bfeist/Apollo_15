# Task: promote the fast denoise prototype to the production accelerated backend

Repository: `F:\_repos\Apollo_15` (Windows coordinator). Use `uv run python ...` for
every command. Read `AGENTS.md`, `docs/07_2026-11-15_denoise_cluster.md`, and
`docs/04_2026-11-15_script_layout.md` first. Production is currently **stopped** by the owner;
do not start it. Do not modify any completed audio, checkpoints, manifests, or
anything under `D:/Apollo_15_30-Track/denoised`.

## Background

Stage 03 (`scripts/30-track/03_denoise/`) denoises 1,169 mono 8 kHz channels with the
frozen hum model `denoise_v2.py` + `denoise_v2_refinement.py` in
`scripts/30-track/03_denoise/runtime/scripts/`. 365 channels are complete, 12 have
resumable checkpoints, ~12,900 channel-hours remain. Workers run one thread
each on two machines: Windows (Ryzen 9 9950X3D, keep **6 workers** for developer
headroom) and Genus (Ryzen 9 5900XT, 16C/32T, Ubuntu, dedicated; use whatever
worker count is optimal).

Profiling showed the reference implementation is memory-bandwidth bound, not
compute bound: `np.exp` oscillator synthesis, un-fused full-array numpy passes,
and `np.interp` dominated, and the 63-harmonic loop streamed ~8 GB per model
window. A prototype port in `research/profile_denoise/fast/` fixes this:

- `fast_kernels.py` – single-thread Numba kernels (block-reseeded phasor
  recurrence `tone`, fused tone+observation, `harmonic_observations` /
  `accumulate_harmonics`, uniform-grid interp, fused accumulate/demodulate,
  `fit_groups`, `compact_weight_and_oscillator`).
- `fast_sincos.py` – vectorizable Cody-Waite/Cephes sincos (0.7 ulp).
- `denoise_v2_fast.py`, `denoise_v2_refinement_fast.py` – copies of the frozen
  modules with the hot paths replaced by those kernels. The algorithm,
  thresholds, decision logic, record structure, and `corrected_chunks` contract
  are unchanged. Only floating-point rounding differs.

Measured: one 136 s model window 6.05 s → 1.8 s; three 64 s `corrected_chunks`
steps 33.7 s → 9.6 s; aggregate throughput at 6 Windows workers 39 → 122
source-hours per wall-hour (3.1×). Equivalence against the reference on 6 real
windows (two tapes) and the 7 synthetic qualification cases: identical component
sets, refinement pass counts and bypass decisions; float64 model peak difference
≤ 4e-12; float32 output differences at most a few samples by one ulp.

Supporting scripts in `research/profile_denoise/`: `compare_fast.py`
(production vs fast on real `corrected_chunks` intervals), `synthetic_check.py`,
`test_kernels.py` (kernel unit checks vs numpy), `scaling.py` (N-worker
aggregate throughput), `profile_window.py` / `profile_fast.py` /
`profile_chunk_fast.py` (profilers).

## Required deployment path: new accelerated execution ID, frozen files untouched

The existing architecture already separates the reviewed *run identity*
(`denoise_v2.py`, `denoise_v2_refinement.py`, `denoise_v2_production.py`,
`denoise_v2_cluster.py`, inventory, review – hashed into `frozen`/run ID) from
the *accelerated execution identity* (`denoise_v2_numba.py` +
`denoise_v2_accelerated.py`, hashed into a `numba-<hash>` execution ID with
per-span provenance in checkpoints). Use that path so the run ID, the 365
completed channels and the 12 checkpoints remain valid:

1. Move the four prototype modules into `scripts/30-track/03_denoise/runtime/scripts/`
   (keep names: `fast_kernels.py`, `fast_sincos.py`, `denoise_v2_fast.py`,
   `denoise_v2_refinement_fast.py`). Register them in `scripts/layout.json`
   and add their hashes to `scripts/30-track/03_denoise/runtime/source_manifest.json`.
   Fix the imports so they resolve from that folder (the `_bootstrap.install()`
   path setup puts `runtime/scripts` on `sys.path`).
2. Rewrite `denoise_v2_numba.py`: load `denoise_v2_fast` instead of the
   reference copy, drop the `_NumpyWithExp` / `fused_observations` patching,
   set a new `BACKEND_ID` (e.g. `apollo15-v2.16-numba-fused-v3`), make
   `warmup()` call `fast_kernels.warmup()`, keep `FEATURE_POLICY`/
   `active_features` keys in `backend_configuration()` (describe the fused
   feature set identically on both hosts), and keep exporting `ALGORITHM`,
   `SETTINGS`, `Settings`, `configuration`, `model_window`, `corrected_chunks`.
   `ALGORITHM` must stay `'apollo15-hum-subtraction-v2.16'`.
3. `denoise_v2_accelerated.py`: extend `CODE_FILES` with the four new files and
   `validate_report`'s `expected_keys` accordingly. Keep everything else
   (provenance spans, backups, remote command rewriting) unchanged.
4. `scripts/30-track/03_denoise/runtime/research/qualify_denoise_numba.py`: the current
   pass criteria require bit-exact float64 models and identical records, which
   the port cannot meet by design. Replace with a tolerance qualification:
   - float32 corrected output peak difference ≤ 2⁻²² and RMS ≤ 2⁻²⁵ (the
     existing cross-platform tolerance in `denoise_v2_cluster.compare_probe`),
   - float64 model peak difference ≤ 1e-9,
   - decision structure identical: same `(kind, round(hz, 2))` component
     multiset, same `bypass`/`upper_bypass`, same refinement candidate list
     with equal `stop` and pass counts, same compact-bank harmonics,
   - saved cross-platform probe comparison unchanged (`windows-*.npy`).
   Record the measured differences in the report. Add the four new files to
   `implementation_sha256`. Do not reuse an existing report path.
5. Add/adjust tests under `tests/` that cover the backend: kernel checks vs
   numpy (port `test_kernels.py`), the synthetic equivalence cases, and
   `validate_report` accepting the new keys. Run the existing denoise tests
   (`uv run python -m pytest tests -k denoise -q`) and make them pass without
   weakening unrelated assertions.
6. Qualify on both hosts while production is stopped:
   `uv run python scripts/run.py qualify_denoise_numba.py --output research/benchmarks/denoise_fused_<date>/windows_qualification.json`
   on Windows, and the equivalent on Genus over SSH using the numba venv
   (`/home/bfeist/.local/share/apollo15-numba-venv/bin/python`, repo at
   `/mnt/f/_repos/Apollo_15`, data at `/mnt/d/Apollo_15_30-Track`). The Numba
   cache dir is per machine (`NUMBA_CACHE_DIR` set in `denoise_v2_accelerated`).
   Then on Windows:
   `uv run python scripts/run.py denoise_v2_accelerated.py prepare --config <base cluster config.json> --windows-report ... --genus-report ...`
   which validates both reports and environments and writes a new
   `active_accelerator.json`. Preserve the previous execution folder.
7. Benchmark Genus worker count with the new backend
   (`research/profile_genus_workers.py --backend numba`, or adapt
   `research/profile_denoise/scaling.py`) at 6, 8, 12, 16 workers and pick the
   best. Update `GENUS_CPUS`/defaults in `denoise_v2_launcher.py` and the
   console so Genus uses that count (likely `--genus-cpus none` or an explicit
   16-CPU mask) while Windows stays at 6 (keep the existing day/night schedule
   logic but cap at 6 unless the owner says otherwise).
8. Do a bounded smoke run (`--run-seconds 180`) with one worker per host,
   confirm both resume their actual checkpoints, commit accelerated audio with
   the new executor recorded in `acceleration.segments`, and stop cleanly with
   exit 75. Verify the completed-channel count is unchanged.
9. Update `docs/07_2026-11-15_denoise_cluster.md` ("Qualified accelerated execution"
   section) and `research/benchmarks/denoise_numba_20261110/README.md` (or a
   new dated folder) with the backend description, tolerance policy, measured
   throughput on both hosts, and the new execution ID. Commit with
   conventional-commit messages; keep DSP, adapter, docs and evidence in
   separate commits.

## Constraints

- Do **not** edit `denoise_v2.py`, `denoise_v2_refinement.py`,
  `denoise_v2_production.py`, `denoise_v2_cluster.py`, `denoise_v2_rollout.py`,
  or anything whose hash is in `frozen['implementation_sha256']`; the workers
  refuse to run and checkpoints become unresumable if those change.
- Keep every kernel single-threaded (`parallel=False`), `fastmath=False`
  except where explicitly measured faster (none currently; SIMD reassociation
  made the reductions slower). `cache=True` is fine.
- No new dependencies beyond `numpy`, `scipy`, `soundfile`, `numba`.
- Preserve the `corrected_chunks(read, source_frames, start, end, seeds, settings)`
  generator contract exactly, including record fields consumed by
  `denoise_v2_production.render_float` and `denoise_v2_quality.residual_windows`
  (`anchor_frame`, `context_frames`, `context_float64_sha256`, `components`,
  `refinement`, compact-bank rows with `true_absence_intervals_seconds`).
- Do not change thresholds, window sizes, ordering of accepted components in
  `record['components']`, or the sequential dependency in
  `stationary_shifted_comb` (each harmonic observes `samples - model` after
  the previous one was applied).
- If any real-window comparison shows a decision difference (component
  gained/lost, pass count changed), stop and report it with the window; do not
  tune tolerances to make it pass.

## Deliverables

- Code and tests as above, qualification reports for both hosts, the new
  active execution ID, Genus worker sweep numbers, docs update, and a short
  summary with measured before/after throughput per host and the projected
  remaining time for the archive.
