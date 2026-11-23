"""Measure VAD retention of checked Spur onsets and occupancy on a tape census."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import time

from benchmark_parakeet import read_excerpt
from transcribe import ASR_RATE, AudioSource, source_sha256, write_json
from vad_audio import boost_voice_band


VAD_PARAMETERS = dict(min_silence_duration_ms=300, speech_pad_ms=400, max_speech_duration_s=28)


def extend_windows(windows, extra, length):
    merged = []
    for left, right in windows:
        left, right = max(0, left - extra), min(length, right + extra)
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def benchmark_suite(path, pool, vad, vad_options):
    specs = json.loads(path.read_text(encoding="utf-8-sig"))
    start = time.perf_counter()
    sources = [(item, AudioSource(item)) for item in specs]
    metadata = {item["id"]: source.metadata() for item, source in sources}
    clips = list(pool.map(read_excerpt, sources))
    source_seconds = {identifier: len(audio) / ASR_RATE for identifier, _, audio in clips}
    io_wall = time.perf_counter() - start
    variants = {}

    def get_windows(clip, options, transform):
        identifier, origin, waveform = clip
        vad_audio = transform(waveform) if transform else waveform
        spans = [(int(row["start"]), int(row["end"])) for row in vad(vad_audio, options)]
        return identifier, origin, spans

    for key, threshold, transform in (
        ("baseline_012", 0.12, None),
        ("threshold_006", 0.06, None),
        ("voice_band_rms_012", 0.12, boost_voice_band),
    ):
        options = vad_options(threshold=threshold, **VAD_PARAMETERS)
        start = time.perf_counter()
        results = list(pool.map(lambda clip: get_windows(clip, options, transform), clips))
        variants[key] = dict(
            vad_and_transform_seconds=time.perf_counter() - start,
            ranges={identifier: (origin, spans) for identifier, origin, spans in results},
        )
    start = time.perf_counter()
    extended_ranges = {
        identifier: (origin, extend_windows(spans, round(1.1 * ASR_RATE), round(source_seconds[identifier] * ASR_RATE)))
        for identifier, (origin, spans) in variants["baseline_012"]["ranges"].items()
    }
    variants["padding_1500ms_012"] = dict(
        vad_and_transform_seconds=variants["baseline_012"]["vad_and_transform_seconds"] + time.perf_counter() - start,
        ranges=extended_ranges,
    )

    for key, variant in variants.items():
        groups = {}
        for identifier, (_, spans) in variant["ranges"].items():
            coverage = sum(right - left for left, right in spans) / ASR_RATE
            group = identifier.rsplit("_", 1)[-1]
            group = group if group in ("early", "middle", "late") else "matched"
            entry = groups.setdefault(group, dict(source_seconds=0, retained_seconds=0))
            entry["source_seconds"] += source_seconds[identifier]
            entry["retained_seconds"] += coverage
            if identifier == "t510093_vad_empty":
                variant["ch30_control_retained_seconds"] = coverage
        variant["retained_seconds"] = sum(row["retained_seconds"] for row in groups.values())
        variant["retained_fraction"] = variant["retained_seconds"] / sum(source_seconds.values())
        for entry in groups.values():
            entry["retained_fraction"] = entry["retained_seconds"] / entry["source_seconds"]
        variant["groups"] = groups
        variant["source_read_and_vad_seconds"] = io_wall + variant["vad_and_transform_seconds"]
        print(f"{path.name} {key}: {variant['retained_fraction']:.2%} retained, "
              f"{variant['source_read_and_vad_seconds']:.3f}s source prep", flush=True)

    for spec, source in sources:
        if source.metadata() != metadata[spec["id"]]:
            raise RuntimeError(f"Source changed during VAD benchmark: {source.path}")
    return dict(source_read_resample_and_probe_seconds=io_wall, source_seconds=sum(source_seconds.values()),
                variants=variants, sources=metadata)


def score_onsets(path, matched, output):
    with path.open(encoding="utf-8", newline="") as handle:
        picks = list(csv.DictReader(handle, delimiter="\t"))
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, dialect="excel-tab", lineterminator="\n")
        writer.writerow(["variant", "event_id", "source_id", "pick_s", "includes_pick", "includes_pick_bounds"])
        for key, variant in matched["variants"].items():
            occurrence_count = bound_count = 0
            source_counts = {}
            for row in picks:
                identifier = row["source_id"]
                window_id = "dvd_disc04_title02_part01" if identifier == "dvd_d04_t02_p01" else identifier
                origin, spans = variant["ranges"][window_id]
                time_s = float(row["observed_local_s"])
                pick, low, high = (round((float(row[field]) - origin) * ASR_RATE)
                                   for field in ("observed_local_s", "local_lower_s", "local_upper_s"))
                present = any(left <= pick < right for left, right in spans)
                bounds = any(left <= low and high < right for left, right in spans)
                writer.writerow([key, row["event_id"], identifier, time_s, present, bounds])
                occurrence_count += present
                bound_count += bounds
                source_counts[identifier] = source_counts.get(identifier, 0) + present
            variant.update(picks_included=occurrence_count, full_pick_bounds_included=bound_count,
                           picks_included_by_source=source_counts, picks=len(picks))
            print(f"{key}: {occurrence_count}/{len(picks)} Spur starts, {bound_count} complete bounds", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("census_inputs", type=Path)
    parser.add_argument("matched_inputs", type=Path)
    parser.add_argument("spur_picks", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    from faster_whisper.vad import VadOptions, get_speech_timestamps, get_vad_model

    start = time.perf_counter()
    args.output.mkdir(parents=True, exist_ok=True)
    get_vad_model()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        matched = benchmark_suite(args.matched_inputs, pool, get_speech_timestamps, VadOptions)
        census = benchmark_suite(args.census_inputs, pool, get_speech_timestamps, VadOptions)
    score_onsets(args.spur_picks, matched, args.output / "spur_onset_inclusion.tsv")
    write_json(args.output / "vad_experiment.json", {
        "inputs_sha256": {str(path): source_sha256(path)
                           for path in (args.census_inputs, args.matched_inputs, args.spur_picks)},
        "vad_defaults": VAD_PARAMETERS, "workers": args.workers, "matched": matched, "census": census,
        "actual_wall_seconds": time.perf_counter() - start,
    })


if __name__ == "__main__":
    main()
