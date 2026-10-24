# Local deflutter evidence desk

From `F:\_repos\Apollo_15`, start the application with the existing environment:

```powershell
.venv\Scripts\python.exe scripts\deflutter_review.py
```

Open **http://127.0.0.1:8765**. Alternatively use `uv run python scripts/deflutter_review.py`. No new Python packages, JavaScript packages, CDN, or build step are needed. Stop with Ctrl+C. `--port` selects another port. The server binds only to loopback.

To exercise listening, decisions, exclusions and simulated acceptance without mission processing:

```powershell
.venv\Scripts\python.exe scripts\deflutter_review.py --demo
```

The demo makes six two-minute synthetic recordings, regional evidence, denoised fixtures and independently checked continuous maps under `reports/deflutter_demo/`. These are harmonic voice-like signals and measurement tones, not real speech or scientific qualification. Demo plans and acceptance records are explicitly rejected by CLI production/planning gates. A changed implementation creates a separate fixture directory; prior demo records remain intact.

## Workflow

1. **Mission evidence** inventories every canonical tape, verifies survey/plan artifacts, checks stage-03 cache compatibility and displays missing/stale inputs. Coverage sums channel-seconds within recorded reference-support spans: direct numeric support, independent transfer-group support, proposals, bypasses and partial-support fades. It reports search-budget exhaustion and failed/insufficient final-map evidence. Refresh is read-only.
2. **Listening sample** previews selection and estimated listening time before generating audio. Choose methodology or a production cohort, adjust the budget/seed/clip length, and optionally select tapes. The default is at most **30 comparisons of 20 seconds**, about **20 minutes** for both versions, plus replay/notes. Length is adjustable from 15–25 seconds and the explicit budget from 1–100 comparisons. A small candidate pool creates fewer tasks and reports missing coverage.
3. **Compare & judge** loads only the current pair of bounded WAV excerpts. Both Web Audio sources start on the same audio clock; A/B switching changes gains with a short click-suppression ramp, preserving position. Loop, scrub, or use Space, A/B, L, left/right arrows (two seconds), 1–4 (choose judgment), and N (next). Record improved, unchanged, worse or uncertain, independent quality flags, reviewer and optional notes. Labels may stay blinded until the judgment is saved. Confirm faint speech only when it is actually audible; a quiet RMS measurement alone does not establish speech.
4. **Validate maps for chosen cohort** is an explicit background numeric job. It constructs/caches full-channel maps and evaluates reserved events for every selected channel using existing denoised inputs. It generates no full-channel audio. Queue creation only prepares channels needed by the selected comparisons. Unsampled channels need the cohort map job before production acceptance.
5. **Preview approval** shows exact tapes, channels, interval coordinates, support spans, excluded proposals/bypasses, condition coverage, failures, map/plan hashes and reviewer. One deliberate acceptance action writes the eligible scope. Excluded channels remain excluded. Nothing in this app starts production rendering.

The server runs one background job at a time, reports progress, caches maps/excerpts, persists queues, and resumes saved judgments. Every judgment is a new immutable event; edits cannot erase previous adverse findings. Use the queue selector to resume after restarting. An interrupted queue's `selection.json` remains available for audit; start a new bounded queue to retry.

## Sampling policy: `representative-listening-v1`

The selector uses stable ordering and an explicit seeded PRNG. Approximately one quarter of slots target the highest-risk exceptions; one fifth is reserved for a reproducible random sample of otherwise ordinary eligible regions. Remaining slots greedily cover recorded conditions, with seeded supplementary samples if slots remain. The random quota remains a requirement after deduplication; inadequate ordinary evidence is reported and cannot be accepted.

Conditions include all five named pilots (T510083, T510077, T510073, T510092 and T510076), a newly inventoried tape outside the prior 16-tape study, head, reference/carrier identity, tracking method, evidence class, early/middle/late passages, strong corrections, faint speech, reference changes, fades, dropouts and no-beep transfer-group targets. Discovered carrier islands add a required island condition; carrier detection never authenticates a clock. Per-channel production scope requires coverage of its recorded cohort, tape, head, method, reference, evidence class and available passage/risk conditions. Cohorts are groups for evaluating listening behavior, **not assertions of common playback phase across tapes**. Every local transfer still requires its own demonstrated group evidence and expiry boundaries.

