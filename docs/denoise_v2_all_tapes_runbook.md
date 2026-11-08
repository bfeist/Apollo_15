# All-tapes denoise v2 rollout

Prepared November 8, 2026. The production launcher uses the frozen v2.16
candidate. The full-channel listening decision gates the archive cutover.
The production tree is still v1.

The three v2.13 spots received "sounded okay" feedback. The requested long
comparison is in `reports/denoise_v2_original_review_20261108/`. Preserve that
review, the original `reports/listening_long_20261103/A.wav`, the other approved
listening WAVs, and the preferred 05 trial. Use the long comparison to assess
the six-second coverage-edge transitions before committing the archive run.

Listening found remaining low hum around 25:30–26:20 in that long comparison.
The frozen v2.16 candidate addresses the release of established low harmonics
during upper-reference dropout and independently checks residual reinforcement.
Its review is in
`reports/denoise_v2_late_hum_20261108/`; retain the late 25:30–26:20 passage,
preferred-05 transient check, and three earlier spots as rollout regressions.
Acceptance must include measured output residuals during activity and gaps,
not only whole-file or quiet-frame averages.
The inventory's v2.13 code hashes and runtime estimate remain historical
planning evidence; they are not approval to launch that revision unchanged.

## Exact directories and inventory

The owner's naming policy is:

| Purpose | Absolute directory |
| --- | --- |
| Immutable originals | `D:/Apollo_15_30-Track/orig_concatenated` |
| Current old output, before cutover | `D:/Apollo_15_30-Track/denoised` |
| Preserved old output, after cutover | `D:/Apollo_15_30-Track/denoised_v1` |
| New v2 production output | `D:/Apollo_15_30-Track/denoised` |

Do not use `denoised_v2` as the new production root. Rename the old tree on the
same volume; do not copy, merge, clean up, or regenerate it. The rename is part
of the version-aware production cutover below, after the review and running
readers have finished. The canonical name must not be handed to the legacy
writer during migration.

The read-only, source-bound job inventory is
`reports/denoise_v2_rollout_20261108/inventory.json`. Its generator is
`inventory_rollout.py` in that folder. It checks canonical WAV headers, source
metadata, existing manifests, and saved cleanup-log hashes; it does not claim
to have decoded and independently rehashed every old audio file.

- 39 tapes; 1,169 numbered source channels, all mono 8 kHz.
- 1,130 denoise jobs, totaling **18,551.60 channel-hours**.
- 39 timing-only jobs: copy the original timing WAV bytes without denoising.
  Record a distinct `copy_original_timing` action. This fills the new channel
  layout while preserving timing/reference evidence.
- All 1,130 denoise jobs have saved v1 hint logs whose source metadata and
  recorded log hashes match. After cutover, read those hints from `denoised_v1`.
  They are frequency hints; always fit and subtract from original samples.
- T510084 CH11 is absent. The one unlabelled T510078 recording is retained in
  originals and explicitly excluded from numbered-channel jobs. Do not invent
  a channel assignment or synthesize a replacement.
- Existing v1 audio occupies about 2.01 TiB. New float32 denoise audio plus
  unchanged timing WAVs is estimated at **1.98 TiB**, excluding logs and
  checkpoints. About 12.93 TiB was free on D: at inventory time.
- Longest source: 17.71 hours. Current estimated outputs fit ordinary WAV,
  but the writer must support RF64 when the size calculation requires it.

Revalidate the inventory's source size, nanosecond modification time, frames,
sample rate, role, and channel before each job. Reinventory explicitly if any
source differs; never silently update a frozen job's identity.

