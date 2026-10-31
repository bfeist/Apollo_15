# Iterative deflutter research

The authority and evaluation protocol are in
[`docs/deflutter_iterative_research_runbook.md`](../docs/deflutter_iterative_research_runbook.md).
This research does not modify the production planner or audio archive.

The tracked [research findings](../docs/deflutter_research_findings.md) summarize
the completed October 30 campaign and its stopping decisions. No challenger
qualified for production. Research modules and tests are retained for
reproduction; generated evidence stays local under `reports/`.

`deflutter_*.py` belongs to the October 30 iterative campaign. `v5_*.py`
contains the earlier October 27 planner audits, evidence exports and bounded
evaluations. Those historical scripts include fixed local paths and some run
on import; use their declared command entry points only when reproducing a
specific protocol. They are not batch production commands.

Campaign state and generated evidence are in
`reports/deflutter_iterative_20261030/` (Git ignored). Read `INDEX.md`,
`CONTINUE.md`, `active_jobs.json`, `state.json`, `NEXT.md`, and the latest
iteration decisions before resuming. `auxiliary_jobs.json` records bounded
independent diagnostics when a fixed batch controller owns the main job list.
Do not rerun `init` into the existing campaign or rerun completed iteration IDs.
Every experiment snapshots its exact implementation before reading its outcomes.

## Entry points

```powershell
.venv/Scripts/python.exe research/deflutter_campaign.py --help
.venv/Scripts/python.exe research/deflutter_motion_experiment.py --help
.venv/Scripts/python.exe research/deflutter_real_experiment.py --help
```

The campaign driver provides inventory, fixed split construction, source-bound
baseline replay, synthetic discovery comparisons, and bounded discovery from
explicit passage manifests.
`deflutter_catalog.py` has no target-event/correction evaluator imports. Its
detector receives samples/rate/configuration, never circuit names. Original and
cached cleaned sources are searched separately. Hann resolution-derived mains
masks are recorded; they are not a physical-origin classifier. Withheld response
bands and discarded short ridges have explicit counts.

The first detector generation falsely counted two overlapping eight-second
noise windows as ten seconds of persistence. The second requires ten seconds
between observed window centers. A coarser robust-background grid reduces cost;
its exact configuration and synthetic comparison are preserved. Amplitude
modulation sidebands remain hypotheses, not independent carrier evidence.

`deflutter_motion_experiment.py` tests known signed motion and an independently
generated response oscillator on the same channel. It freezes its extraction
choice on calibration before testing validation. The synthetic warp convention
is explicit: recorded `y(t)=s(t+d(t))`, relative speed `d'(t)`.

`deflutter_real_experiment.py` merges catalog provenance, keeps separate source
extractions, assigns response roles, freezes calibration choices,
and evaluates exact supported/anchored maps. It permits same-channel references
outside the independent response band. Failed validation is consumed, never a
prompt to cycle through alternatives on those same outcomes. Budget curves are
calibration only. The frozen saved baseline map is reconstructed for its full
intended duration before taking the comparison passage. No short excerpt is
reanchored and called the saved baseline.

## Current scientific restrictions

Use `--lag-aware-roles` for new real gates. The original half-second coincidence
grouping missed delayed copies: one exposed example had approximately1.404s
pairwise delay. Its earlier attractive results, including already-inspected
final events, are quarantined in `evidence_restrictions.json` and
`exposed_response_families.json`. They remain retrospective numerical gains,
never independent qualification. The new grouping estimates each pair's own
delay, withholds ambiguous patterns and preserves one role per response family.
It does not establish authenticated transport/head/feed independence or online
role persistence.

`--bounded-acquisition` is a separate research method. It freezes the earliest
event-ready common interval before predictor scoring, estimates source amplitude
from fixed source-only windows, requires continuous physical support across its
local calibration/validation, and leaves map speed zero outside the attempt.
Controlled tests distinguish this from demanding one reference across every
event in300seconds. Local validation cannot approve an entire tape or extend a
map past its measured lifetime. These are offline maps, not streaming-prefix
qualification.

`deflutter_map_preflight.py` independently checks exact main/control maps before
target outcomes. Local anchoring can push the pre-bounded AM control over the
unchanged5% speed limit; one failed real attempt reached5.30%. The check rejects
that condition without clipping/refitting. It is isolated from the frozen I016
batch; consult its protocol before enabling it in a new generation.

`deflutter_adaptive_mains.py` and `deflutter_high_rejection.py` are development
extraction/stream prototypes. They never modify canonical originals or cached
cleanup. A matched synthetic ablation showed that response-burst contamination
of a very weak reference can defeat same-channel extraction even when a separate
reference with identical timing motion succeeds. The80dBPCM16near60Hz case still
failed. Neither narrow spectral purity nor successful synthetic separation
authenticates a real historical oscillator. Full-band adaptive-prefilter
discovery remains unassessed after an execution interruption; preserve the
original discovery path in any retry.

## Execution and output assessment

All numerical limits remain frozen. Mark validation consumed before outcome
access; never retry a consumed failed or interrupted gate. An execution
interruption before any consumption marker/result may be recovered in a new
directory only after checking worker liveness and frozen identities. The
one-off `deflutter_recover_gate.py` records the I016 interruption evidence and
runs the unconsumed recovery singly. It does not provide general safe resume
for arbitrary historical gates. Real tracking currently lacks per-candidate
resume; discovery and source-only controlled cases have durable checkpoints.

Actual output assessment includes decoded WAV integrity, clipping, energy,
transients and model-viewed spectrograms. No supported human/model audio
listening capability is available in this environment. Do not describe rendered
review files alone as listening, claim intelligibility, or ask a listener to
approve a numerical failure merely to close a loop.

`housekeeping_completed.json` records recoverable consolidation of campaign
test/cache scratch. Preserve source snapshots, decisions, failures, maps,
source manifests and exposure records. Do not move unrelated research/user files
or delete canonical audio. Reports remain ignored and research source/tests
are retained separately from generated output.

## Limits

All 39 v5 tapes are potentially previously exposed. New real results are
retrospective and cannot establish an untouched final claim. Discovered ridges
are not proven historical carriers; trackability is not predictive validity;
numerical validity is not human listening or production approval. Original-only
references remain eligible. No cleanup rerun, external upload, or production
restoration is authorized by these scripts.

The archived corpus remains `not_searched` by the new method outside explicitly
recorded passages. The fixed six-passage development set is only an initial
experiment, not an archive-wide recommendation.

## Tests

Meaningful new checks are in `tests/test_deflutter_*_research.py` and
`tests/test_deflutter_research_maps.py`, covering discovery, map boundaries,
source extraction, delayed-family roles, source-only acquisition and exact-map
preflight. The relevant pre-existing scientific and
checkpoint suite is recorded in the campaign's `legacy_tests.log`.

Run the complete suite from the repository root:

```powershell
uv run --with pytest python -m pytest tests -q
```

For just the iterative research regressions:

```powershell
uv run --with pytest python -m pytest tests -q -k "deflutter and research"
```

The original campaign used a read-only cached pytest via `PYTHONPATH` and
campaign-local temporary directories because its managed environment could
not write the default cache or reach PyPI. Those execution details are recorded
in the local campaign logs; they are not project setup requirements.
