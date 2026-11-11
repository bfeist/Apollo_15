# Denoising performance evidence

See [PRODUCTION.md](PRODUCTION.md) for the deployed backend, worker-count results,
Windows day/night schedule, and successful checkpoint/resume verification.

The retained current evidence is:

- `windows_platform_kernels_qualification.json` and
  `genus_platform_kernels_qualification.json`: final 13-case qualification on
  each host, bound to the production backend and unchanged reference sources.
- `windows_worker_sweep.json` and `genus_worker_sweep.json`: final optimized
  sweeps across 1–32 workers.
- `genus_worker_confirmation.json`: longer six/eight-worker confirmation.

The qualification command is `uv run python scripts/run.py qualify_denoise_numba.py`;
the production console and numbered source folders are described in
[the script layout guide](../../../docs/script_layout.md). The supported tools include
`research/profile_genus_workers.py`. The latter now benchmarks either machine;
use `--backend numba`, with `--balanced-affinity` only on Genus. Run benchmarks
while production is stopped and use a new output path for each run.

## Archived development history

`history.zip` contains 22 superseded files: the four discarded prototype/profiling
scripts, earlier and incomplete experiments, the original investigation writeup,
and the pre-optimization worker sweep. Members retain their original repository
paths and exact file bytes. `MANIFEST.json` records each member's SHA-256 and
size; every member was verified before its loose copy was removed.

This archive preserves previous results and the experiments behind decisions
such as rejecting accumulation fusion for changed rounding and retaining NumPy
exponentials on Linux. It is historical evidence, not a second implementation
to use in production. Restore individual members into a separate scratch folder
if needed; do not overwrite active code or current qualification reports.

The 14 original Genus-side research outputs were consolidated, without changing
their contents, under
`/home/bfeist/.local/share/apollo15-denoise/research/20261110/` instead of leaving
them scattered in the login home directory. Active qualification descriptors
and production checkpoints stay in their existing locations.
