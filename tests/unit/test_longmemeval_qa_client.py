from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from evaluation.tools import run_longmemeval_benchmark as runner
from evaluation.tools.longmemeval import qa_client
from hl_mem.settings import Settings


class LongMemEvalQAClientTests(unittest.TestCase):
    def test_judge_transport_uses_independent_environment_overrides(self) -> None:
        self._assert_judge_transport(
            {
                "HL_MEM_EVAL_JUDGE_MODEL": " qwen3.7-plus ",
                "HL_MEM_EVAL_JUDGE_BASE_URL": " https://judge.example.test/v1 ",
                "HL_MEM_EVAL_JUDGE_API_KEY": " judge-key ",
            },
            model="qwen3.7-plus",
            base_url="https://judge.example.test/v1",
            api_key="judge-key",
        )

    def test_judge_transport_falls_back_to_qa_configuration(self) -> None:
        self._assert_judge_transport(
            {},
            model="glm-5.3-flash",
            base_url="https://reader.example.test/v1",
            api_key="reader-key",
        )

    def _assert_judge_transport(self, overrides: dict[str, str], *, model: str, base_url: str, api_key: str) -> None:
        requests: list[httpx.Request] = []
        real_client = httpx.Client

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            content = "Paris" if len(requests) == 1 else '{"correct": true, "reason": "matches"}'
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                },
            )

        case = runner.normalize_case(
            {
                "question_id": "judge-config",
                "question_type": "single-session-user",
                "question": "Where do I live?",
                "answer": "Paris",
                "question_date": "2024/01/03 (Wed) 12:00",
                "answer_session_ids": ["session-1"],
                "haystack_session_ids": ["session-1"],
                "haystack_dates": ["2024/01/01 (Mon) 12:00"],
                "haystack_sessions": [[{"role": "user", "content": "I live in Paris."}]],
            }
        )
        settings = Settings(llm_api_key="settings-key", llm_base_url="https://settings.example.test/v1")
        environment = {
            "HL_MEM_EVAL_QA_MODEL": "glm-5.3-flash",
            "HL_MEM_EVAL_QA_BASE_URL": "https://reader.example.test/v1",
            "HL_MEM_EVAL_QA_API_KEY": "reader-key",
            **overrides,
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                qa_client.httpx,
                "Client",
                side_effect=lambda **_kwargs: real_client(transport=httpx.MockTransport(handle_request)),
            ),
        ):
            result = runner._run_qa(None, case, [], settings)
            usages: list[qa_client.QAUsage] = []
            judgment, tokens = runner._judge_longmemeval_answer(
                api_key="reader-key",
                base_url="https://reader.example.test/v1",
                model="glm-5.3-flash",
                case_id=case.case_id,
                question_type=case.question_type,
                question=case.question,
                answer=case.answer,
                predicted_answer="Paris",
                usage_details=usages,
            )
            self.assertEqual(len(requests), 3)
            for request in requests[1:]:
                self.assertEqual(str(request.url), f"{base_url}/chat/completions")
                self.assertEqual(request.headers["Authorization"], f"Bearer {api_key}")
                payload = json.loads(request.content)
                self.assertEqual(payload["model"], model)
                self.assertFalse(payload["enable_thinking"])
                self.assertEqual(payload["temperature"], 0.0)
                self.assertEqual(payload["response_format"], {"type": "json_object"})
            args = runner.parse_args([])
            args.dataset = Path(__file__).resolve().parents[1] / "fixtures" / "longmemeval_small.json"
            args.dataset_sha256 = "fixture-sha256"
            for report_builder, validate_resume in (
                (runner._report, runner._validate_resume_report),
                (runner._full_context_report, runner._validate_full_context_resume_report),
                (runner._native_rag_report, runner._validate_native_rag_resume_report),
            ):
                report = report_builder(args, settings, [], "2024-01-03T12:00:00+00:00", "completed")
                self.assertEqual(report["run"]["models"]["judge"], model)
                self.assertEqual(report["run"]["models"]["reader"], "glm-5.3-flash")
                validate_resume(report, args, settings)

        self.assertEqual(str(requests[0].url), "https://reader.example.test/v1/chat/completions")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer reader-key")
        reader_payload = json.loads(requests[0].content)
        self.assertEqual(reader_payload["model"], "glm-5.3-flash")
        self.assertTrue(reader_payload["enable_thinking"])
        self.assertEqual(result["model"], "glm-5.3-flash")
        self.assertTrue(result["correct"])
        self.assertEqual(result["usage"], {"reader_tokens": 12, "judge_tokens": 12, "total_tokens": 24})
        self.assertTrue(judgment["correct"])
        self.assertEqual(tokens, 12)
        self.assertEqual(usages, [qa_client.QAUsage(10, 2, 0, 12)])

    def test_detailed_chat_reports_usage_and_honors_control_timeout(self) -> None:
        detailed_chat = getattr(qa_client, "qa_dashscope_chat_detailed", None)
        self.assertIsNotNone(detailed_chat, "full-context controls need detailed QA usage")
        observed_timeouts: list[float] = []
        real_client = httpx.Client

        def handle_request(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Paris"}}],
                    "usage": {
                        "prompt_tokens": 120_000,
                        "completion_tokens": 2_200,
                        "total_tokens": 122_200,
                        "completion_tokens_details": {"reasoning_tokens": 2_048},
                    },
                },
            )

        def client_factory(*, timeout: float) -> httpx.Client:
            observed_timeouts.append(timeout)
            return real_client(transport=httpx.MockTransport(handle_request))

        with patch.object(qa_client.httpx, "Client", side_effect=client_factory):
            text, usage = detailed_chat(
                "key",
                "https://example.test/v1",
                "deepseek-v4-flash-0731",
                "system",
                "user",
                enable_thinking=True,
                thinking_budget=2_048,
                max_tokens=2_560,
                timeout_seconds=300.0,
            )

        self.assertEqual(text, "Paris")
        self.assertEqual(observed_timeouts, [300.0])
        self.assertEqual(usage.input_tokens, 120_000)
        self.assertEqual(usage.output_tokens, 2_200)
        self.assertEqual(usage.reasoning_tokens, 2_048)
        self.assertEqual(usage.answer_tokens, 152)
        self.assertEqual(usage.total_tokens, 122_200)


if __name__ == "__main__":
    unittest.main()
