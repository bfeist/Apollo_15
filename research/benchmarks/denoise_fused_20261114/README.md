# Fused denoise execution qualification — November 14, 2026

Backend: `apollo15-v2.16-numba-fused-v3`.
Active execution: **`numba-00f8bef508a7a5b15bb7509f`**.
Reviewed run: `v2-f0ba61776e7e0735d5d0571f`.
Previous execution: `numba-6f139b80fb62bde0c2bbd876`, preserved in its original folder.

Production stayed stopped throughout. Smoke tests used independent copies of
actual resumable checkpoints and never published audio. The completed-channel
count remains **365**. Frozen reference source, configuration, production status
and completed output metadata remain unchanged; see `preservation_before.json`
and `preservation_after.json`.

## Backend and qualification policy

The former fast prototype now lives in `scripts/03_denoise/runtime/scripts/`:
`denoise_v2_fast.py`, `denoise_v2_refinement_fast.py`, `fast_kernels.py` and
`fast_sincos.py`. The execution adapter hashes all four alongside its own code.
The frozen reference and algorithm ID `apollo15-hum-subtraction-v2.16` are unchanged.

Both hosts use the same fused observations, block-reseeded oscillators, harmonic
banks, uniform-grid interpolation, accumulation and compact-union kernels.
Numba uses `parallel=False`, `fastmath=False` and host-local caches. No production
dependency was added. Pytest was installed locally as test tooling.

Qualification accepts:

- Float64 model peak difference at most `1e-9`.
- Corrected float32 peak difference at most `2^-22` and RMS at most `2^-25`.
- Identical `(kind, round(hz, 2))` component multisets and order; bypass and
  upper-bypass decisions; refinement candidates, origins, stops and pass counts;
  compact-bank harmonics and absence intervals; anchor/context bindings.
- The unchanged saved-probe comparison and source/hash bindings.

Each host passed all **13 cases**: seven synthetic model cases and six real
corrected-chunk intervals, including four saved probes, the actual final partial
context and an arbitrary crop. Reports retain measured differences and both
decision structures. Non-identical floating diagnostic coefficients are expected.

| Host | Model peak difference | Float32 peak difference | Float32 RMS difference |
| --- | ---: | ---: | ---: |
| Windows | 1.7983e-11 | 1.4901e-8 | 2.5633e-11 |
| Genus | 1.7945e-11 | 1.4901e-8 | 2.5932e-11 |

`genus_windows_probe_comparison.json` additionally compares new Genus output
directly against the four immutable `windows-*.npy` probes using the frozen
`compare_probe` function: peak at most 1.4901e-8, RMS at most 2.6045e-11.

Reports: `windows_qualification.json`, `genus_qualification.json`.
Three additional 128-second corrected-chunk comparisons on a second tape,
`A15_T510077/CH03` at 2048, 2112 and 2176 seconds, also passed with identical
decisions. Model peak difference was at most 2.123e-12; see
`additional_real_comparisons.json`. The comparator now explicitly selects the
frozen reference so promotion cannot accidentally compare the backend to itself.

The prepared descriptor and selection are copied here as `execution.json` and
`active_accelerator.json`; production retains the authoritative copies under
`cluster/accelerators/<execution-id>/` and `cluster/active_accelerator.json`.

## Throughput and worker selection

The synchronized benchmark uses a 136-second real model context from
`A15_T510077/CH03`, with 64 new source seconds credited per model. Each process
warms the actual full model before the start barrier, then performs four timed
iterations. Source input hashes match across workers and hosts. Output hashes
match among workers within each run. Genus masks spread physical cores across
both cache groups; Windows stays at six workers.

Previous-backend measurements used temporary copies of the previous adapter and
frozen reference, without changing the active adapter. Those copies and the
benchmark snapshot option were removed after measurement; Git commit `b685645`
retains the measurement source history. These are model throughput measurements,
excluding publication and full production I/O/verification.

