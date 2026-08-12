from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from evaluation.tools.longmemeval import qa_client


class LongMemEvalQAClientTests(unittest.TestCase):
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
