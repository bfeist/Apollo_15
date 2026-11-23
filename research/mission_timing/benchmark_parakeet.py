"""Measure batched Parakeet TDT on an explicit list of Apollo audio excerpts."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
import json
import os
from pathlib import Path
from statistics import median
import time

from transcribe import ASR_RATE, AudioSource, command_output, source_sha256, write_json


MODEL = "nemo-parakeet-tdt-0.6b-v3"
MODEL_REPOSITORY = "istupakov/parakeet-tdt-0.6b-v3-onnx"
MODEL_REVISION = "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"


def benchmark(args):
    import onnx_asr
    import onnxruntime as ort

    specs = json.loads(args.inputs.read_text(encoding="utf-8-sig"))
    if not specs or len({entry["id"] for entry in specs}) != len(specs):
        raise ValueError("Supply a nonempty excerpt list with unique IDs")
    sources = [(spec, AudioSource(spec)) for spec in specs]
    gpu = command_output(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]).decode().strip()
    dll_handles = [os.add_dll_directory(str(directory)) for directory in args.cuda_dll_dir]
    try:
        run(args, ort, onnx_asr, sources, gpu)
    finally:
        for handle in dll_handles:
            handle.close()


def recognize(model, segments, batch_size, text_path):
    rows = []
    out_of_range = 0
    for offset in range(0, len(segments), batch_size):
        batch = segments[offset:offset + batch_size]
        result = model.recognize([segment[2] for segment in batch], sample_rate=ASR_RATE)
        for (identifier, start, waveform), decoded in zip(batch, result, strict=True):
            duration = len(waveform) / ASR_RATE
            tokens = []
            for token, stamp, probability in zip(decoded.tokens or [], decoded.timestamps or [],
                                                 decoded.logprobs or [], strict=True):
                local = round(start + stamp, 8) if 0 <= stamp < duration else None
                out_of_range += local is None
                tokens.append({"token": token, "start": local, "clip_offset_s": stamp, "logprob": probability})
            rows.append({
                "source_id": identifier, "clip_start_seconds": start,
                "clip_end_seconds": start + duration, "asr_samples": len(waveform),
                "text": decoded.text, "tokens": tokens,
            })
    with text_path.open("w", encoding="utf-8") as handle:
        for row in sorted(rows, key=lambda row: (row["source_id"], row["clip_start_seconds"])):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return sum(len(row["tokens"]) for row in rows), out_of_range


def load_speech_windows(paths, sources):
    cached = {}
    manifests = {}
    for path in paths:
        windows = json.loads(path.read_text(encoding="utf-8"))
        source_path = path.with_name("sources.json")
        source_manifest = json.loads(source_path.read_text(encoding="utf-8"))["sources"]
        for identifier, item in windows.items():
            if identifier in cached:
                raise ValueError(f"Speech windows repeat source ID {identifier}")
            cached[identifier] = item
            manifests[identifier] = source_manifest[identifier]
    for spec, source in sources:
        identifier = spec["id"]
        if identifier not in cached:
            raise ValueError(f"Speech windows do not cover {identifier}")
        source_fields = source.metadata()
        if any(manifests[identifier].get(key) != value for key, value in source_fields.items()):
            raise ValueError(f"Speech windows refer to a different source or excerpt for {identifier}")
    return cached, manifests


def read_excerpt(item):
    spec, source = item
    waveform, frames = source.read(source.start, source.end)
    if frames != source.end - source.start:
        raise ValueError(f"Short read for excerpt {spec['id']}")
    return spec["id"], source.start / source.rate, waveform


def retained_segments(args, entries, cached):
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    with ThreadPoolExecutor(max_workers=args.workers) as workers:
        began = time.perf_counter()
        excerpts = list(workers.map(read_excerpt, entries))
        read_seconds = time.perf_counter() - began
        began = time.perf_counter()
        options = VadOptions(threshold=args.vad_threshold, min_silence_duration_ms=300,
                             speech_pad_ms=400, max_speech_duration_s=28)
        current = list(workers.map(lambda item: get_speech_timestamps(item[2], options), excerpts))
        vad_seconds = time.perf_counter() - began
    segments = []
    for (identifier, origin, waveform), detected in zip(excerpts, current, strict=True):
        entry = cached[identifier]
        cached_frames = [(round((left - origin) * ASR_RATE), round((right - origin) * ASR_RATE))
                         for left, right in entry["windows"]]
        detected_frames = [(item["start"], item["end"]) for item in detected]
        if (abs(entry["start"] - origin) > 0.5 / ASR_RATE
                or abs(entry["end"] - origin - len(waveform) / ASR_RATE) > 0.5 / ASR_RATE
                or detected_frames != cached_frames):
            raise ValueError(f"Cached VAD ranges differ from the timed VAD rerun for {identifier}")
        segments.extend((identifier, origin + left / ASR_RATE, waveform[left:right])
                        for left, right in cached_frames)
    segments.sort(key=lambda item: (len(item[2]), item[0], item[1]))
    return segments, read_seconds, vad_seconds


def run(args, ort, onnx_asr, sources, gpu):
    benchmark_start = time.perf_counter()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cached, cached_manifests = load_speech_windows(args.speech_windows, sources) if args.speech_windows else ({}, {})
    vad_load_seconds = 0.0
    if cached:
        from faster_whisper.vad import get_vad_model

        began = time.perf_counter()
        get_vad_model()
        vad_load_seconds = time.perf_counter() - began
    provider = "CUDAExecutionProvider"
    began = time.perf_counter()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    cuda_options = {"cudnn_conv_algo_search": "HEURISTIC"}
    model = onnx_asr.load_model(MODEL, path=args.model_dir,
                                providers=[(provider, cuda_options), "CPUExecutionProvider"],
                                sess_options=options).with_timestamps()
    providers = {"encoder": model.asr._encoder.get_providers(), "decoder": model.asr._decoder_joint.get_providers()}
    if any(provider not in assignments for assignments in providers.values()):
        raise RuntimeError(f"Cannot benchmark CUDA with provider assignments {providers}")
    load_seconds = time.perf_counter() - began

    grouped = {}
    metadata = {}
    for spec, source in sources:
        grouped.setdefault(spec.get("suite", "all"), []).append((spec, source))
        metadata[spec["id"]] = source.metadata()
    chunk_samples = args.chunk_seconds * ASR_RATE
    # Initialize kernels on realistic audio before collecting steady-state timings.
    spec, source = sources[0]
    source_stop = min(source.end, source.start + args.chunk_seconds * args.batch_size * source.rate)
    warm, _ = source.read(source.start, source_stop)
    began = time.perf_counter()
    model.recognize([warm[i:i + chunk_samples] for i in range(0, len(warm), chunk_samples)], sample_rate=ASR_RATE)
    warm_decode_seconds = time.perf_counter() - began

    results = {}
    for suite, entries in grouped.items():
        duration = sum((source.end - source.start) / source.rate for _, source in entries)
        trials = []
        destination = args.out_dir / f"{suite}_tokens.jsonl"
        for _ in range(args.repeats):
            began = time.perf_counter()
            if cached:
                segments, read_seconds, vad_seconds = retained_segments(args, entries, cached)
            else:
                segments = [
                    (identifier, origin + offset / ASR_RATE, waveform[offset:offset + chunk_samples])
                    for identifier, origin, waveform in map(read_excerpt, entries)
                    for offset in range(0, len(waveform), chunk_samples)
                ]
                read_seconds = time.perf_counter() - began
                vad_seconds = 0.0
            preparation_seconds = time.perf_counter() - began
            decode_start = time.perf_counter()
            token_count, out_of_range = recognize(model, segments, args.batch_size, destination)
            trials.append({
                "source_io_and_resample_seconds": read_seconds,
                "vad_seconds": vad_seconds,
                "prepare_seconds": preparation_seconds,
                "decode_and_emit_seconds": time.perf_counter() - decode_start,
                "end_to_end_seconds": time.perf_counter() - began,
                "tokens": token_count,
                "tokens_outside_source_span": out_of_range,
            })
        trial_seconds = [trial["end_to_end_seconds"] for trial in trials]
        throughput = duration / median(trial_seconds)
        attended_samples = sum(len(clip[2]) for clip in segments)
        batches = [segments[i:i + args.batch_size] for i in range(0, len(segments), args.batch_size)]
        padded_samples = sum(max(len(clip[2]) for clip in batch) * len(batch) for batch in batches)
        results[suite] = {
            "source_seconds": duration, "chunks": len(segments), "seconds_per_repeat": trial_seconds,
            "audio_preparation_seconds_per_repeat": [trial["prepare_seconds"] for trial in trials],
            "retained_audio_seconds": attended_samples / ASR_RATE,
            "batch_padded_audio_seconds": padded_samples / ASR_RATE,
            "trials": trials,
            "median_audio_hours_per_processing_hour": throughput,
            "hours_for_19200_source_hours_at_median_rate": 19200 / throughput,
            "tokens_path": destination.name,
        }
        print(f"{suite}: {throughput:.1f}x end to end on {duration:g} seconds", flush=True)
    for spec, source in sources:
        identifier = spec["id"]
        if source.metadata() != metadata[identifier]:
            raise RuntimeError(f"Source changed during benchmark: {source.path}")
        metadata[identifier].update({field: cached_manifests[identifier][field]
                                     for field in ("sha256", "review_sha256", "hash_origin")
                                     if field in cached_manifests.get(identifier, {})})
        metadata[identifier]["review"] = spec.get("review")
    write_json(args.out_dir / "parakeet_timing.json", {
        "model_repository": MODEL_REPOSITORY, "model_revision": MODEL_REVISION, "model_directory": str(args.model_dir),
        "onnx_asr_version": version("onnx-asr"), "onnxruntime_gpu_version": ort.__version__, "gpu": gpu,
        "providers": providers, "cuda_options": cuda_options,
        "precision": "unquantized export with ONNX Runtime CUDA precision defaults",
        "vad": bool(cached), "vad_threshold": args.vad_threshold if cached else None,
        "vad_window_files": {str(path): source_sha256(path) for path in args.speech_windows},
        "chunk_seconds": None if cached else args.chunk_seconds, "batch_size": args.batch_size,
        "batch_order": "duration_then_source_time" if cached else "input_then_source_time",
        "audio_timestamp_output": "token_onsets_only",
        "cpu_workers": args.workers if cached else 1,
        "model_load_seconds_excluded_from_throughput": load_seconds,
        "vad_model_load_seconds_excluded_from_throughput": vad_load_seconds,
        "warm_decode_seconds_excluded_from_throughput": warm_decode_seconds,
        "actual_benchmark_wall_seconds": time.perf_counter() - benchmark_start,
        "sources": metadata, "suites": results,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, help="JSON list of AudioSource specifications with optional suite names")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--cuda-dll-dir", type=Path, action="append", default=[])
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--chunk-seconds", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--speech-windows", type=Path, action="append", default=[])
    parser.add_argument("--vad-threshold", type=float, default=0.12)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if min(args.batch_size, args.chunk_seconds, args.repeats, args.workers) <= 0:
        parser.error("Batch size, chunk seconds, repeats and workers must be positive")
    if not 0 < args.vad_threshold < 1:
        parser.error("VAD threshold must be between zero and one")
    benchmark(args)


if __name__ == "__main__":
    main()
