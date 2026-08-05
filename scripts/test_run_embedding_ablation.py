"""Standalone unit checks for run_embedding_ablation.py (no network, no pytest)."""

from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path

import numpy as np

RUNNER_PATH = Path(__file__).with_name("run_embedding_ablation.py")
SPEC = importlib.util.spec_from_file_location("run_embedding_ablation", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class RequestConstructionTests(unittest.TestCase):
    def test_stepwise_native_parameters_are_isolated(self) -> None:
        _, q1_doc = runner.build_request(runner.CONFIGS["Q1"], "document", ["d"])
        _, q1_query = runner.build_request(runner.CONFIGS["Q1"], "query", ["q"])
        _, q2_doc = runner.build_request(runner.CONFIGS["Q2"], "document", ["d"])
        _, q2_query = runner.build_request(runner.CONFIGS["Q2"], "query", ["q"])
        _, q3_doc = runner.build_request(runner.CONFIGS["Q3"], "document", ["d"])
        _, q3_query = runner.build_request(runner.CONFIGS["Q3"], "query", ["q"])
        _, q4_doc = runner.build_request(runner.CONFIGS["Q4"], "document", ["d"])
        _, q4_query = runner.build_request(runner.CONFIGS["Q4"], "query", ["q"])

        self.assertEqual(q1_doc["parameters"], {"dimension": 2048})
        self.assertEqual(q1_query["parameters"], {"dimension": 2048})
        self.assertEqual(q2_doc["parameters"], {"dimension": 2048, "text_type": "document"})
        self.assertEqual(q2_query["parameters"], {"dimension": 2048, "text_type": "query"})
        self.assertNotIn("instruct", q3_doc["parameters"])
        self.assertEqual(q3_query["parameters"]["instruct"], runner.QUERY_INSTRUCT)
        self.assertEqual(q4_doc["parameters"]["output_type"], "dense&sparse")
        self.assertEqual(q4_query["parameters"]["output_type"], "dense&sparse")
        self.assertNotIn("instruct", q4_doc["parameters"])
        self.assertEqual(q4_query["parameters"]["instruct"], runner.QUERY_INSTRUCT)

    def test_compatible_payload_uses_flat_input_and_dimensions(self) -> None:
        path, payload = runner.build_request(runner.CONFIGS["V0"], "query", ["a", "b"])
        self.assertEqual(path, "/compatible-mode/v1/embeddings")
        self.assertEqual(
            payload,
            {"model": "text-embedding-v4", "input": ["a", "b"], "dimensions": 2048},
        )


class ResponseParsingTests(unittest.TestCase):
    def test_native_dense_and_sparse_are_parsed_in_text_index_order(self) -> None:
        response = {
            "output": {
                "embeddings": [
                    {
                        "text_index": 1,
                        "embedding": [0.0, 1.0],
                        "sparse_embedding": [{"index": 8, "value": 0.5}],
                    },
                    {
                        "text_index": 0,
                        "embedding": [1.0, 0.0],
                        "sparse_embedding": [{"index": 3, "value": 0.25}],
                    },
                ]
            },
            "usage": {"total_tokens": 7},
        }
        dense, sparse, tokens = runner.parse_api_response(
            runner.CONFIGS["Q4"], response, expected_count=2, expected_dim=2
        )
        np.testing.assert_allclose(dense, np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
        self.assertEqual(sparse, [{3: 0.25}, {8: 0.5}])
        self.assertEqual(tokens, 7)

    def test_compatible_rows_are_sorted_by_index(self) -> None:
        response = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ],
            "usage": {"total_tokens": 4},
        }
        dense, sparse, tokens = runner.parse_api_response(
            runner.CONFIGS["V0"], response, expected_count=2, expected_dim=2
        )
        np.testing.assert_allclose(dense, np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
        self.assertIsNone(sparse)
        self.assertEqual(tokens, 4)


class MetricTests(unittest.TestCase):
    def test_average_precision_and_false_merge_rate(self) -> None:
        labels = np.asarray([1, 0, 1, 0], dtype=np.int8)
        scores = np.asarray([0.9, 0.8, 0.7, 0.1], dtype=np.float64)
        self.assertTrue(math.isclose(runner.average_precision(labels, scores), 5.0 / 6.0))
        metrics = runner.classification_metrics(labels, scores, threshold=0.75)
        self.assertEqual(metrics["tp"], 1)
        self.assertEqual(metrics["fp"], 1)
        self.assertTrue(math.isclose(metrics["precision"], 0.5))
        self.assertTrue(math.isclose(metrics["recall"], 0.5))
        self.assertTrue(math.isclose(metrics["false_merge_rate"], 0.5))

    def test_low_score_threshold_calibration_separates_no_answer(self) -> None:
        labels = np.asarray([1, 1, 0, 0], dtype=np.int8)
        scores = np.asarray([0.1, 0.2, 0.8, 0.9], dtype=np.float64)
        selected = runner.select_threshold(labels, scores, positive_when_low=True)
        self.assertGreater(selected["threshold"], 0.2)
        self.assertLessEqual(selected["threshold"], 0.8)
        self.assertEqual(selected["f1"], 1.0)

    def test_rrf_uses_rank_not_raw_score(self) -> None:
        dense = np.asarray([0.9, 0.8, 0.1], dtype=np.float64)
        sparse = np.asarray([0.1, 0.8, 0.9], dtype=np.float64)
        fused = runner.rrf_fuse(dense, sparse, dense_weight=0.6, rank_constant=60)
        self.assertEqual(np.argsort(-fused, kind="stable").tolist(), [0, 1, 2])

    def test_rrf_ranking_uses_dense_similarity_as_confidence(self) -> None:
        rows = [{"id": "q1"}]
        ranking_scores = np.asarray([[0.7, 0.9]], dtype=np.float64)
        dense_confidence = np.asarray([[0.8, 0.4]], dtype=np.float64)
        rankings, top1 = runner._rankings(
            rows,
            ["a", "b"],
            ranking_scores,
            confidence_scores=dense_confidence,
        )
        self.assertEqual(rankings["q1"][:2], ["b", "a"])
        self.assertEqual(top1["q1"], 0.4)

    def test_recall_metrics_use_strict_gold_and_report_group_aware_diagnostic(self) -> None:
        rows = [
            {
                "id": "q1",
                "gold_ids": ["a"],
                "gold_groups": [["a", "x"]],
                "no_answer": False,
            },
            {
                "id": "q2",
                "gold_ids": ["b"],
                "gold_groups": [["b"]],
                "no_answer": False,
            },
            {"id": "q3", "gold_ids": [], "gold_groups": [], "no_answer": True},
            {"id": "q4", "gold_ids": [], "gold_groups": [], "no_answer": True},
        ]
        rankings = {"q1": ["x", "a", "z"], "q2": ["b", "z"], "q3": ["z"], "q4": ["z"]}
        top1 = {"q1": 0.9, "q2": 0.8, "q3": 0.2, "q4": 0.4}
        metrics = runner.recall_metrics(rows, rankings, top1, no_answer_threshold=0.3, top_k=5)
        self.assertEqual(metrics["hit_at_5"], 1.0)
        self.assertEqual(metrics["mrr"], 0.75)
        self.assertEqual(metrics["group_aware_hit_at_5"], 1.0)
        self.assertEqual(metrics["group_aware_mrr"], 1.0)
        self.assertEqual(metrics["no_answer_precision"], 0.5)
        self.assertEqual(metrics["predicted_no_answer_precision"], 1.0)
        self.assertTrue(math.isclose(metrics["no_answer_f1"], 2.0 / 3.0))
        self.assertEqual(metrics["answerability_balanced_accuracy"], 0.75)

    def test_best_by_metric_preserves_ties_and_uses_balanced_answerability(self) -> None:
        results = [
            {
                "config": "Q0",
                "pair_metrics": {"pr_auc": 0.8, "best_f1": 0.7},
                "recall_metrics": {
                    "hit_at_5": 0.9,
                    "mrr": 0.7,
                    "no_answer_precision": 0.2,
                    "answerability_balanced_accuracy": 0.8,
                },
            },
            {
                "config": "Q4",
                "pair_metrics": {"pr_auc": 0.8, "best_f1": 0.6},
                "recall_metrics": {
                    "hit_at_5": 0.9,
                    "mrr": 0.6,
                    "no_answer_precision": 1.0,
                    "answerability_balanced_accuracy": 0.5,
                },
            },
        ]
        best = runner._best_by_metric(results)
        self.assertEqual(best["pair_pr_auc"], {"value": 0.8, "configs": ["Q0", "Q4"]})
        self.assertEqual(best["recall_hit_at_5"], {"value": 0.9, "configs": ["Q0", "Q4"]})
        self.assertEqual(best["answerability_balanced_accuracy"], {"value": 0.8, "configs": ["Q0"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