Use the measured [throughput trials](#throughput-and-gpu) below for planning.
The earlier single-track, ideal-scaling estimate is in the local archive.

## Freeze and output contract

Keep the DSP and its settings unchanged from the approved review while adding
batch plumbing. Freeze and hash `denoise_v2.py`, `denoise_v2_refinement.py`,
the new production writer, settings, Python/NumPy/SciPy/soundfile versions,
and the exact inventory. Bind a unique run ID to these identities. A code or
configuration change requires a different run identity and review of reuse.

For each numbered channel use `denoised/<tape>/CHnn/` containing:

- `denoised.wav`: original-length mono float32 for denoised channels, at
  original rate and **unity gain**. No peak normalization or limiting. Float
  output may represent samples above full scale without clipping; report them
  and use matched fixed gain for later PCM listening exports. Timing jobs
  retain the source file's original encoding and bytes.
- `denoised.json`: schema 2, explicit cleanup version/algorithm/action, source
  identity, complete source-frame range, canonical channel start offset,
  settings and implementation hashes, hint provenance, output format and
  hashes, measured peak, and `running`/`failed`/`complete` status.
- `cleanup.jsonl`: streamed per-anchor decisions and compact-bank coverage,
  accepted residual updates/rollbacks, boundaries and source/context hashes.
  Write each anchor once, not once per overlapping output interval. Timing
  copies use an explicit bypass record instead of fabricated model evidence.

Use the same `corrected_chunks` DSP, source-grid anchors, neighboring-bank
union and finite-response context as the review. No splitting independent
audio windows and concatenating differently fitted results. Process source
frames `[0, frames)` and record existing canonical offsets; do not trim,
pad, stretch, align, or apply a different tape-motion map.

Do not use the listening pair exporter for whole-channel production: it
writes an original copy, corrected copy and float64 scratch. The production
writer must stream one corrected output and bounded metadata. No rereading
or regenerating old surveys is needed for this denoise run.

## Production code and cutover requirements

The dedicated `scripts/denoise_v2_rollout.py` launcher owns v2 preparation,
cutover and batch scheduling. Stage 03 supports explicit v2 dispatch after
cutover. The v1 stage 03 writer checks the canonical version before writing,
and `render_denoise_v2.py` remains a review exporter.

| File in `scripts/` | Role |
| --- | --- |
| `denoise_v2_background.py` | Starts the rollout with lower Windows process priority |
| `denoise_v2_rollout.py` | Entry point for the canary, archive cutover and batch scheduling |
| `denoise_v2_production.py` | Streams whole channels, checkpoints work and verifies published WAVs |
| `denoise_v2_quality.py` | Measures residuals and checks the canary against the saved review |
| `denoise_v2.py` and `denoise_v2_refinement.py` | Fit and subtract hum using the frozen model |
| `denoise_archive.py` | Shares version checks and historical path resolution across writers and readers |
| `render_denoise_v2.py` | Exports listening comparisons and supplies the frozen frequency-hint reader |
| `deflutter_readiness.py` | Reports downstream readiness and supplies the legacy acceptance template |

1. **Production writer and restart.** Add a v2 whole-channel writer using the
   output contract above. Use a per-channel lock and a run-owned staging
   directory. Persist a checkpoint at complete absolute 64-second boundaries;
   flush audio and atomically replace the checkpoint. Resume only after
   verifying the source, code/settings/hints and written prefix. Rebuild the
   necessary neighboring models from originals at resume. Never append an
   incompatible run, and never treat a partial file as complete. Publish only
   after checking full length, finite samples, output hashes and log hash.
   A failed job remains visible and restartable; it must not overwrite a
   completed incompatible channel.
2. **Version-aware cache readers.** Update `channel_review.completed_denoised`
   and `ensure_denoised`, stage-03 dispatch in `audio_stage.py`, pipeline
   scheduling, readiness, and review readers to distinguish v1, v2 and timing
   copies. A v2 cache mismatch must fail closed or be scheduled as a v2 job;
   it must never fall through to the legacy denoiser. Keep old v1 validation
   available explicitly for historical reproductions. The old implementation
   fingerprint alone must never authorize replacing v2 audio or archived v1.
3. **Root/version guard and migration.** Add an exclusive archive-level lock
   and canonical root marker identifying v2, the run ID and frozen identities.
   Both old and new writers must check that marker and their expected cleanup
   version before writing. Archived `denoised_v1` is read-only to pipeline
   writers. Explicitly supplied paths must not bypass these checks.
4. **Batch scheduler.** Consume the frozen inventory, expose v2 as an explicit
   version, record every job status and failure, and support dry-run, canary,
   resume and full-run modes. Timing-copy jobs and missing/excluded channels
   must appear distinctly in the final accounting. No automatic retry of a
   mismatched completed cache. Pin numerical library threads to one per job;
   parallelize channels first, not independently fitted audio windows.
5. **Historical path resolution.** Existing v1 manifests and review/planning
   records contain absolute paths under the old `denoised` name. Preserve their
   bytes. Write an external relocation record from that old root to
   `denoised_v1`, and resolve historical bindings by expected version and hash.
   Never let an old saved path silently resolve to new v2 audio. Migrate the
   v5 discovery/resume validation and review evidence readers accordingly;
   reuse matching saved artifacts rather than rerunning surveys because a
   directory was renamed. Historical replay commands use explicit v1 roots.

The existing pipeline and cache tests must continue to cover legacy behavior.
Add tests for version refusal, original-source enforcement, float/RF64 output,
timing-copy identity, interrupted writes and resume, incompatible checkpoints,
root-rotation recovery, historical path resolution, and no writes into the v1
archive. Exercise compact-bank boundaries in the production writer, not only
synthetic chunks without the compact stage.

## Canary, cutover and launch sequence

1. Complete the integration and tests above. Dry-run the frozen inventory:
   expect 1,130 v2 cleanup jobs and 39 timing copies, with zero unidentified
   format/source failures. The existing legacy `--all-tapes` command is not
   a substitute for this dry-run.
2. Run one full-channel canary from original T510083 CH20 to a run-owned staging
   root. Verify that its review crops reproduce the frozen 30-minute result
   within the declared float/PCM quantization tolerance. Inspect beginning,
   end, original 09:28 problem, the late 25:30–26:20 hum, coverage transitions,
   and other activity levels. Measure each established line in original and
   output across active and quiet windows. Record output-to-original change,
   neighboring-floor excess, upper-reference outages, and repeated model
   reinforcement; do not hide intermittent failures in a whole-file average.
   Freeze numerical review thresholds from the accepted canary. Flag failing
   windows for bounded re-evaluation and retain them in the job report; never
   declare a job acoustically accepted solely because WAV rendering succeeded.
   Any retry must start from the original and its recorded model, preserve
   both versions, and stop at the declared iteration limit rather than silently
   stacking additional denoise passes or widening suppression indefinitely.
   Run a timing-copy canary as well. Publish neither into the old v1 tree.
3. Benchmark one channel worker, then a small concurrent set. This host had
   32 logical processors, 125.7 GiB physical RAM and about 70.5 GiB free RAM
   at inspection. Start with 2–4 channel workers, then use measured throughput
   and peak memory to choose a larger safe count. Reserve memory and at least
   100 GiB of free disk beyond estimated outstanding output and staging.
   Report channel-hours/worker-hour, not a tape-duration sum. Extrapolations
   from the 30-minute review are provisional; channel content affects cost.
   The previous rollout took about six days using concurrent processes. The
   source and output share a hard-drive array. Three active tapes were the
   practical balance last time, so the scheduler independently caps active
   tapes and concurrent channel workers. The conservative default is three
   of each. Increasing channel workers on the same three tapes needs its
   own benchmark because the workers share the same reads and writes.
4. With the root lock held, stop scheduling legacy writers and verify that no
   active job or review renderer is using the old tree. Capture a v1 inventory
   of relative filenames, sizes, times, metadata hashes and recorded audio
   identities. Verify the destination `denoised_v1` does not already exist,
   both resolved paths are exactly the intended D: roots, and neither is a
   junction/reparse redirection. Fail rather than merge with an existing tree.
5. Record a recoverable migration transaction outside both trees. Rename the
   old root, create the new empty canonical root and its v2 marker, and verify
   the archived file inventory remains identical. The filesystem operations
   performed by the migration command are the native equivalents of:

   ```powershell
   Move-Item -LiteralPath 'D:\Apollo_15_30-Track\denoised' -Destination 'D:\Apollo_15_30-Track\denoised_v1'
   New-Item -ItemType Directory -Path 'D:\Apollo_15_30-Track\denoised'
   ```

   These lines alone are not the migration command: locking, exact-path and
   version checks, manifest recording, and crash recovery must surround them.
   After cutover, use `denoised_v1` for all legacy hint logs. Promote validated
   canary outputs into the new root only under their unchanged run identities.
6. Launch the production scheduler against the new canonical root. Log a run
   summary and per-channel status continuously. Stop scheduling on source/code
   identity change, disk exhaustion, repeated systemic failure or version
   mismatch. Independent channel failures remain explicit; do not silently
   claim whole-tape completion or replace failed output with v1 audio.
7. Reconcile all 1,169 expected actions, the known missing channel and unlabelled
   source. Verify every completed output and retain restartable failure records.
   Check representative quiet/active/strong-hum passages across tapes, as well
   as bank coverage ends. Archive a completion report with run identities,
   channel metrics, failures and source/output provenance.

If interrupted between the rename and new-root creation, resume the recorded
transaction. If rollback is needed after v2 files exist, preserve the entire
new tree under a unique quarantine name before restoring the v1 root; do not
delete either version or overwrite a populated directory. Rollback is an
explicit operation, not a side effect of retrying a job.

## Downstream compatibility and handoff

Keep the saved tape-level motion evidence and maps. New downstream order is
v2 cleanup, crosstalk using v2 target and donors, then the existing single
shared tape map with original offsets. Do not mix v1 donors into v2 targets,
and do not infer new listening approval from old approval-bound v1 exports.
This rollout does not refit per-channel timing maps or run downstream stages.

## Commands

Run these from the repository root. The dry run validates the frozen sources,
frequency hints and disk budget. The canary processes the entire 17.1-hour
T510083 CH20 source into its own folder and verifies the IRIG copy. Repeating
the canary command resumes its checkpoints.

The inventory JSON and the `CH20_30min` review manifest and reference WAV
are local inputs under `reports/`. Restore the exact saved files before
running from a new checkout. Their hashes identify the run and its checkpoints.
See the [local evidence index](denoise_v2_plan.md#local-evidence) for retained
inputs and archived findings.

```powershell
uv run python -u scripts/denoise_v2_rollout.py --version v2 --mode dry-run
uv run python -u scripts/denoise_v2_background.py --version v2 --mode canary
```

Inspect `canary.json` and the matched passages in Adobe Audition using the
original source and the canary WAV at unity gain. The report includes the
numerical comparison with the saved 30-minute review and measurements from
active and quiet windows, including the two reported trouble spots. The canary
and all logs are under `D:/Apollo_15_30-Track/.denoise_v2_rollout/<run-id>/`.
Run `approve-canary` only after accepting the listening result. Before the
first full launch, close audio editors, review renderers and any legacy writers
using the denoise tree. The launcher also checks for open file handles before
rotation on Windows.

```powershell
uv run python -u scripts/denoise_v2_rollout.py --version v2 --mode approve-canary
uv run python -u scripts/denoise_v2_background.py --version v2 --mode full --tapes 3 --workers 6 --readers-stopped
```

The background launcher runs the scheduler and its workers at below-normal
Windows priority. Six workers across three tapes leave headroom for software
development. Both launchers use the same run identity and checkpoints. The
launcher requires a compatible throughput benchmark. The current frozen run
has a report for these limits. Use these commands to resume after an
interruption and to verify the finished archive.

```powershell
uv run python -u scripts/denoise_v2_background.py --version v2 --mode resume --tapes 3 --workers 6
uv run python -u scripts/denoise_v2_rollout.py --version v2 --mode reconcile
```

If interrupted during cutover, close the same readers and add
`--readers-stopped` when resuming. `status.json` tracks every scheduled
channel. `reconciliation.json` records full file-hash verification and links
the acoustic flags through each channel's cleanup log. Listen to representative
quiet, active and strong-hum spans and coverage transitions before declaring
acoustic acceptance. The launcher records flags for review without retrying
or changing the reviewed algorithm. An explicit `--mode rollback
--readers-stopped` preserves the v2 tree under a unique quarantine name and
restores the original canonical v1 name.

### Throughput and GPU

The 256-second excerpt trials measured approximately 5.6–6.0 channel-hours per
wall-hour with one worker, 13.4 with three workers on three tapes, 26.0–26.9
with nine workers and 31.6 with fifteen workers. The fifteen-worker trial
corresponds to roughly 24 days, but used enough CPU that it is a poor starting
point during development. Six below-normal workers reached 23.0 channel-hours
per wall-hour, a roughly 34-day estimate at that rate. All six workers inherited
the lower priority and the focused production tests passed in the foreground
during the trial. The largest observed worker was about 630 MiB. Content, job
lengths and foreground work affect the completion rate. To change worker count
after launch, stop with Ctrl+C and resume with the new limit.

The RTX 4090 can accelerate batched FFTs and some signal filters. The reviewed
implementation combines those operations with repeated per-anchor fitting,
CPU-side decisions and float64 phase calculations. Moving individual filters
to the GPU would add transfers between these steps. On a profiled 96-second
late-hum crop, direct FFT kernels and the SOS filter accounted for about 2.1 of
32.5 seconds. The float32 output matched the saved PCM24 reference within
`1.32e-7` at the historical comparison gain. Production uses the frozen CPU
implementation. Evaluate a GPU candidate on whole model windows, including
transfers, and verify the samples and listening regressions before using it for
production.

## Progress at preparation

- [x] Inventory all tapes, source formats, exclusions, legacy hints and capacity.
- [x] Specify `denoised` → `denoised_v1`, with new v2 using `denoised`.
- [x] Specify writer, cache/version guards, restart, migration and validation.
- [x] Record the long v2.13 listening feedback and verify the v2.16 follow-up.
- [x] Implement and test production writer, scheduler and reader migration.
- [x] Benchmark one, three and nine workers on the archive array.
- [x] Verify the full T510083 timing-channel copy byte for byte.
- [ ] Complete and accept the full-channel audio canary.
- [ ] Perform the guarded rename and create the new canonical root.
- [ ] Run and reconcile the full archive.
