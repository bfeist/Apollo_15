# Windows and Genus denoising

`scripts/03_denoise/denoise_v2_console.py` provides the master console, backed by
`denoise_v2_launcher.py` and the frozen `denoise_v2_cluster.py` coordinator.
The default backend is now the qualified Numba execution adapter. Frozen
reference DSP/writer files, originals, v1 archive and approved listening files
remain unchanged; new rendered spans use the separately identified accelerated
backend, with provenance added to checkpoints and newly completed manifests.

Run the master console on Windows from `F:\_repos\Apollo_15`:

```powershell
uv run python -u scripts/03_denoise/denoise_v2_console.py
```

Defaults are **6 Windows workers during the day, 12 overnight, 6 Genus workers,
and 3 active tapes total**. Windows switches to 12 at **1am America/New_York**
and back to 6 at **9am**, including daylight-saving time changes. The console
checkpoints both machines at a boundary, then automatically resumes with the
new Windows limit; Genus retains its configured count. Leave the console open.
Ctrl+C still stops it rather than triggering an automatic restart. Worker
failures also stop rather than restart. `--run-seconds` remains a bounded trial.

Omit `--local-workers` to use the schedule. An explicit `--local-workers 0` through
`8` sets a fixed limit and disables automatic switching; twelve workers are
reserved for the automatic overnight window.
Genus workers inherit CPU affinity `0,2,4,8,10,12`, spreading the six workers
across three physical cores in each of its two L3 cache groups. The final
optimized sweep confirmed about 98.6% of eight workers' peak throughput with
six workers, so six remains the practical default. The console uses
`denoise_v2_launcher.py` to wrap each remote worker command with `taskset`,
including replacement workers, and verifies the mask over SSH before starting.
This wrapper preserves the frozen cluster adapter, qualification, reviewed
output contract, and existing checkpoint audio. `--genus-cpus none` restores default
Linux scheduling; an explicit comma-separated CPU list overrides the mask.
Worker counts exceeding the mask's CPU count are rejected.
Windows assigns every channel once and starts each Genus worker over SSH.
There is no Linux batch command to start separately. The console displays each
machine's worker count, active tapes, channel progress, measured processing rate,
phase, and age of the last update. It estimates time remaining per channel,
per active tape and for the whole archive. Genus sends progress every ten seconds.
Rates mean source audio hours processed per elapsed hour and use up to five
minutes of observed progress. Estimates warm up for at least 45 seconds and
exclude audio already present in resumed checkpoints. Content and storage
contention affect these estimates. `--all-tapes` also displays queued and
completed tapes; queued tapes have no individual ETA until assigned workers.
An unchanged sample cursor can mean a model is still fitting; heartbeat age
distinguishes that from an SSH connection that stopped reporting.

To change concurrency, press Ctrl+C, wait for both machines to finish stopping
at checkpoints, then repeat with the desired limits:

```powershell
uv run python -u scripts/03_denoise/denoise_v2_console.py --genus-workers 6 --tapes 3
```

The tape limit applies across **both** machines. Increasing Genus workers does
not automatically increase the number of active tapes. Start with the six-worker
balanced-affinity default and compare useful source-hours processed per elapsed
hour before changing it. To test more than six workers, expand `--genus-cpus`
or use `--genus-cpus none`. CPU/cache/memory contention limited scaling in the
profiling trials; shared storage can also affect production. Windows workers run
at below-normal priority. Numerical libraries use one thread per worker.

Ctrl+C stops new assignments and asks both machines to stop at their next
committed checkpoint. Keep the console open until that finishes. The master
also stops scheduling after a worker/SSH failure. Repeat the same command to
resume. Windows checkpoints stay on Windows; Genus checkpoints stay on Genus.
Do not launch the legacy batch concurrently: the same archive lock prevents
two coordinators from owning production.

## Storage and remote environment

Genus is `bfeist@192.168.0.240`, with D: at `/mnt/d` and F: at `/mnt/f`.
It uses the same repository at `/mnt/f/_repos/Apollo_15`. Its Python environment
for reference qualification is `/home/bfeist/.local/share/apollo15-venv`.
Accelerated workers use `/home/bfeist/.local/share/apollo15-numba-venv`, with
the same pinned numerical versions plus Numba 0.68.0 and llvmlite 0.50.0.
Both are separate from the Windows `.venv`.
Commands run through `/home/bfeist/.local/bin/uv run --no-project --python ...`
so uv never tries to replace the shared Windows environment.

