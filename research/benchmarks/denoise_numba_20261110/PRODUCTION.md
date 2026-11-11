# Production rollout and worker sweep — November 10, 2026

The final qualified backend is enabled for the next console run. Defaults honor
the owner's Windows availability: six workers from 09:00 to 01:00 Eastern,
twelve from 01:00 to 09:00. Genus uses six workers pinned across both cache groups
on CPUs `0,2,4,8,10,12`. At a schedule boundary the console checkpoints both hosts
and resumes with the new Windows limit. A fixed Windows override is capped at
eight and disables the schedule. Production is not left running by these tests.

```powershell
uv run python -u scripts/03_denoise/denoise_v2_console.py --genus-workers 6 --tapes 3
```

## Final optimized worker-count measurements

Model-equivalent source audio hours per elapsed hour:

| Workers | Windows | Genus, balanced affinity |
|---|---:|---:|
| 1 | 9.74 | 9.83 |
| 2 | 19.05 | 18.24 |
| 4 | 33.10 | 29.94 |
| 6 | 40.15 | 35.48 |
| 8 | 44.51 | 35.70 |
| 10 | 49.06 | 34.86 |
| 12 | 49.57 | 34.08 |
| 16 | 49.09 | 33.15 |
| 24 | 49.94 | 32.27 |
| 32 | 48.02 | 31.36 |

Each initial trial ran two full real-audio model iterations per worker with a
synchronized start. Genus confirmation in reverse count order ran three each:
eight workers 35.91, six workers 35.41. Six delivers about 98.6% of eight's
confirmed throughput and uses fewer resources. Against the earlier matched
six-worker reference baseline of 25.62, six optimized workers improve this
model workload's throughput about 38%.

The complete Windows sweep finished before the owner specified daytime limits.
A subsequent 12-worker confirmation was stopped immediately on that instruction
and is excluded. Daytime throughput preference is explicitly subordinate to
keeping Windows usable: use six, with at most eight for a fixed override.
The measured Windows overnight choice of twelve is on the throughput plateau;
24 workers offered less than 1% more than twelve in the initial sweep.

Both machines used the final `denoise_v2_numba.py` backend. The workload is the
same original A15_T510077 CH03 interval, 2048–2184 seconds, with saved v1 hints.
Loading and compilation occur before the start barrier. Genus spreads physical
cores across both L3 groups before adding SMT siblings; Windows uses ordinary
scheduling. Results count 64 new audio seconds per overlapping model, not the
136-second context. These are in-memory model measurements, not archive-wide
render/publication predictions. Raw data: `windows_worker_sweep.json`,
`genus_worker_sweep.json`, and `genus_worker_confirmation.json`.

## Qualification and preservation

Each host passed 13 source-bound synthetic/model and real corrected-chunk
comparisons. Float64 samples, float32 outputs and full decision records were
exactly equal to that host's reference. Saved qualification probes matched.
Windows additionally uses the qualified strict complex exponential kernel;
Genus retains NumPy exp because the alternate was slower there. Accumulation
fusion was excluded because it changed floating-point rounding.

The acceleration adapter records a distinct execution identity and contiguous
frame-span provenance while retaining the original reviewed output contract.
Existing checkpoint identity/audio/log hash checks remain in force. Old records
are backed up at transitions; committed audio prefixes are reused. Completed
reference channels are not rewritten. A dedicated pinned Linux environment
leaves the original qualified environment unchanged.

Schedule lifecycle tests cover clean checkpoint exit 75, genuine failures during
shutdown, stale status, Ctrl+C at restart and between sessions, completed runs,
and fixed worker overrides. Genuine failures never authorize automatic restart.
The console also recognizes both reference and accelerated SSH worker commands
when verifying shutdown.

Final verification: 45 relevant tests passed. A bounded production smoke run
used one Windows worker and one Genus worker, resumed their actual existing
checkpoints, and stopped both with clean exit 75. Windows A15_T510073/CH30
retained its reference prefix through frame 226,816,000 and committed accelerated
audio through 227,840,000. Genus A15_T510076/CH27 retained its reference prefix
through 26,624,000 and committed through 28,672,000. Both checkpoint manifests
record contiguous executor spans. Completed-channel count remained 83 and no
workers remained running afterward. The active execution identity is
`numba-6f139b80fb62bde0c2bbd876`; earlier execution snapshots remain preserved.
