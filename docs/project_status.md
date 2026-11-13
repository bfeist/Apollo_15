# Project status and local artifacts

## November 13 shared-map refactor

Stage 05a now imports saved all-channel tone evidence and compiles one native
shared tape map. Stage 05b and the Audition comparison exporter use completed
v2 denoised inputs and that identical map, preserving recorded channel offsets
and v2 gain. Excerpts crop the common map without independent anchoring.
Legacy planner/transfer/v1 rendering options, the old command alias, review
desk, sampling/approval machinery, and bounded shared-map adapter are removed.
The numerical readers needed for saved evidence remain. Removed source is
recoverable from Git and research snapshots; approval-bound artifacts are unchanged.
See [the operating guide](shared_deflutter.md), including the read-only tone
sighting lookup. The refactor ran synthetic regression checks only; no new
archive survey, map compilation, or listening render was performed.

The approximately 20% saved eligible coverage on T510076 is not an estimate of
tone occurrence or the final correctable fraction. Visual sightings in original
WAVs can be compared against discovery, tracking, and shared selection records
before deciding whether targeted new analysis is needed.

Reviewed November 3, 2026, against repository history through `b72d45e`, the
local research records, and the data folders. The listening updates below
record subsequent work and user feedback.

## November 8 listening update

V2.16 is the current denoise candidate. It addresses low-hum recurrence at
25:30–26:20 in the T510083 CH20 comparison. The current matched review and
its measurements are indexed in the [denoise findings](denoise_v2_plan.md).
The preferred 05 and earlier feedback-bound comparisons remain at their
original paths. Superseded experiment reports are in the local archive.

The [all-tapes rollout runbook](denoise_v2_all_tapes_runbook.md) covers 1,130
denoise jobs and 39 timing copies. The full-channel canary is the remaining
listening gate before moving the current output to `denoised_v1`. New
production output will use `denoised`.

The owner accepted the 30-minute T510083 CH20 shared-map comparison:
"C sounds great. I think this is a viable approach". The decision is bound to
the WAV and map hashes in `reports/listening_long_20261103/C_listening_approval.json`.
It covers the delivered CH20 passage at canonical seconds 13,200–15,000.
The B approval and both generation manifests retain their original records.

The same saved inverse map was applied to CH17 for a second listening review.
`reports/listening_shared_CH17_20261108/A.wav` is denoised only, and `C.wav`
adds shared-map correction. Both use the existing CH17 denoised cache, the
same canonical interval as CH20, and the same fixed gain as the CH20 review.
`review.json` records both WAV hashes, the source offset, and the map hash.
CH17 listening and whole-tape production acceptance are pending.

## November 3 listening update and working direction

**Methodology correction:** the owner reaffirmed that all 30 simultaneous tracks
share tape motion. The reviews below mistakenly gated application on CH20 alone.
Their listening approvals remain intact, but that selection rule is superseded
by one shared tape map drawn from all channels. See
[the tape-level rule](tape_level_deflutter.md) and repository `AGENTS.md`.

The corrected 30-minute comparison is now `reports/listening_long_20261103/C.wav`.
It reuses existing regional maps from all channels and completed v5 all-channel
continuous streams. One shared map supplies 960 seconds of regional correction
plus 150.275 seconds from v5 in remaining gaps: **1,110.275 seconds (61.68%)**
corrected, with 689.725 seconds bypassed. B had 660 corrected seconds. C uses the
same denoised source, duration, 24-bit WAV format, and fixed gain as A/B; both
earlier files retain their approved hashes. The tape map was verified identical
when reconstructed for CH17 and CH20. No new discovery or fitting was run.
`C_review.json`, `C_protocol.json`, and `C_shared_plan/selection.json` preserve
the source choices and checks. See the November 8 update for C's listening decision.

The T510083 CH20 regional method reproduced the previously approved three-minute
audio exactly, then supplied a ten-minute review at canonical seconds
13,800-14,400 using the completed denoised cache and identical gain for A/B.
Correction is active at 13,920-14,280; the outer sections bypass local speed
correction. Short samples are exact crops of the joined ten-minute output.

The user preferred B: "B is better and holds up for quite a while," while
reporting that some Quindar tones retain flutter and others do not. The user
accepted the compromise: "fix where fixable and leave where not." Exact listened
clips and duration were not specified, so this does not assert approval of every
second, every transition, other channels, or whole-tape production.