Candidates come from planned region centers, reference changes and support gaps. Correction strength is measured on the actual full map; a peak local speed correction of at least 0.5% is labeled strong. Optional prior passage annotations in `<plan folder>/review_conditions.json` help locate rare conditions without searching hundreds of listening samples:

```json
{
  "r00012": {
    "20": {
      "conditions": ["faint_speech"],
      "evidence": "Reviewed passage annotation; region-center excerpt contains faint speech"
    }
  }
}
```

Annotations are evidence, not automatic numeric approval. They are hash-bound and changing/adding them invalidates affected map evidence. The interface can also record human-confirmed faint speech in the judgment ledger. Missing conditions remain visible; the application never silently expands the listening budget.

Deduplication requires identical before **and** after audio, with every source coordinate and supporting binding retained. Content lag is neither fitted nor used to move maps. Blinding order is randomized separately from reproducible sample selection and persisted server-side.

Worse, uncertain, or any quality flag marks the affected listening cohort. Reference-boundary findings cover the overlapping cohorts; adverse bypass findings conservatively implicate adjacent correction cohorts in that channel. The interface offers an explicit follow-up of **at most five** fresh candidates, never hundreds. If no unused candidates remain, it reports that gap. Follow-up success does not erase a failed holdout or unlock its cohort. Changed methodology requires fresh passages; overlap with previously reviewed material under another implementation blocks acceptance. Old maps, results, decisions and judgments remain on disk.

## Full-channel maps, bounded audio

The app calls the existing `continuous_map` and `joined_map_checks`, with conservative transfer settings. It inverts the saved **full-channel** nominal map at each selected excerpt's output times and uses the existing resampler and 256-sample halos. The corrected excerpt is the corresponding slice of production behavior, with no local re-anchoring. A regression test compares it against a monolithic full-map render. Playback gain is equal RMS with joint peak limiting, recorded explicitly. The source and corrected audio share the same prepared stage-03 intermediate; cleanup is never rerun here.

For bounded pilots, the app makes a separate, explicitly **review-only** derived plan that covers the channel's entire duration. Unplanned exterior spans are identity bypasses; the full-duration map is joined and integrated once. This avoids a circular rollout dependency: pilots can qualify methodology before full-tape planning is authorized. These derived plans cannot authorize production. After full planning, production needs new evidence from each actual full-tape map.

The normal plan location is `<planning root>/<tape>/plan.json`. If absent, the latest bounded audit under that tape is used for pilot review. `plan_overrides` in the config can select a particular saved pilot audit. The dashboard labels pilots separately from complete production plans. It does not claim to merge contradictory or overlapping pilot plans.

## Acceptance and CLI integration

Methodology acceptance requires hash-bound, explicitly accepted `carrier_qualification`, `cross_tape_pilots` and `fresh_holdouts` JSON reports. In real mode each report must identify the current `implementation_sha256` and `policy_sha256`; reports must document the freeze, fresh evaluation coordinates, measurements and failures. The app supplies the fourth report, `speech_listening`, from actual recorded judgments and coverage. It cannot qualify a carrier detector or manufacture independent holdouts by clicking a checkbox.

Production acceptance requires an accepted methodology, complete compatible full-tape plans, complete original carrier surveys, current prepared denoised inputs, strong direct or demonstrated transfer evidence for applied corrections, passed reserved checks through the final joined map, required sampling coverage and no adverse findings in included cohorts. Unknown, sparse, experimental, failed or search-exhausted corrections cannot become supported through sampling. Proposals remain conservatively bypassed and explicitly excluded from accepted correction intervals. Identity-only outputs can be accepted as bypasses when their required listening and no-harm evidence pass.

Records include policy version, reviewer/time, seed, exact coordinates, all judgments, exclusions, scopes, plan/region/map/survey hashes, hashes of full prepared audio and its cleanup provenance, and evidence bindings. Large audio is hashed in streaming blocks. Browsing uses cached file identities; deliberate approval and CLI production validation rehash evidence. Methodology records and certificates are versioned; per-tape acceptance files are current CLI pointers, with immutable decision history retained. Later adverse findings revoke affected scopes through the shared ledger.

`flutter_policy.require_production` dispatches explicitly to this versioned policy; legacy per-channel approvals retain their existing checks. Sampled approval never masquerades as per-channel listening checkboxes. `require_rollout` also validates sampled methodology and retained failures. Both are used by the existing planning, readiness and production commands.

