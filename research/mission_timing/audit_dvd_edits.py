"""Sweep the pilot's DVD passages for changes in relative timing.

The checks use the same PAO speech windows against the DVD and T510093 CH18.
Each positive match requires two adjacent acoustic queries. Intervals without
matches remain untested. An interval difference alone cannot identify an edit
on a particular source.
"""

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from acoustic_search import DT, features, read_audio
from transfer_calls import file_hash, measure


def paired_hit(bank, bank_start, expected, rate, queries, radius):
    found = []
    for shift, query in queries:
        point = expected + shift * rate
        lo = max(0, int((point - bank_start - radius) / DT))
        hi = min(len(bank), int((point - bank_start + radius + 4) / DT))
        if hi - lo <= len(query) * rate + 20:
            return None
        lag, score, other = measure(bank[lo:hi], query, rate)
        if other is None or score < 0.50 or score - other < 0.08:
            return None
        found.append((bank_start + lo * DT + lag - shift * rate, score))
    if abs(found[0][0] - found[1][0]) > 0.20:
        return None
    return (found[0][0] + found[1][0]) / 2


def plot_windows(rows, path):
    from matplotlib import pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(10, 6.8), layout="constrained")
    fig.suptitle(
        "Disc 04, Title 02 · continuity of matched audio",
        fontsize=16,
        fontweight="bold",
    )
    for part, ax in enumerate(axes, start=1):
        pairs = [
            r for r in rows if int(r["part"]) == part and r["dvd_s"] and r["tape_s"]
        ]
        x = np.array([float(r["pao_s"]) for r in pairs])
        for key, slope, label, color in [
            ("dvd_s", 1.004, "DVD", "#af4f28"),
            ("tape_s", 1.03, "T510093 CH18", "#176c80"),
        ]:
            values = np.array([float(r[key]) for r in pairs]) - slope * x
            ax.scatter(
                x / 60,
                values - values[0],
                s=26 if key == "dvd_s" else 8,
                label=label,
                edgecolors=color,
                facecolors="none" if key == "dvd_s" else color,
                linewidths=1,
            )
        ax.set_title(f"Part {part:02}", loc="left")
        ax.set_ylabel("Timing departure (s)")
        ax.set_xlabel("Elapsed time on PAO reel 572-AAA (min)")
        ax.grid(axis="y", alpha=0.25)
        ax.legend(frameon=False, loc="lower left")
        ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pao", type=Path)
    parser.add_argument("tape", type=Path)
    parser.add_argument("dvd_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--plot", type=Path, help="Optional two-panel plot of matched departures"
    )
    args = parser.parse_args()
    # Search only the pilot overlap. Read the large PCM reel in bounded blocks.
    reference = np.concatenate(
        [read_audio(args.pao, t, 30) for t in range(0, 1800, 30)]
    )
    rate = 4000
    tape_start = 37100
    tape = features(read_audio(args.tape, tape_start, 2100))
    rows = []
    sources = {}
    for part, first, last, intercept, end in [
        (1, 4, 370, 1510, 1856),
        (2, 400, 1780, -396, 1500),
    ]:
        path = (
            args.dvd_directory / f"Apollo 15 - Disc 04 - Title 02 - Part {part:02}.mp4"
        )
        sources[path.name] = file_hash(path)
        bank = features(read_audio(path, 0, end))
        for t in range(first, last, 4):
            queries = []
            for shift in (0.0, 3.2):
                start = round((t + shift) * rate)
                queries.append((shift, features(reference[start : start + 3 * rate])))
            dvd = paired_hit(bank, 0, intercept + 1.004 * t, 1.004, queries, 70)
            tape_time = paired_hit(
                tape, tape_start, 37234 + 1.03 * t, 1.03, queries, 24
            )
            rows.append(
                dict(disc=4, title=2, part=part, pao_s=t, dvd_s=dvd, tape_s=tape_time)
            )
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "windows.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    candidates = []
    for part in (1, 2):
        paired = [
            r
            for r in rows
            if r["part"] == part and r["dvd_s"] is not None and r["tape_s"] is not None
        ]
        for a, b in zip(paired, paired[1:]):
            dt = b["pao_s"] - a["pao_s"]
            dvd_jump = (b["dvd_s"] - a["dvd_s"]) - 1.004 * dt
            tape_jump = (b["tape_s"] - a["tape_s"]) - 1.03 * dt
            if abs(dvd_jump) >= 0.8:
                candidates.append(
                    dict(
                        part=part,
                        pao_bracket=[a["pao_s"], b["pao_s"]],
                        dvd_bracket=[a["dvd_s"], b["dvd_s"]],
                        dvd_departure_s=dvd_jump,
                        tape_departure_s=tape_jump,
                        unmatched_span_s=dt - 6.2,
                    )
                )
    sources[args.pao.name] = file_hash(args.pao)
    sources["T510093/CH18/" + args.tape.name] = file_hash(args.tape)
    (args.output / "intervals.json").write_text(
        json.dumps(
            dict(
                source_sha256=sources,
                query_duration_s=3,
                query_separation_s=3.2,
                step_s=4,
                source_rates=dict(dvd=1.004, tape=1.03),
                examined=len(rows),
                both_matched=sum(
                    r["dvd_s"] is not None and r["tape_s"] is not None for r in rows
                ),
                candidate_intervals=candidates,
            ),
            indent=2,
        )
    )
    if args.plot:
        plot_windows(rows, args.plot)
    print(json.dumps(candidates, indent=2))


if __name__ == "__main__":
    main()