Working direction: pursue useful regional correction, preserve denoised audio
where support is absent or correction worsens it, and assess speech and joins
through matched listening. Residual flutter alone does not erase an accepted
audible improvement. Preserve the distinction between listening acceptance and
authenticated physical timing correction; the October 30 validator limitations
still apply. The follow-up reviews use bounded passages to assess additional
voices and correction/bypass boundaries before a whole-tape pilot.

Exact audio, maps, reproduction checks, and hash-bound user feedback are in
`reports/listening_extension_20261103/`, especially `listening_approval.json`.
Its feedback supersedes the generation-time listening-pending status within
the stated scope; the original render manifest remains unchanged.

The next bounded review is now rendered in `reports/listening_long_20261103/`:
T510083 CH20 at canonical seconds 13,200-15,000, thirty minutes with eleven
minutes of saved regional correction and nineteen minutes bypassed. Three
short correction samples lie outside the earlier ten-minute review, and two
additional clips cross correction/bypass boundaries. All clips are exact crops
of the single thirty-minute output, with identical A/B cleanup and gain. The
historical three-minute audio and previous ten-minute map reproduced exactly.
Audio integrity, map limits, bypass behavior, page switching logic, and local
delivery were checked. After receiving the full A/B WAVs for Audition, the user
approved this result: "that sounds acceptable. corrections were very good where
available and not where not." The thirty-minute result is now listening-approved
as useful regional correction with bypass. Exact audio and map hashes are bound
in `reports/listening_long_20261103/listening_approval.json`; the pre-listening
render manifest remains unchanged. This acceptance covers the delivered result,
not other channels or the full tape, and does not assert separate approval of
each individual tone or transition.

Delivery preference: provide matched WAV files directly for review in Adobe
Audition; listening web pages and local servers are unnecessary. For the
thirty-minute review, `reports/listening_long_20261103/A.wav` is denoised only
and `B.wav` adds regional defluttering. Both are mono, 8 kHz, 24-bit PCM,
with the same duration, source interval, cleanup, and common gain.

## Where the audio work stands

Concatenation, original-file archiving, and denoising are recorded as completed
pipeline stages. The external data root still contains 39 canonical-original
tape folders, 39 denoised tape folders, and 39 v5 all-channel planning bundles.
All 39 saved survey manifests report completion; saved assessments still
distinguish proposed correction from production acceptance.
The folder check does not independently revalidate every original or denoised
WAV. The old `defluttered/` and misspelled `defulterred_retimed/` folders each
contain only T510083; their existence is not evidence of an accepted archive-wide
restoration. All external data was left in place.

The October 30 Dot campaign finished its reporting through I054, but **did not
qualify a general deflutter method or any production correction time**. It
searched 1,200 original/denoised source excerpts, totaling 88 source-hours across
nine tapes: 0.22916% of available source/version time. Thirty tapes remain
unsearched by that campaign, despite the earlier v5 planner having surveyed all
39. These are different searches with different methods.

| Campaign outcome | Tapes | Meaning |
| --- | ---: | --- |
| Numerical leads | 2 | T510103 and T510108; unqualified, above the absolute residual targets |
| Too few suitable events in the sampled passage | 2 | T510085 and T510098; not proof that useful references are absent |
| Sampled validation failures | 5 | Local candidate failures, not whole-tape impossibility |
| Unsearched by this campaign | 30 | No campaign-level negative finding |

The critical finding was that stationary electrical interference could pass
the correction checks even when there was no flutter to correct. Repairing a
held-out feature-selection leak did not fix that physical ambiguity: two
stationary cases still passed the repaired checks. Lower measured tone jitter
therefore does not establish corrected tape motion or cleaner speech.

The bounded independent-witness investigations also stopped: no valid coded
time frames in the fixed excerpts, no adequate delayed broadband copy in the
tested pair, and no electrical donor with established independence from target
content. Those results are limited to the tested paths. The useful next step
needs new source/transfer provenance or a distinct positive reference lead,
followed by a frozen qualification experiment and listening assessment. More
tuning on the same consumed examples would not resolve the ambiguity.

