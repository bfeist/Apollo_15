"""Join visually picked onset brackets to their edition, printed row and ASR evidence."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np


def rows(path, delimiter=","):
    with path.open(encoding="utf-8-sig") as file:
        return list(csv.DictReader(file, delimiter=delimiter))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("catalog"))
    args = parser.parse_args()
    base = args.run
    reviewed = {
        r["event_id"]: r
        for r in rows(args.catalog / "pao_572aaa_transcript_reviews.tsv", "\t")
    }
    crops = {r["anchor_id"]: r for r in rows(base / "onset_review/crops.csv")}
    audited = {
        r["event_id"]: r
        for r in map(
            json.loads, (base / "acoustic_audit.jsonl").read_text().splitlines()
        )
    }
    manifest = json.loads((base / "asr/pao_572aaa_large_v3/manifest.json").read_text())
    source = manifest["source"]
    anchors = []
    for pick in rows(args.catalog / "pao_572aaa_onset_picks.csv"):
        key = pick["anchor_id"]
        lo, hi = float(pick["crop_start_bound_s"]), float(pick["crop_end_bound_s"])
        if not 0 <= lo <= hi <= 4:
            raise ValueError(f"Review outside the displayed window: {key}")
        origin = float(pick["crop_origin_s"])
        if (
            origin != float(crops[key]["crop_origin_s"])
            or pick["source_sha256"] != source["sha256"]
        ):
            raise ValueError(f"Audio edition or crop changed for reviewed pick {key}")
        sample = round((origin + (lo + hi) / 2) * source["sample_rate"])
        r, word = reviewed[key], audited[key]["words"][0]
        anchors.append(
            dict(
                anchor_id=key,
                exchange_id=r["exchange_group"],
                source_seconds=sample / source["sample_rate"],
                source_sample=sample,
                get_seconds=r["get_seconds"],
                direction=r["direction"],
                speaker=r["printed_speaker"],
                pdf_page=r["pdf_page"],
                printed_page=r["printed_page"],
                printed_stamp=r["printed_stamp"],
                transcript_locator=r["scan_url"],
                text=r["printed_text"],
                landmark=pick["landmark_note"],
                source_path="Audio/Internet Archive/572-AAA.wav",
                source_sha256=source["sha256"],
                channel="equal stereo mean",
                sample_rate=source["sample_rate"],
                onset_lo_s=origin + lo,
                onset_hi_s=origin + hi,
                pick_sigma_seconds=0.25,
                transcript_sigma_seconds=1.0,
                whisper_start=word["whisper_start"],
                ctc_start=word["emission_start"],
                status="verified",
            )
        )
    with (base / "pao_anchors.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=anchors[0])
        writer.writeheader()
        writer.writerows(anchors)
    by_engine = {}
    for engine in ["whisper_start", "ctc_start"]:
        errors = np.array([r[engine] - r["source_seconds"] for r in anchors])
        by_engine[engine] = dict(
            count=len(errors),
            median_signed_s=float(np.median(errors)),
            median_abs_s=float(np.median(abs(errors))),
            p90_abs_s=float(np.percentile(abs(errors), 90)),
            max_abs_s=float(np.max(abs(errors))),
            within_250ms=int(sum(abs(errors) <= 0.25)),
        )
    by_engine["anchors_sha256"] = hashlib.sha256(
        (base / "pao_anchors.csv").read_bytes()
    ).hexdigest()
    (base / "boundary_accuracy.json").write_text(json.dumps(by_engine, indent=2))
    print(json.dumps(by_engine, indent=2))


if __name__ == "__main__":
    main()
