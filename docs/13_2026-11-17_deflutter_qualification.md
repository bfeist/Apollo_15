# Stage 05 archive analysis and qualification

Implemented November 17, 2026 from section 4 of
[next steps](12_2026-11-15_deflutter_next_steps.md). This is the operating
guide for everything between a completed detection census
([10_2026-11-15_deflutter_detection.md](10_2026-11-15_deflutter_detection.md))
and stage-05a map compilation. Nothing here changes the detector, its gates,
its four hashed files or the census run ID.

## What it does

The census collects evidence; it does not authorize application. The analysis
reads the immutable census and decides, under one frozen policy, which tape
time may be corrected by the shared map:

1. **Assess (pass 2).** Account for every canonical and every scheduled tape
   (`complete`, `incomplete`, `failed`, `stale`, `excluded`, `not_scheduled`).
   For each complete tape, compute tape-time unions (analysed,
   candidate-supported, unsupported, unmeasured; qualified stays `null`),
   overlap agreement of eligible window curves, gate failures, clipping, sign
   conflicts, family/channel-group support, and audit availability.
   Raw beep audit rows are collapsed into **physical events**: the same channel
   in overlapping windows, duplicate feeds, and simultaneous beeps on several
   channels (±50 ms) count once; possible delayed copies (1.2–1.45 s on other
   channels) are listed as uncertain associations, not merged. Each physical
   event is assigned once, by seeded hash, to a **decision** half (may flag and
   resolve contradictions) or a **held-out** half (validation only).
2. **Classify and queue.** Each window is `candidate`, `needs_analysis` or
   `unsupported` (analysis states, not render permissions). Eligible windows
   are flagged for contradictory decision-half audits (`adverse_audit`: median
   physical-event ratio > 1 with ≥ 3 events; `reversed_audit`: reversed curve
   better than normal), reversed concentration witnesses, `sign_conflict`,
   inconsistent overlapping curves (r < 0.8), near-threshold gates and
   supported/unsupported boundaries. Ineligible windows are queued only when
   near a threshold or at a boundary (missed-opportunity checks). A seeded
   random sample of unflagged candidate and unsupported windows is added
   (2 % each, at most 4 per tape). Missing beeps never raise a flag.
3. **Follow-up (pass 3).** Queued windows of a tape are merged into regions
   (gap ≤ 30 s, ≤ 480 s, 12 s context) and each region is read once for all
   channels. The census numerics are rerun (the census code must be unchanged)
   and the follow-up saves what the census does not retain: every competing
   cluster's curve, the reproduction correlation with the census curve,
   family-versus-rest consensus correlations and the largest single feed share,
   mirror/mains-adjacent explanation of anti-correlated clusters, concentration
   of tones above 200 Hz (member tones dewarped _leave-group-out_, plus
   non-member tones), and per-beep sufficient statistics of beep FM against
   the census curve at lags ±0.3 s. Results are content-addressed and reused
   by any later analysis of the same census.
4. **Qualify.** `qualification_policy.resolve` turns pass-2 rows and follow-up
   measurements into `apply`, `bypass_unsupported`,
   `bypass_unresolved_contradiction` or `incomplete`:
   - ineligible windows bypass as unsupported (policy v1 does not accept new
     hypotheses from follow-up; they are reported only);
   - unflagged candidates apply; a flagged window without follow-up is
     `incomplete`, never silently bypassed;
   - a contradiction is resolved to `apply` only when the decision-half beeps
     are measured and independent of the tested motion (|r| < 0.3 at zero lag
     and |r| < 0.6 across the lag sweep), at least two > 200 Hz tones from
     distinct feed groups and frequency families
     tones concentrate under the curve and none prefers the reversed sign,
     the census curve is reproduced (r ≥ 0.9), families agree (r ≥ 0.7), no
     single feed carries over half the weight and any sign conflict is
     explained. Inverted beeps (r ≤ −0.3) or reversed tones leave it
     `bypass_unresolved_contradiction`. No sign is flipped and no lag is added.

   Decisions are resolved once on the canonical grid with precedence
   incomplete > contradiction > apply > unsupported, so an overlapping passing
   window cannot leak correction into a contradicted interval. Applied census
   curves are joined once (`census.join_windows`) into
   `qualified.f64` with zero support everywhere else.

