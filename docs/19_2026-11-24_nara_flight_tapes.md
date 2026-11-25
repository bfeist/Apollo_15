# NARA flight tapes: IRIG-B respeed and transcription

`scripts/sg_nara/` respeeds the digitized NARA "Historical Apollo 15 Flight Tape" recordings
(`255-PAOa-608-AAA` and `640-AAA`..`655-AAA`, GET -002:22 to 285:49) to mission rate from the
IRIG-B timecode recorded on them, and transcribes the result with the same recognizer as
`sg_retimed_transcribe.py`. The decoder is a vectorized reimplementation of the logic of
`Apollo_Audio_Processing/processing_scripts/irig_b.py` (SBS check of option `-m`), fast enough to
search every file of every tape in full.

```
uv run python scripts/run.py nara_irig_survey.py --tape all --workers 14   # 1: decode IRIG-B, all files
uv run python scripts/run.py nara_align.py --tape all --workers 8          # 2: file start offsets
uv run python scripts/run.py nara_tape_map.py --tape all                   # 3: one timing map per tape
uv run python scripts/run.py nara_respeed.py --tape all --workers 4        # 4: render (genus can share)
uv run python scripts/run.py nara_retimed_transcribe.py run --tape all --batch-size 8 --prep-workers 4
```

Products are under `F:/Apollo_15_data/processing/sg_nara/` (`irig/`, `align/`, `maps/`, `respeed/`,
`retimed_transcripts/`). Each step binds its output to the checksums of its inputs and of its own
source, reuses a matching output, and moves a stale one aside.

## What the recordings are

- Four mono MP3 files per tape (`-tr1`..`-tr4`, 44.1 kHz, 128 kb/s), about 6 h 25 min each.
- The playback head did not sit on the tape's track pitch. Each file is a mix of neighbouring recorded
  tracks, and the timecode track moves between files from tape to tape and drifts within a tape.
  Usually one file carries it for 70-87 % of the tape, and it bleeds onto a neighbour some of the time
  (640 and 642 carry it on three files).
- All four files of a tape come from one simultaneous capture through one transport. They were cut at
  different start points: 0-6 s apart on most tapes, 44 s on 655 and 80 s on 650. Where two files
  both carry timecode, their offset is constant to a fraction of a millisecond for the whole tape
  (640 tr1/tr4: 0.0002 s at every hour, 0.00 ppm drift). One start offset per file is therefore
  enough, and one timing map serves all four files.
- The CSM Vox tapes (602-605, stereo) and the DSE dumps (AVT 15-03413/03414) carry no timecode on
  either channel. They are surveyed but not respeeded.
- **653-AAA has no timecode** (3 frames on the whole tape). It is not respeeded: no correction is
  forced where none is measured.

## 1. Survey (`nara_irig_survey.py`, `nara_irig.py`)

The decoder demodulates the 1 kHz carrier and tracks the 10 ms symbol clock (acquisition over 12
symbols and ±1.2 % in speed, then a phase/period loop). Each symbol is classified by the
envelope level in its 2-5 ms and 5-8 ms windows. A **strict** frame has all 11 position markers in
place, its fields in range, and straight binary seconds equal to its hours, minutes and seconds.
Other frame candidates (at least 9 markers) carry timing only. A frame's time is the leading edge of
Pr, in original-file seconds of the sequential ffmpeg decode. On a synthetic signal played 0.3 % fast,
times are within 0.5 ms (`tests/test_sg_nara.py`). The SBS check does not cover the day field. Misread
days and hours do occur on weak files; step 3 removes them.

## 2. Alignment (`nara_align.py`)

Evidence for each pair of files:

- `irig`: the same strict second on both files (median offset; MAD ≤ 2 ms);
- `irig_candidates`: strict frames on one file against frame candidates (timecode bleed) on the other,
  paired by decoded label. Misread labels scatter, so the dominant offset cluster is taken;
- `audio`: GCC-PHAT of the programme (8 kHz, IRIG band 550-1600 Hz removed, 16 windows of 20 s,
  ±150 s search, Tukey tapers). At least 5 windows must agree within 25 ms, and a drift above 10 ppm
  (a separate pass) is rejected. Where both kinds of evidence exist, audio reproduces the timecode
  offsets to 0.02 ms (640, 642).

**Timecode and programme sit 0.683 s apart on several tapes.** On 608, 644, 646, 650 and 652, the file
offsets measured on the timecode differ from those measured on the voice by a constant 680-684 ms
(for example, 650 tr1-tr3 is -80.405 s on timecode and -79.721 s on voice, both constant over the
tape). The same value on independent tapes points at fixed head geometry: the timecode track and the
voice tracks do not sit at the same along-tape position. Two placements are therefore kept:

- `start_s` (programme): audio first, timecode only where there is no audio link. Rendered files keep
  their speech mutually aligned, with no echo between files in the mix or in Audition.
- `clock_start_s` (timecode): timecode evidence only. The map pools frames with it.

The rate correction does not depend on this: the speed curve changes by a few ppm per minute. GET is
that of the timecode. Whether voice on a given tape is 0 or 0.683 s from it is **open**; it can be
calibrated where the respeeded tapes overlap the GET-placed IA SG reels (640, 641).

651-AAA: tr1, tr2 and tr4 share no programme or timecode with the timecode file tr3. They are rendered
at the reference file's start (`start_assumed`). Their rate correction holds, but their GET placement
is provisional by the unknown cut offset (usually under 6 s).

## 3. Timing map (`nara_tape_map.py`)

