# Stage 07i: retiming the time-code tapes from IRIG-B

**Status (2026-11-26):** implemented; plans built for the five time-code tapes. 07b applies them by
default. 07a stays available for these tapes and is the only timing method for every other tape.
**Superseded placement (v2, [docs/23](23_2026-11-26_head_rows_and_real_time.md)):** the time code is on
the earlier head row, which is real time, so v2 plans (`irig-timing-v2`) place GET from it directly.
The "voice offset" below (time-code track minus voice reference group, +0.28 to +0.36 s) was the
Internet Archive reel offset. v1 plans are kept but 07b no longer applies them.

## What was found

Stage 10 (channel identification) showed that the delivered channel numbers run opposite to the
track sheet and that true CH01, the IRIG-B time-code track, lands on delivered CH30 on some reels.
A full-length decode of delivered CH28-CH30 on every tape (`10_channelid/channelid_irig_nara.py`,
NARA decoder) found time code on five tapes, all on delivered CH30, and on no other channel:

| Tape | Head | Strict frames, original | Strict frames, 05b | Frame residual of the 07i map | Tape time timed from time code |
|---|---|---:|---:|---:|---:|
| T510075 | HR1L | 53,794 | 56,399 | 0.67 ms rms | 99 % |
| T510076 | HR2L | 38,278 | 52,492 | 2.03 ms rms | 94 % |
| T510077 | HR2U | 17,085 | 54,668 | 1.36 ms rms | 100 % |
| T510078 | HR1U | 8,486 | 46,952 | 2.51 ms rms | 90 % |
| T510085 | HR1U | 7,525 | 43,324 | 1.43 ms rms | 99 % |

On the other tapes true CH01 falls past the last delivered channel (stage-10 maps), so it was not
digitized. The 1 kHz carrier survives stages 03-05b, and deflutter makes it much easier to decode,
so 07i decodes the 05b output, which is already on the canonical tape clock.

**No IRIG deflutter.** Speed measured from the carrier's instantaneous frequency matches the blind
stage-05 map at correlation 0.89-0.99 in 60 s windows across all five reels. The difference integrates
to 1-7 ms over a minute. The time code adds absolute rate and placement, not better flutter.

## Method (`scripts/30-track/07_retime/irig_timing.py`)

1. **Decode** each candidate 05b channel (default CH28-CH30) in full: `irig/<tape>/decode/CHnn.json`,
   bound to the WAV identity, its review checksum and its canonical origin. Frame times are canonical
   tape seconds.
2. **Time-code curve.** The channel with the most strict frames (at least 1,000) is the time-code
   track. These reels play 1.2-3.5 % slow, and their speed changes along the reel (late-reel
   slow-downs by 1 % within minutes). So a rate trend is removed first: medians of consecutive-frame
   slopes in 60 s bins, a 5-bin running median, and 300 s bins where frames are sparse. Its integral
   is exact (piecewise quadratic). Then the NARA map logic (`nara_tape_map`) applies unchanged: chains
   of consistent frames, recorder-stop segments, and a 10 s binned smoothing spline (λ = 1000). An
   extra segment with fewer than 300 frames or a fit worse than 50 ms rms is a run of misreads where
   the time code fades, not a recorder stop. Its frames are rejected.
3. **Coverage.** Inside a segment, time code with frames no more than 60 s apart defines the curve.
4. **Everywhere else (hybrid).** If a complete 07a anchors edition exists for the tape:
   - The SG text, audio and Quindar evidence is fitted with `sg_curve` together with time-code
     positions every 30 s (σ 5 ms).
   - Across a gap inside a segment, that curve gets a cubic Hermite correction, so value and rate meet
     the time code at both edges exactly.
   - Before the first and after the last frame, the rate correction decays over 300 s.

   Without SG evidence, gaps are bridged by a cubic Hermite in GET and the rate is held beyond the
   ends, as on the NARA tapes.
5. **Voice placement.** The time-code track is not at the voice channels' along-tape position (stagger
   geometry, as on the NARA tapes, where time code and programme are 0.683 s apart). The offset is
   measured directly where the time code is the truth: for each SG reference segment, the median of
   time-code GET minus SG event GET. NARA (IRIG-referenced) segments decide when they have 50 events.
   Otherwise the median of the IA segment medians decides, and their spread is the uncertainty.
   Time-code positions enter the hybrid fit shifted by this offset, so the fit also measures the IA
   reel offsets against the time code (`evidence.hybrid.ia_offsets_s`).

The plan has the 07a layout (`quindar_tape.load_shared_plan` / `target_map`): `plan.json`, `curve.json`,
`timing_map.npz` (input, output, rate = dGET/dt). One inverse map serves every channel of the tape.
`get_placement.segments` give `GET = output + get_equals_output_plus_s` for the voice reference stagger
group, and `timecode_track_get_equals_output_plus_s` for the time-code track. Without SG evidence the
placement is the time-code track's, and the plan says so in `review_flags.timecode`.

### Measured

- **End-to-end (T510075).** 07b retimed CH30 with the 07i plan. The time code decoded from the
  *retimed* output lands at output + placement:
  - 56,406 strict frames;
  - residual median +0.00 ms, rms 0.67 ms, p99 1.80 ms;
  - rate error 0.00 ppm in every hour. The input ran about 16,000 ppm slow.
- **Voice offset.**
  - Time-code track minus voice reference group: +0.36 s ± 0.05 (T510075), +0.28 s ± 0.21 (T510078).
  - Per-IA-segment medians span −0.19 to +0.79 s. That spread is the IA reel offset error, which these
    early-mission tapes cannot separate from the voice offset. No NARA SG reel overlaps them.
- **SG text method against the time code.**
  - Median |error| 0.17 s (T510075), 0.22 s (T510078).
  - p90 1.47 s and 0.90 s.

## Commands

```
uv run python scripts/run.py 07i_irig_retime.py decode --all-tapes      # 05b CH28-CH30 of every tape (~2 min/channel, 6 workers)
uv run python scripts/run.py 07a_sg_retime.py anchors --tape TAPE       # optional, enables the hybrid
uv run python scripts/run.py 07i_irig_retime.py fit --all-tapes         # plans where time code exists
uv run python scripts/run.py 07i_irig_retime.py status --all-tapes
uv run python scripts/run.py 07b_retime.py --tape TAPE --all-channels   # applies the 07i plan when one exists
```

07b order: `--quindar-plan`, else `--irig-fit` or the newest complete 07i plan, else `--sg-fit` or the
newest final 07a plan, else the Quindar plan. `--no-irig` skips 07i plans. Artifacts:
`D:/Apollo_15_30-Track/stage_evidence/retiming/irig/<tape>/` (`decode/`, `fit-<digest>/`). Editions are
named by the digest of their inputs (decodes, channel reviews, anchors edition, settings, code) and
written once.

## Open

- **Hybrid on three tapes.** T510076, T510077 and T510085 have no 07a anchors edition yet. Their plans
  are time-code-only and placed on the time-code track. Build anchors, refit, and the voice offset is
  measured.
- **Whether the voice offset is a fixed geometry constant.** 0.28-0.36 s here, against 0.683 s on the
  NARA tapes. It could be pinned down on a tape that overlaps a NARA SG reel; none of the five does.
- **`decode --all-tapes` on 05b.** Not yet run. The full-length decode of the originals found no
  time code on CH28-CH30 of the other 34 tapes.
