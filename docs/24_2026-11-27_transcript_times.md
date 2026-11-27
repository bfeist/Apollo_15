# Transcript times against IRIG-B time code

**Status (2026-11-27):** implemented. 07b writes the check for every retimed channel
(`transcript_times.py`, `transcript-times-v1`). It was run before 07b on the five time-coded
30-track tapes (`preview`) and on the 16 time-coded NARA flight tapes
(`research/event_check/nara_transcript_times.py`).

## The question

Are the GETs printed in the 1971 transcripts (TEC, PAO) and the journals (AFJ, ALSJ) correct?
The audio placed on GET by IRIG-B time code answers this independently of the transcripts.

## Method

- **Find each utterance.** Every timed utterance is looked for in the speech recognition of the
  audio.
  - Its first 4 comparable words must be heard as one run within 4 s.
  - The run must occur once within ±90 s of the transcript GET.
  - At least half of the next 12 words must follow it.
- **Measure.** `d = heard GET of the first word − transcript GET`.
  - Stamps are whole seconds, so a correct stamp that was truncated gives 0 ≤ d < 1.
  - The recognizer's onset error adds a few tenths of a second.
- **Which matches count.** Only words timed from the time code are independent (`timing = timecode`).
  - 07a plans are fitted to these same transcripts, so their matches are labelled `sg_fit`. They
    are kept out of the independent statistics.
  - Journal rows with `inherited` times and PAO-edition header times have no stamp of their own.
    They are reported separately.
- **Where the heard GET comes from.**
  - 30-track: the 07i plan, or the time-code curve inside its coverage (the two agree to 1 ms on
    T510075), with the 07b head-row shift.
  - NARA: the respeed map, less the voice-row lag of `catalog/nara_voice`.

## Results

| Audio | GET covered | Matches (TEC/AFJ) | Median d | MAD | Within −1..+2 s |
|---|---|---:|---:|---:|---:|
| 30-track T510075, T510076 | 4:30–17:44 | 175/177, 167/168 | +0.24, +0.27 s | 0.35–0.40 s | 86–92 % |
| 30-track T510077, T510078 | 24:11–37:32 | 399/416, 339/357 | +0.33 s | 0.31 s | 94 % |
| 30-track T510085 | 81:11–97:23 | 245/271 | +0.37 s | 0.37 s | 91 % |
| NARA 608, 640–655 | −2:22–3:56, 190–268 h | 506/560 | +0.35 s | 0.35 s | 91 % |

- **The stamps are correct to their whole second.** This holds across launch, translunar coast,
  lunar orbit and transearth coast.
- **The AFJ stamps are the TEC's.** The median difference is 0 in every chapter, so the AFJ is not
  an independent witness. It differs from the TEC only where an editor changed a time.
- **The voice-row correction is confirmed.** Without it, the NARA tapes on the later row show
  +0.98 s instead of +0.35 s.
- **Not tested:**
  - The ALSJ. Its surface period (104–171 h) is on no time-coded tape.
  - Individual PAO rows. Their times are segment headers.

### Wrong stamps

These were measured where the time code is dense. The same error on two tapes (T510075 and
T510076, or T510077 and T510078) rules out a matching error.

| GET (printed) | Error | Where |
|---|---|---|
| 3:25–3:39 | TEC/AFJ 2–8 s early; 3:36:12 and 3:36:28 heard 62 s later | NARA 608 (TV during TD&E) |
| 4:32:06–4:32:20 | TEC 1 min early, AFJ (4:34:xx) 1 min late; heard 4:33:05–4:33:18 | T510075, T510076 |
| 4:38:44–4:39:31 | TEC/AFJ 1 min late | T510075, T510076 |
| 7:35:28, 11:40:44, 12:18:29 | 1 min early | T510075, T510076 |
| 7:41–7:53 | 4–5 s late | T510075, T510076 |
| 27:28–27:53 | 3.5–7.6 s late | T510077, T510078 |
| 83:23:56–83:24:08 | TEC 1 min late | T510085 |
| 238:52:52 | 61 s late | NARA 648 |
| 242:09–242:18 | 4–5 s early (Worden EVA) | NARA 648 |

Short generic calls ("15, this is Houston.") off by tens of seconds are probably other
occurrences of the same words. Listen before correcting them. Each check's `outliers` list has
the full set.

### NARA 646, 227–229 h

The transcripts fit 646 with no voice-row lag there. The catalog's 0.683 s puts them about 0.7 s
early, so the catalog's 0 s interval may need to run past 226.0 h. This contradicts the 30-track
continuity evidence recorded there, so the catalog is unchanged.

## Artifacts

- **Per retimed channel (07b):** `transcript_times.tsv` and `.json` beside `timed_transcript.tsv`.
  - They are written after the timed transcript, both when it is written and when it is reused.
  - They are bound to its TSV checksum, the retime record, the scraped mission files and the code.
  - When the journals are re-scraped, the earlier pair is kept as
    `transcript_times.superseded-<digest>.*`.
  - Without `.local/mission_data` (for example on Genus), the check is skipped and the render
    still completes.
- **Per tape:** `D:/…/stage_evidence/retiming/transcript_times/<tape>/` holds `retimed-<digest>/`
  after 07b (written by the coordinator for every done tape) and `preview-<digest>/` before it.
  - `events.tsv`: one row per utterance, with the median over the channels that heard it.
  - `summary.json`: statistics, shifted stretches, outliers and per-channel medians. A channel more
    than 0.5 s from the tape's utterance medians is listed in `channels_off_tape_median`. A head-row
    error shows as about 1.38 s.

```
uv run python scripts/run.py transcript_times.py preview --timecode-tapes   # before 07b
uv run python scripts/run.py transcript_times.py tape --tape A15_T510075    # after 07b (also automatic)
uv run python research/event_check/nara_transcript_times.py                 # NARA flight tapes
```
