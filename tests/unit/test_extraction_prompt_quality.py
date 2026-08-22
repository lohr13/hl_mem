"""极简 LLM 提取 prompt 的质量约束测试。"""

from __future__ import annotations

import unittest

from hl_mem.ingest.llm_extractor import ENGLISH_SYSTEM_PROMPT, SYSTEM_PROMPT


class ExtractionPromptQualityTest(unittest.TestCase):
    """防止紧凑输出契约和准入边界回退。"""

    def test_prompt_contains_compact_contract_and_core_guidance(self) -> None:
        for expected in (
            "对未来有价值的原子事实",
            "脱离上下文仍可理解",
            "subject 用标准名称",
            '"kind"',
            '"notability"',
            '"evidence_quote"',
            '"source_event_indices"',
            "confidence",
            "max 20 claims per chunk",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

    def test_prompt_defines_all_six_kinds(self) -> None:
        for expected in (
            "preference：用户偏好/习惯/工作方式",
            "architecture：已执行的架构决策、系统结构、组件关系",
            "identity：用户名、硬件、角色等身份信息",
            "config：端口、路径、模型名、API 地址等技术配置",
            "fact：其他客观事实，包括一次性事件及其可回答细节",
            "plan：已确认的计划和截止日期",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

    def test_prompt_leaves_derived_fields_to_postprocessing(self) -> None:
        for removed in (
            "canonical_attribute",
            "canonical_slot",
            "topic_tags",
            "importance",
            "occurred_start",
            "scope×volatility",
            "四门",
        ):
            self.assertNotIn(removed, SYSTEM_PROMPT)

    def test_prompt_keeps_notability_and_exclusion_boundaries(self) -> None:
        for expected in (
            "high：核心身份、永久偏好、关键架构决策",
            "medium：重要配置、项目特征、一般事实",
            "low：一次性事件及其数字、时间、地点、专名或耗时细节，进入 episodic 层",
            "服务健康快照、CI 测试数量、版本号查询结果、过程进度、纯问候、未确认建议",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

        self.assertIn("Low means episodic, not disposable", ENGLISH_SYSTEM_PROMPT)
        self.assertIn("IKEA bookcase", ENGLISH_SYSTEM_PROMPT)
        self.assertIn("IKEA 书架", SYSTEM_PROMPT)

    def test_prompts_align_compound_relation_and_enumeration_guidance(self) -> None:
        """双语提示都应保护复合属性、关系动作和枚举原子项。"""
        for expected in (
            "只输出「用户将参加首席记者能力提升营」",
            "参加 Emily 的婚礼",
            "把旧靴子退回 Zara",
            "一次性事件也不得省略",
            "枚举中的每个可独立回答项",
            "不得计算原文未明确陈述的总数",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

        for expected in (
            "emit only that the user will attend the Lead Reporters Development Camp",
            "attended Emily's wedding",
            "return the old boots to Zara",
            "Do not omit one-off events",
            "Each independently answerable item in an enumeration",
            "Do not calculate a total that the source does not explicitly state",
        ):
            self.assertIn(expected, ENGLISH_SYSTEM_PROMPT)

    def test_prompts_forbid_replacing_named_entities_with_generic_roles(self) -> None:
        """双语提示必须把具体名字作为不可被摘要覆盖的原始事实。"""
        for expected in (
            "具体名字是不可丢失信息",
            "刘梅泛化成“陌生人”",
            "小飞（熊飞）",
            "张强是小飞的同学",
            "具体人物：刘晓",
            "subject=刘晓",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

        for expected in (
            "A specific name is lossless source data",
            "Maya with a generic role such as stranger",
            "May (Maya)",
            "Maya is Priya's college classmate",
            "Named person: Maya",
            "subject=Maya",
        ):
            self.assertIn(expected, ENGLISH_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