See [the research findings](deflutter_research_findings.md) for the numerical
limits and [the campaign guide](../research/DEFLUTTER_CAMPAIGN.md) for reproduction.
The campaign state records no active workers and no production acceptance.
There is no evidence here of established speech-quality benefit from that
campaign; its ASR and NISQA runs were exploratory measurements.

## Local successes worth retaining

Earlier listening judgments remain useful within their exact scope. They
predate the October 30 campaign and do not approve whole-tape processing.

- `reports/channel_review_smoke_T510083_CH20/`: the three-minute CH20 passage
  at canonical seconds 13,920–14,100, with the recorded approval of both
  transitions and hashes binding it to the exact audio.
- `reports/cyclic_flutter_20261017/`: the two T510077 CH15 examples documented
  as listening-approved in the historical research journal.
- `reports/accepted_flutter/`: historical T510088 local comparison material.
- `reports/bespoke_expanded_20261017/`: earlier bounded comparisons and summary;
  current review code also refers to this summary. These are not blanket
  production approvals.

The ignored `docs/IRIG_FREE_FLUTTER_RESEARCH.md` preserves the earlier listening
decisions and failures. Its October 17 statements about the then-current
pipeline are historical. Some of its linked trials are now archived and must
be restored before following their original file links.

## What is kept where

| Location | Purpose and retention |
| --- | --- |
| `scripts/`, `tests/`, `research/`, `docs/` | Tracked implementation, regression checks, methodology and summaries; retained |
| `reports/deflutter_iterative_20261030/` | Primary I000–I054 evidence, exact code snapshots, maps, failures, source manifests, exposure ledgers, review audio and decisions; retained |
| `reports/deflutter_discovery_audit_20261030/` | Audit that motivated the campaign; retained |
| `reports/research_continuous_20261026/`, `research_coverage_20261027/`, `research_v5_allchannel_20261027/` | Earlier planner implementation and qualification evidence; retained |
| `runs/` | Pipeline execution/provenance logs, including denoising and v5 batch/recovery; retained |
| `archive/2026-10-16_experiments/` | Pre-existing retired experiments and source; retained |
| `archive/historical_reports_20261103.zip` | Consolidated older report bundles, review caches, demo/smoke outputs, and retired UI artifacts; verified against original SHA-256 hashes |
| `sites/` | Listening-site checkout, its nested Git history, and versioned exports; retained because they are not established duplicates |
| `.venv/` | Working project environment, also used by editor services; retained |
| `reports/repository_cleanup_20261103/` | Full before-inventory, move manifest, hashes, archive verification, data-root checks, and cleanup record |
| `2 delete/20261103/` | Recoverable originals of archived bundles, plus expendable caches and test scratch |

Source snapshots that look repetitive are part of the experimental record:
different iterations bind decisions and measurements to those exact versions.
They were not deduplicated or edited. The current campaign's downloaded NISQA
weights and install/download manifests remain; its isolated installed dependency
directory was quarantined. Repeating that optional model assessment requires
restoring or reinstalling those dependencies. Historical test scratch is also
quarantined; its housekeeping manifests remain as historical records.

## Cleanup and restoration

The cleanup was restricted to this repository. No canonical recordings,
denoised channels, external planning bundles, source files, Git history, or
approval records were deleted. The older reports were consolidated first, and
each archived file was checked against its original SHA-256 digest before its
source was moved. Installed dependency copies and disposable scratch were
identified separately; they are not included in the historical-report ZIP.

Read `2 delete/20261103/README.md` and the audit's `manifest.json` for the exact
relocations. Deleting `2 delete/20261103/` reclaims the quarantined space;
moving it there alone does not free disk space. Keep the historical ZIP and
audit manifest if you want to reproduce old trials.

To restore a particular item while the quarantine exists, use
`reports/repository_cleanup_20261103/restore.ps1` with its original
repository-relative path. It refuses to overwrite an existing destination.
After deleting the quarantine, archived evidence can be extracted from the ZIP
into a temporary directory and the needed `reports/...` subtree restored to
its original position. Old reports and queues can contain absolute paths;
viewing them inside the ZIP is not equivalent to restoring them. Current
campaign evidence and the retained approved examples keep their original paths.

Tracked changes from this audit are documentation and the quarantine ignore
rule only. Validation checks the archive, relocated bytes, retained files, and
approval-bound artifact hashes; processing tests are not rerun for this cleanup.
