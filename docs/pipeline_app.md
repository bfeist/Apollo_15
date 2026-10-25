# Apollo 15 pipeline control room

Double-click **start_pipeline_app.cmd** to start the server and open your default browser. Keep its console open; Ctrl+C stops the application. Alternatively, run from the repository root:

```powershell
uv run python scripts/pipeline_app.py
```

Open **http://127.0.0.1:8766/**. This is a local application backed by the existing Python pipeline, with no additional dependencies, build step, account or hosting service. Keep the server running while processing. Ctrl+C stops its active process tree and preserves the queue and logs. `--port` selects another loopback port.

If the app is already running, open its existing URL. A second launch reports an occupied port or workspace without a traceback. On Windows the server claims its port exclusively, and the workspace lock prevents opening the same queue on a different port. A stopped server releases both locks automatically; there is no lock file to delete.

The listening desk is at **/review** in the same application. It uses the existing `reports/deflutter_review` state, judgments, maps, queues and acceptance records. The standalone review server on port 8765 still works. Tape links open the listening desk with that tape selected. A return link leads back to the control room.

## Archive and review status

The register shows assembled originals, prepared denoised speech, carrier surveys, bounded pilots, full plans, production acceptance, defluttered audio, post-deflutter Quindar surveys, frozen speed curves and retimed audio. All canonical Apollo 15 tapes are inventoried from disk; no mission selector or generalized ingestion service is introduced.

Click a tape for every numbered channel, source labels, timing-only exclusions, stage prerequisites, saved listening activity and a tape journal. Search and filters narrow the register. Select one or more tapes to prepare a processing pass.

Completion is derived from artifacts, not manually toggled. Canonical assembly means WAV headers and filename timelines passed inventory; it does not claim verification of every delivered fragment. Stage 03 uses the existing compatible-cache check. Carrier inventory validates the survey index against every original WAV and the hashed channel checkpoints, including completion, implementation/settings and current source metadata. It does not reread the feature blocks; planning, evidence use and acceptance retain full feature-hash validation. A partial carrier benchmark remains partial. Plan artifacts use the existing loaders and implementation/policy identities. Rendered audio needs a complete manifest, the current source/plan identity, compatible headers and unchanged timing artifacts. Retimed output must refer to the current defluttered input and frozen shared curve.

Browser polling serves the last completed inventory and never rehashes bound audio. An explicit refresh uses the existing acceptance gates, which can hash large bound audio files once acceptance records exist; allow significant I/O for that validation. Ordinary output inspection checks headers and provenance rather than decoding full recordings. Processing and deliberate acceptance retain their full verification checks.

The **Accepted** column is current production acceptance, checked through the existing gate. It is separate from historical listening counts. Older judgments, adverse findings and notes stay visible; notes never approve methodology or production. Refresh after listening acceptance to update the register.

Existing pre-v2 experiments remain outside the default production roots and are not relabeled as completed v2 work. Missing or unidentified recordings remain exceptions. In particular, T510078's unlabelled recording blocks exhaustive Quindar analysis until identified, as required by the existing survey CLI.

## Prepare and execute work

1. Select tapes, choose **Plan work**, and choose the action. Actions cover denoising, carrier benchmarking/full surveys, bounded pilots, full planning, accepted deflutter rendering, post-deflutter Quindar surveys, speed-curve freezing and retiming. Denoising and rendering can target one channel. Pilot start/duration and worker counts are explicit.
2. **Preview commands & prerequisites** reads current evidence and shows exact commands and per-tape blockers. It launches no processing. Adding ready jobs leaves blocked tapes out of the queue. Pending duplicates are ignored.
3. **Start queue** executes one tape job at a time. A stage may use its configured worker pool internally. Select a job for its command and bounded live log tail. Full logs and job records remain on disk. Job completions during an inventory request one follow-up refresh, so the final jobs cannot be lost behind an older scan.
4. **Pause after current** lets the current job finish. The stop button on a running job terminates its process tree and pauses the queue; the button on a waiting job cancels that queue entry. Failed or newly blocked jobs pause the queue so their output can be inspected.

