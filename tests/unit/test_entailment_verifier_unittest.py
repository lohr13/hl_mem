"""Directly runnable tests for extraction entailment verification.

These tests deliberately avoid pytest so the audit-only rollout can be
verified under the task's no-pytest constraint.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import fields, replace
from unittest.mock import patch

from hl_mem.components import make_extractor
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.ingest.verifier import EntailmentVerifier
from hl_mem.llm.types import LLMRequest, LLMResponse
from hl_mem.observability.audit import audit_scope
from hl_mem.settings import Settings


class _SequenceClient:
    class _Provider:
        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return next(self.responses)


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def emit(self, phase, action, outcome, *, detail=None, **_dimensions):
        self.events.append((phase, action, outcome, detail or {}))
        return True


class _FailingVerifier:
    last_usage_tokens = 0
    last_input_tokens = 0
    last_output_tokens = 0
    last_call_count = 0

    def verify_batch(self, claims, source_text):
        del claims, source_text
        raise RuntimeError("verifier unavailable")


class _ShortVerifier(_FailingVerifier):
    def verify_batch(self, claims, source_text):
        del claims, source_text
        return []


def _claim(index: int = 0) -> ExtractedClaim:
    return ExtractedClaim(
        subject="hl_mem",
        predicate="配置",
        value=f"hl_mem 的超时配置为 {30 + index} 秒",
        qualifiers={},
        scope="permanent",
    )


def _extraction_value(index: int) -> str:
    return f"hl_mem 的超时配置项 {index} 为 {30 + index} 秒"


def _extraction_source(claim_count: int) -> str:
    return "\n".join(_extraction_value(index) for index in range(claim_count))


def _extraction_response(claim_count: int) -> str:
    claims = [
        {
            "subject": "hl_mem",
            "value": _extraction_value(index),
            "kind": "config",
            "confidence": 0.9,
            "notability": "medium",
            "evidence_quote": _extraction_value(index),
        }
        for index in range(claim_count)
    ]
    return json.dumps(
        {
            "claims": claims,
            "should_memorize": bool(claims),
        },
        ensure_ascii=False,
    )


class EntailmentVerifierTests(unittest.TestCase):
    def test_default_off_preserves_single_extraction_call(self) -> None:
        client = _SequenceClient([LLMResponse(_extraction_response(1), "stop", 4)])

        claims = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract(_extraction_source(1))

        self.assertEqual(len(claims), 1)
        self.assertEqual(len(client.requests), 1)

    def test_batch_verifier_orders_results_by_claim_index(self) -> None:
        client = _SequenceClient(
            [
                LLMResponse(
                    {
                        "results": [
                            {
                                "claim_index": 1,
                                "support_label": "unsupported",
                                "rationale": "原文没有第二条配置",
                            },
                            {
                                "claim_index": 0,
                                "support_label": "entailed",
                                "rationale": "原文直接陈述",
                            },
                        ]
                    },
                    "stop",
                    17,
                    input_tokens=12,
                    output_tokens=5,
                )
            ]
        )

        verifier = EntailmentVerifier(client)
        results = verifier.verify_batch(
            [_claim(0), _claim(1)],
            "hl_mem 的超时配置为 30 秒。",
        )

        self.assertEqual([item.support_label for item in results], ["entailed", "unsupported"])
        self.assertEqual(client.requests[0].structured_output.name, "entailment_verification")
        self.assertIn('"results"', client.requests[0].messages[0].content)
        request_payload = json.loads(client.requests[0].messages[1].content)
        self.assertEqual(request_payload["source_text"], "hl_mem 的超时配置为 30 秒。")
        self.assertEqual([item["claim_index"] for item in request_payload["claims"]], [0, 1])
        self.assertEqual(verifier.last_usage_tokens, 17)
        self.assertEqual(verifier.last_input_tokens, 12)
        self.assertEqual(verifier.last_output_tokens, 5)
        self.assertEqual(verifier.last_call_count, 1)

    def test_batch_verifier_rejects_missing_claim_result(self) -> None:
        client = _SequenceClient([LLMResponse({"results": []}, "stop", 3)])

        with self.assertRaisesRegex(ValueError, "exactly one result"):
            EntailmentVerifier(client).verify_batch([_claim()], "原文")

    def test_batch_verifier_requires_rationale_in_json_object_fallback(self) -> None:
        client = _SequenceClient(
            [
                LLMResponse(
                    {"results": [{"claim_index": 0, "support_label": "entailed"}]},
                    "stop",
                    3,
                )
            ]
        )

        with self.assertRaisesRegex(ValueError, "rationale"):
            EntailmentVerifier(client).verify_batch([_claim()], "原文")

    def test_audit_mode_verifies_large_chunk_without_filtering_claims(self) -> None:
        verification = {
            "results": [
                {
                    "claim_index": index,
                    "support_label": "unsupported" if index == 5 else "entailed",
                    "rationale": "审计标签",
                }
                for index in range(6)
            ]
        }
        client = _SequenceClient(
            [
                LLMResponse(_extraction_response(6), "stop", 11, input_tokens=8, output_tokens=3),
                LLMResponse(verification, "stop", 7, input_tokens=5, output_tokens=2),
            ]
        )
        verifier = EntailmentVerifier(client)
        extractor = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 2),
            verifier=verifier,
            verification_mode="audit",
        )
        audit = _RecordingAudit()

        with audit_scope(audit):
            claims = extractor.extract(_extraction_source(6))

        checked = [event for event in audit.events if event[1] == "entailment_checked"]
        self.assertEqual(len(claims), 6)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual([event[2] for event in checked].count("unsupported"), 1)
        self.assertEqual(extractor.last_usage_tokens, 18)
        self.assertEqual(extractor.last_input_tokens, 13)
        self.assertEqual(extractor.last_output_tokens, 5)

    def test_enforce_mode_is_currently_audit_only_for_small_chunk(self) -> None:
        client = _SequenceClient(
            [
                LLMResponse(_extraction_response(1), "stop", 4),
                LLMResponse(
                    {
                        "results": [
                            {
                                "claim_index": 0,
                                "support_label": "unsupported",
                                "rationale": "未找到直接支持",
                            }
                        ]
                    },
                    "stop",
                    2,
                ),
            ]
        )
        extractor = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 2),
            verifier=EntailmentVerifier(client),
            verification_mode="enforce",
        )

        claims = extractor.extract(_extraction_source(1))

        self.assertEqual(len(claims), 1)
        self.assertEqual(len(client.requests), 2)

    def test_verifier_failure_is_audited_and_does_not_change_claims(self) -> None:
        client = _SequenceClient([LLMResponse(_extraction_response(6), "stop", 4)])
        extractor = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 2),
            verifier=_FailingVerifier(),
            verification_mode="audit",
        )
        audit = _RecordingAudit()

        with audit_scope(audit):
            claims = extractor.extract(_extraction_source(6))

        failed = [event for event in audit.events if event[1] == "entailment_verification_failed"]
        self.assertEqual(len(claims), 6)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][2], "error")

    def test_incomplete_verifier_result_is_fail_open(self) -> None:
        client = _SequenceClient([LLMResponse(_extraction_response(6), "stop", 4)])
        extractor = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 2),
            verifier=_ShortVerifier(),
            verification_mode="audit",
        )
        audit = _RecordingAudit()

        with audit_scope(audit):
            claims = extractor.extract(_extraction_source(6))

        failed = [event for event in audit.events if event[1] == "entailment_verification_failed"]
        self.assertEqual(len(claims), 6)
        self.assertEqual(len(failed), 1)

    def test_long_empty_chunk_records_possible_under_extraction_without_verifier_call(self) -> None:
        client = _SequenceClient([LLMResponse(_extraction_response(0), "stop", 4)])
        extractor = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 2),
            verifier=_FailingVerifier(),
            verification_mode="audit",
            verification_empty_text_threshold=10,
        )
        audit = _RecordingAudit()

        with audit_scope(audit):
            claims = extractor.extract("这是一段长度超过阈值但没有抽取结果的原文。")

        suspected = [event for event in audit.events if event[1] == "possible_under_extraction"]
        self.assertEqual(claims, [])
        self.assertEqual(len(suspected), 1)
        self.assertEqual(len(client.requests), 1)


class EntailmentSettingsTests(unittest.TestCase):
    def test_settings_adds_disabled_verification_mode(self) -> None:
        settings = Settings()

        self.assertEqual(len(fields(Settings)), 157)
        self.assertEqual(settings.verification_mode, "off")
        self.assertEqual(settings.snapshot()["verification_mode"], "off")

    def test_settings_rejects_unknown_verification_mode(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "extraction.verification_mode"):
            replace(Settings.for_test(), verification_mode="unknown").validate()

    def test_component_factory_wires_audit_verifier_to_extraction_client(self) -> None:
        client = _SequenceClient([])
        settings = replace(
            Settings.for_test(),
            extractor_mode="llm",
            llm_api_key="test-key",
            verification_mode="audit",
        )

        with patch("hl_mem.components.make_llm_client", return_value=client):
            extractor = make_extractor(settings)

        self.assertEqual(extractor.verification_mode, "audit")
        self.assertIs(extractor.llm_client, client)
        self.assertIs(extractor.verifier.llm_client, client)


if __name__ == "__main__":
    unittest.main()
