"""Focused unit checks for the no-answer calibration analysis helpers.

Run directly; this intentionally does not depend on pytest.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.run_no_answer_calibration import (  # noqa: E402
    binary_metrics,
    scan_and_gate,
    scan_thresholds,
    select_operating_point,
)


class CalibrationMetricTests(unittest.TestCase):
    def test_binary_metrics_reports_both_answerability_directions(self) -> None:
        metrics = binary_metrics(
            scores=[0.90, 0.70, 0.60, 0.20],
            answerable=[True, False, True, False],
            threshold=0.65,
        )

        self.assertEqual(metrics["confusion"], {"tp": 1, "fp": 1, "fn": 1, "tn": 1})
        self.assertEqual(metrics["accepted_queries"], 2)
        self.assertEqual(metrics["rejected_queries"], 2)
        self.assertAlmostEqual(metrics["precision"], 0.5)
        self.assertAlmostEqual(metrics["recall"], 0.5)
        self.assertAlmostEqual(metrics["no_answer_precision"], 0.5)
        self.assertAlmostEqual(metrics["no_answer_recall"], 0.5)
        self.assertAlmostEqual(metrics["macro_f1"], 0.5)

    def test_threshold_scan_is_inclusive_and_uses_decimal_steps(self) -> None:
        scan = scan_thresholds([0.9, 0.2], [True, False])

        self.assertEqual(len(scan), 51)
        self.assertEqual(scan[0]["threshold"], 0.30)
        self.assertEqual(scan[-1]["threshold"], 0.80)

    def test_selection_prefers_highest_recall_under_precision_constraint(self) -> None:
        scan = scan_thresholds(
            scores=[0.95, 0.85, 0.75, 0.65, 0.70, 0.20],
            answerable=[True, True, True, True, False, False],
        )

        selected = select_operating_point(scan, min_precision=0.90)

        self.assertEqual(selected["selection_rule"], "precision>=0.90_then_max_recall")
        self.assertAlmostEqual(selected["metrics"]["recall"], 0.75)
        self.assertGreaterEqual(selected["metrics"]["precision"], 0.90)

    def test_and_gate_requires_both_features_to_pass(self) -> None:
        scan = scan_and_gate(
            dense_scores=[0.90, 0.90, 0.40, 0.40],
            reranker_scores=[0.90, 0.40, 0.90, 0.40],
            answerable=[True, False, False, False],
            thresholds=[0.50],
        )

        self.assertEqual(len(scan), 1)
        self.assertEqual(scan[0]["confusion"], {"tp": 1, "fp": 0, "fn": 0, "tn": 3})


if __name__ == "__main__":
    unittest.main()