Restart always leaves the queue paused. An unfinished running record becomes **interrupted**; no interrupted job is automatically retried. A potentially surviving process blocks starting more work until it exits. Recreate a preview to retry work, letting the CLI's cache/resume rules decide what can be reused. Existing nonempty 05b outputs are preserved and require inspection rather than automatic replacement. A workspace lock prevents two dashboard servers from owning the same queue. Separately launched CLI processes are not adopted or stopped by the dashboard; avoid competing writes to their output folders.

Every job rechecks prerequisites just before launch. Bounded pilots require a complete survey; full planning requires accepted methodology; production rendering requires exact current plan/channel acceptance and existing denoised intermediates. An all-channel render previews only remaining channels and refuses a batch containing an unaccepted pending channel; select an accepted channel to render a smaller scope. The adapter rechecks acceptance before each channel. Demo mode cannot launch processing.

For a one-time dataset, this is a durable local queue and artifact inventory, not a scheduled service. It does not start recurring jobs or process tapes when the server is closed. Reconstructing original deliveries, carrier-detector qualification, independent IRIG decoding/authentication, and scientific evidence report preparation remain the specialist workflows in the [v2 runbook](deflutter_v2.md). The app shows their acceptance gate; it does not fabricate those reports. Original assembly is already complete for this archive and is therefore inspection-only here.

## State and configuration

The default data folders remain under `D:/Apollo_15_30-Track`. Pipeline state is `reports/pipeline_control`; review state stays `reports/deflutter_review`. Jobs live in `jobs/<id>.json` with accompanying logs. `inventory.json` is the last completed scan, available immediately on restart. Tape notes are timestamped records under `notes/<tape>/`. These generated artifacts are ignored by Git.

The Quindar CLI writes one `survey.json` per analysis root. New dashboard jobs isolate each tape under `<quindar>/per_tape/<tape>/`, with the CLI's tape artifacts beneath that root. The dashboard passes this same root to curve freezing and retiming. This prevents a second tape survey from replacing the first tape's index. Existing flat CLI shared plans at `<quindar>/<tape>/shared_plan/plan.json` are also recognized; new surveys use isolated roots.

Use `--config path/to/local.json` for root or evidence overrides. Paths should be absolute, and input, output and state roots must be separate. Review configuration keys are shared with the listening desk:

```json
{
  "originals": "D:/Apollo_15_30-Track/orig_concatenated",
  "delivery": "D:/Apollo_15_30-Track/orig_delivery",
  "denoised": "D:/Apollo_15_30-Track/denoised",
  "surveys": "D:/Apollo_15_30-Track/carrier_survey_v2",
  "plans": "D:/Apollo_15_30-Track/deflutter_planning_v2",
  "defluttered": "D:/Apollo_15_30-Track/defluttered_v2",
  "quindar": "D:/Apollo_15_30-Track/quindar_post_deflutter_analysis_v2",
  "retimed": "D:/Apollo_15_30-Track/defluttered_retimed_v2",
  "state": "F:/_repos/Apollo_15/reports/deflutter_review",
  "pipeline_state": "F:/_repos/Apollo_15/reports/pipeline_control"
}
```

Optional `rollout_approval` and `production_approval_root` point at existing accepted records. Otherwise the defaults are `<state>/methodology/rollout_approval.json` and `<state>/approvals/`. Methodology report bindings, `evidence_roots`, `plan_overrides` and clock reports use the [listening-desk configuration](deflutter_review_app.md).

`--demo` uses separate synthetic review fixtures. It can exercise the archive and listening UI; processing commands remain disabled and demo decisions never qualify real tapes.

The server binds to loopback, checks Host/Origin and requires a session token for writes. The browser cannot supply shell commands or arbitrary log paths. Commands are fixed argument arrays generated from known Apollo 15 tapes. Do not expose the server through a network proxy.

## Verification

```powershell
uv run python -m unittest discover -s tests -p test_pipeline_app.py -v
node --check scripts/pipeline_web/app.js
node --check scripts/pipeline_web/review-link.js
```

The tests cover artifact-derived completion, exact acceptance gates, bounded pilots, isolated Quindar roots, allowlisted commands, preview-only behavior, queue deduplication, a real bounded subprocess and log, failure pauses, stopping an owned process, restart recovery, exclusive ownership, the mounted review UI and HTTP write boundaries. Tests use temporary synthetic files and never process mission recordings.
