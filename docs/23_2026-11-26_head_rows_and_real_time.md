# Head rows and real time: which copy of a 30-track sound is on time

**Status (2026-11-26):** decided and implemented. 07a `sg-retime-v3`, 07i plans `irig-timing-v2`,
07b head-row shift (`head_rows.py`, `head-rows-v1`), staged archive I/O (`staged_io.py`), stage 10
`channelid-shared-v2`, NARA voice table `catalog/nara_voice/nara_voice_offsets.json`
(`nara-voice-v1`). The README summary is §10.5.

## The question

Every 30-track tape shows two stagger groups of channels. In the delivered capture, the same
sound reaches the channels of one group **1.36–1.39 s** after the other group. Every tape plan
(07i, 07a) is one curve per tape, so it can place only one group exactly on mission time. The
question was which group is the real time, and what to do with the other.

**Answer.**
- The **earlier** row is on real time.
- The **later** row is late by its measured stagger. 07b moves each of its channels earlier by that
  stagger, through the same tape map.

## Physical model

The 1971 recorders wrote the 30 tracks with two staggered head stacks: odd true tracks on one,
even true tracks on the other. The digitizing playback read all tracks in line, so the content of
the two stacks comes out about 1.38 s apart. The measurements bear this out:

- **Track parity.** In the 07a stagger tables, channels on the earlier row read odd true tracks
  and channels on the later row read even ones. This holds for 29 of the 34 channels whose stage-10
  identity the language model confirmed independently (T510073, T510075, T510078). The 5
  exceptions sit where the stage-10 map placed a skipped track wrongly.
- **Time-code row.** The IRIG-B time code is true CH01, an odd track. It shares the earlier row's
  stack with true CH03. On T510075 its bits leak into true CH03 at zero lag, at 19× the noise
  floor. That is stronger than the leak into true CH02 (15×), its neighbour on the tape. A copy at
  zero lag between stack-mates is what in-stack crosstalk at recording produces.
- **Head slides.** A delivered channel can move to the next track partway through a reel, as on
  T510075 CH19 (later row, then earlier row from about 5.5 h) and CH20. Where the playback head
  straddles two tracks, the channel carries both copies (`mixed` intervals).

## How we got there

1. **Documented events (NASA SP-4029, *Apollo by the Numbers*; copy at `F:/Apollo_15_data/raw/documents/sp-4029.pdf`).**
   - *Midcourse correction burn* (GET 28:40:22.00, on time-coded T510077/T510078):
     - CAPCOM's cue "okay guys, go ahead" comes 6.6 s (earlier row) or 5.2 s (later row) before
       ignition, measured against the tape's own time code.
     - The controller's "we got a burn, flight" comes 6.2 s or 7.6 s after it.
     - Both rows are physically possible, so the burn does not decide.
   - *Lunar-orbit burns on T510085* (DOI GET 82:39:49.09 and later): these were behind the Moon,
     with no live callouts.
2. **Launch countdown on NARA 608** (time-coded) against the documented range zero, 13:34:00 GMT:
   - Every number is heard 0.96 s after its tick (±0.14 s).
   - Also heard: "all engines running" at +0.88 s, "launch commit" +2.07 s, "lift off" +3.25 s.
   - A NARA voice late by its 0.683 s row gap leaves the announcer 0.28 s behind the clock,
     which is believable.
3. **Withdrawn result.** Tying T510073 (speech-matched to NARA 608) to time-coded T510075 through
   their overlap first suggested the later row. It needs T510073's clock 40 minutes past the end
   of NARA 608. Extending it with a constant or a changing rate gives clocks 0.26 s or 1.27 s
   apart, enough to flip the answer, so this link is not used.
4. **Parity and leakage** (above) put the time code on the earlier row. The earlier row is
   therefore real time.
5. **Check.** After 07b, words heard on T510075 CH28 (earlier row) and CH29 (later row) agree to
   −0.001 s (MAD 0.010 s, 25,951 shared sequences). Before 07b they were +1.359 s apart.

## NARA voice rows (`catalog/nara_voice`)

The NARA flight tapes have the same geometry, at twice the tape speed: a row gap of **0.683 s**.
The respeeded mix places its voice at the GET of its time code. Where the voice comes from a track
on the other row, it is 0.683 s late.

How each tape's value was measured:

- **File splits.** Within one tape, files placed by time code and by programme differ by the
  row gap: 608 −679.8 ms, 644 −684.2, 646 +683.7, 650 −683.3, 652 −684.3.
- **Overlap steps between consecutive NARA tapes:** 641−640 +0.678 s; 646−645 −0.680 s (in the
  first hour of 646 only); 648→652 within 0.005 s.
- **Internet Archive reels.**
  - Against the same IA segment (583-AAA:0), 640 and 641 differ by 0.63 s.
  - After the correction, IA minus NARA is −0.45 s (608), −0.50 s (640) and −0.52 s (641).
