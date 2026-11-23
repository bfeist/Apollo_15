import csv
from pathlib import Path


from acoustic_search import DT, features, scores, read_audio
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator, FormatStrFormatter
import numpy as np
from scipy import signal

root = Path("runs/mission_timing_pilot/spur_review")
root.mkdir(parents=True, exist_ok=True)
base = Path("D:/! Media Files/A15/Spacecraft Films")
paths = {
    "PAO 572-AAA": Path(
        "N:/Projects/Apollo/Mission Data/Apollo 15/Audio/Internet Archive/572-AAA.wav"
    ),
    "T510092 CH23 05b": Path(
        "D:/Apollo_15_30-Track/defluttered_shared/A15_T510092/CH23/deflutter.wav"
    ),
    "DVD 04.02 part 01": base / "Apollo 15 - Disc 04 - Title 02 - Part 01.mp4",
}
query = paths["PAO 572-AAA"]
searches = {
    "debris": (19, 8, {"T510092 CH23 05b": 60291.3, "DVD 04.02 part 01": 1519.9}),
    "tv_and_spur": (103, 8, {"T510092 CH23 05b": 60293.5, "DVD 04.02 part 01": 1516.7}),
    "rover_readout": (
        112,
        27,
        {"T510092 CH23 05b": 60293.9, "DVD 04.02 part 01": 1516.8},
    ),
    "shadow": (140, 10, {"T510092 CH23 05b": 60294.4, "DVD 04.02 part 01": 1517.1}),
}
for name, (start, length, shifts) in searches.items():
    q = features(read_audio(query, start, length))
    for source, offset in shifts.items():
        w0 = round(start + offset - 4, 2)
        target = features(read_audio(paths[source], w0, length + 8))
        v = scores(target, q)
        k = np.argmax(v)
        best = w0 + k * DT
        mask = np.abs(np.arange(len(v)) - k) > 13
        print(
            name,
            source,
            start,
            round(best, 3),
            round(best - start, 3),
            round(float(v[k]), 4),
            "alt",
            round(float(v[mask].max()), 4),
        )

# Comparable windows. The panels show local audio time without ASR or transcript marks.
spans = {
    "tv": ([102.5, 60396.0, 1619.2], 9.5, 0.5),
    "readout_start": ([110.5, 60404.2, 1627.2], 9.0, 0.5),
    "readout_early_numbers": ([116.0, 60409.86, 1632.78], 12.0, 1.0),
    "readout_late_numbers": ([127.5, 60421.72, 1644.57], 13.0, 1.0),
    "shadow": ([139.5, 60434.0, 1656.55], 9.0, 0.5),
    "debris": ([19.5, 60310.86, 1539.2], 8.0, 0.5),
}
with (root / "visual_crops.csv").open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(
        [
            "panel",
            "source",
            "local_start_s",
            "duration_s",
            "sample_rate_hz",
            "window_samples",
            "overlap_samples",
            "shown_min_hz",
            "shown_max_hz",
        ]
    )
    for name, (start_times, length, sp) in spans.items():
        fig, axes = plt.subplots(3, 1, figsize=(13, 6.1), layout="constrained")
        for ax, (src, path), start in zip(axes, paths.items(), start_times):
            wav = read_audio(path, start, length, rate=8000)
            fq, t, power = signal.spectrogram(wav, 8000, nperseg=144, noverlap=112)
            db = 10 * np.log10(np.maximum(power, 1e-15))
            top = np.percentile(db[(fq >= 250) & (fq < 3200)], 99.7)
            ax.pcolormesh(
                t + start,
                fq,
                db,
                vmin=top - 44,
                vmax=top,
                shading="auto",
                cmap="magma",
                rasterized=True,
            )
            ax.set_title(src, loc="left")
            ax.set_xlim(start, start + length)
            ax.set_ylim(200, 3200)
            ax.xaxis.set_major_locator(MultipleLocator(sp))
            ax.xaxis.set_major_formatter(FormatStrFormatter("%.1f"))
            ax.xaxis.set_minor_locator(MultipleLocator(sp / 5))
            ax.grid(axis="x", linewidth=0.48, alpha=0.5)
            ax.set_ylabel("Hz")
            writer.writerow([name, src, start, length, 8000, 144, 112, 200, 3200])
        axes[-1].set_xlabel("Source-local audio time (s)")
        fig.savefig(root / f"{name}.png", dpi=118)
        plt.close(fig)

# Test close alternatives one phrase at a time. The exploratory centres only
# locate each passage. Neither the transcript stamp nor the visual pick enters
# the similarity score.
short_windows = [
    ("tv", 105.60, 1.45, 60399.10, 1622.22),
    ("go_ahead", 107.30, 1.20, 60400.80, 1623.96),
    ("crater", 109.51, 1.35, 60403.13, 1626.21),
    ("shadow_device", 112.65, 1.80, 60406.30, 1629.40),
    ("initial_numbers", 116.10, 5.00, 60409.80, 1632.80),
    ("late_numbers", 128.00, 8.00, 60422.10, 1645.00),
    ("standing_by", 141.50, 2.00, 60435.97, 1658.54),
]
with (root / "short_phrase_matches.tsv").open(
    "w", newline="", encoding="utf-8"
) as handle:
    writer = csv.writer(handle, dialect="excel-tab", lineterminator="\n")
    writer.writerow(
        [
            "phrase",
            "pao_query_start_s",
            "query_duration_s",
            "source",
            "search_start_s",
            "search_duration_s",
            "source_query_match_s",
            "source_minus_pao_s",
            "score",
            "highest_other_score_outside_0.30_s",
        ]
    )
    for name, start, duration, ch_centre, dvd_centre in short_windows:
        query_features = features(read_audio(query, start, duration))
        centres = (("T510092 CH23 05b", ch_centre), ("DVD 04.02 part 01", dvd_centre))
        for source, centre in centres:
            search_start = round(centre - 3, 3)
            search_duration = duration + 6
            target = features(read_audio(paths[source], search_start, search_duration))
            similarity = scores(target, query_features)
            peak = int(np.argmax(similarity))
            location = search_start + peak * DT
            competitor = similarity[
                np.abs(np.arange(len(similarity)) - peak) > 15
            ].max()
            writer.writerow(
                [
                    name,
                    start,
                    duration,
                    source,
                    search_start,
                    search_duration,
                    round(location, 3),
                    round(location - start, 3),
                    round(float(similarity[peak]), 4),
                    round(float(competitor), 4),
                ]
            )
