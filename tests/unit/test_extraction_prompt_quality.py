"""极简 LLM 提取 prompt 的质量约束测试。"""

from __future__ import annotations

import unittest

from hl_mem.ingest.llm_extractor import ENGLISH_SYSTEM_PROMPT, SYSTEM_PROMPT


class ExtractionPromptQualityTest(unittest.TestCase):
    """防止紧凑输出契约和准入边界回退。"""

    def test_prompt_contains_compact_contract_and_core_guidance(self) -> None:
        for expected in (
            "对未来有价值且上下文完整的记忆",
            "脱离上下文仍可理解",
            "subject 用标准名称",
            '"kind"',
            '"notability"',
            '"evidence_quote"',
            '"source_event_indices"',
            "confidence",
            "最多 16 条",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

    def test_prompts_define_context_rich_claim_budget(self) -> None:
        zh_lines = (
            "- 粒度：一条记忆对应一个可独立更新、冲突或遗忘的主题、决策或状态变化；同一主题且生命周期相同的背景应合并。",
            "- 完整性：长期偏好、已采用决策、重要配置、明确计划和状态迁移只要有证据就必须提取；不同主题或可独立变化的槽位不得合并或遗漏；这些类别任一存在时返回空 claims 属于错误。",
            "- 精确性：不得改写、互换或推算日期、时间、频率、持续期、数量及审批条件；状态迁移应保留旧值、新值、生效时间和原因；value 中的每个断言都必须由 evidence_quote 直接支持。",
            "- 数量由原文决定：可以输出 0 条，不需要凑数；通常不超过 12 条，最多 16 条；必须先按 notability 的 high、medium、low 顺序，再按 confidence 从高到低排列。",
            "- 保真：在相关记忆中保留具体姓名、日期、数字、单位和约束；不要仅因一句话包含多个名词、数字或从句就拆成多条。",
        )
        en_lines = (
            "- Granularity: one memory represents a topic, decision, or state change that can be independently updated, contradicted, or forgotten; merge context with the same topic and lifetime.",
            "- Completeness: always extract evidenced lasting preferences, adopted decisions, important configuration, confirmed plans, and state transitions; never merge or omit distinct topics or independently changing slots; returning empty claims is an error when any of these categories is present.",
            "- Precision: never rewrite, swap, or calculate dates, times, frequencies, durations, quantities, or approval conditions; a state transition must retain its old value, new value, effective time, and reason; every assertion in value must be directly supported by evidence_quote.",
            "- Let the source determine the count: zero is valid and no padding is needed; normally return at most 12 claims, never more than 16; order strictly by notability high, medium, low, then by confidence descending.",
            "- Fidelity: keep relevant names, dates, numbers, units, and constraints inside the corresponding memory; do not split merely because a sentence has multiple nouns, numbers, or clauses.",
        )

        for expected in zh_lines:
            self.assertEqual(SYSTEM_PROMPT.count(expected), 1)
        for expected in en_lines:
            self.assertEqual(ENGLISH_SYSTEM_PROMPT.count(expected), 1)
        self.assertIn(f"{zh_lines[4]}\n限制：", SYSTEM_PROMPT)
        self.assertIn(f"{en_lines[4]}\nLimits:", ENGLISH_SYSTEM_PROMPT)
        self.assertNotIn("12–30", SYSTEM_PROMPT)
        self.assertNotIn("12–30", ENGLISH_SYSTEM_PROMPT)
        self.assertNotIn("Maximum 30 claims per chunk", ENGLISH_SYSTEM_PROMPT)

    def test_prompt_defines_all_six_kinds(self) -> None:
        for expected in (
            "preference：用户偏好/习惯/工作方式",
            "architecture：已执行的架构决策、系统结构、组件关系",
            "identity：用户名、硬件、角色等身份信息",
            "config：端口、路径、模型名、API 地址等技术配置",
            "fact：已完成的动作、当前状态、已生效决定及其他客观事实；后文已确认完成时，不得因前文出现“决定将”“将”或“计划”仍分类为 plan。",
            "plan：明确仍待执行的行动，尤其是有未来日期、截止时间、周期、时间窗或条件的安排。",
        ):
            self.assertIn(expected, SYSTEM_PROMPT)

    def test_prompts_define_personal_semantics_speaker_and_status_contract(self) -> None:
        zh_lines = (
            "- 个人语义：显式归因给某人的观点、信念、理解、感受、行为原因和实践原则若对未来问答有用，必须保留其内容；不得只记该人物讨论过某主题。",
            "- 说话人绑定：形如「姓名：发言」时，第一人称代词和个人陈述属于冒号前姓名，不得改成泛化的“用户”；提问、未采纳引语和助手通识不得当作该人物的观点。",
            "- 助手边界：助手关于自身身份、偏好、感受、计划或对话承诺的陈述不进入长期记忆，除非内容本身是可复用交付物、配置或已采纳项目决策。",
            "- fact：已完成的动作、当前状态、已生效决定及其他客观事实；后文已确认完成时，不得因前文出现“决定将”“将”或“计划”仍分类为 plan。",
            "- plan：明确仍待执行的行动，尤其是有未来日期、截止时间、周期、时间窗或条件的安排。",
        )
        en_lines = (
            "- Personal meaning: preserve the content of an explicitly attributed viewpoint, belief, interpretation, feeling, behavioral reason, or practice principle when it can help a future answer; do not retain only that the person discussed a topic.",
            "- Speaker binding: in `Name: utterance`, first-person references and personal assertions belong to the name before the colon, never a generic `user`; a question, unadopted quotation, or generic assistant explanation is not that person's viewpoint.",
            "- Assistant boundary: skip assistant self-statements about identity, preferences, feelings, plans, or conversational promises unless the content itself is a reusable deliverable, configuration, or adopted project decision.",
            "- fact: a completed action, current state, effective decision, or other objective fact; when later context confirms completion, earlier words such as `decided to`, `will`, or `plan to` must not keep it classified as a plan.",
            "- plan: an explicitly pending action, especially one with a future date, deadline, recurrence, time window, or condition.",
        )
        for line in zh_lines:
            self.assertEqual(SYSTEM_PROMPT.count(line), 1)
        for line in en_lines:
            self.assertEqual(ENGLISH_SYSTEM_PROMPT.count(line), 1)
        for benchmark_name in ("张小红", "尼采", "徐佳", "Meet World"):
            self.assertNotIn(benchmark_name, SYSTEM_PROMPT)
            self.assertNotIn(benchmark_name, ENGLISH_SYSTEM_PROMPT)

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
