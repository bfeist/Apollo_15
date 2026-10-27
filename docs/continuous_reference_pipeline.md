# Continuous-reference pipeline design

Stage `05a` uses one continuous-reference engine. Stage `05b` consumes saved
maps and prepared stage-03 channels. Use `render_channel_review.py` for bounded
listening pilots; headless evidence and acceptance utilities remain available.

## Run and resume

```powershell
uv run python scripts/pipeline.py run --stages 05a --tape A15_T510083 --plain
uv run python scripts/05a_plan_deflutter.py --input D:/Apollo_15_30-Track/orig_concatenated/A15_T510083 --max-windows 1
```

The second command checkpoints after one new monitoring passage. Repeat it
without `--max-windows` to continue; compatible unfinished plans resume
automatically. A complete compatible plan is reused without reanalysis.
`--monitor-seconds` (default 60) controls evidence/revalidation cadence;
`--chunk-seconds` (default 7) controls bounded reference I/O. Changing the I/O
size does not change candidate choices, lifetime boundaries or the intended map.
Pipeline equivalents are `--flutter-monitor-seconds` and
`--flutter-chunk-seconds`. `--reference-limit` bounds candidate screening; its
pipeline equivalent is `--flutter-reference-limit`.

Default roots beneath `D:/Apollo_15_30-Track` are `deflutter_planning_v4`,
`defluttered_v4`, `quindar_post_deflutter_analysis_v4`, and
`defluttered_retimed_v4`. Denoised intermediates are reused. Existing v2 and v3 outputs
are preserved. Continuous plans discover their own references and do not require
an all-channel 1 kHz survey. The retired regional planner and excerpt-manifest
renderer are no longer command options. Historical map readers remain for saved
evidence. Code changes invalidate implementation-bound plans and acceptance;
use a fresh destination for planning after this cleanup. Stage-03 cleanup code
and its cache fingerprint are unchanged.

## Acquisition and quality policy

T510073's v3 run accepted no reference lifetimes. Retrospective audit found that all 382 selected decisions failed the absolute restoration goals, while 273 met the old relative improvement rules before support/controls. Reconstructed development cases showed that some strong reductions passed all controls and uninterrupted support. This does not establish full-tape speech preservation or fresh methodology qualification.

The v4 planner distinguishes `restoration_target_met`, `supported_improvement`, and `unqualified`. Planning eligibility requires every validation margin ratio <=0.8 and median <=0.5, with the existing meaningful-baseline, independent-window, continuous-support and negative-control requirements. Absolute residual <=6 Hz and <=5 cents additionally earns the restoration label. Partial spans keep their label during monitoring, joined-map validation and listening cohort creation; they cannot silently become fully restored. Reserved checks must still show strong improvement for a partial span. Production/listening acceptance remains separate.

Low-carrier acquisition now tests the original capture and a modulation-aware alternative. A 117.5 Hz reference's old 12 Hz capture attenuated FM sidebands; widened alternatives improve a known synthetic case from 8.47 to 0.89 Hz worst residual. Wider capture sometimes worsens real passages, so calibration chooses between candidates and competing-tone checks remain. Discovery defaults to eight candidates per channel and eight shortlisted references. The selected trial's actual calibration loss is recorded.

A checkpointed per-channel evidence ledger assigns immutable C,C,V,V,F roles before candidate scoring. It accumulates up to 300 seconds, consumes attempted passages before retry, and never pools channel copies as independent events. Only continuously supported acquisition evidence may qualify. Once a waiting interval validates, its supported portion may be backfilled retrospectively; already qualified spans are never replaced by that backfill. Early passages with no detected check tones remain genuine evidence gaps.

`optimization_outcome` and CLI summaries distinguish accepted correction coverage from successful execution. Existing research artifacts remain local comparisons; execution success alone does not qualify a tape for correction.

## Reference lifetime follows evidence

Find a useful reference early in a tape, establish that its correction improves
independent evidence on the intended target or playback group, and continue
using it while the signal and correction remain valid. Ideally the same
reference and method cover the entire tape. Do not rediscover, reselect, fade or
reset displacement merely because a minute or another processing chunk ends.

`continuous_tracking.py` carries complex demodulation, FIR filter state,
decimation phase, phase unwrapping, amplitude/capture support and clipping
exclusions across arbitrary I/O chunks. Its known finite filter latency is
removed from original-sample coordinates. Mean-frequency normalization follows
slow local drift within the declared capture band without applying slow
retiming. Capture loss requires reacquisition. The first implementation tries
signed 2-15 Hz and 2-30 Hz modulation bands; it does not claim to remove all wow
or all flutter. An internal chunk boundary neither resets state nor changes
the intended map.

Continue monitoring carrier amplitude, phase continuity, capture loss, clipping,
competing tones, agreement with independent references, and available target
check tones. A carrier remaining audible is necessary but does not by itself
establish that its correction remains appropriate for every target. Playback
groups require demonstrated shared motion; do not infer them from content lag.