Genus reads originals and saved v1 frequency hints from D:, but renders unity
audio, normalization scratch, and resumable checkpoints under
`/home/bfeist/.local/share/apollo15-denoise` on its local disk. Completed results
upload to an isolated inbox on D:. Windows verifies their source binding,
format, sample count, finite samples, audio hash, cleanup log and normalization,
then publishes under the existing per-channel lock. Publication uses hard links
on D: and preserves any prior output required by the existing writer.
Genus reclaims local completed files only after Windows acknowledges publication.
If an acknowledged channel directory contains unexpected files, housekeeping
retains the entire directory and logs its extra or missing filenames while the
worker continues. The existing free-space check still applies. This housekeeping
wrapper applies to both the reference and accelerated backends; it leaves the
qualified implementation and completed audio unchanged. After a worker failure,
the console prints the channel, exit code, log path and recent worker log output.

Linux has its own execution identity, including its actual Python/library
versions and mount mapping. Imported manifests retain that execution identity
and the Windows/Linux qualification hash while binding the delivered channel to
the reviewed production configuration. Original Linux manifests are retained
under `cluster/executions/<tape>/CHnn/`. No source timestamps or frozen inventory
paths are rewritten to make Linux match Windows.

## Qualified accelerated execution

`denoise_v2_numba.py` loads reference DSP in a private namespace. It fuses the
observation projection on both hosts and complex128 oscillator exponentials on
Windows. The latter was slower on Genus and is deliberately disabled there.
The kernels use strict arithmetic and one thread per worker; no fastmath or
internal parallel pool is enabled. `numba` is a project dependency, and workers
keep compiled caches on their own machine.

Each host passed 13 synthetic and real qualification cases, including all four
saved probes, the final partial context, and arbitrary crop boundaries. Samples
and complete diagnostic records matched the reference exactly on each host.
See `research/benchmarks/denoise_numba_20261110/` for source-bound evidence.

The adapter has a distinct `numba-...` execution ID covering its code hashes,
compiler/numerical environments, feature policy and qualification hashes.
Descriptors, reports and implementation snapshots are preserved under
`cluster/accelerators/<execution-id>/`; `active_accelerator.json` selects the
qualified adapter for future jobs. Workers refuse modified code, dependencies
or qualification evidence. The original frozen run ID continues to identify
the reviewed algorithm/output contract, not a claim that every span ran through
the reference implementation.

On resume, the original writer still verifies checkpoint identity and committed
audio/log hashes. The adapter retains the committed prefix, backs up its old
checkpoint and staging manifest, and records contiguous frame spans for the
reference and accelerated executors in `acceleration` metadata. Newly committed
checkpoints and final manifests carry that metadata through remote adoption.
Completed channels are reused without rewriting their audio or manifests.
`--backend reference` also uses the provenance adapter once activated, so an
explicit fallback retains earlier accelerated spans. Use the console, not the
legacy direct cluster/batch entry points, to resume accelerated checkpoints.

To qualify a changed backend, run `scripts/run.py qualify_denoise_numba.py` separately
on each host while production is stopped, preserving both reports. Then run
`denoise_v2_accelerated.py prepare --config <base-cluster-config> --windows-report
<report> --genus-report <report>` on Windows. Preparation validates both reports
and actual environments before activating the new execution ID. It does not
rewrite completed outputs or existing audio prefixes.

## Preparation and status files

One-time preparation (with the old batch stopped):

```powershell
uv run python -u scripts/run.py denoise_v2_cluster.py prepare
```

Preparation verifies frozen code/library identities and compares four source-grid
audio intervals on Windows and Genus: beginning, the original hum problem, late
hum, and near the end. Original decoded model contexts must hash identically.
Float32 output must differ by at most `2^-22` peak and `2^-25` RMS. This is a
platform regression check, not a new whole-archive listening approval.

Control artifacts are in
`D:/Apollo_15_30-Track/.denoise_v2_rollout/<production-run-id>/cluster/`:

- `config.json`, `jobs.json`: immutable session and source-bound assignments.
- `probe.json`, `windows-*.npy`, `genus-*.npy`, `qualification.json`: compatibility evidence.
- `status.json`: all channel states, host assignments, progress, and heartbeat times.
- `progress.json`: derived per-worker, per-host, per-tape and archive rates/ETAs.
- `console.log`: coordinator startup, verification and error messages.
- `logs/<tape>_CHnn.log`: worker stdout and errors, including SSH failures.
- `inbox/`, `adopting/`, `executions/`, `acks/`: verification/publication records.
- `stop_requested`: shared graceful-stop signal; the coordinator clears it on resume.

The master also updates the original run's `status.json`, so existing progress
readers see the combined archive count. `--plain` provides periodic text status
instead of the live console. `--run-seconds 120` runs a bounded checkpoint trial.
Do not edit this launcher or frozen implementation files while workers run.