5. **Validate.** Held-out physical events only, by deterministic tape split
   (A15_T510073, A15_T510083 and A15_T510106 are always development): coverage,
   median ratio and degradation inside applied intervals, the same inside
   bypassed contradictions, cross-channel transfer (events on channels outside
   the window's agreeing set), identity/normal/reversed/shift controls, applied
   segment counts, overlap agreement and a no-leak check. Only a contributing
   window can supply an applied-event measurement. An event observed on both
   agreeing and outside channels contributes its outside-channel measurements
   to the transfer table. A pass requires complete qualification, ≥ 20 held-out
   validation events, ≤ 5 % worse, median ratio ≤ 0.5, a better median than
   both control curves, and zero support outside applied intervals. The report is
   written once per analysis ID.
6. **QA batch.** One frozen, stratified random listening selection: tape
   qualified-coverage band (low < 20 %, mid < 60 %, high) × held-out audit band
   (improved, marginal, unaudited) over applied excerpts, preferring distinct
   tapes, plus a separately labelled diagnostic stratum of flagged windows.
   Applied segments shorter than the requested excerpt length are sampled at
   their own duration. For each excerpt the channel is the first in a seeded order whose excerpt
   has ≥ 5 s of stage-07-style voiced speech; speech-free selections are kept
   and counted separately. Rendering uses the qualified 05a plans.

The regression command applies the pass-2 contradiction rule to the saved
production-regression windows. With the committed policy it flags no
confirmation window and exactly one sweep3 window, A15_T510073 36002
(8 decision-half events, median ratio 1.66, reversed 0.88), with no
tape-specific code; held-out events in the kept sweep3 windows give median
ratio 0.22 with 2.2 % worse, the removed ones 2.0.

## Folders

| Under `D:/Apollo_15_30-Track/deflutter_analysis/<census-run-id>/` | Contents                                                                                |
| ----------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `<analysis-id>/config.json`                                       | Identity: census run, input-manifest hash, analysis code hashes, settings, policy, seed |
| `<analysis-id>/input_manifest.json`                               | Every tape's status and evidence/stream hashes                                          |
| `<analysis-id>/archive_summary.json`, `tapes.json`                | Archive and per-tape summaries                                                          |
| `<analysis-id>/windows.jsonl`, `events.jsonl`                     | Window index (flags, class) and physical beep events                                    |
| `<analysis-id>/followup_queue.json`, `followup/`                  | Queue, job plan and status with yield summary                                           |
| `<analysis-id>/qualification/<tape>/`                             | `decisions.json` and `qualified.f64`                                                    |
| `<analysis-id>/validation.json`, `qa/selection.json`              | Held-out validation; frozen QA selection                                                |
| `followup_cache/<key>/`                                           | Shared follow-up results and hypothesis curves                                          |

An analysis ID (`analysis-<16 hex>`) changes with the census state, the
analysis code (`qualification_policy.py`, `flutter_analysis.py`,
`flutter_followup.py`, `flutter_qualification.py`), settings, policy or seed.
The assessment and qualification manifests bind their data files by SHA-256.
Cached follow-up jobs also verify their measurement and curve files on reuse.

## Commands

From `F:\_repos\Apollo_15`, after the census run has finished:

```powershell
$census = 'D:/Apollo_15_30-Track/deflutter_detection_census/<run-id>'
$run = uv run python scripts/run.py 05_analyze_flutter.py run --census-dir $census --exclude-tape A15_T510096 --workers 4 | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Qualification or validation needs attention. See $($run.validation_report)" }
$a = $run.analysis_dir
```

`run` performs assessment, follow-up, qualification and validation. Repeating
it reuses completed artifacts. It returns the analysis directory and validation
result as JSON and exits with code 2 for an unfinished pass or failed validation.
The individual subcommands remain available for inspecting or retrying a stage.
`regression` measures the saved development corpus independently of a census run.

`followup` resumes from cached regions; failed regions are listed in
`followup/status.json`. `qualify` refuses to run until the follow-up pass is
complete. For diagnosis, `--allow-incomplete` writes a separate snapshot under
`qualification_previews/` with unfinished windows marked `incomplete`.
Stage 05a reads the finalized qualification. Running `run` again can finish
the same analysis after a partial snapshot.

Compile qualified maps into fresh roots that name both IDs:

```powershell
$plans = Join-Path 'D:/Apollo_15_30-Track/deflutter_planning_qualified' "$(Split-Path $census -Leaf)/$($run.analysis_id)"
uv run python scripts/pipeline.py run --stages 05a --all-tapes --exclude-tape A15_T510096 --flutter-qualification-root $a --flutter-planning-root $plans --plain
uv run python scripts/30-track/05_deflutter/deflutter_readiness.py --planning-root $plans --qualification-root $a
```

The plan's `census` block has `kind = qualified`, the analysis ID, policy and
decision/stream hashes and the excluded intervals; `plan.qualified` is true and
`map.excluded_seconds` reports contradiction time that bypassed every source
(including saved regional/v5 evidence). `05a --raw-census-root <census run>`
still compiles raw detector candidates for research; such plans have
`kind = raw_candidates` and `qualified = false`.

Freeze and render the listening batch (stage-04 audio by default):

```powershell
uv run python scripts/run.py 05_analyze_flutter.py qa-batch --analysis-dir $a
uv run python scripts/run.py 05_analyze_flutter.py qa-render --analysis-dir $a --planning-root $plans --output reports/listening_qualified_<date>
```

Each render folder holds matched A.wav/B.wav through the tape's one shared map;
`manifest.json` binds the selection and map hashes. Report failures as well as
improvements and record decisions with those hashes.

## Not implemented

- Acceptance of new follow-up hypotheses (alternative clusters, unsupported
  regions with additional evidence). They are measured and reported; using them
  requires a new policy, which is a new analysis ID, and fresh validation.
- Genus workers for the follow-up pass; it runs on Proprius (≤ 6 processes).
- Event ratios in validation use the census window curve that measured each
  event, not a re-measurement through the joined qualified stream.
