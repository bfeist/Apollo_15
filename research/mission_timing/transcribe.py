"""Write source-relative faster-whisper word and segment evidence to JSONL."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import time

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


ASR_RATE = 16_000
MODELS = {
    "large-v3": ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478"),
    "distil-large-v3.5": ("distil-whisper/distil-large-v3.5-ct2", "9793ccc07920e0f830e1dba0343efcdf0ef8c903"),
}


def command_output(args):
    return subprocess.check_output(args, stderr=subprocess.PIPE)


class AudioSource:
    def __init__(self, spec):
        self.path = Path(spec["source"]).resolve(strict=True)
        self.track = spec.get("stream", 0)
        self.channel = spec.get("channel")
        self.volume = spec.get("volume")
        self.expected_sha256 = spec.get("sha256")
        try:
            info = sf.info(self.path) if self.track == 0 else None
        except sf.LibsndfileError:
            info = None
        self.direct = info is not None and info.format in {"WAV", "WAVEX", "RF64", "FLAC", "AIFF"}
        if self.direct:
            self.rate = info.samplerate
            self.channels = info.channels
            self.frames = info.frames
            self.format = f"{info.format}/{info.subtype}"
            self.pts_origin = 0.0
        else:
            probe = json.loads(command_output([
                "ffprobe", "-v", "error", "-select_streams", f"a:{self.track}",
                "-show_streams", "-show_format", "-of", "json", str(self.path),
            ]))
            if not probe["streams"]:
                raise ValueError(f"No audio stream a:{self.track} in {self.path}")
            stream = probe["streams"][0]
            self.rate = int(stream["sample_rate"])
            self.channels = int(stream["channels"])
            self.pts_origin = float(stream.get("start_time", 0))
            duration = stream.get("duration") or probe["format"].get("duration")
            if duration in (None, "N/A"):
                raise ValueError(f"No audio duration in {self.path}")
            self.frames = round(float(duration) * self.rate)
            self.format = stream["codec_name"]

        if self.channel is not None and not 0 <= self.channel < self.channels:
            raise ValueError(f"Channel {self.channel} outside the {self.channels} source channels")
        self.start = round(float(spec.get("start", 0)) * self.rate)
        self.end = round(float(spec["end"]) * self.rate) if "end" in spec else self.frames
        if not 0 <= self.start < self.end <= self.frames:
            raise ValueError(f"Requested range outside audio source: {self.path}")

    def read(self, start, end):
        if self.direct:
            with sf.SoundFile(self.path) as audio:
                audio.seek(start)
                samples = audio.read(end - start, dtype="float32", always_2d=True)
        else:
            # Count decoded samples from the start of the selected stream. Input-side
            # approximate seeking can add a format-specific timestamp shift.
            raw = command_output([
                "ffmpeg", "-v", "error", "-nostdin", "-i", str(self.path),
                "-map", f"0:a:{self.track}", "-af", f"atrim=start_sample={start}:end_sample={end}",
                "-t", f"{end / self.rate:.9f}", "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
            ])
            samples = np.frombuffer(raw, dtype="<f4").reshape(-1, self.channels)

        mono = samples[:, self.channel] if self.channel is not None else samples.mean(axis=1)
        if not len(mono):
            raise ValueError(f"Decoded no audio at {start / self.rate:.3f} s in {self.path}")
        if self.rate != ASR_RATE:
            mono = resample_poly(mono, ASR_RATE, self.rate)
        return np.ascontiguousarray(mono, dtype=np.float32), len(samples)

    def metadata(self):
        stat = self.path.stat()
        metadata = {
            "path": str(self.path), "file_size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "stream": self.track, "channel": self.channel, "source_channels": self.channels,
            "mix": "equal_weight_mean" if self.channel is None else "single_channel",
            "format": self.format, "sample_rate": self.rate, "frames": self.frames,
            "start_frame": self.start, "end_frame": self.end,
            "stream_start_pts_s": self.pts_origin, "time_domain": "decoded_stream_elapsed_seconds",
        }
        if self.volume is not None:
            metadata["volume"] = self.volume
        return metadata

    def verify_sha256(self, observed):
        if self.expected_sha256 is not None and observed != self.expected_sha256:
            raise ValueError(f"Source SHA-256 differs from the input manifest: {self.path}")


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_sha256(path):
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def bounded_time(value, origin, end):
    return round(min(end, max(origin, origin + float(value))), 8)


def run_source(pipeline, source, out, settings):
    out.mkdir(parents=True)
    metadata = {"status": "running", "source": source.metadata(), "asr": settings}
    write_json(out / "manifest.json", metadata)
    began = time.perf_counter()
    prep_seconds = decode_seconds = speech_seconds = 0.0
    segments = words = processed_frames = 0
    block_samples = round(settings["block_seconds"] * source.rate)
    context_samples = round(settings["context_seconds"] * source.rate)
    with (out / "segments.jsonl").open("x", encoding="utf-8", buffering=1) as rows, (
        out / "blocks.jsonl"
    ).open("x", encoding="utf-8", buffering=1) as blocks:
        for block_index, core_start in enumerate(range(source.start, source.end, block_samples)):
            core_end = min(core_start + block_samples, source.end)
            read_start = max(source.start, core_start - context_samples)
            read_end = min(source.end, core_end + context_samples)
            origin = read_start / source.rate
            prep_start = time.perf_counter()
            audio, decoded_frames = source.read(read_start, read_end)
            read_end = read_start + decoded_frames
            endpoint = read_end / source.rate
            preparation = time.perf_counter() - prep_start
            prep_seconds += preparation
            infer_start = time.perf_counter()
            rows_before = segments
            options = {
                "language": "en", "beam_size": settings["beam_size"],
                "batch_size": settings["batch_size"], "temperature": 0.0,
                "condition_on_previous_text": False, "word_timestamps": True,
                "without_timestamps": False, "vad_filter": True,
                "vad_parameters": dict(settings["vad_parameters"]), "chunk_length": 30,
            }
            generated, info = pipeline.transcribe(audio, **options)
            for segment in generated:
                row = asdict(segment)
                start = bounded_time(row["start"], origin, endpoint)
                end = bounded_time(row["end"], origin, endpoint)
                mid = (start + end) * source.rate / 2
                if mid < core_start or (mid >= core_end and core_end != source.end):
                    continue
                row.update(id=segments, start=start, end=end, block=block_index)
                for word in row["words"] or []:
                    for field in ("start", "end"):
                        word[field] = bounded_time(word[field], origin, endpoint)
                rows.write(json.dumps(row, ensure_ascii=False) + "\n")
                segments += 1
                words += len(row["words"] or [])
            processing = time.perf_counter() - infer_start
            decode_seconds += processing
            speech_seconds += info.duration_after_vad
            processed_frames += max(0, min(core_end, read_end) - core_start)
            blocks.write(json.dumps({
                "block": block_index, "core_start_frame": core_start, "core_end_frame": core_end,
                "read_start_frame": read_start, "read_end_frame": read_end,
                "chunk_origin_s": origin, "segments": segments - rows_before,
                "preparation_seconds": preparation, "decode_seconds": processing,
                "seconds_after_vad": info.duration_after_vad,
            }) + "\n")
            print(f"{out.name}: through {core_end / source.rate:.2f} s, {segments} segments", flush=True)

    elapsed = time.perf_counter() - began
    source_seconds = processed_frames / source.rate
    hash_start = time.perf_counter()
    source_hash = source_sha256(source.path)
    source.verify_sha256(source_hash)
    if metadata["source"] != source.metadata():
        raise RuntimeError(f"Source changed while processing: {source.path}")
    metadata["source"]["sha256"] = source_hash
    metadata.update(status="complete", metrics={
        "segments": segments, "words": words, "source_seconds": source_seconds,
        "vad_seconds_with_context": speech_seconds, "preparation_seconds": prep_seconds,
        "decode_seconds": decode_seconds, "processing_wall_seconds": elapsed,
        "source_hash_seconds": time.perf_counter() - hash_start,
        "audio_hours_per_processing_hour": source_seconds / elapsed,
    })
    write_json(out / "manifest.json", metadata)


def add_cuda_options(parser):
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--dll-dir", type=Path, action="append", default=[])


def load_model(model, device_index):
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    from huggingface_hub import snapshot_download

    model_id, revision = MODELS[model]
    snapshot_started = time.perf_counter()
    model_path = snapshot_download(
        repo_id=model_id, revision=revision,
        allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"],
    )
    snapshot_seconds = time.perf_counter() - snapshot_started
    load_began = time.perf_counter()
    pipeline = BatchedInferencePipeline(WhisperModel(
        model_path, device="cuda", device_index=device_index, compute_type="float16", cpu_threads=4,
    ))
    model_load_seconds = time.perf_counter() - load_began
    gpu = command_output([
        "nvidia-smi", f"--id={device_index}", "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    ]).decode().strip().split(", ")
    return pipeline, {
        "model": model, "model_id": model_id, "revision": revision,
        "faster_whisper": importlib.metadata.version("faster-whisper"),
        "ctranslate2": importlib.metadata.version("ctranslate2"),
        "device": "cuda", "device_index": device_index, "compute_type": "float16",
        "gpu_name": gpu[0], "gpu_driver": gpu[1], "gpu_total_mib": int(gpu[2]),
        "language": "en", "word_timestamps": True, "without_timestamps": False,
        "temperature": 0, "condition_on_previous_text": False,
        "asr_sample_rate": ASR_RATE, "cpu_threads": 4, "model_snapshot_seconds": snapshot_seconds,
        "model_load_seconds": model_load_seconds,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--source", type=Path)
    inputs.add_argument("--inputs", type=Path, help="JSON array of {id, source, start?, end?, stream?, channel?}")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--stream", type=int, default=0, help="Zero-based audio-stream index")
    parser.add_argument("--channel", type=int, help="Zero-based channel index. Default: equal-weight mono mix")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--block-seconds", type=float, default=300)
    parser.add_argument("--context-seconds", type=float, default=2)
    parser.add_argument("--vad-threshold", type=float, default=0.30)
    add_cuda_options(parser)
    args = parser.parse_args()

    if not 0 < args.vad_threshold < 1 or not 0 <= args.context_seconds < args.block_seconds:
        parser.error("Require 0 < VAD threshold < 1 and 0 <= context < block length")
    if min(args.batch_size, args.beam_size) < 1 or min(args.stream, args.device_index) < 0:
        parser.error("Require positive batch/beam sizes and nonnegative stream/device indices")

    if args.inputs:
        specs = json.loads(args.inputs.read_text(encoding="utf-8-sig"))
        if not specs:
            parser.error("Empty input manifest")
        ids = [spec["id"] for spec in specs]
        if any(name in {"", ".", ".."} or Path(name).name != name for name in ids) or len(set(ids)) != len(ids):
            parser.error("Input IDs must be unique single directory names")
        outputs = [args.output / name for name in ids]
    else:
        spec = {"source": args.source, "start": args.start, "stream": args.stream, "channel": args.channel}
        if args.end is not None:
            spec["end"] = args.end
        specs, outputs = [spec], [args.output]

    sources = [AudioSource(spec) for spec in specs]
    if any(out.exists() for out in outputs):
        parser.error("Output already exists. Choose a new output directory")
    dll_handles = [os.add_dll_directory(str(path.resolve(strict=True))) for path in args.dll_dir]
    pipeline, settings = load_model(args.model, args.device_index)
    settings.update({
        "batch_size": args.batch_size, "beam_size": args.beam_size,
        "block_seconds": args.block_seconds, "context_seconds": args.context_seconds,
        "vad_filter": True, "vad_parameters": {
            "threshold": args.vad_threshold, "min_silence_duration_ms": 300,
            "speech_pad_ms": 400,
        },
        "chunk_length_seconds": 30,
    })
    for source, output in zip(sources, outputs):
        run_source(pipeline, source, output, settings)
    for handle in dll_handles:
        handle.close()


if __name__ == "__main__":
    main()
