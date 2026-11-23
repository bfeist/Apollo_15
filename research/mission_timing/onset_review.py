"""Render local radio spectrograms for an acoustic review with times hidden.

The renderer uses a reproducible jittered crop around the proposed line.
Neither ASR boundaries nor printed GETs are drawn. The CSV reviewer's
bounds refer to the displayed x axis; retain 'unclear' if ambiguous.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import signal

from acoustic_search import read_audio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = [
        a
        for a in map(json.loads, args.audit.read_text().splitlines())
        if a.get("words")
    ]
    cases.sort(key=lambda a: a["event_id"])
    cases_by_page = [cases[i : i + 8] for i in range(0, len(cases), 8)]
    rows = []
    for page, cases in enumerate(cases_by_page, start=1):
        fig, axes = plt.subplots(
            len(cases), 1, figsize=(13, len(cases) * 1.4), squeeze=False
        )
        for ax, case in zip(axes[:, 0], cases):
            first = case["words"][0]
            key = hashlib.sha256(case["event_id"].encode()).hexdigest()
            lead = 0.8 + (int(key[:4], 16) % 130) / 100
            origin = round((first["emission_start"] - lead) * 100) / 100
            x = read_audio(args.audio, origin, 4, rate=8000)
            freqs, times, powers = signal.spectrogram(x, 8000, nperseg=128, noverlap=96)
            db = 10 * np.log10(np.maximum(powers, 1e-15))
            top = np.percentile(db, 99.7)
            ax.pcolormesh(
                times, freqs, db, vmin=top - 39, vmax=top, shading="auto", cmap="magma"
            )
            ax.set_ylim(160, 3200)
            ax.set_yticks([])
            ax.set_xticks(np.arange(0, 4.01, 0.2))
            ax.grid(axis="x", linewidth=0.4, alpha=0.45)
            ax.set_title(
                case["event_id"] + "    " + " ".join(w["word"] for w in case["words"]),
                loc="left",
                fontsize=10,
            )
            rows.append(
                dict(anchor_id=case["event_id"], panel=page, crop_origin_s=origin)
            )
        fig.tight_layout()
        fig.savefig(args.output / f"onsets_{page}.png", dpi=105)
        plt.close(fig)
    with (args.output / "crops.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