The implementation uses this tracking lifecycle:

1. Acquire a candidate and calibrate its correction on development evidence.
2. Validate on separate evidence, establishing the target/group scope.
3. Track continuously, carrying state across chunks and monitoring quality.
4. On deterioration, retain the failure, bypass the affected passage, and search
   a subsequent fresh passage. Avoid switching a healthy incumbent merely
   because another candidate exists.
5. On unsupported signal loss, stop correction with a controlled transition.
   Reacquire and revalidate before resuming; do not extrapolate across the gap.
6. Finalize the continuous map, check reserved evidence and prepare bounded
   listening comparisons before export.

A quality-triggered switch, dropout or source boundary defines a correction
span. A processing boundary does not. Retain exact sample coordinates, method,
reference identity, evidence, support, failures and switch reasons for each span.

Monitoring is conservative and retrospective. A short supported gap ends the
current lifetime at the first unsafe sample; resumption waits for independent
validation on a subsequent monitoring passage. A detected target-quality failure
bypasses that whole monitoring passage. The implementation does not promise
instantaneous switching at the exact acoustic change. Direct transfer evidence
expires after five minutes without a new independent check. Playback groups
require a directly validated donor and a distinct target carrier with matching
zero-lag motion; the target carrier is streamed independently and group
membership is rechecked on each passage. Group-carrier loss or disagreement
ends the affected group's lifetime without restarting a healthy donor.

## Optimize the result automatically

Carrier discovery, detailed tracking, parameter trials and map construction run
inside one resumable planning job. Broad original-audio tone discovery is
screened against target calibration events before the bounded expensive
shortlist. Calibration-comparable candidates prefer reference quality; the
selected choice is frozen before separate validation. Healthy incumbents are
reused forward. Unresolved channels without new qualifying evidence do not
trigger exhaustive discovery on every passage. Group reacquisition can reuse a
healthy directly supported donor. This is a bounded search, not proof of a
globally optimal reconstruction.

Compare independent residual flutter, worst-event worsening, supported coverage
and transitions. Include identity as a candidate, but report unsupported bypass
separately from successful correction. Improvement on the reference used to fit
the map is not independent validation. Numerical improvement does not establish
speech preservation; retain representative listening and explicit failures.

Adaptive search may use calibration/development evidence. Freeze the selected
method before final holdouts. A failed final holdout cannot be repeatedly reused
to choose a replacement and then presented as fresh evaluation. Report budget
exhaustion and insufficient evidence rather than manufacturing success.

Every fifth separated detected target event receives a stable reserved role before acquisition and monitoring. Playback groups reserve a separate final portion of each independent
carrier window. Those original-coordinate checks run through the exact final
joined, support-faded, duration-anchored inverse map. Failed or insufficient
final checks remain explicit; no alternative is fitted in response. Listening
still establishes speech preservation, not the numeric optimizer.

Schema-2 plans have `kind: continuous_reference`, actual per-target reference
spans and append-only 200 Hz binary speed/support streams. The single
`regions` entry is a compatibility envelope for existing evidence consumers,
not a fixed analysis region. Only physical reference/support edges are faded;
the renderer integrates once over the complete channel and preserves constant
displacement through bypasses. Binary stream validation hashes bounded blocks.
Generation-named tracker state, detail and checkpoint files commit through
`plan.json` last. Resume verifies the committed prefix before discarding any
uncommitted stream suffix. Original identities, evidence and stream hashes are
checked; changed settings/code require a fresh destination.

The methodology is `continuous-carrier-v4`. Older rollout/production acceptance
does not authorize these maps. The existing final-map and representative
listening gates remain in force; completing a plan does not approve production.

Keep fast flutter and slow Quindar speed estimation distinct inside the job.
The first implementation may retain the existing post-deflutter intermediate.
Later, evaluate composing maps for one final audio resampling pass. Absolute
historical time anchoring is a future process, not part of this redesign.

## Independent IRIG-B discovery

`scripts/irig_search.py` implements a separate full-timeline search on numbered
channels 1, 2, 3 and 4 of each canonical tape. Labels do not decide which of those
channels are searched. Channels outside that scope and unlabelled recordings
are not silently substituted. Missing channels make negative coverage incomplete.

Reuse the existing bounded 700-1300 Hz carrier screen and original-sample IRIG
decoder, with separate search caches. This bandwidth comes from IRIG discovery;
it is not a restriction on the future deflutter reference search. The CH01-CH04 screening cache is separate from reference acquisition in stage 05a.

The search report distinguishes screening-only/incomplete results, carrier
candidates, provisional decoded frames and qualifying sequential runs. It keeps
hash-bound links to all decoded frames, original sample positions, filter and
polarity trials, discontinuities, and the decoder implementation. Overlapping
decode trials can describe the same run and are not independent clock evidence.

