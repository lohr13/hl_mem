from types import SimpleNamespace

import pytest

from hl_mem.domain.claims.state_projection import project_state_coordinate
from hl_mem.ingest.llm_extractor import ENGLISH_SYSTEM_PROMPT, SYSTEM_PROMPT
from hl_mem.ingest.schemas import temporal_gate_extraction_response_json_schema
from hl_mem.ingest.state_contract import canonicalize_state_fields


def _canonicalize(subject: str, value: str, *, evidence: str | None = None, kind: str = "fact"):
    return canonicalize_state_fields(
        SimpleNamespace(
            value=value,
            evidence_quote=value if evidence is None else evidence,
            assertion_kind="observation",
            kind=kind,
        ),
        subject,
        "fact.other",
        None,
        {},
    )


def test_product_prompt_adds_state_rules_without_replacing_assertion_schema():
    assert "坐标自包含" in SYSTEM_PROMPT and "Self-contained coordinates" in ENGLISH_SYSTEM_PROMPT
    assert "不要跳过有明确证据" in SYSTEM_PROMPT and "Do not skip an evidence-grounded" in ENGLISH_SYSTEM_PROMPT
    assert "服务健康快照、CI 测试数量、版本号查询结果" not in SYSTEM_PROMPT
    schema = temporal_gate_extraction_response_json_schema()
    assert schema["$defs"]["CompactExtractedClaimSchema"]["required"] == [
        "subject",
        "value",
        "kind",
        "confidence",
        "notability",
        "evidence_quote",
        "assertion_kind",
    ]


def test_version_owner_alias_is_invariant_between_subject_and_value():
    left = _canonicalize("gateway-1", "gateway-1 的 8200 服务版本为 v1")
    right = _canonicalize("gateway-1 的 8200 服务", "gateway-1 版本为 v1")

    assert left[:3] == right[:3] == ("gateway-1", "config.version", "config.version")
    assert left[3] == right[3] == {"_state_context": "current"}
    assert project_state_coordinate(
        namespace="tenant-a", subject=left[0], canonical_slot=left[2], qualifiers=left[3]
    ) == project_state_coordinate(namespace="tenant-a", subject=right[0], canonical_slot=right[2], qualifiers=right[3])


@pytest.mark.parametrize(
    ("value", "kind", "context", "has_slot"),
    [
        ("gateway-1 曾经运行 v1", "fact", "historical", True),
        ("gateway-1 计划升级到 v2", "plan", "non_asserted", False),
        ("要求 gateway-1 必须运行 v2", "fact", "non_asserted", False),
        ("文档写道 gateway-1 运行 v2", "fact", "non_asserted", False),
        ("gateway-1 并不是运行 v2", "fact", "non_asserted", False),
    ],
)
def test_non_current_context_never_becomes_a_current_state_axis(value, kind, context, has_slot):
    _, attribute, slot, qualifiers = _canonicalize("gateway-1", value, kind=kind)

    assert attribute == "config.version"
    assert (slot is not None) is has_slot
    assert qualifiers["_state_context"] == context


def test_state_qualifiers_are_source_bounded_and_unknown_owner_fails_closed():
    owner, _, slot, qualifiers = _canonicalize(
        "gateway-1", "gateway-1 production API service is healthy", evidence="gateway-1 is healthy"
    )
    assert (owner, slot) == ("gateway-1", "state.service_health")
    assert qualifiers == {"_state_context": "current", "service": "gateway-1"}

    _, _, unknown_slot, _ = _canonicalize("API", "API service version is v1")
    _, _, drifted_slot, _ = _canonicalize("gateway-1", "gateway-2 service version is v1")
    assert unknown_slot is None and drifted_slot is None
