from __future__ import annotations

import unittest

from evaluation.tools import run_longmemeval_benchmark as runner
from evaluation.tools.longmemeval import full_context


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


if __name__ == "__main__":
    unittest.main()
