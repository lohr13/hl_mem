from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.tools import run_longmemeval_benchmark as runner
from evaluation.tools.longmemeval import full_context
from evaluation.tools.longmemeval.qa_client import QAUsage
from hl_mem.settings import Settings


class LongMemEvalFullContextRendererTests(unittest.TestCase):
    def test_renderer_orders_sessions_stably_and_preserves_complete_raw_text(self) -> None:
        record = {
            "question_id": "full-context-render",
            "question_type": "multi-session",
            "question": "What happened?",
            "answer": "Earlier, then later.",
            "question_date": "2024/01/03 (Wed) 12:00",
            "answer_session_ids": ["later"],
            "haystack_dates": [
                "2024/01/02 (Tue) 09:00",
                "2024/01/01 (Mon) 09:00",
                "2024/01/01 (Mon) 09:00",
            ],
            "haystack_session_ids": ["later", "same-time-a", "same-time-b"],
            "haystack_sessions": [
                [{"role": "assistant", "content": "later text"}],
                [{"role": "user", "content": "first same-time text", "has_answer": True}],
                [{"role": "user", "content": "第二条原文 " + "x" * 20_000}],
            ],
        }
        case = runner.normalize_case(record)

        rendered = full_context.render_full_context_user_prompt(case)

        self.assertEqual(
            rendered.selected_session_ids,
            ("same-time-a", "same-time-b", "later"),
        )
        self.assertEqual(rendered.session_count, 3)
        self.assertEqual(rendered.message_count, 3)
        self.assertLess(
            rendered.prompt.index("first same-time text"),
            rendered.prompt.index("第二条原文"),
        )
        self.assertLess(
            rendered.prompt.index("第二条原文"),
            rendered.prompt.index("later text"),
        )
        self.assertIn("第二条原文 " + "x" * 20_000, rendered.prompt)
        self.assertNotIn("has_answer", rendered.prompt)
        self.assertIn("Session Date: 2024-01-01T09:00:00+00:00", rendered.prompt)
        self.assertIn("Current Date: 2024-01-03T12:00:00+00:00", rendered.prompt)
        self.assertTrue(rendered.prompt.endswith("Question: What happened?\nAnswer:"))


class LongMemEvalFullContextRunnerTests(unittest.TestCase):
    def test_mode_selects_a_distinct_default_output(self) -> None:
        production = runner.parse_args([])
        control = runner.parse_args(["--mode", "full-context"])

        self.assertEqual(production.mode, "hl-mem")
        self.assertEqual(production.output, runner.DEFAULT_OUTPUT)
        self.assertEqual(control.mode, "full-context")
        self.assertEqual(control.output, runner.DEFAULT_FULL_CONTEXT_OUTPUT)
        self.assertTrue(control.output.name.startswith("longmemeval_fullcontext"))

    def test_control_run_bypasses_production_pipeline_and_records_identity(self) -> None:
        record = {
            "question_id": "control-case",
            "question_type": "multi-session",
            "question": "What degree did I study?",
            "answer": "Business Administration",
            "question_date": "2024/01/03 (Wed) 12:00",
            "answer_session_ids": ["answer-session"],
            "haystack_dates": ["2024/01/01 (Mon) 09:00"],
            "haystack_session_ids": ["answer-session"],
            "haystack_sessions": [
                [{"role": "user", "content": "I studied Business Administration."}],
            ],
        }
        settings = Settings(
            llm_api_key="test-key",
            llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            llm_model="deepseek-v4-flash-0731",
        )
        reader_usage = QAUsage(1_000, 220, 180, 1_220)
        judge_usage = QAUsage(100, 20, 0, 120)
        responses = [
            ("Business Administration", reader_usage),
            ('{"correct":true,"reason":"matches"}', judge_usage),
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "control.json"
            output = root / "longmemeval_fullcontext_smoke.json"
            dataset.write_text(json.dumps([record]), encoding="utf-8")
            with (
                patch.object(runner, "load_settings", return_value=settings),
                patch.object(
                    runner,
                    "initialize_process",
                    side_effect=AssertionError("production initialization must be bypassed"),
                ),
                patch.object(runner, "_qa_dashscope_chat_detailed", side_effect=responses) as chat,
            ):
                exit_code = runner.main(
                    [
                        "--mode",
                        "full-context",
                        "--dataset",
                        str(dataset),
                        "--output",
                        str(output),
                        "--limit",
                        "1",
                    ]
                )

            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(chat.call_count, 2)
        reader_call = chat.call_args_list[0]
        self.assertEqual(reader_call.kwargs["timeout_seconds"], 300)
        self.assertEqual(reader_call.kwargs["thinking_budget"], 2048)
        self.assertIn("complete timestamped chat history", reader_call.args[3])
        self.assertEqual(report["control"], "full-context")
        self.assertEqual(report["run"]["mode"], "full-context")
        self.assertEqual(report["run"]["reader_timeout_seconds"], 300)
        self.assertEqual(len(report["dataset"]["sha256"]), 64)
        case = report["cases"][0]
        self.assertIsNone(case["database"])
        self.assertEqual(case["retrieval"]["selector"], "all-sessions")
        self.assertTrue(case["retrieval"]["all_sessions_selected"])
        self.assertEqual(case["qa"]["usage"]["reader_input_tokens"], 1_000)
        self.assertEqual(case["qa"]["usage"]["reader_reasoning_tokens"], 180)
        self.assertEqual(case["qa"]["usage"]["judge_output_tokens"], 20)
        self.assertEqual(case["cost"]["currency"], "CNY")
        self.assertGreater(case["cost"]["total_cny"], 0)
        self.assertEqual(report["metrics"]["overall"]["retrieval_reported_cases"], 0)
        self.assertEqual(report["metrics"]["overall"]["extraction_coverage_denominator"], 0)


if __name__ == "__main__":
    unittest.main()
