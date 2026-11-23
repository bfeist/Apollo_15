# Source-relative ASR evidence

For the archive first pass, the pilot favors source-isolated distil-large-v3.5 with `--batch-beam 16 1 --vad-threshold 0.12 --vad-voice-band-rms`. The detector receives a gain-assisted copy and the recognizer receives the original samples. The three-tape and sustained trials give different estimates because speech density varies. [Measured timing, recall, commands and projections](asr_timing_benchmarks.md)

The pilot uses faster-whisper 1.2.1 and CTranslate2 4.9.1 on CUDA FP16. The scripts pin the Hugging Face model snapshots. `--dll-dir` can be repeated on Windows to supply cuDNN 9 and CUDA 12 binaries. With those directories already discoverable, omit the arguments. The benchmark also uses FFmpeg and ffprobe for compressed sources.

Run a full recording or a bounded excerpt with `transcribe.py`. The default input is audio stream 0 with an equal-weight mix of its channels. `--stream` and `--channel` are zero-based. Omit `--start` and `--end` to use the whole stream.

```powershell
$cudaDirs = @('--dll-dir', 'C:\Program Files\NVIDIA\CUDNN\v9.10\bin\12.9', '--dll-dir', 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin')
$pao = 'N:/Projects/Apollo/Mission Data/Apollo 15/Audio/Internet Archive/572-AAA.wav'
uv run --with faster-whisper==1.2.1 --with ctranslate2==4.9.1 python research/mission_timing/transcribe.py --source $pao --output runs/mission_timing_pilot/asr/pao_example --model large-v3 @cudaDirs
```

The new output directory contains `segments.jsonl` and `blocks.jsonl`, which can be read as the run progresses. The manifest reaches `complete` after the source hash is checked. Segment and word `start` and `end` are elapsed seconds on the input stream. `blocks.jsonl` records the read bounds and chunk origins in source coordinates. Faster-whisper restores pauses removed by its VAD before the block origin is added. The WAV file’s clock, any stage-05b channel origin, PTS and GET remain distinct.

For many excerpts, use `benchmark_asr.py` with a JSON array. Clip `start` and `end` are source elapsed seconds. The optional 05b review supplies the published audio checksum and is itself hashed. Other source files are hashed directly. Each new output directory records settings, JSONL transcripts, the retained speech windows, processing speed and whole-run duration.

The [sample specification](asr_samples.json) records the exact pilot intervals and release identities. Build the sample manifest from the local media roots. The builder checks the published 05b receipt fingerprints or the sizes of the external media. The ASR drivers verify the expected source checksum before marking a run complete. Use `census_T510077`, `census_T510093`, `census_T510100`, `sustained_T510093`, `matched`, `dvd`, `pao_full`, `census_audit` or `audit_with_silence` as the set name. The silence set generates its exact 60-second PCM control beside the manifest. The matched set needs the roots for 05b, PAO, DVD and the ALSJ Spur MP3. [ALSJ Spur clip listing](https://apollojournals.org/alsj/a15/a15.spur.html)

```powershell
$src = 'research/mission_timing'
$stage05 = 'D:/Apollo_15_30-Track/defluttered_shared'
$run = 'runs/mission_timing_pilot/asr'
uv run python "$src/build_asr_samples.py" --set sustained_T510093 --stage05-root $stage05 --output "$run/sustained_T510093_inputs.json"
uv run --with faster-whisper==1.2.1 --with ctranslate2==4.9.1 python "$src/benchmark_asr.py" --inputs "$run/sustained_T510093_inputs.json" --output "$run/repeat_sustained" --model distil-large-v3.5 --batch-beam 16 1 --vad-threshold 0.12 @cudaDirs
```

```json
[
  {
    "id": "T510093_CH14_middle",
    "source": "D:/Apollo_15_30-Track/defluttered_shared/A15_T510093/CH14/deflutter.wav",
    "start": 30990,
    "end": 31110,
    "review": "D:/Apollo_15_30-Track/defluttered_shared/A15_T510093/CH14/review.json"
  }
]
```

```powershell
uv run --with faster-whisper==1.2.1 --with ctranslate2==4.9.1 python research/mission_timing/benchmark_asr.py --inputs runs/mission_timing_pilot/asr/census_inputs.json --output runs/mission_timing_pilot/asr/census_example --model distil-large-v3.5 --batch-beam 16 1 --batch-beam 32 1 @cudaDirs
```

The benchmark holds its inputs in memory, so keep each job bounded. The pilot census used 90 excerpts of 120 seconds. By default every decoder context belongs to one input excerpt. The GPU batch can include contexts from different excerpts. The decoder context identifies the source, and its inverse VAD map restores each word's source time. `--max-vad-gap 1` keeps original intervening samples for gaps up to one second and starts a new decoder context after larger gaps. `--mix-sources` reproduces the experimental first pass that allowed one decoder context to combine excerpts. Its source attribution depends on estimated word boundaries.

Compare timing at the same excerpt list and settings. `--no-vad` provides a recall check. `--disable-timestamp-tokens` still requests attention-derived word timestamps. The benchmark marks word endpoints clipped at a decoder or retained audio boundary as `timing_clipped`, and skips timestamps wholly outside their decoder context. Zero-duration, clipped and implausible timings need review. Evidence used for a mission-time fit needs confirmation against the source audio and original transcript.
