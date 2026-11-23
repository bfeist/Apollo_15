"""Fit and validate an elapsed-time to GET map for one continuous PAO reel."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import BSpline, PPoly, make_smoothing_spline


@dataclass
class Anchor:
    row: dict
    exchange: str
    source: float
    get: float
    pick_sigma: float
    transcript_sigma: float
    direction: str

    @property
    def sigma(self):
        return np.hypot(self.pick_sigma, self.transcript_sigma)


def read_anchors(path):
    accepted = defaultdict(list)
    excluded = []
    payload = path.read_bytes()
    with io.StringIO(payload.decode("utf-8-sig"), newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"exchange_id", "source_seconds", "get_seconds"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Anchor CSV requires {', '.join(sorted(required))}")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Anchor CSV line {reader.line_num} does not match the header")
            row["anchor_id"] = (row.get("anchor_id") or "").strip() or f"row-{reader.line_num}"
            if (row.get("status") or "accepted").strip().lower() not in {"accepted", "verified"}:
                excluded.append(dict(row, fit_disposition="excluded by input status"))
                continue
            exchange = row["exchange_id"].strip()
            try:
                source = float(row["source_seconds"])
                get = float(row["get_seconds"])
                pick = float(row.get("pick_sigma_seconds") or 0.25)
                transcript = float(row.get("transcript_sigma_seconds") or 1.0)
            except ValueError as error:
                raise ValueError(f"{row['anchor_id']} has an invalid numeric field") from error
            if (not exchange or not np.isfinite([source, get, pick, transcript]).all()
                    or min(source, get, pick) < 0 or transcript <= 0):
                raise ValueError(f"{row['anchor_id']} has an invalid exchange, time or uncertainty")
            direction = (row.get("direction") or "unknown").strip().lower() or "unknown"
            event = (row.get("transcript_event_id") or "").strip()
            identity = event or ((row.get("speaker") or "").strip().casefold(), get)
            accepted[exchange, identity].append(Anchor(row, exchange, source, get, pick, transcript, direction))

    anchors = []
    for copies in accepted.values():
        copies.sort(key=lambda item: (item.pick_sigma, item.sigma, item.row["anchor_id"]))
        if len({copy.get for copy in copies}) > 1:
            raise ValueError(f"Copies of {copies[0].row['anchor_id']} have conflicting printed GET stamps")
        anchors.append(copies[0])
        for copy in copies[1:]:
            excluded.append(dict(copy.row, fit_disposition=f"same printed row as {copies[0].row['anchor_id']}"))
    if not anchors:
        raise ValueError("The CSV contains no accepted anchors")
    return sorted(anchors, key=lambda item: item.source), excluded, hashlib.sha256(payload).hexdigest()


@dataclass
class Map:
    kind: str
    origin: float
    start: float
    stop: float
    offset: float
    rate_adjustment: float = 0.0
    spline: BSpline | None = None
    half_amplitude_period_minutes: float | None = None

    def predict(self, source):
        source = np.asarray(source, dtype=float)
        drift = self.offset + self.rate_adjustment * (source - self.origin)
        if self.spline is not None:
            x = (source - self.origin) / 3600
            bounded = np.clip(x, 0, (self.stop - self.origin) / 3600)
            # Extend the rate at each endpoint as a constant outside the fitted interval.
            drift = self.offset + self.spline(bounded) + self.spline.derivative()(bounded) * (x - bounded)
        return source + drift

    def rate_extrema(self):
        if self.spline is None:
            rate = 1 + self.rate_adjustment
            return rate, rate
        end = (self.stop - self.origin) / 3600
        extrema = PPoly.from_spline(self.spline).derivative(2).roots(extrapolate=False)
        extrema = extrema[np.isfinite(extrema) & (extrema > 0) & (extrema < end)]
        points = np.r_[0, extrema, self.spline.t, end]
        rates = 1 + self.spline.derivative()(points) / 3600
        return float(rates.min()), float(rates.max())

    def describe(self):
        elapsed_change = self.rate_adjustment * (self.stop - self.start)
        if self.spline is not None:
            elapsed_change = float(self.spline((self.stop - self.origin) / 3600) - self.spline(0))
        result = {
            "kind": self.kind,
            "fit_source_interval_seconds": [self.start, self.stop],
            "fit_get_interval_seconds": self.predict([self.start, self.stop]).tolist(),
            "rate_get_seconds_per_source_second_range": self.rate_extrema(),
            "origin_source_seconds": self.origin,
            "offset_seconds": self.offset,
            "rate_adjustment_seconds_per_source_second": self.rate_adjustment,
            "get_elapsed_minus_source_elapsed_seconds": elapsed_change,
        }
        if self.spline is not None:
            result["spline"] = {
                "knots_source_hours_after_origin": self.spline.t.tolist(),
                "drift_coefficients_seconds": self.spline.c.tolist(),
                "degree": self.spline.k,
                "approximate_half_amplitude_period_minutes": self.half_amplitude_period_minutes,
                "tail": "linear at endpoint rate",
            }
        return result


def group_indices(anchors):
    groups = defaultdict(list)
    for index, anchor in enumerate(anchors):
        groups[anchor.exchange].append(index)
    return groups


def fit(anchors, kind, period=None):
    if not anchors:
        return None
    source = np.array([item.source for item in anchors])
    first, last = float(source.min()), float(source.max())
    drift = np.array([item.get for item in anchors]) - source
    origin_offset = float(np.median(drift))
    y = drift - origin_offset
    sigma = np.array([item.sigma for item in anchors])
    groups = group_indices(anchors)
    weights = 1 / sigma**2
    for indices in groups.values():
        weights[indices] /= len(indices)

    if kind == "affine" and (len(groups) < 2 or first == last):
        return None
    if kind == "smooth":
        x = (source - first) / 3600
        unique, inverse = np.unique(x, return_inverse=True)
        if len(groups) < 5 or len(unique) < 5 or last - first < period * 60:
            return None
        # For regularly spaced observations this penalty halves the response to a
        # sinusoid at the specified period. Irregular sampling makes it approximate.
        penalty = weights.sum() / (x.max() - x.min()) * (period / (60 * 2 * np.pi)) ** 4
    else:
        origin = float(np.mean(source))
        scale = max(last - first, 1.0)
        design = np.ones((len(source), 1))
        if kind == "affine":
            design = np.column_stack([design, (source - origin) / scale])

    fitted_weights = weights.copy()
    previous = np.full(y.shape, np.inf)
    for _ in range(40):
        if kind == "smooth":
            combined = np.bincount(inverse, weights=fitted_weights)
            mean = np.bincount(inverse, weights=fitted_weights * y) / combined
            try:
                spline = make_smoothing_spline(unique, mean, w=combined, lam=penalty)
            except (ValueError, np.linalg.LinAlgError):
                return None
            prediction = spline(x)
        else:
            root = np.sqrt(fitted_weights)
            coefficients = np.linalg.lstsq(design * root[:, None], y * root, rcond=None)[0]
            prediction = design @ coefficients
        if np.max(np.abs(prediction - previous)) < 1e-7:
            break
        previous = prediction
        fitted_weights = weights / np.maximum(1, np.abs(y - prediction) / (1.5 * sigma))

    if kind == "smooth":
        model = Map(kind, first, first, last, origin_offset, spline=spline, half_amplitude_period_minutes=period)
    else:
        rate_adjustment = float(coefficients[1] / scale) if kind == "affine" else 0.0
        model = Map(kind, origin, first, last, origin_offset + float(coefficients[0]), rate_adjustment)
    return model if model.rate_extrema()[0] > 0 else None


def reserve(anchors, start):
    if start is None:
        return anchors, []
    held = {anchor.exchange for anchor in anchors if anchor.source >= start}
    train = [anchor for anchor in anchors if anchor.exchange not in held]
    test = [anchor for anchor in anchors if anchor.exchange in held]
    if not train or not test:
        raise ValueError("Late reserve threshold must leave at least one complete exchange on each side")
    return train, test


def blocks(anchors, count):
    boundaries = np.linspace(anchors[0].source, anchors[-1].source, count + 1)[1:-1]
    results = [[] for _ in range(count)]
    for indices in group_indices(anchors).values():
        position = np.median([anchors[index].source for index in indices])
        block = int(np.searchsorted(boundaries, position, side="right"))
        results[block].extend(indices)
    return [sorted(result) for result in results if result]


def residuals(anchors, model):
    return model.predict([anchor.source for anchor in anchors]) - [anchor.get for anchor in anchors]


def score(anchors, errors):
    if errors is None or not anchors:
        return None
    groups = group_indices(anchors)
    absolute = [np.median(np.abs(errors[indices])) for indices in groups.values()]
    signed = [np.median(errors[indices]) for indices in groups.values()]
    edge_count = max(1, len(groups) // 3)
    return {
        "calls": len(anchors),
        "exchanges": len(groups),
        "median_absolute_seconds": float(np.median(absolute)),
        "max_absolute_seconds": float(np.max(np.abs(errors))),
        "p90_exchange_median_absolute_seconds": float(np.quantile(absolute, 0.90)),
        "median_signed_seconds": float(np.median(signed)),
        "early_to_late_signed_drift_seconds": (
            float(np.median(signed[-edge_count:]) - np.median(signed[:edge_count]))
            if len(groups) > 1 else None
        ),
        "source_interval_seconds": [min(anchor.source for anchor in anchors), max(anchor.source for anchor in anchors)],
        "printed_get_interval_seconds": [min(anchor.get for anchor in anchors), max(anchor.get for anchor in anchors)],
    }


def cross_validate(anchors, folds, kind, period=None):
    predictions = np.full(len(anchors), np.nan)
    block_scores = []
    if len(folds) < 2:
        return predictions, None, block_scores
    for indices in folds:
        held = set(indices)
        training = [anchor for index, anchor in enumerate(anchors) if index not in held]
        test = [anchors[index] for index in indices]
        model = fit(training, kind, period)
        if model is not None:
            predictions[indices] = residuals(test, model)
            block_scores.append(score(test, predictions[indices]))
        else:
            block_scores.append(None)
    overall = score(anchors, predictions) if np.isfinite(predictions).all() else None
    return predictions, overall, block_scores


@dataclass
class Candidate:
    training_map: Map
    blocked_errors: np.ndarray
    blocked_score: dict | None
    block_scores: list
    late_errors: np.ndarray | None
    late_score: dict | None
    final_map: Map | None = None

    def describe(self):
        return {
            "training_map": self.training_map.describe(),
            "blocked_development": self.blocked_score,
            "development_blocks": self.block_scores,
            "forward_late_holdout": self.late_score,
            "full_refit": self.final_map.describe() if self.final_map else None,
        }


def choose_candidate(candidates, sigma):
    selected = "offset"
    decisions = {}
    for name in ("affine", "smooth"):
        if name not in candidates:
            decisions[name] = "Unavailable with the development evidence and validation blocks."
            continue
        baseline, candidate = candidates[selected], candidates[name]
        if not candidate.late_score or candidate.late_score["exchanges"] < 2:
            decisions[name] = "Fewer than two independent late exchanges for confirmation."
            continue
        late_gain = baseline.late_score["median_absolute_seconds"] - candidate.late_score["median_absolute_seconds"]
        if late_gain <= 0:
            decisions[name] = (f"Late median absolute error increased by {-late_gain:.3f} s."
                               if late_gain < 0 else "Late median absolute error was unchanged.")
            continue
        if late_gain <= sigma:
            decisions[name] = f"Late median absolute error improved by {late_gain:.3f} s, within the {sigma:.3f} s timing scale."
            continue
        if not baseline.blocked_score or not candidate.blocked_score:
            decisions[name] = "Incomplete blocked development comparison."
            continue
        blocked_gain = (baseline.blocked_score["median_absolute_seconds"]
                        - candidate.blocked_score["median_absolute_seconds"])
        if blocked_gain <= 0:
            decisions[name] = "The candidate did not improve the blocked development median error."
            continue
        if name == "smooth":
            interior = zip(baseline.block_scores[1:-1], candidate.block_scores[1:-1])
            if not any(left["exchanges"] >= 2 and left["median_absolute_seconds"]
                       - right["median_absolute_seconds"] > sigma for left, right in interior):
                decisions[name] = "No interior block with at least two exchanges improved beyond the timing scale."
                continue
        decisions[name] = f"Median absolute error improved {blocked_gain:.3f} s across development blocks and {late_gain:.3f} s on the late block."
        selected = name
    return selected, decisions


def gap_intervals(anchors, duration, threshold):
    positions = sorted({anchor.source for anchor in anchors})
    gaps = []
    if positions[0] > 0:
        gaps.append({"kind": "unanchored reel start", "source_interval_seconds": [0, positions[0]]})
    for left, right in zip(positions, positions[1:]):
        if right - left >= threshold:
            gaps.append({"kind": "internal anchor gap", "source_interval_seconds": [left, right]})
    if duration > positions[-1]:
        gaps.append({"kind": "unanchored reel end", "source_interval_seconds": [positions[-1], duration]})
    return gaps


def direction_statistics(anchors, errors):
    results = {}
    for direction in sorted({anchor.direction for anchor in anchors}):
        indices = [index for index, anchor in enumerate(anchors) if anchor.direction == direction]
        subset = [anchors[index] for index in indices]
        stats = score(subset, errors[indices])
        peers = defaultdict(list)
        for index in indices:
            peers[anchors[index].exchange].append(index)
        differences = []
        for group in peers.values():
            for left, right in zip(group, group[1:]):
                if anchors[right].source - anchors[left].source <= 90:
                    differences.append(float(errors[right] - errors[left]))
        stats["same_exchange_neighbor_pairs_at_most_90_seconds"] = len(differences)
        stats["neighbor_difference_median_absolute_seconds"] = (
            float(np.median(np.abs(differences))) if differences else None
        )
        results[direction] = stats
    return results


def paired_direction_statistics(anchors, errors):
    deltas = []
    for indices in group_indices(anchors).values():
        uplink = [errors[index] for index in indices if anchors[index].direction == "uplink"]
        downlink = [errors[index] for index in indices if anchors[index].direction == "downlink"]
        if uplink and downlink:
            deltas.append(np.median(uplink) - np.median(downlink))
    return {"exchanges": len(deltas), "median_uplink_minus_downlink_seconds": float(np.median(deltas))} if deltas else None


def source_metadata(path, anchors):
    if path is None:
        return None
    payload = path.read_bytes()
    metadata = json.loads(payload)
    source = metadata["source"]
    if metadata.get("status") != "complete" or not source.get("sha256"):
        raise ValueError("Source manifest must be complete and carry the audio checksum")
    rate = source["sample_rate"]
    for anchor in anchors:
        sample = anchor.row.get("source_sample")
        if sample and abs(int(sample) - anchor.source * rate) > 0.501:
            raise ValueError(f"{anchor.row['anchor_id']} source sample and elapsed time disagree")
    return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(), "audio": source}


def analyze(args, anchors, excluded, anchor_csv_sha256):
    provenance = source_metadata(args.source_manifest, anchors)
    media_duration = (provenance["audio"]["frames"] / provenance["audio"]["sample_rate"]) if provenance else None
    duration = args.duration_seconds if args.duration_seconds is not None else media_duration
    if media_duration is not None and abs(duration - media_duration) > 1 / provenance["audio"]["sample_rate"]:
        raise ValueError("Supplied reel duration disagrees with the source manifest")
    duration_inferred = duration is None
    if duration_inferred:
        duration = anchors[-1].source
    if not np.isfinite(duration) or duration <= 0 or anchors[-1].source > duration:
        raise ValueError("Reel duration must be positive and at or beyond the last accepted anchor")
    start = args.holdout_start_seconds
    if start is None and len(group_indices(anchors)) > 1 and anchors[-1].source > anchors[0].source:
        start = anchors[0].source + 0.8 * (anchors[-1].source - anchors[0].source)
    train, late = reserve(anchors, start)
    folds = blocks(train, args.blocks)
    candidates = {}
    for kind in ("offset", "affine"):
        model = fit(train, kind)
        if model:
            blocked_errors, blocked_score, block_scores = cross_validate(train, folds, kind)
            late_errors = residuals(late, model) if late else None
            candidates[kind] = Candidate(model, blocked_errors, blocked_score, block_scores,
                                         late_errors, score(late, late_errors))

    smooth_trials = []
    best = None
    for period in sorted(set(args.smooth_period_minutes), reverse=True):
        model = fit(train, "smooth", period)
        blocked_errors, blocked_score, block_scores = cross_validate(train, folds, "smooth", period)
        smooth_trials.append({"period_minutes": period, "development_fit_available": model is not None,
                              "blocked_score": blocked_score, "block_scores": block_scores})
        if model and blocked_score and (best is None or blocked_score["median_absolute_seconds"]
                                       < best.blocked_score["median_absolute_seconds"]):
            best = Candidate(model, blocked_errors, blocked_score, block_scores, None, None)
    if best:
        best.late_errors = residuals(late, best.training_map) if late else None
        best.late_score = score(late, best.late_errors)
        candidates["smooth"] = best

    assumed_sigma = float(np.median([np.median([train[index].sigma for index in indices])
                                     for indices in group_indices(train).values()]))
    selected, decisions = choose_candidate(candidates, assumed_sigma)
    for name, candidate in candidates.items():
        candidate.final_map = fit(anchors, name, candidate.training_map.half_amplitude_period_minutes)
    chosen = candidates[selected]
    scored = [train[index] for index in np.flatnonzero(np.isfinite(chosen.blocked_errors))]
    scored_errors = chosen.blocked_errors[np.isfinite(chosen.blocked_errors)]
    if late:
        scored += late
        scored_errors = np.r_[scored_errors, chosen.late_errors]
    provisional = (len(group_indices(anchors)) < 6 or not chosen.blocked_score
                   or not chosen.late_score or chosen.late_score["exchanges"] < 2)
    gap_seconds = args.gap_minutes * 60
    summary = {
        "schema_version": 1,
        "source_id": args.source_id,
        "source_duration_seconds": duration,
        "duration_from_last_anchor": duration_inferred,
        "source_manifest": provenance,
        "anchor_csv": str(args.anchors),
        "anchor_csv_sha256": anchor_csv_sha256,
        "accepted_calls": len(anchors),
        "accepted_exchanges": len(group_indices(anchors)),
        "anchor_printed_get_interval_seconds": [min(anchor.get for anchor in anchors), max(anchor.get for anchor in anchors)],
        "excluded_rows": len(excluded),
        "holdout_start_source_seconds": start,
        "holdout_boundary_explicit": args.holdout_start_seconds is not None,
        "training_exchanges": list(group_indices(train)),
        "holdout_exchanges": list(group_indices(late)),
        "development_blocks": [list(dict.fromkeys(train[index].exchange for index in fold)) for fold in folds],
        "median_working_sigma_seconds": assumed_sigma,
        "selected_candidate": selected,
        "selection_status": "provisional" if provisional else "evaluated",
        "selection": decisions,
        "candidates": {name: candidate.describe() for name, candidate in candidates.items()},
        "smooth_trials": smooth_trials,
        "internal_gap_threshold_seconds": gap_seconds,
        "gaps": gap_intervals(anchors, duration, gap_seconds),
        "out_of_sample_by_direction": direction_statistics(scored, scored_errors),
        "paired_out_of_sample_uplink_downlink": paired_direction_statistics(scored, scored_errors),
    }
    return train, late, candidates, summary


def write_predictions(path, train, late, candidates, excluded):
    rows = []
    for label, anchors in (("development", train), ("late holdout", late)):
        for index, anchor in enumerate(anchors):
            row = dict(anchor.row, fit_disposition=label, combined_sigma_seconds=round(anchor.sigma, 6))
            for name, candidate in candidates.items():
                forecast_error = (candidate.blocked_errors if label == "development" else candidate.late_errors)[index]
                row[f"{name}_out_of_sample_error_seconds"] = (
                    round(forecast_error, 6) if np.isfinite(forecast_error) else ""
                )
                row[f"{name}_training_map_error_seconds"] = round(
                    float(candidate.training_map.predict(anchor.source) - anchor.get), 6
                )
            rows.append(row)
    rows.extend(excluded)
    fields = dict.fromkeys(key for row in rows for key in row)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("anchors", type=Path, help="CSV with exchange_id, source_seconds and get_seconds")
    parser.add_argument("--out-dir", type=Path, required=True, help="JSON and anchor predictions destination")
    parser.add_argument("--report-dir", type=Path, help="Destination for the PNG and Markdown summary")
    parser.add_argument("--source-id", default="572-AAA")
    parser.add_argument("--source-manifest", type=Path, help="Complete ASR provenance manifest for the reel")
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--holdout-start-seconds", type=float)
    parser.add_argument("--blocks", type=int, default=5)
    parser.add_argument("--smooth-period-minutes", type=float, nargs="+", default=[30, 60, 90])
    parser.add_argument("--gap-minutes", type=float, default=20)
    args = parser.parse_args(argv)
    values = [args.gap_minutes, *args.smooth_period_minutes]
    if args.blocks < 3 or not np.isfinite(values).all() or min(values) <= 0:
        parser.error("Use at least three development blocks and positive smoothing and gap periods")
    if args.holdout_start_seconds is not None and not np.isfinite(args.holdout_start_seconds):
        parser.error("The reserve threshold must be finite")
    return args


def main(argv=None):
    args = parse_args(argv)
    anchors, excluded, anchor_csv_sha256 = read_anchors(args.anchors)
    train, late, candidates, summary = analyze(args, anchors, excluded, anchor_csv_sha256)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "pao_timing_metrics.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    write_predictions(args.out_dir / "pao_anchor_predictions.csv", train, late, candidates, excluded)
    if args.report_dir:
        from pao_timing_report import write_report

        write_report(args.report_dir, args.out_dir, train, late, candidates, summary)
    print(f"{summary['source_id']}: {summary['accepted_exchanges']} exchanges, selected {summary['selected_candidate']}")


if __name__ == "__main__":
    main()
