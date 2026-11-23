"""The PAO forward test holds out entire exchanges before selecting a map."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from research.mission_timing.fit_pao import Anchor, analyze, blocks, fit, main, parse_args, reserve


def anchor(exchange, source, get=None):
    return Anchor({}, exchange, source, source + 520000 if get is None else get, 0.20, 1.0, "downlink")


class PaoTimingTests(unittest.TestCase):
    def test_exchange_stays_together_at_late_boundary_and_in_blocks(self):
        anchors = [anchor("a", 100), anchor("b", 250), anchor("a", 300), anchor("c", 400), anchor("c", 510)]
        training, held = reserve(anchors, 500)
        self.assertEqual({row.exchange for row in training}, {"a", "b"})
        self.assertEqual([row.source for row in held], [400, 510])
        folds = blocks(anchors, 5)
        for exchange in "abc":
            self.assertEqual(sum(any(anchors[index].exchange == exchange for index in fold) for fold in folds), 1)

    def test_line_recovers_rate_despite_bad_stamp_and_cli_writes_predictions(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "anchors.csv"
            with csv_path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["anchor_id", "exchange_id", "source_seconds", "get_seconds", "pick_sigma_seconds",
                                 "status", "transcript_event_id", "pdf_page"])
                for index, source in enumerate(range(100, 11700, 450)):
                    get = round(520000 + 1.001 * source) + (50 if index == 3 else 0)
                    writer.writerow([f"a{index}", index, source, get, 0.25, "verified", f"event-{index}", 810])
                    if index == 6:
                        writer.writerow(["duplicate", index, source + 22, get, 1, "verified", f"event-{index}", 810])
                writer.writerow(["unknown", "pending", 9000, 1, "", "pending", "pending", 860])
            output = Path(folder) / "result"
            argv = [str(csv_path), "--out-dir", str(output), "--duration-seconds", "12000", "--holdout-start-seconds", "9360"]
            main(argv)
            record = json.loads((output / "pao_timing_metrics.json").read_text())
            self.assertEqual(record["selected_candidate"], "affine")
            self.assertEqual(record["excluded_rows"], 2)
            estimate = record["candidates"]["affine"]["training_map"]
            self.assertAlmostEqual(estimate["rate_adjustment_seconds_per_source_second"], 0.001, delta=0.0001)
            self.assertLess(record["candidates"]["affine"]["forward_late_holdout"]["median_absolute_seconds"], 0.7)
            with (output / "pao_anchor_predictions.csv").open(newline="", encoding="utf-8") as handle:
                rows = {row["anchor_id"]: row for row in csv.DictReader(handle)}
            self.assertEqual(rows["duplicate"]["fit_disposition"], "same printed row as a6")
            self.assertEqual(rows["a6"]["pdf_page"], "810")
            self.assertEqual(rows["unknown"]["fit_disposition"], "excluded by input status")

    def test_smooth_span_selection_does_not_see_late_get_times(self):
        def sample(late_shift):
            return [anchor(str(index), source, 520000 + source + 3 * np.sin(source / 700)
                           + (late_shift if source >= 9360 else 0))
                    for index, source in enumerate(np.arange(100, 11700, 190))]

        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "input.csv"
            csv_path.touch()
            args = parse_args([str(csv_path), "--out-dir", folder, "--holdout-start-seconds", "9360"])
            original = analyze(args, sample(0), [], "fixture")[3]
            perturbed = analyze(args, sample(100), [], "fixture")[3]
        first = original["candidates"]["smooth"]
        second = perturbed["candidates"]["smooth"]
        self.assertEqual(first["training_map"], second["training_map"])
        self.assertEqual(original["smooth_trials"], perturbed["smooth_trials"])
        self.assertNotEqual(first["forward_late_holdout"], second["forward_late_holdout"])
        self.assertGreater(first["training_map"]["rate_get_seconds_per_source_second_range"][0], 0)
        self.assertLess(first["training_map"]["rate_get_seconds_per_source_second_range"][0], 1)
        self.assertGreater(first["training_map"]["rate_get_seconds_per_source_second_range"][1], 1)

    def test_smoothing_extends_endpoint_rates_linearly(self):
        anchors = [anchor(str(index), source, 520000 + source + 3 * np.sin(source / 350))
                   for index, source in enumerate(range(100, 6000, 150))]
        curve = fit(anchors, "smooth", 30)
        last = curve.stop
        mapped = curve.predict([last, last + 100, last + 200])
        np.testing.assert_allclose(np.diff(mapped)[0], np.diff(mapped)[1], atol=1e-10)