Strict frames from all files are pooled on the tape axis. Duplicates of the same second are merged.
Frames are chained when their offsets o = GMT - t agree within 10 ms + 0.5 % of the elapsed time, and
every frame joins whichever open chain it continues, so a misread frame forms a short orphan chain
instead of breaking the run. Chains under 8 frames are dropped. Chains join one segment when the line
fits on either side agree at the gap centre (50 ms + 0.4 % of the gap); otherwise the clock stepped (a
recorder stop) and a new segment starts. Every tape turned out to be one segment.

Per segment, frames are binned at 10 s and a cubic smoothing spline (λ = 1000) is fitted. λ was chosen
on 608 and 640: there, the spline's rate matches independent 60 s line fits to 10-14 ppm rms, while
GCV's choice follows the noise (up to 7,000 ppm at a segment end). If the rate leaves −0.5..+1 %, λ is
raised tenfold, which happens on sparse tapes such as 654. Beyond the frames, the rate is held at a
60 s line fit of the segment end. Across gaps between segments it ramps linearly.

D(τ) = ∫ o'(τ) dτ gives output seconds u = τ + D(τ), and GET = u + c for each segment.

| tape | strict frames | timecode coverage | rate ppm (min..max) | added s | residual rms ms | GET first..last |
|---|---|---|---|---|---|---|
| 608-AAA | 11,123 | 77 % | 1441..6198 | 88.0 | 0.505 | -002:21:5..003:56:33 |
| 640-AAA | 9,974 | 80 % | 392..2960 | 44.2 | 0.347 | 189:26:29..195:49:45 |
| 641-AAA | 9,424 | 94 % | 1746..4372 | 71.4 | 0.328 | 195:28:35..201:53:17 |
| 642-AAA | 13,266 | 92 % | 378..3356 | 45.2 | 0.374 | 201:35:58..207:56:42 |
| 643-AAA | 11,954 | 98 % | 1362..4675 | 72.8 | 0.328 | 207:26:18..213:50:14 |
| 644-AAA | 15,847 | 98 % | 161..3049 | 37.6 | 0.286 | 213:32:52..219:53:29 |
| 645-AAA | 10,326 | 91 % | 916..4357 | 61.5 | 0.776 | 219:46:15..226:07:55 |
| 646-AAA | 9,908 | 94 % | 416..2976 | 39.0 | 0.343 | 225:26:39..231:50:38 |
| 647-AAA | 10,047 | 77 % | 1181..4422 | 65.8 | 0.281 | 231:26:01..237:48:13 |
| 648-AAA | 12,165 | 99 % | -141..2596 | 34.5 | 0.284 | 237:26:19..243:47:57 |
| 649-AAA | 10,891 | 96 % | 1428..4278 | 63.1 | 0.311 | 243:33:25..249:52:48 |
| 650-AAA | 13,834 | 96 % | -374..2681 | 32.2 | 0.296 | 249:27:33..255:50:45 |
| 651-AAA | 7,477 | 45 % | 1744..4359 | 67.8 | 0.279 | 255:29:02..261:49:37 |
| 652-AAA | 12,728 | 99 % | 200..2927 | 33.2 | 0.309 | 261:26:24..267:48:49 |
| 653-AAA | 3 | none | not respeeded | | | |
| 654-AAA | 1,274 | 29 % | 624..2274 | 39.7 | 0.433 | 273:32:36..279:30:39 |
| 655-AAA | 16,883 | 99 % | 832..4348 | 64.8 | 0.329 | 279:26:05..285:48:42 |

Coverage is the union of tape time supported by frames less than 30 s apart, from all files
together. Outside it, the rate is held or interpolated; nothing is forced into the gaps.

## 4. Render (`nara_respeed.py`)

Every file is resampled through one inverse map at its `start_s`, using `sg_respeed.sinc_at` (64-tap
Kaiser sinc). All outputs of a tape have the same length and timeline: `<tape>_tr1..4.wav` (mono,
24-bit) and `<tape>_mix.wav`. The mix is the equal-weight mean of the files with each file gated out,
with 1 s ramps, within 3 s of its own timecode frame candidates, so IRIG-B does not reach the
recognizer. Where every file carries timecode the plain mean is used.

**Transcription.** `nara_retimed_transcribe.py` is `sg_retimed_transcribe.py` with the 16 tape
mixes as sources (102.4 h), its own store and lock, and the same recognizer defaults. A test window on
640 (output 9,360-9,960 s) transcribed cleanly: the press briefing with Jack Schmitt on the
Hadley-Apennine geology, with no timecode artefacts.

**Check.** Decoding the timecode in the respeeded 640 tr4 gives labels equal to u + c to 0.02 ms
median (0.52 ms rms). The rate error left in each hour is ≤ 0.25 ppm, against 400-3,000 ppm before. On 655
(rendered on genus): -0.03 ms median, 0.33 ms rms, 0.00 ppm in every hour.

**Genus.** A tape being rendered holds `respeed/<tape>.lock`, so render processes on this machine and on
genus (`bfeist@192.168.0.3`, Ubuntu; `/mnt/f` and `/mnt/d` are this machine's shares) share one list.
Genus uses its own venv, `~/.local/share/apollo15-nara-venv`, with this repo's locked numpy, scipy,
numba and soundfile; the denoise cluster's venv is left alone. Its ffmpeg 8.0.1 decodes the MP3s
bit-identically to this machine's 9.0.1 (same SHA-256 for 300 s of 640 tr4 and all of AVT 03413).
The command is in `nara_respeed.py`'s docstring.
Note that `_bootstrap.GENUS_SSH` still routes `192.168.0.3` to `.4`, which no longer answers.

## Open items

1. Voice-versus-timecode position (0 or 0.683 s per tape): calibrate against the IA SG reels where they
   overlap.
2. 653-AAA: its first and last ~20 minutes overlap 652 and 654. Correlating against their respeeded
   audio could give 653 a mean rate and a GET placement.
3. 651-AAA tr1/tr2/tr4 start offset (see above).
