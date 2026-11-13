# Stage 07: speech-level normalization

Order: `05b` shared deflutter, optional `06` Quindar retiming, then `07`
speech normalization. IRIG research utilities now live in `08_irig/`.
The stage writes separate `normalized.wav` and `normalization.json` files.
It does not change deflutter, retiming, or existing listening outputs.
Current stage-03 console/launcher workers write unity-gain audio; final speech
gain is set here.
There are no new dependencies, servers, or listening pages.

## Commands

Preview one completed defluttered channel without writing anything:

```powershell
uv run python scripts/07_normalize/07_normalize.py --tape A15_T510083 --channel 7 --dry-run
```

Remove `--dry-run` to write under
`D:/Apollo_15_30-Track/speech_normalized/A15_T510083/CH07/`.
Use `--output-root` to select a different destination. A nonempty destination
is rejected; comparisons and completed outputs are never overwritten.

One explicit WAV (or a channel folder containing `deflutter.wav`/`retimed.wav`):

```powershell
uv run python scripts/07_normalize/07_normalize.py --input D:/audio/deflutter.wav --output D:/audio_normalized/CH07 --dry-run
```

All available channels on a tape:

```powershell
uv run python scripts/07_normalize/07_normalize.py --tape A15_T510083 --all-channels --dry-run
```

For retimed inputs, set `--input-root` to the retimed root. Batch mode requires
completed source-stage manifests. A failed channel is reported and other
selected channels continue; the command exits unsuccessfully if any failed.

The main runner supports planning, one channel, or batch execution:

```powershell
uv run python scripts/pipeline.py plan --stages 07 --all-tapes
uv run python scripts/pipeline.py run --stages 07 --tape A15_T510083 --channel 7
uv run python scripts/pipeline.py run --stages 07 --all-tapes --normalize-from retimed
```

Runner `plan` displays commands only. The standalone `--dry-run` actually
measures audio. Combined single-channel selection `--stages 05b,06,07`
automatically feeds retimed audio to normalization; `--stages 05b,07` feeds
defluttered audio. Batch mode continues to run one stage at a time.

## Measurement and safeguards

This initial detector is a conservative signal-processing heuristic for
**voiced speech candidates**, not a neural speech classifier, transcript,
ITU P.56 implementation, or LUFS meter. Its thresholds need listening
calibration on the Apollo channels. It can miss weak or unusual speech and
can mistake sufficiently voice-like interference for speech.

Analysis reads the complete file in bounded audio blocks and stores compact
20 ms features. It requires speech-band energy above a robust background
estimate, periodicity in the voice pitch range, and energy spread across
more than one narrow tone. High-crest impulses and nearby frames are excluded
throughout the recording. Syllables may bridge short gaps, but accepted
activity must form sustained groups. Only measured voiced frames contribute
to the speech level; intervening gaps are not counted as speech.

Candidates must also clear the same noise margin against the **full-band**
background. This second gate was added after full-channel testing on T510077:
the owner confirmed that CH01 and CH02 contain no speech, but the initial
band-only detector admitted about ten seconds of weak periodic activity per
channel. Their full-band contrasts were only 7.72 and 7.61 dB. Method v2 rejects
them below the 9 dB margin, leaving both gain and peak limiting bypassed.
Reports distinguish candidate duration from accepted speech duration.

| Setting | Default | Meaning |
| --- | --- | --- |
| `--target-speech-dbfs` | -23 | Median RMS level across accepted voiced frames, not peak or LUFS |
| `--max-boost-db` | Disabled | Optional explicit limit on constant gain |
| `--max-noise-dbfs` | Disabled | Optional explicit ceiling on estimated background RMS after gain |
| `--min-snr-db` | 9 | Required margins against both the speech-band floor and full-band background |
| `--min-speech-seconds` | 3 | Minimum accepted voiced duration, independent of total tape length |
| `--peak-ceiling-dbfs` | -1 | Sample-peak ceiling for qualified speech renders |

The main runner exposes the first three as `--speech-target-dbfs`,
`--speech-max-boost-db`, and `--speech-max-noise-dbfs`.

Channels without enough qualifying speech retain **exactly their input sample
values** in a float WAV, including their spikes; they receive neither gain nor
limiting. `--timing-only` explicitly selects this bypass. Identified IRIG input
filenames or timing-only source roles also bypass. CH01 is not automatically
assumed to be timing-only based on its number.

Qualified channels request the gain required to reach the speech RMS target,
then check the available headroom of all ordinary audio. The final gain is
the smaller of those two values. The RMS target can remain unmet when reaching
it would overload loud passages. Stage 07 does not inherit the old denoiser's
+12 dB cap or impose a default background-level ceiling. Noise-only rejection
is a separate decision made before gain calculation. Explicit policy caps
remain available as optional flags.

The headroom scan exempts only isolated sharp impulses: peak/RMS above 8,
peak more than 3 times its surrounding peaks, and at most 40 ms of candidate
frames with usable context on both sides. Within those frames, the impulse
and its decay above the surrounding peak are recorded as exact sample intervals.
Every other sample, including uncertain disturbances, protects the gain ceiling.

The same constant gain applies across the complete file; there is no silence
gate, section leveling, or per-utterance automatic gain. Isolated exceptional peaks are restrained by a symmetric
5 ms peak-envelope limiter with block halos, preserving sample positions and
duration. This is a **listening render**, with local nonlinear changes around
limited peaks. It is not click repair, a true-peak limiter, or a new analysis
master. Rendering fails rather than publishing if limiting touches any sample
outside the recorded impulse intervals and their 10 ms processing halos.
Reports count affected samples, maximum reduction, and protected samples altered
(required to be zero). A sustained or ambiguous disturbance remains protected
and can constrain gain.

The v3 CH04 trial was rejected after listening: deriving +20.09 dB from the
quiet speech median forced approximately 207 seconds of louder audio into
the limiter. V4 adds the ordinary-audio headroom constraint above, rather than
restoring an arbitrary boost cap. The total-file percentage of limited samples
is no longer an acceptance test; on a long sparse recording it hid this failure.

Completed v2 denoise outputs retain their recorded gain. Current console/launcher workers omit
the former endpoint-aware peak normalization and write at unity gain, without
an extra gain scan or WAV rewrite. Stage 07 measures its supplied
input directly; any explicitly requested boost cap limits additional gain,
and a bypass retains any earlier gain. No denoising or tone survey is repeated. Existing retime gain
behavior is also unchanged.

## Output verification

Each report binds the source path, size, timestamp, decoded float32 hash,
settings, implementation hash, accepted frame intervals, speech/noise levels,
gain decision, output hash, and limiter statistics. The writer checks the
source again while rendering and reads the output back before publication.
Output is mono 32-bit float WAV/RF64 with identical sample rate and frame count.
Source manifests are preserved as `source_denoised.json`, `source_review.json` or
`source_retime.json`; `markers.csv` is copied with unchanged coordinates.
No timing map is fitted or altered by normalization.

Use the dry-run measurements and matched Audition excerpts to calibrate before
an archive-wide run. Synthetic regressions cover sparse speech, silence,
noise and tone bursts, interior spikes, gain caps, limiter block boundaries,
source mutation, and output immutability. These checks do not establish speech
recognition accuracy on every historical channel.
