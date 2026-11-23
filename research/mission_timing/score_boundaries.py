"""Score full-reel lexical retrieval and opening words on the visual review set."""

import argparse
import csv
import json
from pathlib import Path

import numpy as np


def digest(errors):
    values = np.asarray(errors)
    if not len(values):
        return {"n": 0}
    return dict(
        n=len(values),
        median_signed_s=float(np.median(values)),
        median_absolute_s=float(np.median(abs(values))),
        p90_absolute_s=float(np.percentile(abs(values), 90)),
        worst_absolute_s=float(np.max(abs(values))),
        within_250ms=int(np.sum(abs(values) <= 0.25)),
    )


def records(path):
    return {r["event_id"]: r for r in map(json.loads, path.read_text().splitlines())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    base = args.run
    with (base / "pao_anchors.csv").open(encoding="utf-8") as stream:
        anchors = list(csv.DictReader(stream))
    detail = []
    summary = {}
    pairs = [
        ("large-v3", "call_candidates.jsonl", base),
        ("distil-large-v3.5", "distil_candidates.jsonl", base / "distil_review"),
    ]
    for model, candidates_path, audit_path in pairs:
        hits = records(base / candidates_path)
        audits = records(audit_path / "acoustic_audit.jsonl")
        native, ctc, qualified = [], [], []
        for row in anchors:
            key = row["anchor_id"]
            truth = float(row["source_seconds"])
            audit = audits.get(key, {})
            candidate = hits.get(key, {}).get("best", {})
            out = dict(
                anchor_id=key,
                model=model,
                visual_start_s=truth,
                lexical_score=candidate.get("score"),
                native_error_s=None,
                ctc_error_s=None,
                ctc_qualified=False,
            )
            detail.append(out)
            if (
                not candidate
                or candidate["matched"] < 6
                or candidate["leading_missing"]
                or candidate["score"] < 0.66
            ):
                continue
            delta = candidate["start"] - truth
            native.append(delta)
            out["native_error_s"] = delta
            if audit.get("words"):
                delta = audit["words"][0]["emission_start"] - truth
                out["ctc_error_s"] = delta
                ctc.append(delta)
                if audit["status"] == "qualified":
                    qualified.append(delta)
                    out["ctc_qualified"] = True
        summary[model] = dict(
            references=len(anchors),
            native=digest(native),
            ctc=digest(ctc),
            ctc_qualified=digest(qualified),
        )
    (base / "model_boundary_scores.json").write_text(json.dumps(summary, indent=2))
    with (base / "model_boundary_trials.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=detail[0])
        writer.writeheader()
        writer.writerows(detail)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