| Host, 6 workers | Previous source-hours/hour | Fused source-hours/hour | Speedup |
| --- | ---: | ---: | ---: |
| Windows | 36.98 | 115.19 | 3.12x |
| Genus | 35.72 | 103.66 | 2.90x |

| Genus workers | CPU mask | Source-hours/hour |
| ---: | --- | ---: |
| 6 | 0,2,4,8,10,12 | 103.66 |
| 8 | 0,2,4,6,8,10,12,14 | 103.20 |
| 12 | 0,1,2,3,4,6,8,9,10,11,12,14 | 98.75 |
| 16 | 0–15 | 95.41 |

Six is the measured best Genus count, so its existing balanced six-core mask
remains the default. Windows day/night scheduling is retained with a six-worker
cap; explicit larger limits are rejected.

At **12,917.4 remaining channel-hours**, the combined 218.85 source-hours/hour
projects **59.0 wall hours, or 2.46 days**, once the owner resumes production.
The previous measured combined rate projects 177.7 hours. Content, storage and
verification costs can extend both estimates; this is not a production ETA.

Raw runs: `windows_workers.json`, `windows_previous_workers.json`,
`genus_workers.json`, `genus_previous_workers.json`. `summary.json` includes the
calculation, test results and benchmark/smoke source hashes.

## Isolated checkpoint smoke tests

One writer per host ran with `--run-seconds 180`. The harness invokes the original
production `render_float` resume verifier through the private accelerated writer,
using exact copies of the committed audio, journal, checkpoint and staging
manifest. It does not start the coordinator, publish results or change stop flags.

| Host | Actual checkpoint copied | Cursor before | Cursor after | Stop |
| --- | --- | ---: | ---: | ---: |
| Windows | A15_T510086/CH18 | 29,696,000 | 94,208,000 | 75 |
| Genus | A15_T510086/CH17 | 52,736,000 | 103,424,000 | 75 |

Both retained their original audio/log prefixes, preserved the previous executor
span, appended contiguous new execution spans, and stopped at committed boundaries
in approximately 180 seconds. Source staging file SHA-256 hashes match before and
after. Reports: `windows_smoke.json`, `genus_smoke.json`. The isolated host-local
smoke sandboxes and old-backend benchmark caches were removed after verification.
Their recorded paths describe the completed experiment.

Read-only audits verified all 12 active checkpoint identities, WAV headers,
committed audio and journal hashes. The Genus audit also found six historical
scratch checkpoints and verified their prefixes without altering them.
See `windows_checkpoint_audit.json` and `genus_checkpoint_audit.json`.

## Commands and tests

All Windows commands use `uv run python`. Genus uses its own uv environment with
`/home/bfeist/.local/share/apollo15-numba-venv/bin/python`, the shared repo at
`/mnt/f/_repos/Apollo_15`, and data at `/mnt/d/Apollo_15_30-Track`.

Qualification and activation used:

```text
uv run python scripts/run.py qualify_denoise_numba.py --output research/benchmarks/denoise_fused_20261114/windows_qualification.json
# Same qualification on Genus with its environment and a new genus report path.
uv run python scripts/run.py denoise_v2_accelerated.py prepare --config D:/Apollo_15_30-Track/.denoise_v2_rollout/v2-f0ba61776e7e0735d5d0571f/cluster/config.json --windows-report research/benchmarks/denoise_fused_20261114/windows_qualification.json --genus-report research/benchmarks/denoise_fused_20261114/genus_qualification.json
```

Benchmark entry point: `research/profile_genus_workers.py --backend numba` with
`--counts 6 8 12 16 --repeats 4 --balanced-affinity` on Genus, and `--counts 6`
on Windows. The historical measurements used a temporary snapshot option that
was removed after the comparison finished.
Never reuse completed output report paths.

Validation passed:

- `uv run python -m pytest tests -k denoise -q`: 128 tests, 20 subtests.
- `uv run python -m pytest tests/test_script_layout.py -q`: 6 tests, 37 subtests.
- Frozen implementation hashes, both qualification environments, activation,
  isolated smoke prefixes, checkpoint audits and preservation checks.
