"""紧凑提取候选的准入与旧格式兼容性测试。"""

from __future__ import annotations

import json
import unittest

from hl_mem.ingest.admission import (
    MemoryCandidate,
    admission_rules_fingerprint,
    admit_claim,
    evidence_quote_matches,
)
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMRequest, LLMResponse


class _FakeLLMClient:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.last_request: LLMRequest | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(json.dumps(self.response, ensure_ascii=False), "stop", 1)


def _candidate(**overrides: object) -> MemoryCandidate:
    values: dict[str, object] = {
        "subject": "hl_mem",
        "value": "hl_mem 使用 SQLite WAL 模式",
        "kind": "architecture",
        "confidence": 0.9,
        "notability": "high",
        "evidence_quote": "使用 SQLite WAL 模式",
    }
    values.update(overrides)
    return MemoryCandidate(**values)  # type: ignore[arg-type]


class AdmissionPolicyTest(unittest.TestCase):
    def test_accepts_locatable_durable_fact(self) -> None:
        decision = admit_claim(_candidate(), "当前 hl_mem 使用 SQLite WAL 模式。")

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "accepted")

    def test_rejects_low_notability_and_unlocatable_evidence(self) -> None:
        self.assertEqual(
            admit_claim(_candidate(notability="low"), "hl_mem 使用 SQLite WAL 模式").reason,
            "low_notability",
        )
        self.assertEqual(
            admit_claim(_candidate(evidence_quote="原文没有这段"), "hl_mem 使用 SQLite WAL 模式").reason,
            "no_evidence",
        )

    def test_rejects_operational_snapshots(self) -> None:
        snapshots = (
            "hl_mem 服务已运行最新代码",
            "环境变量已清空",
            "935 tests passed",
            "CI 已全绿",
            "healthz 返回 ok",
            "测试通过",
            "Codex 正在跑 benchmark",
            "正在重启服务",
            "hl_mem 当前版本为 v0.22.0",
        )
        for value in snapshots:
            with self.subTest(value=value):
                decision = admit_claim(
                    _candidate(value=value, kind="fact", evidence_quote=value),
                    value,
                )
                self.assertEqual(decision.reason, "operational_snapshot")

    def test_rejects_one_shot_install_code_test_and_file_operations(self) -> None:
        snapshots = (
            "安装脚本会 print 安装成功",
            "已经修复 adapter 不可达代码 bug",
            "去掉了旧兼容分支",
            "删除了废弃的重试代码",
            "新增单元测试并测试通过",
            "新增文件 scripts/install.py",
            "本次改动代码行数为 42 行",
            "commit hash 为 abc123def",
            "__version__ 已更新为 0.23.0",
            "pyproject.toml 版本已改为 0.23.0",
        )
        for value in snapshots:
            with self.subTest(value=value):
                decision = admit_claim(
                    _candidate(value=value, kind="fact", evidence_quote=value),
                    value,
                )
                self.assertEqual(decision.reason, "operational_snapshot")

    def test_preserves_stable_architecture_despite_operation_keywords(self) -> None:
        stable_facts = (
            "hl_mem 使用 SQLite WAL 模式存储",
            "hl_mem 的安装目录固定为 REDACTED_PATH",
            "hl_mem 使用单元测试作为回归验证机制",
            "hl_mem 的删除策略采用软删除",
            "现有 180 个测试必须全部通过以保持向后兼容",
        )
        for value in stable_facts:
            with self.subTest(value=value):
                decision = admit_claim(
                    _candidate(value=value, kind="architecture", evidence_quote=value),
                    value,
                )
                self.assertEqual(decision.reason, "accepted")

    def test_preserves_stable_preferences_and_policies_with_operation_words(self) -> None:
        stable_claims = (
            ("preference", "用户偏好提交前先修复 bug"),
            ("fact", "用户要求删除文件前先备份"),
            ("architecture", "团队规定每个功能必须新增单元测试"),
        )

        for kind, value in stable_claims:
            with self.subTest(kind=kind, value=value):
                decision = admit_claim(
                    _candidate(subject="用户", value=value, kind=kind, evidence_quote=value),
                    value,
                )

                self.assertEqual(decision.reason, "accepted")

    def test_evidence_requires_exact_structured_numeric_tokens(self) -> None:
        self.assertTrue(evidence_quote_matches("代理端口为 8200", "当前代理端口为 8200。"))
        self.assertFalse(evidence_quote_matches("代理端口为 8200", "当前代理端口为 8300。"))
        self.assertFalse(evidence_quote_matches("代理地址为 127.0.0.1:8200", "代理地址为 127.0.0.1:8300"))

    def test_uses_stricter_evidence_threshold(self) -> None:
        self.assertEqual(admission_rules_fingerprint()["evidence_fuzzy_threshold"], 0.80)

    def test_preserves_durable_ci_policy(self) -> None:
        value = "CI 失败只允许重跑一次，此后必须停止并报告"

        decision = admit_claim(
            _candidate(value=value, kind="config", evidence_quote=value),
            value,
        )

        self.assertEqual(decision.reason, "accepted")

    def test_rejects_empty_numeric_and_secret_values(self) -> None:
        cases = (
            (_candidate(value="", evidence_quote=""), "", "empty_value"),
            (_candidate(value="935", evidence_quote="935"), "935", "low_value"),
            (
                _candidate(value="API key=plain-text-secret", evidence_quote="API key=plain-text-secret"),
                "API key=plain-text-secret",
                "secret_assignment",
            ),
        )
        for candidate, source, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(admit_claim(candidate, source).reason, expected)


