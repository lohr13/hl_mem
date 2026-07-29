"""LLM 提取 prompt 的质量约束测试。"""

from __future__ import annotations

import unittest

from hl_mem.ingest.llm_extractor import SYSTEM_PROMPT


class ExtractionPromptQualityTest(unittest.TestCase):
    """防止自足性、主体和 attribute 边界约束回退。"""

    def test_prompt_contains_self_containment_subject_and_attribute_guidance(
        self,
    ) -> None:
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

    def test_prompt_keeps_confidence_independent_from_memory_deduplication(
        self,
    ) -> None:
        self.assertIn("不判断它们是否与已有记忆冲突", SYSTEM_PROMPT)
        self.assertIn(
            "confidence 只表示当前 evidence 是否足以支持 claim 的内容和归因",
            SYSTEM_PROMPT,
        )
        self.assertIn("不表示 importance", SYSTEM_PROMPT)
        self.assertIn("与已有记忆是否一致", SYSTEM_PROMPT)
        self.assertNotIn("已有事实的补充或改写", SYSTEM_PROMPT)
        self.assertNotIn("confidence 降到 0.5 以下", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