- **30-track continuity.** A 30-track reel runs across several NARA tapes, and its residuals per
  NARA tape show which tapes agree: T510100–T510110.
- **Result.**
  - 0.683 s on 608 and 641–654.
  - 0 on 640, and on 646 for its first hour (225–226 h).
  - 655 unknown: it is fitted as a free offset (prior σ 1 s).

A switch inside a tape shows up in the fit residuals:

```
uv run python research/event_check/nara_voice_check.py <sg_fit edition>
```

## Internet Archive reels

IA GET minus true GET is consistently negative:
- −0.44 s (608), −0.50 s (640) and −0.52 s (641) against the row-corrected NARA voice;
- −0.36 s and −0.28 s against the T510075/T510078 time code.

The IA segment offsets `b_k` are now fitted with prior mean **−0.42 s**
(`sg_inputs.IA_OFFSET_PRIOR_MEAN_S`) and their own held-out σ. Before this they had prior mean 0.

### Level gaps (`sg_levels.py`, `level-gaps-v1`)

Where a tape has no time code, its GET comes from the journals: the IA drift maps are fitted to
TEC/journal anchors. Across a long stretch without anchors, the curve's rate is only weakly
constrained, and 0.03 % over six hours is 6.5 s. The first full v3 fit (`fit-fee097094a89eb4c`)
carried that drift across the gaps into the IA offsets. On the IA-only tapes T510079–T510097
they reached −7.9 to +6.5 s, although each segment follows its own IA audio within 0.03–0.08 s.

Rule of the joint fit (07a `fit`):

- **Gap.** Consecutive position anchors more than 1 h apart, with no recorder cut between them.
- **Free level.** If the island on either side has fewer than 20 NARA or time-code positions, the
  two sides get separate GET levels. The IA-sided island is then placed by its own IA segments:
  their journal placement plus the IA offset prior. Gaps between two time-coded islands keep the
  continuous fit.
- **Continuous map.** The level difference is spread over the whole gap as a smoothstep in the
  rate. The timing map stays continuous and monotone, and `GET = output + segment constant` still
  holds. `curve.json` lists each ramp (`level_ramps`: gap, step, `max_rate_added`). In the first
  run the ramps added at most 0.09 % to the rate.
- **Smoothing unchanged.** λ is chosen per tape exactly as before, without the free levels. Only
  the final joint solve changes, and it is solved on GET minus a unit-rate line. Without that
  conditioning, GET-sized values (6e5 s) lose precision at large λ.
- **Upstream reused.** The reference, anchors, audio and Quindar editions are unchanged. Only the
  fit edition is new; its tape evidence records `level_gaps`.

Result against `fit-fee097094a89eb4c`:

- NARA and time-coded tapes are unchanged: the same λ, and NARA residuals within 0.005 s (p90).
- The 569–575:0 run (T510092–T510095) moves from −5.8..−1.7 s to −2.9..+2.0 s.
- The 579:1–581 run (T510097) moves from +0.3..+6.5 s to −1.5..+3.9 s.
- **Still open: the 561:2–566 block.** It stays at −6.7..+7.5 s. It is joined on continuous tape
  (gaps under 1 h). The 562 → 563 join on T510088/T510089 needs a 12.5 s step: either the 563
  journal map is off (held-out error 2.3 s, five unapplied residual steps) or both recorders
  stopped there. The 564 → 565 join needs −4.2 s.

## Channel maps (stage 10, `channelid-shared-v2`)

- **Parity evidence.** A measured row fixes the parity of the true track: −6 nats for the wrong
  parity, halved for a channel whose row changes along the tape (`channelid_rows.py`).
- **Carrier rule.** A channel with no 1 kHz carrier cannot be true CH01. True CH01 may then be
  skipped like any other track.
- **Per-tape skips.** Skipped tracks follow each reel's lateral position on the head, so they are
  now fitted per tape (`--grouping tape`, the default). One shared pattern per head stack
  contradicted the measured rows on every reel with anchors.
- **Results.**
  - T510073 CH30 reads no track: it has no carrier (−2.0 dB) and only bleed, so true CH01 was not
    digitized.
  - The same holds for T510086–T510090: weak lines of 5–11 dB, no frames.
  - T510075 reads true CH15 on CH17. Its skipped track is 12, at CH19/CH20.
  - T510078 reads true CH08 on CH23.
- **Agreement.** 17 of 303 measured channels still disagree with their map. They are where
  heads straddle or slide.
- **Outputs.** The maps are written to `channel_id/shared-v2/`. The v1 maps stay in `shared/`.
  Rerun `10_channelid.py shared` after the 07a anchors exist for every tape.

## Implementation

