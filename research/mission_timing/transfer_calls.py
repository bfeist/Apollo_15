"""Locate the same reviewed PAO line independently on 05b and DVD audio.

Use two neighboring acoustic queries with separate searches and an unrelated
control. The coarse origins and rates only specify where to read audio.
Accepted timestamps are estimated from matched features in the target file.
"""

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage

from acoustic_search import features, find_peak, read_audio


def measure(target, query, rate):
    stretched = ndimage.zoom(query, (rate, 1), order=1, prefilter=False)
    return find_peak(target, stretched)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_target(name, path, offset, rate, duration, reference, anchors, output):
    checks, accepted = [], []
    review_path = path.with_name("review.json")
    if path.suffix.lower() == ".wav":
        review = json.loads(review_path.read_text())
        info = sf.info(path)
        if (
            review["status"] != "complete"
            or review["files"][path.name]["frames"] != info.frames
        ):
            raise ValueError(f"05b edition mismatch for {path}")
        duration = info.duration
        binding = dict(
            source_path="/".join(path.parts[-4:]),
            source_sha256=review["files"][path.name]["sha256"],
            review_sha256=file_hash(review_path),
            map_sha256=review["map_sha256"],
            canonical_origin_s=review["start_seconds"],
        )
        sample_rate = info.samplerate
    else:
        data = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=sample_rate,start_time,duration",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )
        stream = data["streams"][0]
        sample_rate = int(stream["sample_rate"])
        duration = float(stream["duration"])
        binding = dict(
            source_path="Spacecraft Films/" + path.name,
            source_sha256=file_hash(path),
            stream="a:0",
            stream_pts_origin_s=float(stream["start_time"]),
        )
        if binding["stream_pts_origin_s"] != 0:
            raise ValueError(
                "Pilot DVD transfer expects a zero stream presentation origin"
            )
    for row in anchors:
        q = float(row["source_seconds"])
        center = offset + q * rate
        radius = 90 + 0.012 * q
        start = max(0, np.floor(center - radius))
        end = min(duration, np.ceil(center + radius))
        if end - start < 15 or not -30 < center < duration + 30:
            continue
        target = features(read_audio(path, float(start), float(end - start)))
        # Independent windows overlap by less than one syllable. Neither
        # contains the same material as the distant control in this target window.
        results = []
        control_lead = 4800.0 if q < 4800.0 else -4800.0
        for lead, length in [(-0.24, 3.0), (2.60, 3.2), (control_lead, 3.0)]:
            query = features(read_audio(reference, q + lead, length))
            lag, score, other = measure(target, query, rate)
            results.append(
                dict(
                    onset=float(start + lag - lead * rate)
                    if lead != control_lead
                    else None,
                    matched_window_start_s=float(start + lag),
                    pao_query_start_s=q + lead,
                    score=score,
                    other=other,
                )
            )
        first, second, control = results
        delta = abs(first["onset"] - second["onset"])
        ok = (
            delta <= 0.20
            and min(first["score"], second["score"]) >= 0.37
            and all(r["score"] - r["other"] >= 0.035 for r in [first, second])
            and max(first["score"], second["score"]) > control["score"] + 0.065
        )
        checks.append(
            dict(
                event=row["anchor_id"],
                pao_seconds=q,
                query_peaks=results,
                disagreement_s=delta,
                qualified=ok,
            )
        )
        if ok:
            sample = round(first["onset"] * sample_rate)
            time = sample / sample_rate
            copied = {
                key: row[key]
                for key in [
                    "anchor_id",
                    "exchange_id",
                    "get_seconds",
                    "direction",
                    "speaker",
                    "printed_page",
                    "pdf_page",
                    "printed_stamp",
                    "transcript_locator",
                    "text",
                    "transcript_sigma_seconds",
                ]
            }
            accepted.append(
                dict(
                    **copied,
                    **binding,
                    source_seconds=time,
                    source_sample=sample,
                    sample_rate=sample_rate,
                    channel=name,
                    canonical_seconds=(
                        time + binding["canonical_origin_s"]
                        if "canonical_origin_s" in binding
                        else ""
                    ),
                    reference_equivalent_status="own feed only",
                    pao_seconds=q,
                    landmark="acoustic match of PAO visual onset",
                    pick_sigma_seconds=0.35,
                    status="accepted",
                )
            )
    folder = output / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transfer.json").write_text(
        json.dumps(
            dict(
                source=binding,
                search_offset=offset,
                search_rate=rate,
                duration_s=duration,
                checks=checks,
            ),
            indent=2,
        )
    )
    if accepted:
        with (folder / "anchors.csv").open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=accepted[0])
            writer.writeheader()
            writer.writerows(accepted)
    print(f"{name}: {len(accepted)} qualified of {len(checks)} examined", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("pao", type=Path)
    parser.add_argument("tapes", type=Path, help="05b collection root")
    parser.add_argument("dvd", type=Path, help="Spacecraft Films directory")
    args = parser.parse_args()
    with (args.run / "pao_anchors.csv").open(encoding="utf-8") as stream:
        anchors = list(csv.DictReader(stream))
    targets = [
        (
            "T510093_CH18",
            args.tapes / "A15_T510093/CH18/deflutter.wav",
            37234,
            1.03,
            None,
        ),
        (
            "T510093_CH20",
            args.tapes / "A15_T510093/CH20/deflutter.wav",
            37234,
            1.03,
            None,
        ),
        (
            "T510092_CH23",
            args.tapes / "A15_T510092/CH23/deflutter.wav",
            60291,
            1.027,
            None,
        ),
        (
            "DVD04_02_02",
            args.dvd / "Apollo 15 - Disc 04 - Title 02 - Part 02.mp4",
            -395,
            1.004,
            2416.54,
        ),
    ]
    for name, path, offset, rate, duration in targets:
        run_target(
            name,
            path,
            offset,
            rate,
            duration,
            args.pao,
            anchors,
            args.run / "cross_source",
        )


if __name__ == "__main__":
    main()