Default state is `reports/deflutter_review`. After acceptance, inspect readiness using its approval paths:

```powershell
.venv\Scripts\python.exe scripts\deflutter_readiness.py --rollout-approval reports/deflutter_review/methodology/rollout_approval.json --approval-root reports/deflutter_review/approvals
```

Production remains a separate explicit command, for example for an accepted channel:

```powershell
.venv\Scripts\python.exe scripts\pipeline.py run --stages 05b --tape A15_T510083 --channel 20 --production-approval-root reports/deflutter_review/approvals --plain
```

The existing all-tape command can use the same approval root, after inspecting readiness. This app never invokes it. Copying an acceptance file without its immutable certificate/evidence and failure ledger does not create portable authorization.

## Configuration and filesystem boundaries

Defaults use `D:/Apollo_15_30-Track/{orig_concatenated,carrier_survey_v2,deflutter_planning_v2,denoised}`. `--originals`, `--survey-root`, `--planning-root`, `--denoised-root` and `--state` override them. A `--config` JSON can also supply evidence roots/reports:

```json
{
  "evidence_roots": ["F:/_repos/Apollo_15/reports/qualified_evidence"],
  "methodology_evidence": {
    "carrier_qualification": {"path": "F:/_repos/Apollo_15/reports/qualified_evidence/carrier.json", "sha256": "EXACT_HASH"},
    "cross_tape_pilots": {"path": "F:/_repos/Apollo_15/reports/qualified_evidence/pilots.json", "sha256": "EXACT_HASH"},
    "fresh_holdouts": {"path": "F:/_repos/Apollo_15/reports/qualified_evidence/fresh.json", "sha256": "EXACT_HASH"}
  },
  "clock_reports": {},
  "plan_overrides": {}
}
```

To use an already accepted rollout, set `rollout_approval` to its absolute path and include its parent in `evidence_roots`. `clock_reports` maps tape names to lists of JSON report paths; a report must have `clock_constraints.status` or `status` equal to `consistent`. Missing configured clock evidence, unresolved conflicts and `<plan folder>/clock_conflicts.json` with a non-consistent status block acceptance. Clock authentication and stage-05c alignment remain separate scientific workflows; an absent report is never a claim of authenticated mission time.

Inputs are read-only. All writes are confined to a separate state root; it must not overlap an input root. Artifact/source paths must resolve inside configured roots, and HTTP routes accept opaque queue/sample IDs, never arbitrary paths. Loopback Host checks, a session write token, origin checks and a restrictive content policy protect local mutations. There is no upload, delete, denoise or production-render endpoint.

## Verification and current mission gaps

Run the targeted suite:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_review_sampling.py -v
```

It covers seeded risk/random selection and coverage, deduplication/provenance, no content-lag shifting, stale evidence, retained failures, bounded follow-ups, full-map excerpt equivalence, pilot-only scope, methodology/production lifecycle, production-gate dispatch, changed-method holdouts, configured roots, Host/origin/token checks and static serving.

Final verification on 2026-10-24: `python -m unittest discover -s tests -v` ran **113 tests: 112 passed, one skipped** because the existing local delivery reference fixture was unavailable. `node --check scripts/deflutter_review_web/app.js` and `git diff --check` passed. Chrome verification exercised synthetic queue generation, synchronized A/B playback, judgment persistence, blind-label reveal, queue resume and an approval preview blocked by incomplete listening; the browser reported no client errors. Screenshot: `reports/deflutter_review_screenshot.png`.

The read-only inventory performed on **2026-10-24** found **39 tapes, 1,111 compatible denoised channels of 1,130 expected targets, zero complete v2 carrier surveys and zero current v2 full plans**. The newly inventoried candidate outside the prior study was T510078. Snapshot: `reports/deflutter_review_inventory.json` (local generated report). Denoising may continue, so refresh before using these counts.

A full run is therefore blocked by the remaining denoise inputs, carrier qualification/surveys, revised bounded pilots, frozen-method fresh holdouts, representative real-speech judgments, and subsequent full plans/final-map checks. The old T510083 plan/render is preserved as baseline evidence and is not relabeled as v2 acceptance. Demo results do not close any mission evidence gap.
