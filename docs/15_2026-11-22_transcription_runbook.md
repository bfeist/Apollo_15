# Stage 06a interim timing transcript runbook

Stage 06a transcribes completed stage-05b 30-track channels for one purpose: locating mission speech so the tapes can be placed on mission time before stage 06b retiming. It is an interim product. The archive will likely be transcribed again after retiming. The recognizer and chunk store are shared code in `scripts/00_common/`; space-to-ground reels and DVD streams have their own commands and stores (see the end of this guide). Stage 06a uses the tested pilot configuration: faster-whisper 1.2.1 with `distil-large-v3.5` (pinned revision) on CUDA FP16, batch 16, beam 1. The Silero detector runs at threshold 0.12 on a voice-band, RMS-gained copy of the audio, and the recognizer reads the original samples. Each decoder context holds audio from one chunk of one source, and the GPU batches contexts from many sources. Whisper supplies the words; their times come from forced alignment with a CTC acoustic model, as in WhisperX (see [Word times](#word-times-forced-alignment)). Whisper's own word times are unreliable on this material. [Pilot measurements](../research/mission_timing/asr_timing_benchmarks.md) · [Mission timing plan](14_2026-11-22_mission_timing_plan.md)

## What it writes

| Location                                                                                                      | Contents                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `D:\Apollo_15_30-Track\timing_transcripts\<settings-id>\sources\<collection>\<key>\<identity>\chunks\cNNNNN\` | `segments.jsonl.gz` holds segments and words with aligned source-frame endpoints (`sf`/`ef`), Whisper's endpoints (`wsf`/`wef`), raw decoder times, probabilities, alignment scores (`ap`) and flags. `chunk.json` is the receipt: cut frames, owned-sample SHA-256, retained speech windows, decoder contexts, counts, timings and the `alignment` record. |
| `...\<identity>\source.json`                                                                                  | Source identity: collection-relative path, content checksum, sample rate, frames, canonical origin, and for tapes the 05b review and map checksums.                                                                                                 |
| `D:\Apollo_15_30-Track\stage_evidence\timing_transcription\sessions\<session>\`                               | `session.json` holds scope, settings, GPU and status. `progress.jsonl` has one row per GPU group, including a chunk ledger. `failures.jsonl` lists failed chunks. `sources.json` lists the selected sources.                                        |
| `...	iming_transcription\hash_cache.json`                                                                      | Content checksums for external media, keyed by path, size and mtime. Earlier runs, when this stage also held SG reels and DVDs, recorded them here; the SG and DVD commands seed their own caches from it.                                          |

**Time coordinates.**

- Word and segment `sf`/`ef` are frames on the source's own sample clock.
- For a 30-track channel, canonical post-05b tape seconds equal `canonical_origin_s + sf / sample_rate`. The origin comes from the adjacent `review.json` and already includes the stage-02 capture placement, so add it exactly once.
- GET is not part of this stage.

**Timestamp flags.** Questionable timestamps are kept, never dropped. In an aligned segment (flag `aligned`) Whisper's timing flags are replaced by those of the aligned times, so only `spans_vad_gap`, `long` and `interpolated` remain possible. A segment whose decoder context could not be aligned keeps Whisper's times and flags and gains `align_failed`. Whisper's flags:

- `zero_duration`: a collapsed word.
- `clipped`: an endpoint bounded to its decoder context.
- `spans_vad_gap`: the word's endpoints lie in different retained windows.
- `long`: a word longer than 2.5 s.
- `invalid_time`: a non-finite or inverted time.
- `outside_context`: the word's midpoint falls outside its decoder context.

Segments additionally carry `collapsed_timing`, `high_compression` and `word_spans_vad_gap`. `interpolated` marks a word with nothing to align (punctuation alone), placed between its neighbours.

**Ownership and resume.**

- Each source is cut on a fixed 600 s grid. Each boundary moves to the quietest 200 ms within ±8 s, computed only from the samples in that window, so neighbouring chunks agree on the cut frame. Every frame and word belongs to exactly one chunk.
- A chunk appears only by renaming a complete temporary directory. A stopped job loses at most its in-flight group and never publishes a partial chunk.
- A completed chunk is reused when its settings ID, source identity and nominal range all match. A chunk published before alignment, or by an older aligner, is aligned in place by the next `run`: the new rows and receipt are written aside and renamed into place, and Whisper is not run again.
- A store is found by its transcript-defining settings. Execution-only settings (the batch size) are not part of a new store's identity and need not match an existing one, so `status`, `verify` and `run` find the NARA store (made at batch 8) without `--batch-size`.
- A changed 05b edition, checksum, origin or map gives a new identity directory. The old transcripts stay in place and `status --scan` reports them as superseded.

## Commands

Run from the repository root. CUDA 12.8 and cuDNN 9.10 DLL folders are added automatically when present; pass `--dll-dir` for others. Only one transcription process runs at a time; a lock file enforces this.

```powershell
# Inventory (writes nothing unless --write)
uv run python scripts/run.py 06a_transcribe.py inventory --all-tapes --write

# Estimate the remaining first pass from measured sessions
uv run python scripts/run.py 06a_transcribe.py estimate --all-tapes

# One tape through the pipeline entry point (the validated pilot)
uv run python scripts/pipeline.py run --stages 06a --tape A15_T510093 --plain

# Archive-wide launch: explicit operator command, resumable
uv run python scripts/pipeline.py run --stages 06a --all-tapes --plain

# Monitor from another terminal (coverage, occupancy, rate, failures, ETA)
uv run python scripts/run.py 06a_transcribe.py status --watch 60

# Live dashboard from another terminal: progress, ETA and a scrolling feed of new transcript.
# Read-only; Ctrl+C closes only the watcher. Options: --channel N, --grep REGEX, --min-words N,
# --lines-per-second N (default 6; busier groups are evenly sampled), --no-screen.
uv run python scripts/run.py 06a_transcribe.py watch

# Stop cleanly after the current group; resume by rerunning the same run command
uv run python scripts/run.py 06a_transcribe.py stop

# Time-boxed run, for example overnight
uv run python scripts/run.py 06a_transcribe.py run --all-tapes --max-hours 10

# Aligned-chunk counts and contexts that could not be aligned (reads every receipt)
uv run python scripts/run.py 06a_transcribe.py status --all-tapes --scan

# Verify transcript chunks (add --audio to recompute full source checksums; slow)
uv run python scripts/run.py 06a_transcribe.py verify --all-tapes
```

`--start` and `--end` restrict a run to whole grid chunks inside a window, given in canonical tape seconds or in SG/DVD source seconds. A later full run reuses those chunks. `--channel` limits tape channels.

`--start` and `--end` are canonical tape seconds.

## Space-to-ground reels and DVD streams

These have their own commands with the same subcommands and recognizer defaults (so the settings ID matches). Each writes its own store with its own lock (`timing_transcripts\` and `evidence\` under its folder; set `A15_PROCESSING_ROOT` to move them), so it can run alongside the 30-track run:

| Command                                                              | Sources                                                                                                                                                                                                                                                              | Store                               | Time coordinates                                                                                |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| `scripts/sg-audio/sg_transcribe.py --reel 572-AAA` (or `--reel all`) | The 44 Apollo 15 SG reels, read from the local copy `F:\Apollo_15_data\raw\audio\Internet Archive` (`--sg-root`, or `A15_SG_ROOT`). The masters stay in `N:\Projects\Apollo\Mission Data\Apollo 15\Audio\Internet Archive`; `550-AAG`, Apollo 11, is excluded | `F:\Apollo_15_data\processing\sg\`  | Reel seconds from the first sample                                                              |
| `scripts/video/dvds/dvd_transcribe.py --dvd 'D04-T02-P0[12]'`        | Each audio stream of the Spacecraft Films MP4s                                                                                                                                                                                                                       | `F:\Apollo_15_data\processing\dvd\` | Program PTS = `pts_origin_s + sf / sample_rate`, counting decoded samples from the stream start |

While the local copy is being made, `sg_transcribe.py follow --prep-workers 4` transcribes each reel as soon as its copy is complete: same size and modification time as the master, and no longer held open by the copy. It waits for the rest, passes other `run` options through, and ends when all reels are copied and transcribed or after `stop`.

Before this split, stage 06a held these as collections `pao` (the SG reels, then misnamed "PAO reels") and `dvd`. Those records (`sources/pao/572-AAA` and two DVD streams under the 30-track transcript root) are left unchanged. Collection and key are part of a source identity, so the new commands transcribe those sources again rather than reuse them.

## Measured on T510093 (November 22)

T510093 contains EVA 2 and is among the busiest tapes: 30 channels and 517.50 channel-hours, all transcribed with 0 failures. Every chunk checksum verifies.

| Measure                                                    |                                                                                                 Value |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------: |
| Speech occupancy (retained by the gain-assisted gate)      |                                                                                                52.0 % |
| Total wall time over three sessions, including model loads |                                                                                     4,907 s, **380×** |
| Per-group range                                            |                                                                226× at 60 % occupancy to 841× at 14 % |
| Fitted cost                                                |                                                    0.59 s per source-hour + 17.30 s per retained hour |
| Words / segments                                           |                                                                                   2,271,140 / 321,752 |
| Word flags                                                 | `zero_duration` 19.2 %, `clipped` 1.3 %, `spans_vad_gap` 0.6 %, `long` 0.4 %, `outside_context` 0.2 % |
| Segments flagged `collapsed_timing`                        |                                                                                                17.9 % |

The pipeline overlaps CPU preparation with GPU decoding. Per-source cost fell from the pilot's 2.47 s per hour to 0.59 s per hour, and the GPU stays at 100 % utilisation. Throughput is now set by speech density.

**Projection.** `estimate --all-tapes` found 18,674.91 published 05b channel-hours (1,139 channels). Stage 05b is still publishing; the original archive holds 19,209 channel-hours. That leaves 18,157 hours remaining:

| Archive-average occupancy              | Rate | Remaining first pass |
| -------------------------------------- | ---: | -------------------: |
| 52 % (every tape as busy as T510093)   | 376× |               48.4 h |
| 33.8 % (pilot three-tape census)       | 559× |               32.5 h |
| 68.3 % (pilot sustained busy interval) | 290× |               62.6 h |

The pilot census covered T510077, T510093 and T510100, with occupancies of 34 %, 57 % and 10 %. Most tapes are therefore quieter than T510093, and about 35–45 h is the likely range. The ETA in `status` updates from the archive run's own groups.

**Interruption and resume, on real data.**

- `stop` finished the current group and exited cleanly.
- A forced `taskkill /F` of the worker left a stale lock and no partial chunk. The lock refused a concurrent second run while the worker was still alive, and the next run reclaimed the lock once the holder was dead.
- The resumed run picked up exactly the 2,460 unpublished chunks (3,120 − 660).

## Word times: forced alignment

Whisper's word times come from its cross-attention and are unreliable here. A collapsed segment puts many words at one instant, and openings can be tens of seconds off. `scripts/00_common/mission_align.py` places the recognized words with a character CTC model, as WhisperX does: torchaudio `WAV2VEC2_ASR_BASE_960H`, torch 2.8.0 + cu128 in the project environment.

- **Unit.** One decoder context: its retained windows concatenated as Whisper saw them, and the text of every segment Whisper produced from it, in decoding order. A collapsed segment's own times are not used.
- **Spelling.** Upper-case letters and apostrophes. Numbers of up to two digits become cardinals ("P20" becomes P TWENTY). Longer numbers are read digit by digit ("213" becomes TWO ONE THREE), and "." inside a number becomes POINT.
- **Path.** Frame scores come from the GPU; the Viterbi path (`torchaudio.functional.forced_align`) runs on CPU threads. A frame maps back to its source sample through its window.
- **Output.** `sf`/`ef` are aligned; `wsf`/`wef` keep Whisper's times. `ap` is the mean probability of the word's character frames. On this narrowband audio a low `ap` does not mean a misplaced word: the pilot found words at 0.005–0.02 within 0.16 s of the visual pick. It is therefore not a flag.

**Pilot (November 25, `research/mission_timing/align_pilot.py`, `archive/2026-11-26_reorg/runs/align_pilot/report.json`).** Onset error of the same words, Whisper's times against aligned times, on visually picked call openings:

| Set                                  | Whisper median / p90 / worst | Aligned median / p90 / worst | Aligned within 0.25 s |
| ------------------------------------ | ---------------------------- | ---------------------------- | --------------------- |
| PAO 572-AAA, 30 reviewed openings    | 0.165 / 21.4 / 27.9 s        | 0.076 / 0.228 / 0.303 s      | 28 of 30              |
| Spur on PAO 572-AAA, 4 matched       | 2.04 / 10.2 / 12.9 s         | 0.062 / 0.142 / 0.164 s      | 4 of 4                |
| Spur on T510092 CH23 (05b), 6 matched | 5.00 / 13.6 / 14.5 s         | 0.096 / 3.25 / 6.34 s        | 5 of 6                |

The CH23 miss is a single word. "We're" was pulled 6 s early into speech Whisper did not transcribe, while the next word, "talking", fell 0.05 s from the pick. No context of 572-AAA or T510092 CH23 failed to align. Speed on the RTX 4090, including reading the audio: 3,273× real time on the NARA store, 1,567× on the SG reels (24-bit reads dominate) and about 3,400× on the 30-track.

**Archive alignment (November 25).** Every published chunk was aligned in place with no Whisper decoding:

| Store                         |  Chunks | Channel-hours | Contexts | Not aligned | Words aligned / interpolated | Timestamp flags before → after | Wall    |
| ----------------------------- | ------: | ------------: | -------: | ----------: | ---------------------------- | ------------------------------ | ------: |
| NARA respeeded tapes          |     623 |         102.4 |    2,842 |           2 | 134,971 / 55                 | 28,895 → 1,741                 | 113 s   |
| SG reels                      |     810 |         131.8 |    5,226 |           0 | 248,412 words in all         | 66,580 → 3,167                 | ≈ 330 s |
| 30-track (stage 05b channels) | 112,642 |      18,674.9 | 1,007,525 |        903 | 51,533,274 / 25,976          | → 542,730 (1.05 % of words)    | 5.4 h   |

An earlier "rescue" pass re-decoded flagged spans from continuous audio. It is withdrawn and its layers and session records are deleted. Re-decoding could not fix the timing: after it, about 18,700 words on the NARA tapes still had zero duration.