These are snapshots of the recorded clock for a future anchoring process. Do not
require denoising, a flutter plan, a rendered channel or production acceptance
to discover them. Do not force them into a speed curve or automatically label
their recorded basis as GMT. Existing clock-basis authentication remains a
separate operation. Preserve original coordinates so future maps can transport
anchors explicitly without assuming alignment between channels.

## Reference qualification coverage

The rollout evidence field `carrier_qualification` is retained, but for
`continuous-carrier-v4` it means qualification of the planner's reference
acquisition and continuous tracking. It does not require a standalone survey.
Historical 700-1300 Hz screening and IRIG-decoding reports cannot establish
qualification of the wider deflutter search.
No mission-wide survey rerun is needed for continuous planning.

Qualification evidence must bind the current implementation and policy, exact
original passages and selected parameters, and report failures as well as
successes. Cover these conditions before methodology acceptance:

- Discovery across the supported recorded band, including low carriers near
  117.5 Hz and higher references, with check-tone frequencies excluded from
  fitting. Report candidate limits and missed/unsupported coverage explicitly.
- Original and modulation-aware capture alternatives, independently measured
  correction benefit, nearby competing tones, amplitude modulation and clipping.
  Wider capture must win calibration rather than become an unconditional default.
- Long-lived references, slow drift, arbitrary I/O chunking, brief dropouts,
  capture loss, reacquisition and failure when an audible carrier no longer
  represents the target's motion. Never bridge unsupported gaps.
- Independent target evidence and playback-group transfer, including group
  disagreement/loss, sparse evidence and genuine evidence gaps. Retain immutable
  calibration, validation and final roles; channel copies do not add independence.
- No-motion, wrong-sign, wrong-time and amplitude controls; partial improvement
  versus absolute restoration goals; exact final joined-map checks and bounded
  speech listening on fresh mission passages. Development fixtures alone do not
  establish mission-wide speech preservation or production acceptance.

Existing automated development coverage lives in `test_low_carrier_capture.py`,
`test_continuous_planning.py`, `test_continuous_acquisition.py`,
`test_continuous_quality.py` and `test_continuous_checkpoint.py`. The pipeline
integration test plans and renders synthetic tapes without creating a survey.
These tests support the qualification process; they do not issue acceptance.

## Verification and remaining qualification

Synthetic tests cover arbitrary I/O chunk invariance, actual rendered independent
tone improvement, long reference retention, short dropouts and reacquisition,
live but inappropriate carrier motion, alternative selection on fresh evidence,
playback groups and membership loss, original filename offsets, fractional final
map samples, failed final holdouts, crash recovery and headless evidence use.

```powershell
uv run python -m unittest discover -s tests -p "test_continuous*.py" -v
uv run python -m unittest discover -s tests -p "test_low_carrier_capture.py" -v
```

The new engine has not been qualified on fresh mission audio or accepted through
speech listening. Its thresholds remain development policy. Absolute historical
clock anchoring, a new UI, and a combined single-resampling flutter/slow-speed
export remain future work.

## Listening and production acceptance

Generate an unapproved methodology record outside the source tree:

```powershell
uv run python scripts/deflutter_readiness.py --write-rollout-template reports/acceptance/rollout.json
```

After review, the record must identify the reviewer/date, current implementation
and policy hashes, and hash-bound JSON reports for `carrier_qualification`,
`fresh_holdouts`, and `speech_listening`. Those reports and the rollout record
must explicitly be accepted. Development fixtures cannot authorize production.

Render a review from a saved plan, using the channel's original-timeline start:

```powershell
uv run python scripts/render_channel_review.py --help
```

Whole-channel production uses prepared denoised intermediates. Bounded pilots
can use aligned review caches. `--experimental-transfer` is review-only.

Save `production_acceptance.json` under each tape's planning directory, or under
the tape directory in `--production-approval-root`. The explicit per-channel
contract is:

```json
{
  "status": "accepted",
  "rollout_approval": "ABSOLUTE_PATH_TO_ROLLOUT_JSON",
  "plan_sha256": "EXACT_PLAN_HASH",
  "channels": [
    {
      "channel": 20,
      "speech_preserved": true,
      "transitions_accepted": true,
      "continuous_review": {
        "path": "ABSOLUTE_PATH_TO_REVIEW_JSON",
        "sha256": "EXACT_REVIEW_HASH"
      }
    }
  ]
}
```

Acceptance requires a matching conservative full-channel review, passed final-map
checks and unchanged timing artifacts. Failed or insufficient reserved evidence
cannot be promoted with a listening checkbox. Identity-only bypasses retain their
explicit no-correction status and still require listening review.

`review_evidence.py` and `review_sampling.py` preserve bounded comparisons,
judgments and sampling-certificate validation. Their existing certificate policy
identities remain binding. Historical schema-1 map reading is retained for saved
evidence; it does not expose a second planning engine.

```powershell
uv run python scripts/deflutter_readiness.py --rollout-approval reports/acceptance/rollout.json
```

Exit code 2 means production is not ready. Batches continue after individual
failures and retain logs. Existing stage-05b outputs are not overwritten; use a
new output root for a new render.
