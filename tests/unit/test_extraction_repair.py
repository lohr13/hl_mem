"""qwen3.7-plus 提取 JSON 兼容性测试。"""

from __future__ import annotations

import unittest

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import SYSTEM_PROMPT, LLMExtractor
from hl_mem.ingest.repair import repair_extraction_json
from hl_mem.ingest.schemas import ExtractionResponseSchema
from hl_mem.llm.types import LLMRequest, LLMResponse


class _RetryClient:
    """依次返回非法和合法 JSON 的测试客户端。"""

    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "qwen3.7-plus"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.responses = [
            '{"claims":[],"entities":[],"should_memorize":false,"sensitivity":"机密"}',
            '{"claims":[],"entities":[],"should_memorize":false,"sensitivity":"restricted"}',
        ]

    def complete(self, request: LLMRequest) -> LLMResponse:
        """记录请求并返回下一条预设响应。"""
        self.requests.append(request)
        return LLMResponse(self.responses.pop(0), "stop", 1)


class ExtractionRepairTest(unittest.TestCase):
    """验证确定性修复仍满足严格提取 schema。"""

    def test_repairs_entities_topic_tags_and_sensitivity(self) -> None:
        raw = {
            "claims": [
                {
                    "subject": "用户",
                    "predicate": "使用",
                    "canonical_attribute": "choice.database",
                    "canonical_slot": "choice.database",
                    "topic_tags": ["数据库", "工具选择"],
                    "value": "项目使用 PostgreSQL",
                    "qualifiers": {},
                    "confidence": 0.9,
                    "volatility": "stable",
                    "reason": "用户明确陈述",
                    "scope": "permanent",
                    "importance": 0.8,
                    "occurred_start": None,
                    "occurred_end": None,
                    "entities": "PostgreSQL",
                }
            ],
            "entities": "PostgreSQL",
            "should_memorize": True,
            "sensitivity": "敏感",
        }

        repaired = repair_extraction_json(raw)
        validated = ExtractionResponseSchema.model_validate(repaired)

        self.assertEqual(validated.entities, ["PostgreSQL"])
        self.assertEqual(validated.claims[0].entities, ["PostgreSQL"])
        self.assertEqual(validated.claims[0].topic_tags, ["dependency", "tool_choice"])
        self.assertEqual(validated.sensitivity, "sensitive")

    def test_unknown_topic_tag_is_preserved_for_strict_validation(self) -> None:
        raw = {"claims": [{"topic_tags": ["未知标签"]}], "entities": [], "sensitivity": "normal"}

        repaired = repair_extraction_json(raw)

        self.assertEqual(repaired["claims"][0]["topic_tags"], ["未知标签"])

    def test_prompt_contains_explicit_constraints_and_complete_example(self) -> None:
        self.assertIn("sensitivity 只能是以下三个英文值", SYSTEM_PROMPT)
        self.assertIn("entities 必须是 JSON 数组", SYSTEM_PROMPT)
        self.assertIn('"should_memorize": true', SYSTEM_PROMPT)
        self.assertIn('"topic_tags": ["preference", "behavior"]', SYSTEM_PROMPT)

    def test_schema_error_details_include_invalid_and_allowed_values(self) -> None:
        invalid = {
            "claims": [],
            "entities": "PostgreSQL",
            "should_memorize": True,
            "sensitivity": "敏感",
        }
        with self.assertRaises(Exception) as captured:
            ExtractionResponseSchema.model_validate(invalid)

        details = LLMExtractor._schema_error_details(captured.exception, invalid)

        self.assertTrue(any(item["path"] == "entities" and item["invalid_value"] == "PostgreSQL" for item in details))
        self.assertTrue(
            any(
                item["path"] == "sensitivity"
                and item["invalid_value"] == "敏感"
                and item["allowed_values"] == ["normal", "sensitive", "restricted"]
                for item in details
            )
        )

    def test_retry_contains_previous_json_invalid_value_and_allowed_values(self) -> None:
        client = _RetryClient()

        LLMExtractor(client, ChunkingPolicy(10_000, 0, 2), schema_retries=1).extract("敏感信息")

        retry_prompt = client.requests[1].messages[1].content
        self.assertIn("<previous_invalid_json>", retry_prompt)
        self.assertIn('"sensitivity": "机密"', retry_prompt)
        self.assertIn('"invalid_value": "机密"', retry_prompt)
        self.assertIn('"allowed_values": ["normal", "sensitive", "restricted"]', retry_prompt)


if __name__ == "__main__":
    unittest.main()
