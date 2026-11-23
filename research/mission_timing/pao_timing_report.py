"""Write the PAO pilot's review plot and summary from the scored candidates."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import numpy as np


COLORS = {"offset": "#a44f14", "affine": "#00728a", "smooth": "#705394"}
LABELS = {"offset": "Offset only", "affine": "Constant rate", "smooth": "Smooth rate"}
DIRECTIONS = {"uplink": "#005f85", "downlink": "#b5491a", "unknown": "#626a7c"}


def clock(value, seconds=True):
    hour, remainder = divmod(int(np.floor(value)), 3600)
    minute, second = divmod(remainder, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}" if seconds else f"{hour:02d}:{minute:02d}"


def interval(values):
    return "–".join(dict.fromkeys(clock(value) for value in values))


def write_plot(path, train, late, candidates, summary):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter, MaxNLocator, MultipleLocator

    duration = summary["source_duration_seconds"]
    selected = summary["selected_candidate"]
    chosen = candidates[selected]
    reference = candidates.get("affine", chosen).training_map
    all_source = np.array([anchor.source for anchor in train + late])
    all_get = np.array([anchor.get for anchor in train + late])
    x = reference.predict(all_source)
    reel_get = reference.predict([summary.get("source_portion_start_seconds", 0), duration])
    figure, panels = plt.subplots(3, 1, figsize=(12, 8.75), sharex=True, layout="constrained")
    figure.suptitle(f"PAO reel {summary['source_id']} · transcript timing", fontsize=16,
                    fontweight="bold", x=0.07, ha="left")
    for axis in panels:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", color="#e0e4e7", linewidth=0.7, zorder=0)
        axis.axhline(0, color="#767c85", linewidth=0.8, zorder=1)
        axis.yaxis.set_major_locator(MaxNLocator(6, symmetric=True))
        for gap in summary["gaps"]:
            axis.axvspan(*reference.predict(gap["source_interval_seconds"]), color="#e6e8eb", zorder=-1)
        if summary["holdout_start_source_seconds"] is not None:
            boundary = reference.predict(summary["holdout_start_source_seconds"])
            axis.axvline(boundary, linestyle="--", color="#6c7480", linewidth=1, zorder=2)
        axis.set_xlim(reel_get[0], reel_get[1])

    axis = panels[0]
    for name, candidate in candidates.items():
        model = candidate.training_map
        errors = model.predict(all_source) - all_get
        axis.scatter(x[:len(train)], errors[:len(train)], color=COLORS[name], s=19, alpha=0.66,
                     linewidths=0, zorder=3)
        if late:
            axis.scatter(x[len(train):], errors[len(train):], edgecolors=COLORS[name], facecolors="none",
                         marker="D", s=35, linewidths=1.15, zorder=4)
    axis.set_title("Training-map residuals by candidate", loc="left", fontweight="bold")
    axis.set_ylabel("Predicted − printed GET (s)")
    axis.legend(handles=[Line2D([], [], color=COLORS[name], marker="o", linestyle="none", label=LABELS[name])
                         for name in candidates], loc="upper left", ncols=3, frameon=False)

    axis = panels[1]
    scores = np.r_[chosen.blocked_errors, chosen.late_errors] if late else chosen.blocked_errors
    sigmas = np.array([anchor.sigma for anchor in train + late])
    directions = np.array([anchor.direction for anchor in train + late])
    has_score = np.isfinite(scores)
    late_mask = np.arange(len(x)) >= len(train)
    direction_legend = []
    for direction in sorted(set(directions[has_score])):
        color = DIRECTIONS.get(direction, "#626a7c")
        direction_legend.append(Line2D([], [], color=color, marker="o", linestyle="none", label=direction.title()))
        for held in (False, True):
            mask = has_score & (directions == direction) & (late_mask == held)
            axis.errorbar(x[mask], scores[mask], sigmas[mask], color=color, ecolor="#aab0b7", alpha=0.87,
                          markersize=5, linestyle="none", marker="D" if held else "o",
                          markerfacecolor="white" if held else color, capsize=2, zorder=4)
    if direction_legend:
        axis.legend(handles=direction_legend, loc="upper left", ncols=4, frameon=False)
    axis.set_title(f"{LABELS[selected]} · out-of-sample errors by direction", loc="left", fontweight="bold")
    axis.set_ylabel("Predicted − printed GET (s)")

    axis = panels[2]
    comparison = reference if "smooth" in candidates else candidates["offset"].training_map
    for name, candidate in candidates.items():
        model = candidate.training_map
        if name not in {"affine", "smooth"}:
            continue
        for lower, upper, style in ((0, model.start, ":"), (model.start, model.stop, "-"),
                                    (model.stop, duration, ":")):
            domain = np.linspace(lower, upper, 180)
            if lower != upper:
                axis.plot(reference.predict(domain), model.predict(domain) - comparison.predict(domain),
                          color=COLORS[name], linestyle=style, linewidth=1.9, zorder=3)
    axis.plot(x, np.full(x.shape, 0.028), "|", transform=axis.get_xaxis_transform(),
              color="#444d5a", markersize=9, clip_on=False, zorder=4)
    axis.set_title("Timing correction relative to the constant-rate map" if "smooth" in candidates
                   else "Timing correction relative to the offset-only map", loc="left", fontweight="bold")
    if "affine" not in candidates:
        axis.set_title("Anchor coverage", loc="left", fontweight="bold")
    axis.set_ylabel("Difference (s)")
    width = reel_get[1] - reel_get[0]
    ticks = next((size for size in [30, 60, 120, 300, 600, 900, 1800, 3600, 7200] if width / size <= 9), 14400)
    axis.set_xlabel(f"Ground elapsed time ({'hours:minutes:seconds' if ticks < 60 else 'hours:minutes'}, positioned on the reference map)")
    axis.xaxis.set_major_locator(MultipleLocator(ticks))
    axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _: clock(value, seconds=ticks < 60)))
    axis.legend(handles=[
        Line2D([], [], color="#475261", linewidth=0, marker="D", markersize=5, markerfacecolor="white", label="Late test"),
        Line2D([], [], color="#6c7480", linestyle="--", label="Late reserve cutoff"),
        Line2D([], [], color="#475261", linestyle=":", label="Outside training fit"),
        Patch(facecolor="#e6e8eb", label="Anchor gap"),
        Line2D([], [], color="#475261", linewidth=0, marker="|", markersize=8, label="Anchor"),
    ], loc="upper left", ncols=5, frameon=False)
    figure.savefig(path, dpi=160, facecolor="white")
    plt.close(figure)


def measure(value, signed=False):
    return "—" if value is None else f"{value:+.3f}" if signed else f"{value:.3f}"


def score_cells(score):
    if score is None:
        return ["—"] * 4
    return [measure(score[key], signed=key == "early_to_late_signed_drift_seconds") for key in (
        "median_absolute_seconds", "max_absolute_seconds", "p90_exchange_median_absolute_seconds",
        "early_to_late_signed_drift_seconds",
    )]


def link_path(path, report_dir):
    return quote(Path(os.path.relpath(path, report_dir)).as_posix(), safe="/")


def write_report(report_dir, out_dir, train, late, candidates, summary):
    report_dir.mkdir(parents=True, exist_ok=True)
    plot = report_dir / "pao_timing_error.png"
    write_plot(plot, train, late, candidates, summary)
    selected = summary["selected_candidate"]
    chosen = candidates[selected]
    disposition = "Provisional choice" if summary["selection_status"] == "provisional" else "Selected candidate"
    lines = [
        f"# PAO reel {summary['source_id']} mission timing",
        "",
        f"**{LABELS[selected]}** · {disposition}. "
        f"The analysis used {summary['accepted_calls']} calls in {summary['accepted_exchanges']} exchanges. "
        f"The working timing uncertainty was {summary['median_working_sigma_seconds']:.3f} s per observation.",
        "",
        f"[Anchor table and predictions]({link_path(out_dir / 'pao_anchor_predictions.csv', report_dir)}) · "
        f"[Candidate maps and validation data]({link_path(out_dir / 'pao_timing_metrics.json', report_dir)})",
        "",
        f"![Timing residuals through PAO reel {summary['source_id']}]({plot.name})",
        "",
        "## Validation",
        "",
    ]
    if late:
        count = len(summary["holdout_exchanges"])
        lines += [
            f"The late test held out {count} entire {'exchange' if count == 1 else 'exchanges'} at printed GET "
            f"{interval([min(anchor.get for anchor in late), max(anchor.get for anchor in late)])}. "
            f"The reserve started at reel elapsed {clock(summary['holdout_start_source_seconds'])}. "
            f"Development used {len(summary['training_exchanges'])} exchanges in "
            f"{len(summary['development_blocks'])} consecutive time blocks.",
            "",
        ]
    if summary["source_manifest"]:
        manifest = Path(summary["source_manifest"]["path"])
        lines += [f"The reel duration and source sample rate agree with the audio edition used for transcription. "
                  f"[Audio provenance]({link_path(manifest, report_dir)})", ""]
    lines += [
        "The median is taken across per-exchange absolute medians. The maximum includes every scored call. "
        "Signed drift is the late-third median residual minus the early-third median residual for the scored partition. "
        "The p90 is an empirical error statistic from the scored exchanges.",
        "",
        "| Candidate | Test | Median absolute (s) | Max absolute (s) | P90 absolute (s) | Signed drift (s) |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name, candidate in candidates.items():
        for label, value in (("Blocked development", candidate.blocked_score), ("Forward late block", candidate.late_score)):
            lines.append("| " + " | ".join([LABELS[name], label, *score_cells(value)]) + " |")
    lines.append("")
    for name, reason in summary["selection"].items():
        lines.append(f"- **{LABELS[name]}.** {reason}")
    if summary["smooth_trials"]:
        trial_cells = []
        for trial in summary["smooth_trials"]:
            score = trial["blocked_score"]
            error = f"{score['median_absolute_seconds']:.3f} s" if score else "unavailable"
            trial_cells.append(f"{trial['period_minutes']:g} min ({error})")
        lines.extend(["", "Approximate half-amplitude periods tested in development, with blocked median absolute "
                      "error: " + ", ".join(trial_cells) + "."])

    lines += [
        "", "## Map coverage", "",
        f"The printed anchors span GET {interval(summary['anchor_printed_get_interval_seconds'])}. "
        "Training support spans anchors available for the forward test. The full refits extend the calibration "
        "through the late anchors after scoring. The gap table shows where the anchors leave the rate shape uncertain.",
        "",
        "| Candidate | Training GET span | Full refit GET span | Full refit rate adjustment (ppm) | GET gained over full fitted span (s) |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for name, candidate in candidates.items():
        training = candidate.training_map.describe()
        final = candidate.final_map.describe() if candidate.final_map else None
        if final:
            low, high = final["rate_get_seconds_per_source_second_range"]
            ppm = " to ".join(f"{(rate - 1) * 1e6:+.1f}" for rate in (low, high)) if low != high else f"{(low - 1) * 1e6:+.1f}"
            end_interval = interval(final["fit_get_interval_seconds"])
            elapsed_change = measure(final["get_elapsed_minus_source_elapsed_seconds"], signed=True)
        else:
            ppm = end_interval = elapsed_change = "—"
        lines.append(f"| {LABELS[name]} | {interval(training['fit_get_interval_seconds'])} | {end_interval} | {ppm} | {elapsed_change} |")

    gaps = summary["gaps"]
    if gaps:
        lines += ["", "| Gap | Reel elapsed | Approximate GET | Length (min) |", "| --- | --- | --- | ---: |"]
        reference = chosen.final_map or chosen.training_map
        for gap in gaps:
            start, stop = gap["source_interval_seconds"]
            lines.append(f"| {gap['kind'].capitalize()} | {interval([start, stop])} | {interval(reference.predict([start, stop]))} | {(stop - start) / 60:.1f} |")
    if summary["duration_from_last_anchor"]:
        lines += ["", "The reel duration was inferred from the last supplied anchor. End coverage requires the media duration."]
    lines += ["", "## Direction and local scatter", ""]
    directions = summary["out_of_sample_by_direction"]
    if directions:
        lines += [
            f"The table uses {LABELS[selected].lower()} out-of-sample predictions. It pools blocked development "
            "and the late forward test. The final column compares residuals for neighboring same-direction calls "
            "within an exchange and within 90 s. Direction medians can also reflect which reel intervals each direction covers.",
            "",
            "| Direction | Calls | Exchanges | Median signed (s) | Neighbor pairs | Median absolute pair difference (s) |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for direction, value in directions.items():
            lines.append(f"| {direction.capitalize()} | {value['calls']} | {value['exchanges']} | "
                         f"{measure(value['median_signed_seconds'], signed=True)} | "
                         f"{value['same_exchange_neighbor_pairs_at_most_90_seconds']} | "
                         f"{measure(value['neighbor_difference_median_absolute_seconds'])} |")
        paired = summary["paired_out_of_sample_uplink_downlink"]
        if paired:
            lines += ["", f"Within the {paired['exchanges']} exchanges with both directions, the median uplink "
                      f"residual minus the median downlink residual was {paired['median_uplink_minus_downlink_seconds']:+.3f} s."]
    else:
        lines.append("The available exchanges did not support out-of-sample direction statistics.")
    lines += [
        "",
        "Each residual compares an audible landmark with a whole-second printed timestamp. "
        "Any constant bias shared across the transcript and recording path is absorbed into the origin.",
        "",
    ]
    (report_dir / "PAO_TIMING.md").write_text("\n".join(lines), encoding="utf-8")
