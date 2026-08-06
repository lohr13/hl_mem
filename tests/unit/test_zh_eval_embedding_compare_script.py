from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.run_zh_eval_embedding_compare import (
    _embedding_config,
    build_extraction_content,
    compute_extraction_metrics,
    evaluate_retrieval_rankings,
    extract_once,
)


def _claim(subject: str, predicate: str, value: str) -> dict[str, object]:
    return {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "should_extract": True,
    }


class ZhEvalEmbeddingCompareTests(unittest.TestCase):
    def test_build_extraction_content_preserves_session_boundaries(self) -> None:
        case = {
            "case_id": "case-1",
            "conversation": [
                {"role": "user", "content": "第一天的问题", "session_id": "s01"},
                {"role": "assistant", "content": "第一天的回答", "session_id": "s01"},
                {"role": "user", "content": "第二天的问题", "session_id": "s02"},
                {"role": "assistant", "content": "第二天的回答", "session_id": "s02"},
            ],
        }

        content = build_extraction_content(case)

        self.assertEqual(content["text"].count("[Session"), 2)
        self.assertIn("[Session s01]", content["text"])
        self.assertIn("[Session s02]", content["text"])
        self.assertEqual(
            content["messages"],
            [
                {"role": "user", "content": "第一天的问题"},
                {"role": "assistant", "content": "第一天的回答"},
                {"role": "user", "content": "第二天的问题"},
                {"role": "assistant", "content": "第二天的回答"},
            ],
        )

    def test_extraction_metrics_count_noise_claims_as_over_extraction(self) -> None:
        cases = [
            {
                "case_id": "positive",
                "category": "user_preference",
                "gold_claims": [_claim("用户", "preference", "偏好喝绿茶")],
            },
            {"case_id": "noise", "category": "noise", "gold_claims": []},
        ]
        extracted = {
            "positive": [
                _claim("用户", "preference", "偏好喝绿茶"),
                _claim("用户", "fact", "今天杯子放在桌上"),
            ],
            "noise": [_claim("用户", "fact", "页面刚刚刷新完成")],
        }

        metrics = compute_extraction_metrics(cases, extracted, value_threshold=0.62)

        self.assertEqual(metrics["matched_claims"], 1)
        self.assertEqual(metrics["predicted_claims"], 3)
        self.assertEqual(metrics["over_extracted"], 2)
        self.assertEqual(metrics["noise_over_extracted"], 1)
        self.assertAlmostEqual(metrics["precision"], 1 / 3)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertAlmostEqual(metrics["f1"], 0.5)

    def test_retrieval_metrics_use_first_matching_claim_rank(self) -> None:
        tea = _claim("用户", "preference", "偏好在早上喝绿茶")
        swim = _claim("用户", "event", "每周日去游泳")
        cases = [
            {
                "case_id": "case-1",
                "category": "mixed",
                "gold_claims": [tea, swim],
            }
        ]
        extracted = {"case-1": [tea, swim]}
        rankings = {
            "case-1:g000": [swim, tea],
            "case-1:g001": [swim, tea],
        }

        metrics = evaluate_retrieval_rankings(
            cases,
            extracted,
            rankings,
            value_threshold=0.62,
        )

        self.assertEqual(metrics["queries"], 2)
        self.assertEqual(metrics["recall_at_1"], 0.5)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 0.75)
        self.assertEqual(metrics["precision_at_5"], 0.5)
        self.assertAlmostEqual(metrics["f1_at_5"], 2 / 3)

    def test_extract_once_reuses_matching_persistent_cache(self) -> None:
        class FakeExtractor:
            def __init__(self) -> None:
                self.calls = 0
                self.last_input_tokens = 11
                self.last_output_tokens = 7
                self.last_usage_tokens = 18

            def extract(self, content: object, context: object) -> list[dict[str, object]]:
                del content, context
                self.calls += 1
                return [_claim("用户", "fact", f"事实 {self.calls}")]

        cases = [
            {
                "case_id": "case-1",
                "category": "identity_info",
                "conversation": [
                    {"role": "user", "content": "我的职业是编辑"},
                    {"role": "assistant", "content": "知道了"},
                ],
                "gold_claims": [],
            },
            {
                "case_id": "case-2",
                "category": "identity_info",
                "conversation": [
                    {"role": "user", "content": "我住在昆明"},
                    {"role": "assistant", "content": "知道了"},
                ],
                "gold_claims": [],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "extractions.json"
            first = FakeExtractor()
            first_result = extract_once(
                cases,
                first,
                cache,
                dataset_sha256="dataset-v1",
                model="model-v1",
                extractor_version="prompt-v1",
            )
            second = FakeExtractor()
            second_result = extract_once(
                cases,
                second,
                cache,
                dataset_sha256="dataset-v1",
                model="model-v1",
                extractor_version="prompt-v1",
            )

        self.assertEqual(first.calls, 2)
        self.assertEqual(second.calls, 0)
        self.assertEqual(first_result["claims_by_case"], second_result["claims_by_case"])
        self.assertEqual(second_result["api_calls_this_run"], 0)
        self.assertEqual(second_result["cache_hits"], 2)

    def test_embedding_configs_keep_q3_and_q4_roles_distinct(self) -> None:
        q3 = _embedding_config("Q3")
        q4 = _embedding_config("Q4")

        self.assertFalse(q3.use_text_type)
        self.assertTrue(q3.use_instruct)
        self.assertFalse(q3.use_sparse)
        self.assertTrue(q4.use_text_type)
        self.assertFalse(q4.use_instruct)
        self.assertTrue(q4.use_sparse)


if __name__ == "__main__":
    unittest.main()
