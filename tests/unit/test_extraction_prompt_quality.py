"""LLM 提取 prompt 的质量约束测试。"""

from __future__ import annotations

import unittest

from hl_mem.ingest.llm_extractor import SYSTEM_PROMPT


class ExtractionPromptQualityTest(unittest.TestCase):
    """防止自足性、主体和 attribute 边界约束回退。"""

    def test_prompt_contains_self_containment_subject_and_attribute_guidance(self) -> None:
        for expected in (
            "脱离原对话上下文",
            "speaker_entity",
            "semantic_subject",
            "fact.architecture",
            "fact.api_design",
            "config.timeout",
            "config.policy",
            "preference.workflow",
            "config.path",
            "confidence",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

    def test_prompt_contains_required_counterexamples(self) -> None:
        for expected in (
            '❌ "串行" → ✅ "LLM 提取任务串行执行"',
            '❌ "90s" → ✅ "LLM 请求超时为 90 秒"',
            "subject=coding-workflow",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

    def test_prompt_keeps_confidence_independent_from_memory_deduplication(self) -> None:
        self.assertIn("每条 claim 独立判断事实可信度", SYSTEM_PROMPT)
        self.assertIn("不要猜测它与已有记忆的关系", SYSTEM_PROMPT)
        self.assertNotIn("已有事实的补充或改写", SYSTEM_PROMPT)
        self.assertNotIn("confidence 降到 0.5 以下", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
