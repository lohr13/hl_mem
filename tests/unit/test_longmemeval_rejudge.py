from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.tools import rejudge_longmemeval_results as rejudge
from evaluation.tools import run_longmemeval_benchmark as runner
from hl_mem.settings import Settings


class LongMemEvalJudgePromptTests(unittest.TestCase):
    def test_generic_prompt_accepts_core_answer_with_extra_information(self) -> None:
        system_prompt, user_prompt = runner._longmemeval_judge_prompts(
            case_id="118b2229",
            question_type="single-session-user",
            question="How long is my daily commute to work?",
            answer="45 minutes each way",
            predicted_answer="45 minutes",
        )

        self.assertIn("substantive correct answer", system_prompt)
        self.assertIn("Extra information is allowed", user_prompt)
        self.assertIn("nonessential qualifier", user_prompt)
        self.assertIn("45 minutes each way", user_prompt)

    def test_temporal_prompt_allows_off_by_one_duration(self) -> None:
        _, prompt = runner._longmemeval_judge_prompts(
            case_id="temporal-case",
            question_type="temporal-reasoning",
            question="How many days ago did this happen?",
            answer="18 days",
            predicted_answer="19 days",
        )

        self.assertIn("off-by-one", prompt)
        self.assertIn("days, weeks, months", prompt)

    def test_knowledge_update_prompt_accepts_old_value_with_updated_answer(self) -> None:
        _, prompt = runner._longmemeval_judge_prompts(
            case_id="update-case",
            question_type="knowledge-update",
            question="Where do I live now?",
            answer="Berlin",
            predicted_answer="I moved from Paris to Berlin.",
        )

        self.assertIn("previous information", prompt)
        self.assertIn("updated answer", prompt)

    def test_preference_prompt_does_not_require_every_rubric_point(self) -> None:
        _, prompt = runner._longmemeval_judge_prompts(
            case_id="preference-case",
            question_type="single-session-preference",
            question="What should I cook?",
            answer="Suggest vegetarian and spicy meals.",
            predicted_answer="Try a spicy lentil curry.",
        )

        self.assertIn("does not need to cover every rubric point", prompt)

    def test_abs_case_uses_unanswerable_rule_before_question_type(self) -> None:
        _, prompt = runner._longmemeval_judge_prompts(
            case_id="case_abs",
            question_type="multi-session",
            question="What is my passport number?",
            answer="The conversations never state a passport number.",
            predicted_answer="I do not have enough information.",
        )

        self.assertIn("unanswerable question", prompt)
        self.assertIn("correctly identifies", prompt)

    def test_unknown_answerable_question_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported LongMemEval question_type"):
            runner._longmemeval_judge_prompts(
                case_id="unknown-case",
                question_type="unknown",
                question="Question?",
                answer="Answer",
                predicted_answer="Answer",
            )


class LongMemEvalRejudgeTests(unittest.TestCase):
    def test_main_does_not_forward_extractor_thinking_to_judge(self) -> None:
        report = {
            "schema_version": 1,
            "benchmark": "LongMemEval-S",
            "cases": [
                {
                    "case_id": "case-1",
                    "question_type": "single-session-user",
                    "question": "Q?",
                    "answer": "A",
                    "qa": {"predicted_answer": "A", "correct": True},
                    "error": None,
                }
            ],
        }
        settings = Settings(llm_api_key="settings-key", enable_llm_thinking=True)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            output = Path(directory) / "output.json"
            source.write_text(json.dumps(report), encoding="utf-8")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(rejudge, "load_settings", return_value=settings),
                patch.object(
                    runner,
                    "_judge_longmemeval_answer",
                    return_value=({"correct": True, "reason": "match"}, 3),
                ) as judge,
            ):
                exit_code = rejudge.main([str(source), "--output", str(output)])

        self.assertEqual(exit_code, 0)
        self.assertNotIn("enable_thinking", judge.call_args.kwargs)

    def test_rejudge_preserves_old_verdict_and_reports_both_flip_directions(self) -> None:
        report = {
            "schema_version": 1,
            "benchmark": "LongMemEval-S",
            "cases": [
                {
                    "case_id": "old-wrong",
                    "question_type": "single-session-user",
                    "question": "Q1?",
                    "answer": "A1",
                    "qa": {"predicted_answer": "P1", "correct": False, "reason": "old 1"},
                    "error": None,
                },
                {
                    "case_id": "old-right",
                    "question_type": "single-session-user",
                    "question": "Q2?",
                    "answer": "A2",
                    "qa": {"predicted_answer": "P2", "correct": True, "reason": "old 2"},
                    "error": None,
                },
                {
                    "case_id": "not-evaluated",
                    "question_type": "single-session-user",
                    "question": "Q3?",
                    "answer": "A3",
                    "qa": None,
                    "error": "ReadTimeout",
                },
            ],
        }
        verdicts = {
            "old-wrong": {"correct": True, "reason": "new 1"},
            "old-right": {"correct": False, "reason": "new 2"},
        }

        def judge(case: dict[str, object]) -> tuple[dict[str, object], int]:
            return verdicts[str(case["case_id"])], 7

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shard.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            comparisons = rejudge.rejudge_inputs([path], judge=judge, model="judge-model")

        self.assertEqual([item["change"] for item in comparisons], ["wrong_to_correct", "correct_to_wrong", "skipped"])
        summary = rejudge.summarize_comparisons(comparisons)
        self.assertEqual(summary["total_cases"], 3)
        self.assertEqual(summary["evaluated_cases"], 2)
        self.assertEqual(summary["skipped_cases"], 1)
        self.assertEqual(summary["old_correct"], 1)
        self.assertEqual(summary["new_correct"], 1)
        self.assertEqual(summary["old_accuracy"], 0.5)
        self.assertEqual(summary["new_accuracy"], 0.5)
        self.assertEqual(summary["accuracy_delta"], 0.0)
        self.assertEqual(summary["wrong_to_correct"], 1)
        self.assertEqual(summary["correct_to_wrong"], 1)

    def test_rejudge_rejects_duplicate_case_ids_across_inputs(self) -> None:
        report = {
            "schema_version": 1,
            "benchmark": "LongMemEval-S",
            "cases": [
                {
                    "case_id": "duplicate",
                    "question_type": "multi-session",
                    "question": "Q?",
                    "answer": "A",
                    "qa": {"predicted_answer": "A", "correct": True},
                    "error": None,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            first.write_text(json.dumps(report), encoding="utf-8")
            second.write_text(json.dumps(report), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate case_id"):
                rejudge.rejudge_inputs(
                    [first, second],
                    judge=lambda _case: ({"correct": True}, 1),
                    model="judge-model",
                )


if __name__ == "__main__":
    unittest.main()
