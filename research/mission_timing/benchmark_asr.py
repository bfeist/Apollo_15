"""Benchmark cross-input faster-whisper batching on identical audio excerpts."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from math import isfinite
import os
from pathlib import Path
import time

import numpy as np

if __package__:
    from .transcribe import ASR_RATE, MODELS, AudioSource, add_cuda_options, load_model, source_sha256, write_json
else:
    from transcribe import ASR_RATE, MODELS, AudioSource, add_cuda_options, load_model, source_sha256, write_json


@dataclass(frozen=True)
class Span:
    packed_start: int
    packed_end: int
    source_id: str
    local_start: float


def prepare(item, threshold, vad_transform=None):
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spec, source = item
    audio, _ = source.read(source.start, source.end)
    if threshold is None:
        windows = [{"start": start, "end": min(start + 28 * ASR_RATE, len(audio))}
                   for start in range(0, len(audio), 28 * ASR_RATE)]
    else:
        vad_audio = audio if vad_transform is None else vad_transform(audio)
        windows = get_speech_timestamps(vad_audio, VadOptions(
            threshold=threshold, min_silence_duration_ms=300, speech_pad_ms=400,
            max_speech_duration_s=28,
        ))
    return spec["id"], source.start / source.rate, audio, windows


def pack(inputs, *, mix_sources=False, max_gap_samples=None):
    """Pack retained ranges and retain their exact inverse map in 16 kHz samples."""
    max_length = 29 * ASR_RATE
    guard = np.zeros(round(0.32 * ASR_RATE), dtype=np.float32)
    spans, features_clips, pieces = [], [], []
    offset = chunk_start = 0
    for source_id, origin, audio, windows in inputs:
        previous_end = None
        for window in windows:
            left, right = window["start"], window["end"]
            gap = left - previous_end if previous_end is not None else 0
            different_source = previous_end is None and offset > chunk_start
            padding = len(guard) if mix_sources and different_source else 0
            preserve_gap = max_gap_samples is not None and previous_end is not None
            joined_left = previous_end if preserve_gap else left
            if (different_source and not mix_sources
                    or preserve_gap and gap > max_gap_samples
                    or offset - chunk_start + padding + right - joined_left > max_length):
                features_clips.append((chunk_start, offset))
                chunk_start, padding, joined_left = offset, 0, left
            if padding:
                pieces.append(guard)
                offset += padding
            pieces.append(audio[joined_left:right])
            packed_end = offset + right - joined_left
            if offset > chunk_start and joined_left == previous_end:
                prior = spans[-1]
                spans[-1] = Span(prior.packed_start, packed_end, source_id, prior.local_start)
            else:
                spans.append(Span(offset, packed_end, source_id, origin + joined_left / ASR_RATE))
            offset = packed_end
            previous_end = right
    if offset > chunk_start:
        features_clips.append((chunk_start, offset))
    return (np.concatenate(pieces) if pieces else np.empty(0, dtype=np.float32)), spans, features_clips


def restore(generated, spans, chunks):
    """Map word times through their own decoder context and retained input spans."""
    packed_starts = [span.packed_start for span in spans]
    # faster-whisper's Segment.seek truncates the context offset at 100 mel frames/s.
    chunk_by_seek = {int(start / ASR_RATE * 100): (start, end) for start, end in chunks}
    if len(chunk_by_seek) != len(chunks):
        raise ValueError("Two decoder chunks start within one model frame")
    for segment in generated:
        try:
            chunk_start, chunk_end = chunk_by_seek[segment.seek]
        except KeyError as error:
            raise ValueError(f"Unknown decoder context seek {segment.seek}") from error
        grouped = []
        for word in segment.words or []:
            if not isfinite(word.start) or not isfinite(word.end) or word.end < word.start:
                continue
            packed_start = round(word.start * ASR_RATE)
            packed_end = round(word.end * ASR_RATE)
            if packed_end < chunk_start or packed_start > chunk_end:
                continue
            middle = (packed_start + packed_end) / 2
            middle = min(chunk_end - 0.5, max(chunk_start, middle))
            index = bisect_right(packed_starts, middle) - 1
            if index < 0 or middle >= spans[index].packed_end:
                continue
            span = spans[index]
            lower = max(chunk_start, span.packed_start)
            upper = min(chunk_end, span.packed_end)
            bounded_start = max(lower, min(upper, packed_start))
            bounded_end = max(lower, min(upper, packed_end))
            start = round(span.local_start + (bounded_start - span.packed_start) / ASR_RATE, 8)
            end = round(span.local_start + (bounded_end - span.packed_start) / ASR_RATE, 8)
            if (not grouped or grouped[-1][0] != span.source_id
                    or start - grouped[-1][1][-1]["end"] > 2.0):
                grouped.append((span.source_id, []))
            grouped[-1][1].append({
                "start": start, "end": end,
                "word": word.word, "probability": word.probability,
                "timing_clipped": (bounded_start, bounded_end) != (packed_start, packed_end),
            })
        for source_id, words in grouped:
            yield {
                "source_id": source_id, "start": min(word["start"] for word in words),
                "end": max(word["end"] for word in words),
                "text": "".join(word["word"] for word in words), "words": words,
                "words_without_duration": sum(word["start"] == word["end"] for word in words),
                "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob,
                "compression_ratio": segment.compression_ratio,
            }


def transcribe_runs(args, audio, spans, chunks, metadata, prepare_seconds, kept_seconds):
    pipeline, model_settings = load_model(args.model, args.device_index)
    model_settings["without_timestamps"] = args.disable_timestamp_tokens
    # Whisper takes clip boundaries in seconds and truncates their sample conversion.
    clip_times = [{"start": (start + 0.25) / ASR_RATE, "end": (end + 0.25) / ASR_RATE}
                  for start, end in chunks]
    source_seconds = sum((source["end_frame"] - source["start_frame"]) / source["sample_rate"]
                         for source in metadata.values())
    timing = {}
    for batch, beam in args.batch_beam:
        name = f"batch{batch}_beam{beam}"
        began = time.perf_counter()
        count = words = words_without_duration = 0
        with (args.output / f"{name}.jsonl").open("x", encoding="utf-8", buffering=1) as out:
            if spans:
                generated, _ = pipeline.transcribe(
                    audio, language="en", beam_size=beam, batch_size=batch, temperature=0.0,
                    word_timestamps=True, without_timestamps=args.disable_timestamp_tokens,
                    condition_on_previous_text=False,
                    clip_timestamps=clip_times, vad_filter=False,
                )
                for count, row in enumerate(restore(generated, spans, chunks), start=1):
                    words += len(row["words"])
                    words_without_duration += row["words_without_duration"]
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
        decode_seconds = time.perf_counter() - began
        total_seconds = decode_seconds + prepare_seconds
        timing[name] = {
            "batch_size": batch, "beam_size": beam, "segments": count, "words": words,
            "words_without_duration": words_without_duration,
            "decode_seconds": decode_seconds, "prepare_decode_emit_seconds": total_seconds,
            "prepare_model_decode_emit_seconds": total_seconds + model_settings["model_load_seconds"],
            "audio_hours_per_processing_hour": source_seconds / total_seconds,
            "archive_hours_at_this_rate": args.archive_hours * total_seconds / source_seconds,
        }
        write_json(args.output / "timing.json", {
            "model": model_settings, "source_seconds": source_seconds,
            "retained_seconds": kept_seconds, "retained_fraction": kept_seconds / source_seconds,
            "packed_seconds": len(audio) / ASR_RATE, "model_chunks": len(chunks),
            "mix_sources": args.mix_sources, "max_vad_gap_seconds": args.max_vad_gap,
            "preparation_seconds": prepare_seconds, "workers": args.workers,
            "vad_threshold": args.vad_threshold,
            "vad_preprocessing": "voice_band_rms" if args.vad_voice_band_rms else None,
            "archive_channel_hours": args.archive_hours, "runs": timing,
        })
        print(f"{name}: {timing[name]['audio_hours_per_processing_hour']:.1f}x, {count} segments", flush=True)


def benchmark(args):
    specs = json.loads(args.inputs.read_text(encoding="utf-8-sig"))
    if not specs or len({row["id"] for row in specs}) != len(specs):
        raise ValueError("Input IDs must be unique and the input list nonempty")
    args.output.mkdir(parents=True)
    began = time.perf_counter()
    sources = [(spec, AudioSource(spec)) for spec in specs]
    metadata = {spec["id"]: source.metadata() for spec, source in sources}
    from faster_whisper.vad import get_vad_model

    if args.vad_threshold is not None:
        get_vad_model()
    vad_transform = None
    if args.vad_voice_band_rms:
        from vad_audio import boost_voice_band

        vad_transform = boost_voice_band
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        prepared = list(pool.map(lambda entry: prepare(entry, args.vad_threshold, vad_transform), sources))
    max_gap_samples = None if args.max_vad_gap is None else round(args.max_vad_gap * ASR_RATE)
    audio, spans, chunks = pack(prepared, mix_sources=args.mix_sources, max_gap_samples=max_gap_samples)
    kept_seconds = sum(w["end"] - w["start"] for _, _, _, windows in prepared for w in windows) / ASR_RATE
    write_json(args.output / "speech_windows.json", {
        source_id: {"start": origin, "end": origin + len(audio_from_source) / ASR_RATE,
                    "windows": [[origin + w["start"] / ASR_RATE, origin + w["end"] / ASR_RATE]
                                for w in windows]}
        for source_id, origin, audio_from_source, windows in prepared
    })
    del prepared
    prepare_seconds = time.perf_counter() - began
    print(f"Prepared {len(sources)} excerpts and {len(chunks)} model chunks in {prepare_seconds:.2f}s", flush=True)
    transcribe_runs(args, audio, spans, chunks, metadata, prepare_seconds, kept_seconds)
    hashes = {}
    hash_start = time.perf_counter()
    for spec, source in sources:
        if metadata[spec["id"]] != source.metadata():
            raise RuntimeError(f"Source changed while processing: {source.path}")
        if "review" in spec:
            review = Path(spec["review"]).resolve(strict=True)
            review_data = json.loads(review.read_text(encoding="utf-8"))
            receipt = review_data["files"][source.path.name]
            if (review.parent != source.path.parent or review_data["status"] != "complete"
                    or receipt["frames"] != source.frames or receipt["sample_rate"] != source.rate):
                raise ValueError(f"Review does not match source format or location: {review}")
            source.verify_sha256(receipt["sha256"])
            metadata[spec["id"]].update(sha256=receipt["sha256"], review=str(review),
                                         review_sha256=source_sha256(review), hash_origin="stage_05b_review")
        else:
            if source.path not in hashes:
                hashes[source.path] = source_sha256(source.path)
            source.verify_sha256(hashes[source.path])
            metadata[spec["id"]].update(sha256=hashes[source.path], hash_origin="computed")
    write_json(args.output / "sources.json", {"hash_seconds": time.perf_counter() - hash_start, "sources": metadata})
    write_json(args.output / "completion.json", {"wall_seconds": time.perf_counter() - began})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--batch-beam", type=int, nargs=2, action="append", required=True,
                        metavar=("BATCH", "BEAM"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--archive-hours", type=float, default=19_191.88)
    parser.add_argument("--disable-timestamp-tokens", action="store_true",
                        help="Align words from decoder attention without emitting timestamp tokens")
    packing = parser.add_mutually_exclusive_group()
    packing.add_argument("--mix-sources", action="store_true",
                         help="Experimental. Allow a decoder context to use audio from multiple inputs")
    packing.add_argument("--max-vad-gap", type=float, metavar="SECONDS",
                         help="Keep shorter intervening gaps in place and start a new clip after larger gaps")
    vad = parser.add_mutually_exclusive_group()
    vad.add_argument("--vad-threshold", type=float, default=0.30)
    vad.add_argument("--no-vad", dest="vad_threshold", action="store_const", const=None)
    parser.add_argument("--vad-voice-band-rms", action="store_true",
                        help="Apply a bandpass and local gain to VAD input, using the original samples for ASR")
    add_cuda_options(parser)
    args = parser.parse_args()
    if args.workers < 1 or args.device_index < 0 or any(min(setting) < 1 for setting in args.batch_beam):
        parser.error("Require positive worker, batch and beam counts and a nonnegative GPU index")
    if args.vad_threshold is not None and not 0 < args.vad_threshold < 1:
        parser.error("VAD threshold must be between zero and one")
    if args.vad_voice_band_rms and args.vad_threshold is None:
        parser.error("Voice-band gain requires VAD")
    if args.max_vad_gap is not None and not 0 <= args.max_vad_gap < 29:
        parser.error("Maximum preserved VAD gap must be in [0, 29) seconds")
    dll_handles = [os.add_dll_directory(str(path.resolve(strict=True))) for path in args.dll_dir]
    benchmark(args)
    for handle in dll_handles:
        handle.close()


if __name__ == "__main__":
    main()