class CompactExtractionTest(unittest.TestCase):
    def test_compact_response_maps_to_existing_claim_schema(self) -> None:
        response = {
            "claims": [
                {
                    "subject": "HL_MEM",
                    "value": "hl_mem 使用 SQLite WAL 模式",
                    "kind": "architecture",
                    "confidence": 0.9,
                    "notability": "high",
                    "evidence_quote": "使用 SQLite WAL 模式",
                }
            ],
            "should_memorize": True,
        }
        client = _FakeLLMClient(response)

        claims = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract("hl_mem 使用 SQLite WAL 模式")

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].subject, "hl_mem")
        self.assertEqual(claims[0].predicate, "事实")
        self.assertEqual(claims[0].canonical_attribute, "fact.architecture")
        self.assertEqual(claims[0].importance, 0.9)
        self.assertIn("architecture", claims[0].topic_tags)
        self.assertEqual(claims[0].reason, "accepted")
        self.assertIsNotNone(client.last_request)
        schema = client.last_request.structured_output.schema  # type: ignore[union-attr]
        claim_schema = schema["$defs"]["CompactExtractedClaimSchema"]
        self.assertIn("kind", claim_schema["required"])
        self.assertNotIn("predicate", claim_schema["properties"])

    def test_low_notability_is_filtered_after_compact_validation(self) -> None:
        response = {
            "claims": [
                {
                    "subject": "hl_mem",
                    "value": "hl_mem 服务已运行最新代码",
                    "kind": "fact",
                    "confidence": 0.9,
                    "notability": "low",
                    "evidence_quote": "服务已运行最新代码",
                }
            ],
            "should_memorize": True,
        }

        claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract(
            "hl_mem 服务已运行最新代码"
        )

        self.assertEqual(claims, [])

    def test_compact_choice_restores_database_slot_and_object_entity(self) -> None:
        response = {
            "claims": [
                {
                    "subject": "hl_mem",
                    "value": "hl_mem 使用 PostgreSQL 数据库",
                    "kind": "choice",
                    "confidence": 0.95,
                    "notability": "high",
                    "evidence_quote": "hl_mem 使用 PostgreSQL 数据库",
                }
            ],
            "should_memorize": True,
        }

        claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract(
            "hl_mem 使用 PostgreSQL 数据库"
        )

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].predicate, "使用")
        self.assertEqual(claims[0].canonical_attribute, "choice.database")
        self.assertEqual(claims[0].canonical_slot, "choice.database")
        self.assertEqual(claims[0].qualifiers, {"project": "hl_mem"})
        self.assertIn("PostgreSQL", claims[0].entities or [])

    def test_compact_config_restores_required_service_qualifier(self) -> None:
        response = {
            "claims": [
                {
                    "subject": "hl_mem",
                    "value": "hl_mem 服务端口为 8200",
                    "kind": "config",
                    "confidence": 0.95,
                    "notability": "medium",
                    "evidence_quote": "hl_mem 服务端口为 8200",
                }
            ],
            "should_memorize": True,
        }

        claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract("hl_mem 服务端口为 8200")

        self.assertEqual(claims[0].canonical_attribute, "config.port")
        self.assertEqual(claims[0].canonical_slot, "config.port")
        self.assertEqual(claims[0].qualifiers, {"service": "hl_mem"})

    def test_compact_plan_restores_absolute_time_range(self) -> None:
        value = "数据迁移安排在 2026-08-20 至 2026-08-21"
        response = {
            "claims": [
                {
                    "subject": "用户",
                    "value": value,
                    "kind": "plan",
                    "confidence": 0.9,
                    "notability": "medium",
                    "evidence_quote": value,
                }
            ],
            "should_memorize": True,
        }

        claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract(
            value,
            {"occurred_at": "2026-08-06T10:00:00+08:00"},
        )

        self.assertEqual(claims[0].occurred_start, "2026-08-20T00:00:00+08:00")
        self.assertEqual(claims[0].occurred_end, "2026-08-21T00:00:00+08:00")

    def test_legacy_response_still_uses_existing_parser(self) -> None:
        response = {
            "claims": [
                {
                    "subject": "用户",
                    "predicate": "偏好",
                    "value": "用户偏好简洁回答",
                    "confidence": 0.9,
                }
            ],
            "should_memorize": True,
        }

        claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract("用户偏好简洁回答")

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].predicate, "偏好")
        self.assertEqual(claims[0].value, "用户偏好简洁回答")

    def test_legacy_response_uses_admission_policy(self) -> None:
        response = {
            "claims": [
                {
                    "subject": "hl_mem",
                    "predicate": "事实",
                    "value": "935 tests passed",
                    "confidence": 0.9,
                }
            ],
            "should_memorize": True,
        }

        claims = LLMExtractor(_FakeLLMClient(response), ChunkingPolicy(10_000, 0, 2)).extract("935 tests passed")

        self.assertEqual(claims, [])


if __name__ == "__main__":
    unittest.main()