| Stage | Change |
| ----- | ------ |
| 07a `fit` (`sg_levels.py`) | A separate GET level for each anchor island without time code, beside a gap of more than 1 h; the step is ramped across the gap. The joint solve is conditioned (GET minus a unit-rate line). See [Level gaps](#level-gaps-sg_levelspy-level-gaps-v1). |
| 07a `sg_stage` v3 | Staggers are re-referenced to the earlier row (`rereference_to_real_time`). The curve and every plan define GET for that row. NARA segments are corrected by the voice table. A NARA tape of unknown voice row gets a free offset. IA offsets get prior mean −0.42 s. Time-coded tapes add their time code to the joint fit as fixed positions. `--scratch` stages each tape's WAVs for `refine`/`quindar`. |
| 07i `irig-timing-v2` | The time code is on the real-time row, so the plan places GET from it with no offset. The SG offset against it is reported as `timecode_minus_sg_events` (the IA offset). Decodes are unchanged (v1). |
| 07b | `head_rows.row_plan` gives each channel's row per 30-minute interval, frozen as `retimed/<tape>/head_rows.json`. It applies the shift through the tape map (`quindar_tape.target_map(row_shift=...)`). The timed transcript chooses each word's GET segment from its real-time position. `--no-row-shift` is for comparisons only. |
| 07b `--scratch` | `staged_io.run_staged` (one tape) and `run_stream` (the queue: tapes share one render pool): only the coordinator touches the archive disk, one verified sequential copy at a time, under `DiskLock`, a lock file on D: shared by Proprius and Genus. Workers render from local copies. |

How the row plan decides each channel and interval, in order:

1. **measured:** the stagger measured in that interval (07a anchors).
2. **held:** otherwise the nearest measured interval of the same channel.
   - A change of row is ramped over 600 s, half-way between the runs. That is at most a 0.23 %
     speed change, under the tapes' flutter.
   - A single interval against runs of the other row on both sides is treated as an outlier.
3. **timecode:** the decoded time-code channel is on the earlier row.
4. **neighbours:** a channel with no measurement at all, between two channels on the same row,
   is on the other row.
5. **channel_map:** otherwise the parity of its stage-10 v2 true track.
6. **unknown:** otherwise no shift, and the channel is flagged.

Mixed intervals keep the held row and are flagged.

## Running 07b

Each channel renders in about 400 s on one core. A tape is 30 channels and about 60 GB in and 60 GB
out. With `--scratch`, D: sees about 3–4 s of sequential copy per file in each direction.

- **Proprius:** scratch on F: (`F:/Apollo_15_30-Track/work/retime`), `--workers 4`–`6`.
- **Genus:** scratch on its local SSD (`/home/bfeist/.local/share/apollo15-retime`), with D: at
  `/mnt/d`, `A15_TAPE_ROOT` unset. The host-native paths in its `retime.json` start `/mnt/d`.
- **Both at once:** run them on disjoint tape lists. `DiskLock` keeps their archive copies to one
  at a time.

## Open

- **655-AAA voice row.** Review the free offset after the fit.
- **NARA voice-row switches inside a tape.** Check `nara_voice_check.py` after each fit.
- **Remaining map conflicts.** The 17 channels at straddles and slides can't carry one static
  true track per channel.
- **T510111.** It has no SG curve (no kept pairs).

## Archive run

Run the steps in order. Every step writes new editions named by their inputs, reuses completed
work, and is resumable. Proprius commands run in PowerShell from `F:\_repos\Apollo_15`.

1. **Decode the weak-carrier tapes on 05b** (Proprius, one channel at a time):
   ```powershell
   uv run python scripts/run.py 07i_irig_retime.py decode --tape A15_T510086 --tape A15_T510087 --tape A15_T510088 --tape A15_T510089 --tape A15_T510090 --workers 1
   ```
2. **Build the SG reference and the anchors of every tape** (Proprius; v3, transcripts only):
   ```powershell
   uv run python scripts/run.py 07a_sg_retime.py anchors --all-tapes
   ```
3. **Build the stage-10 v2 maps** from the measured rows (Proprius; no GLM calls). Review
   `D:/Apollo_15_30-Track/channel_id/shared-v2/CHANNEL_MAP.md`:
   ```powershell
   uv run python scripts/30-track/10_channelid/10_channelid.py shared
   ```
4. **Audio refinement, Quindar and the joint fit** (Proprius; audio staged on F:, one tape at a time):
   ```powershell
   uv run python scripts/run.py 07a_sg_retime.py run --all-tapes --scratch F:/Apollo_15_30-Track/work/sg_audio --workers 8
   ```
   If steps 2–4 are already complete and only the fit code changed, refit alone (about a minute;
   no audio is read):
   ```powershell
   uv run python scripts/run.py 07a_sg_retime.py fit --all-tapes
   ```
5. **Review the fit.** The script finds the newest complete fit edition itself:
   ```powershell
   uv run python research/event_check/fit_review.py
   ```
   - Exit 0 and "All checks pass" means: continue to step 6.
   - Otherwise it lists each decision:
     - NARA bins off by a voice-row step, with the NARA tape and a candidate interval for
       `catalog/nara_voice/nara_voice_offsets.json`. Then rerun from step 2.
       - **Not a row switch:** a bin where only scattered text matches move (MAD near 1 s) and the
         audio anchors stay near 0. Examples are 217:00 (644-AAA) and 251:00 (650-AAA). A real
         switch moves both by about 0.68 s, with a tight spread.
     - A 655-AAA free offset that is neither 0 nor 0.683 s.
     - IA offsets more than 0.35 s from −0.42 s.
6. **Refit the time-code plans** (v2, hybrid with the v3 anchors):
   ```powershell
   uv run python scripts/run.py 07i_irig_retime.py fit --all-tapes
   ```
7. **Check which plan each tape gets.** Every tape should show 07i `irig-timing-v2` or 07a
   `sg-retime-v3` and v3 anchors. 07b refuses a tape without them.
   ```powershell
   uv run python research/event_check/plan_inventory.py
   ```
8. **Render 07b on Proprius and Genus with one command** (run on Proprius):
   ```powershell
   uv run python scripts/run.py 07b_retime.py --all-tapes --all-channels --workers 8 --genus-workers 12
   ```
   - The coordinator starts a Genus worker over SSH (`bfeist@192.168.0.4`, the 2.5 Gbit/s link) and
     runs one itself. Both take whole tapes from a queue on the archive disk
     (`retimed/.queue/`, `retime_cluster.py`), so the faster host renders more tapes. Genus lines
     are printed with a `[genus]` prefix.
   - Each host keeps its render workers busy across tape boundaries. Once the current tape has no
     channel left to stage, the host claims the next tape and stages its channels into the workers
     that free up (`staged_io.run_stream`). A tape is marked done when its own channels are
     published. Scratch still holds at most `workers + 1` inputs. A worker process that dies
     fails only its channel; the host starts a new pool and continues.
   - Scratch defaults: `F:/Apollo_15_30-Track/work/retime` on Proprius,
     `/home/bfeist/.local/share/apollo15-retime` on Genus. Every archive copy holds `DiskLock`.
   - `--hosts local` renders on Proprius only. `--tapes A,B,...` limits the set.
   - **Dashboard.** In a terminal, the command shows a live dashboard instead of log lines. It shows
     overall progress and ETA, each machine's render workers and rate, the tapes in progress, and
     every channel in flight with its phase and render percentage. The last log lines are shown
     too. The full log is in `retimed/.logs/07b-<host>-<time>.log`; `--plain` prints the log
     instead. From another terminal on either host (Ctrl-C closes only the view):
     ```powershell
     uv run python scripts/run.py retime_dashboard.py
     ```
     Each host rewrites `retimed/.queue/hosts/<host>.json` every 2 s. Rates come from the last
     15 minutes of progress, so ETAs warm up after a few minutes.
   - **Stopping.** If Ctrl-C on the coordinator's console does not respond, run this from any terminal
     on either host. Both hosts check `retimed/.queue/stop` every 2 s and stop exactly as on Ctrl-C;
     the next run clears it.
     ```powershell
     uv run python scripts/run.py 07b_retime.py --stop
     ```
   - **Ctrl-C stops both hosts at once.** This host's render workers are killed instead of
     finishing their channels. The SSH session to Genus is closed, and the Genus worker stops the
     same way on SIGHUP. Each host releases the claims of its unfinished tapes. Running the command
     again resumes: done tapes and completed channels are skipped, and tapes that failed are
     retried. A tape whose worker died without releasing it is free again 5 minutes after its last
     heartbeat.
   - **Genus network.** Genus mounts the Proprius shares with `srcaddr=192.168.0.4` in
     `/etc/fstab`, so archive copies use the 2.5 Gbit/s link (`enp39s0`), not Wi-Fi. Check with
     `ss -tn dst 192.168.0.2:445` on Genus: the local address must be 192.168.0.4.
   - The summary at the end lists failed tapes with their reasons. Tapes without a v2/v3 plan or
     v3 anchors fail by design (T510111 has no SG curve).

9. **Check the result.**
   - An earlier-row channel and a later-row channel of the same tape should agree to about 0 s
     (as T510075 CH28/CH29: −0.001 s):
     ```powershell
     uv run python research/event_check/retimed_alignment.py D:/Apollo_15_30-Track/retimed/<tape>/CHaa D:/Apollo_15_30-Track/retimed/<tape>/CHbb
     ```
   - Each tape's `head_rows.json` lists the row of every channel and interval, with flags.
