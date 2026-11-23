"""Check the brief repeated PAO passage and fit the two sides independently."""

import argparse
import csv
import json
from pathlib import Path

import numpy as np
from scipy import signal

from acoustic_search import read_audio
from fit_pao import analyze, parse_args, read_anchors, write_predictions
from pao_timing_report import write_report
from transfer_calls import file_hash


def repeat_measurements(pao):
    rate = 8000
    audio = read_audio(pao, 718, 23, rate)
    sos = signal.butter(4, [350, 2750], btype="bandpass", fs=rate, output="sos")
    audio = signal.sosfiltfilt(sos, audio)
    measurements = []
    for start in np.arange(722, 735, 0.5):
        pos = round((start - 718) * rate)
        query = audio[pos : pos + rate // 2]
        target = audio[pos + round(3.8 * rate) : pos + 5 * rate + len(query)]
        dot = signal.correlate(target, query, mode="valid", method="fft")
        power = signal.correlate(
            target**2, np.ones(len(query)), mode="valid", method="fft"
        )
        corr = dot / np.sqrt(np.maximum(power * np.sum(query**2), 1e-24))
        best = int(np.argmax(abs(corr)))
        measurements.append(
            dict(
                first_start_s=start,
                lag_s=3.8 + best / rate,
                signed_correlation=corr[best],
            )
        )
    return measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("audio", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    root = args.run / "pao_edit"
    root.mkdir(parents=True, exist_ok=True)
    picks, _, sha = read_anchors(args.run / "pao_anchors.csv")
    audio_sha = file_hash(args.audio)
    if any(a.row["source_sha256"] != audio_sha for a in picks):
        raise ValueError("PAO edition differs from the reviewed source")
    measurements = repeat_measurements(args.audio)
    interior = [m for m in measurements if 726 <= m["first_start_s"] <= 729.5]
    lags = [m["lag_s"] for m in interior]
    if (
        np.ptp(lags) > 0.003
        or min(abs(m["signed_correlation"]) for m in interior) < 0.25
    ):
        raise ValueError("Repeated-waveform check failed on this edition")
    lag = float(np.median(lags))
    # Bound the uncertain join conservatively. The source keeps both occurrences.
    before = [a for a in picks if a.source < 724]
    after = [a for a in picks if a.source > 736]
    subsets = {"before": before, "after": after}
    result = dict(
        audio_sha256=audio_sha,
        input_anchor_sha256=sha,
        waveform_matches=measurements,
        observed_lag_s=lag,
        unmapped_source_interval_s=[724, 736],
        portions={},
    )
    for name, subset in subsets.items():
        directory = root / name
        directory.mkdir(exist_ok=True)
        with (directory / "anchors.csv").open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=subset[0].row)
            writer.writeheader()
            writer.writerows(a.row for a in subset)
        items, exc, checksum = read_anchors(directory / "anchors.csv")
        # Keep the original source clock and reserve exactly the original late exchanges.
        opts = parse_args(
            [
                str(directory / "anchors.csv"),
                "--out-dir",
                str(directory),
                "--source-id",
                f"572-AAA {name} repeat",
                "--duration-seconds",
                str(11699.323708333333 if name == "after" else 724),
                "--holdout-start-seconds",
                "9360",
            ]
        )
        if name == "before":
            # This short span contains no prespecified late exchange.
            result["portions"][name] = dict(
                calls=len(items),
                local_source_s=[items[0].source, items[-1].source],
                printed_get_s=[items[0].get, items[-1].get],
                rate_status="no late holdout",
                median_printed_minus_source_s=float(
                    np.median([a.get - a.source for a in items])
                ),
            )
            continue
        train, late, candidates, report = analyze(opts, items, exc, checksum)
        report["source_portion_start_seconds"] = 736.0
        # Exclude the earlier edition portion from the rendered coverage gaps.
        gaps = report["gaps"]
        for gap in gaps:
            if gap["kind"] == "unanchored reel start":
                gap["source_interval_seconds"][0] = 736.0
        (directory / "pao_timing_metrics.json").write_text(json.dumps(report, indent=2))
        write_predictions(
            directory / "pao_anchor_predictions.csv", train, late, candidates, exc
        )
        write_report(args.report, directory, train, late, candidates, report)
        result["portions"][name] = dict(
            calls=len(items),
            selected=report["selected_candidate"],
            local_source_s=[items[0].source, items[-1].source],
            printed_get_s=[items[0].get, items[-1].get],
            models={
                k: {
                    "blocked": c.blocked_score,
                    "late": c.late_score,
                    "refit": c.final_map.describe(),
                }
                for k, c in candidates.items()
            },
        )
    (root / "repeat_and_fits.json").write_text(json.dumps(result, indent=2))
    print(
        f"Repeated waveform {lag:.5f} s; {len(before)} pre-repeat and {len(after)} post-repeat picks"
    )


if __name__ == "__main__":
    main()
