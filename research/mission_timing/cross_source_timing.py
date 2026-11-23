"""Compare the Apollo 15 pilot's matched PAO, 05b and DVD speech timing."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from acoustic_search import DT, RATE, features, read_audio
from fit_pao import analyze, fit, group_indices, read_anchors, write_predictions
from pao_timing_report import interval, link_path
from transfer_calls import file_hash, measure


PRIMARY = "T510093_CH18"
CONSISTENCY = "T510093_CH20"
OVERLAP = "T510092_CH23"
DVD = "DVD04_02_02"
HOLDOUTS = {PRIMARY: 37234 + 1.03 * 9360, OVERLAP: 60291 + 1.027 * 1190}
LABELS = {PRIMARY: "T510093 CH18", CONSISTENCY: "T510093 CH20", OVERLAP: "T510092 CH23", DVD: "DVD 04-02-02"}


@dataclass
class Transfer:
    anchors: list
    pao: np.ndarray
    source: dict
    sample: np.ndarray
    duration: float
    audio: Path
    anchor_sha256: str
    transfer_sha256: str

    @property
    def positions(self):
        return np.array([anchor.source for anchor in self.anchors])

    def describe(self):
        return {
            "source": self.source, "audio_duration_seconds": self.duration,
            "calls": len(self.anchors), "exchanges": len(group_indices(self.anchors)),
            "pao_interval_seconds": self.pao[[0, -1]].tolist(),
            "target_interval_seconds": self.positions[[0, -1]].tolist(),
            "local_samples": self.sample[[0, -1]].tolist(),
            "printed_get_interval_seconds": [min(anchor.get for anchor in self.anchors), max(anchor.get for anchor in self.anchors)],
            "target_elapsed_minus_pao_elapsed_seconds": float(np.diff(self.positions[[0, -1]])[0] - np.diff(self.pao[[0, -1]])[0]),
            "anchor_csv_sha256": self.anchor_sha256, "transfer_receipt_sha256": self.transfer_sha256,
        }


def read_transfer(directory, args, pao_events):
    anchors, excluded, anchor_sha256 = read_anchors(directory / "anchors.csv")
    if excluded:
        raise ValueError(f"Acoustic transfer contains excluded or duplicate rows: {directory}")
    receipt = (directory / "transfer.json").read_bytes()
    record = json.loads(receipt)
    binding = record["source"]
    tape = directory.name != DVD
    audio = args.tapes / binding["source_path"] if tape else args.dvd / Path(binding["source_path"]).name
    if file_hash(audio) != binding["source_sha256"]:
        raise ValueError(f"WAV or media checksum disagrees with transfer receipt for {directory.name}")
    if tape and file_hash(audio.with_name("review.json")) != binding["review_sha256"]:
        raise ValueError(f"Stage-05b review checksum disagrees with transfer receipt for {directory.name}")
    qualified = {check["event"] for check in record["checks"] if check["qualified"]}
    converted = []
    pao = []
    samples = []
    for anchor in anchors:
        row = anchor.row
        event = row["anchor_id"]
        if event not in qualified or any(row[field] != binding[field] for field in ("source_path", "source_sha256")):
            raise ValueError(f"Anchor {event} disagrees with the acoustic transfer receipt")
        if tape and any(row[field] != binding[field] for field in ("review_sha256", "map_sha256")):
            raise ValueError(f"Anchor {event} does not use the specified stage-05b release")
        sample = int(row["source_sample"])
        rate = float(row["sample_rate"])
        canonical = float(row["canonical_seconds"]) if tape else anchor.source
        if (abs(anchor.source * rate - sample) > 0.501
                or abs(canonical - anchor.source - binding.get("canonical_origin_s", 0.0)) > 0.501 / rate):
            raise ValueError(f"Anchor {event} has inconsistent time coordinates")
        reference = pao_events[event]
        if abs(reference.source - float(row["pao_seconds"])) > 0.001 or anchor.get != reference.get:
            raise ValueError(f"Anchor {event} does not use the frozen PAO reference")
        converted.append(replace(anchor, source=canonical))
        pao.append(reference.source)
        samples.append(sample)
    if not (np.diff(pao) > 0).all():
        raise ValueError(f"Acoustic matches change order in {directory.name}")
    return Transfer(converted, np.array(pao), binding, np.array(samples), record["duration_s"], audio,
                    anchor_sha256, hashlib.sha256(receipt).hexdigest())


def sweep(pao_audio, target_audio, starts, origin, rate, length):
    first, last = float(starts[0]), float(starts[-1])
    target_start = max(0, int(origin + first * rate) - 17)
    target_stop = int(origin + last * rate) + 26
    reference = read_audio(pao_audio, first, last - first + length + 0.5)
    target = features(read_audio(target_audio, target_start, target_stop - target_start))
    matches = []
    for source in starts:
        expected = origin + source * rate
        left = max(0, int((expected - 12 - target_start) / DT))
        right = int((expected + 12 + length - target_start) / DT)
        i = round((source - first) * RATE)
        query = features(reference[i:i + round(length * RATE)])
        lag, score, other = measure(target[left:right], query, rate)
        actual = target_start + left * DT + lag
        matches.append({"pao_seconds": float(source), "target_seconds": actual, "similarity": score,
                        "distant_competitor": other, "departure_from_search_line_seconds": actual - expected,
                        "qualified": score >= 0.65 and score - other >= 0.10})
    return matches


def dvd_continuity(args, dvd, tape):
    starts = np.arange(1060, 1471, 5)
    ref = dvd.positions[0]
    rate = 1.0042
    origin = ref - dvd.pao[0] * rate
    broad = sweep(args.pao_wav, dvd.audio, starts, origin, rate, 7.0)
    qualified = [match for match in broad if match["qualified"]]
    steps = [(right["departure_from_search_line_seconds"] - left["departure_from_search_line_seconds"], left, right)
             for left, right in zip(qualified, qualified[1:])]
    step, left, right = max(steps, key=lambda item: abs(item[0]))
    if abs(step) < 2:
        raise ValueError("The DVD window sweep does not reproduce the reported interval step")
    fine = np.arange(left["pao_seconds"] - 15, right["pao_seconds"] + 15, 1)
    dvd_fine = sweep(args.pao_wav, dvd.audio, fine, origin, rate, 2.2)
    tape_fine = sweep(args.pao_wav, tape.audio, fine, 37234, 1.03, 2.2)
    level = (left["departure_from_search_line_seconds"] + right["departure_from_search_line_seconds"]) / 2
    before = [match for match in dvd_fine if match["qualified"] and match["departure_from_search_line_seconds"] > level]
    after = [match for match in dvd_fine if match["qualified"] and match["departure_from_search_line_seconds"] < level]
    if step > 0 or not before or not after or before[-1]["pao_seconds"] >= after[0]["pao_seconds"]:
        raise ValueError("The fine DVD sweep does not bracket a single loss of elapsed audio")
    left, right = before[-1], after[0]
    tape_matches = [match for match in tape_fine if match["qualified"]
                    and left["pao_seconds"] <= match["pao_seconds"] <= right["pao_seconds"]]
    tape_range = np.ptp([match["departure_from_search_line_seconds"] for match in tape_matches])
    report = {
        "pao_bracket_seconds": [left["pao_seconds"], right["pao_seconds"]],
        "dvd_bracket_seconds": [left["target_seconds"], right["target_seconds"]],
        "dvd_after_minus_before_seconds": float(np.median([match["departure_from_search_line_seconds"] for match in after])
                                                - np.median([match["departure_from_search_line_seconds"] for match in before])),
        "tape_search_residual_range_seconds_through_bracket": float(tape_range),
        "tape_matches_through_bracket": len(tape_matches),
        "dvd_fine": dvd_fine, "dvd_broad": broad, "tape_fine": tape_fine,
    }
    return report


def matched_positions(primary, other):
    primary_by_event = {anchor.row["anchor_id"]: anchor.source for anchor in primary.anchors}
    common = [(primary_by_event[anchor.row["anchor_id"]], anchor.source, pao)
              for anchor, pao in zip(other.anchors, other.pao) if anchor.row["anchor_id"] in primary_by_event]
    if len(common) < 2:
        raise ValueError("The comparison needs at least two events shared by both tracks")
    return np.array(common).T


def same_tape_consistency(primary, other):
    first, second, _ = matched_positions(primary, other)
    differences = second - first
    return {"matching_calls": len(differences), "median_ch20_minus_ch18_seconds": float(np.median(differences)),
            "maximum_absolute_difference_seconds": float(np.max(np.abs(differences)))}


def write_plot(path, sources, train, late, candidates, continuity):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.3), layout="constrained")
    fig.suptitle("Apollo 15 · timing across PAO, 30-track and DVD audio", fontsize=17, fontweight="bold")
    for axis in axes.flat:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", linewidth=0.7, color="#e4e7ea", zorder=0)
        axis.axhline(0, linewidth=0.8, color="#848a92", zorder=1)
        axis.set_xlabel("PAO reel elapsed (minutes)")

    axis = axes[0, 0]
    for name, color, label in (("affine", "#00728a", "Constant rate"), ("smooth", "#705394", "Smooth rate")):
        if name not in candidates:
            continue
        candidate = candidates[name]
        x = np.array([float(anchor.row["pao_seconds"]) for anchor in train]) / 60
        axis.scatter(x, candidate.blocked_errors, color=color, s=16, label=label, zorder=3)
        if late:
            x = np.array([float(anchor.row["pao_seconds"]) for anchor in late]) / 60
            axis.scatter(x, candidate.late_errors, edgecolor=color, facecolor="none", marker="D", s=26,
                         linewidth=1.15, zorder=4)
    axis.axvline(9360 / 60, color="#777d85", linestyle="--", linewidth=0.85)
    axis.legend(frameon=False, loc="best")
    axis.set_title("T510093 CH18 · transcript validation", loc="left", fontweight="bold")
    axis.set_ylabel("Predicted − printed GET (s)")

    for axis, title, names in ((axes[0, 1], "T510093 · same-feed matches", (PRIMARY, CONSISTENCY)),
                               (axes[1, 0], "T510092 CH23 · early overlap", (OVERLAP,))):
        first = sources[names[0]]
        zero = first.positions[0] - first.pao[0]
        for j, name in enumerate(names):
            source = sources[name]
            axis.scatter(source.pao / 60, source.positions - source.pao - zero,
                         s=24 if j == 0 else 42, edgecolor="#00728a" if j == 0 else "#8b583d",
                         facecolor="#00728a" if j == 0 else "none", linewidth=1.1, label=LABELS[name], zorder=3 + j)
        axis.set_title(title, loc="left", fontweight="bold")
        axis.set_ylabel("Target elapsed − PAO elapsed (s)")
        if len(names) > 1:
            axis.legend(frameon=False, loc="upper left")
    axis = axes[1, 0]
    timing_93, _, pao = matched_positions(sources[PRIMARY], sources[OVERLAP])
    axis.scatter(pao / 60, timing_93 - pao - (timing_93[0] - pao[0]), s=42, edgecolor="#8b583d",
                 facecolor="none", linewidth=1.1, label="CH18 on same calls", zorder=4)
    axis.legend(frameon=False, loc="upper left")

    axis = axes[1, 1]
    dvd = sources[DVD]
    zero = dvd.positions[0] - dvd.pao[0]
    bracket = np.array(continuity["pao_bracket_seconds"]) / 60
    probe = continuity["dvd_broad"]
    x = np.array([match["pao_seconds"] for match in probe if match["qualified"]])
    y = np.array([match["target_seconds"] for match in probe if match["qualified"]])
    axis.scatter(x / 60, y - x - zero, color="#606a78", alpha=0.8, s=9, label="Acoustic windows")
    axis.scatter(dvd.pao / 60, dvd.positions - dvd.pao - zero, color="#a44f14", s=29, label="Transcript anchors")
    axis.axvspan(*bracket, color="#e6b56d", alpha=0.55, label="Audio step")
    axis.set_title("DVD 04-02-02 · separate audio portions", loc="left", fontweight="bold")
    axis.set_ylabel("DVD elapsed − PAO elapsed (s)")
    axis.legend(frameon=False, loc="lower left")
    fig.savefig(path, dpi=165, facecolor="white")
    plt.close(fig)


def write_report(args, overview, sources):
    fit = overview["t510093_fit"]
    fit_candidates = fit["candidates"]
    overlap = overview["sources"][OVERLAP]
    consistency = overview["same_tape_consistency"]
    continuity = overview["dvd_continuity"]
    cross_data = link_path(args.run / "cross_source_timing.json", args.report_dir)
    lines = [
        "# Apollo 15 timing across PAO, 30-track and DVD audio", "",
        "![Comparison of mission-time and relative timing across the matched recordings](cross_source_timing.png)", "",
        "The target timestamps come from two independent acoustic windows for each verified PAO landmark. "
        "The two 30-track reels use their own stage-05b editions and canonical origins. The DVD uses audio stream a:0, "
        "elapsed from its own zero. Matches carry the same printed timestamp as the PAO event. "
        "The relative plots use only the acoustic positions, so printed-second errors cancel. "
        "Diamonds in the transcript panel mark the held-out late exchanges.", "",
        "## Coverage by edition", "",
        "| Edition | Calls | Exchanges | Target elapsed span | Printed GET span | Target minus PAO elapsed (s) |",
        "| --- | ---: | ---: | --- | --- | ---: |",
        f"| PAO 572-AAA | {overview['pao']['calls']} | {overview['pao']['exchanges']} | "
        f"{interval(overview['pao']['elapsed_interval_seconds'])} | {interval(overview['pao']['printed_get_interval_seconds'])} | — |",
    ]
    for name, data in overview["sources"].items():
        lines.append(f"| {LABELS[name]} | {data['calls']} | {data['exchanges']} | {interval(data['target_interval_seconds'])} | "
                     f"{interval(data['printed_get_interval_seconds'])} | {data['target_elapsed_minus_pao_elapsed_seconds']:+.3f} |")
    lines.extend(["", "The interval differences include transfer speed, feed timing and any edit within the span. "
                  "The two DVD portions have separate coverage below.", "", "## T510093 CH18", "",
                  f"The fixed late reserve begins at canonical second {fit['holdout_start_source_seconds']:.2f}. "
                  f"The test uses {len(fit['holdout_exchanges'])} whole exchanges and the development fit uses "
                  f"{len(fit['training_exchanges'])}. Errors below are in seconds.", "",
                  "| Candidate | Blocked median absolute | Late median absolute | Late maximum absolute |",
                  "| --- | ---: | ---: | ---: |"])
    for name, label in (("offset", "Offset only"), ("affine", "Constant rate"), ("smooth", "Smooth rate")):
        if name not in fit_candidates:
            continue
        data = fit_candidates[name]
        lines.append(f"| {label} | {data['blocked_development']['median_absolute_seconds']:.3f} | "
                     f"{data['forward_late_holdout']['median_absolute_seconds']:.3f} | {data['forward_late_holdout']['max_absolute_seconds']:.3f} |")
    selected = fit_candidates[fit["selected_candidate"]]
    label = {"offset": "offset-only", "affine": "constant-rate", "smooth": "smooth-rate"}[fit["selected_candidate"]]
    final = selected["full_refit"]
    rate = final["rate_get_seconds_per_source_second_range"]
    rate_text = " to ".join(dict.fromkeys(f"{value:.7f}" for value in rate))
    if "smooth" in fit_candidates:
        period = fit_candidates['smooth']['training_map']['spline']['approximate_half_amplitude_period_minutes']
        lines += ["", f"The smooth candidate used an approximate {period:g}-minute half-amplitude period. "
                  + fit["selection"]["smooth"]]
    lines.extend(["", f"The {label} refit covers predicted GET {interval(final['fit_get_interval_seconds'])}. "
                  f"Its fitted rate is {rate_text} GET seconds per canonical second. "
                  f"The largest unanchored interval inside the measured overlap spans "
                  f"{max((right - left) / 60 for left, right in zip(sources[PRIMARY].positions, sources[PRIMARY].positions[1:])):.1f} minutes.", "",
                  f"CH20 reproduces {consistency['matching_calls']} of the CH18 event positions with a maximum absolute "
                  f"difference of {consistency['maximum_absolute_difference_seconds']:.3f} s. The matched calls contain the same monitored audio. "
                  "The CH20 checks add no independent transcript events. The source-specific origin absorbs any constant feed delay. "
                  "Stage-06 Quindar products were unavailable for this comparison.", "", "## T510092 CH23", "",
                  f"The last match is at canonical second {overlap['target_interval_seconds'][1]:.3f}, before the prescribed "
                  f"reserve at {HOLDOUTS[OVERLAP]:.2f}. The {overlap['exchanges']} matched exchanges establish a local overlap "
                  f"and an endpoint interval difference of {overlap['target_elapsed_minus_pao_elapsed_seconds']:+.3f} s over "
                  f"{overlap['pao_interval_seconds'][1] - overlap['pao_interval_seconds'][0]:.3f} PAO seconds. "
                  "The available overlap leaves no forward test for a reel-rate map. "
                  f"For the same calls, CH23 elapsed minus CH18 elapsed is {overview['overlap_elapsed_difference_seconds']:+.3f} s.",
                  "", "## DVD 04-02-02", ""])
    bracket = continuity["pao_bracket_seconds"]
    target = continuity["dvd_bracket_seconds"]
    lines.extend([f"The fine acoustic sweep brackets an audio discontinuity at DVD elapsed {target[0]:.2f}–{target[1]:.2f} s, "
                  f"between PAO query starts {bracket[0]:.0f} and {bracket[1]:.0f} s. DVD elapsed timing changes by "
                  f"{continuity['dvd_after_minus_before_seconds']:+.3f} s after allowing for the pre-cut relative rate. "
                  f"CH18 tracks PAO through the same bracket with {continuity['tape_matches_through_bracket']} qualified short windows "
                  f"whose departures from a local constant rate span {continuity['tape_search_residual_range_seconds_through_bracket']:.3f} s. "
                  "The step is specific to this DVD audio edition in the three-source comparison.", "",
                  "| DVD portion | Printed GET of matched calls | DVD elapsed span | Calls | DVD elapsed minus PAO elapsed (s) |",
                  "| --- | --- | --- | ---: | ---: |"])
    for label, data in overview["dvd_portions"].items():
        lines.append(f"| {label} | {interval(data['printed_get_interval_seconds'])} | {interval(data['target_interval_seconds'])} | "
                     f"{data['calls']} | {data['target_elapsed_minus_pao_elapsed_seconds']:+.3f} |")
    lines.extend(["", "Each DVD portion has too few independent transcript exchanges for a forward rate test. "
                  "The acoustic matches establish local timing on both sides of the edit.", "",
                  f"[Full metrics, source checksums and acoustic sweep]({cross_data})", ""])
    (args.report_dir / "CROSS_SOURCE_TIMING.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("pao_wav", type=Path)
    parser.add_argument("tapes", type=Path, help="Root containing defluttered_shared")
    parser.add_argument("dvd", type=Path, help="Spacecraft Films directory")
    parser.add_argument("report_dir", type=Path)
    args = parser.parse_args()
    pao, _, pao_sha = read_anchors(args.run / "pao_anchors.csv")
    pao_audio_sha = file_hash(args.pao_wav)
    if any(anchor.row["source_sha256"] != pao_audio_sha for anchor in pao):
        raise ValueError("PAO audio checksum disagrees with the anchor table")
    pao_metric = json.loads((args.run / "pao_timing_metrics.json").read_bytes())
    if pao_metric["anchor_csv_sha256"] != pao_sha:
        raise ValueError("Cross-source timing must use the frozen PAO fit anchors")
    events = {anchor.row["anchor_id"]: anchor for anchor in pao}
    sources = {name: read_transfer(args.run / "cross_source" / name, args, events)
               for name in (PRIMARY, CONSISTENCY, OVERLAP, DVD)}
    source = sources[PRIMARY]
    fit_args = SimpleNamespace(anchors=args.run / "cross_source" / PRIMARY / "anchors.csv", source_id=PRIMARY,
                               source_manifest=None, duration_seconds=source.duration + source.source["canonical_origin_s"],
                               holdout_start_seconds=HOLDOUTS[PRIMARY], blocks=5, smooth_period_minutes=[30, 60, 90],
                               gap_minutes=20)
    train, late, candidates, fit_record = analyze(fit_args, source.anchors, [], source.anchor_sha256)
    write_predictions(args.run / "cross_source" / PRIMARY / "predictions.csv", train, late, candidates, [])
    continuity = dvd_continuity(args, sources[DVD], source)
    consistency = same_tape_consistency(source, sources[CONSISTENCY])
    split = np.searchsorted(sources[DVD].pao, np.mean(continuity["pao_bracket_seconds"]))
    portions = {}
    for name, items in (("Before the audio step", slice(None, split)), ("After the audio step", slice(split, None))):
        dvd = sources[DVD]
        portion = replace(dvd, anchors=dvd.anchors[items], pao=dvd.pao[items], sample=dvd.sample[items])
        portions[name] = {**portion.describe(), "local_offset_fit": fit(portion.anchors, "offset").describe()}
    timing_93, timing_92, _ = matched_positions(source, sources[OVERLAP])
    overlap_delta = float((timing_92[-1] - timing_92[0]) - (timing_93[-1] - timing_93[0]))
    overview = {"schema_version": 1, "pao_anchor_sha256": pao_sha, "pao_audio_sha256": pao_audio_sha,
                "pao": {"calls": len(pao), "exchanges": len(group_indices(pao)),
                        "elapsed_interval_seconds": [pao[0].source, pao[-1].source],
                        "printed_get_interval_seconds": pao_metric["anchor_printed_get_interval_seconds"]},
                "holdout_canonical_seconds": HOLDOUTS, "sources": {name: source.describe() for name, source in sources.items()},
                "t510093_fit": fit_record, "same_tape_consistency": consistency, "dvd_continuity": continuity,
                "dvd_portions": portions, "overlap_elapsed_difference_seconds": overlap_delta}
    (args.run / "cross_source_timing.json").write_text(json.dumps(overview, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    write_plot(args.report_dir / "cross_source_timing.png", sources, train, late, candidates, continuity)
    write_report(args, overview, sources)
    print(f"{PRIMARY}: selected {fit_record['selected_candidate']}. DVD step {continuity['dvd_after_minus_before_seconds']:+.3f} s")


if __name__ == "__main__":
    main()
