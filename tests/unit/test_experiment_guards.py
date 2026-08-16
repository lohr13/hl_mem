from __future__ import annotations

import sqlite3

import pytest

from hl_mem.evaluation.experiment_guards import (
    assert_gold_free,
    assert_packet_variants_differ,
    assert_pilot_gate,
    bind_authoritative_source_context,
    validate_relation_coverage,
)


def _relation_db(path, relations: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memory_relations(id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO memory_relations(id) VALUES (?)",
            [(f"relation-{index}",) for index in range(relations)],
        )
        connection.commit()
    finally:
        connection.close()


def test_gold_free_guard_rejects_nested_scorer_fields() -> None:
    assert_gold_free({"question": "谁负责项目？", "namespace": "n"})

    with pytest.raises(ValueError, match=r"\$\.packet\[0\]\.forbidden_entities"):
        assert_gold_free({"packet": [{"forbidden_entities": ["secret"]}]})


def test_pilot_gate_requires_three_calls_and_real_persistence() -> None:
    assert_pilot_gate(
        "frozen",
        {
            "preregistration_sha256": "frozen",
            "attempted": 3,
            "accepted": 1,
            "persisted": 1,
            "calls": [{}, {}, {}],
        },
    )

    with pytest.raises(RuntimeError, match="accepted/persisted"):
        assert_pilot_gate(
            "frozen",
            {
                "preregistration_sha256": "frozen",
                "attempted": 3,
                "accepted": 0,
                "persisted": 0,
                "calls": [{}, {}, {}],
            },
        )


def test_authoritative_binding_ignores_model_ids_and_uses_matching_evidence() -> None:
    bound = bind_authoritative_source_context(
        {
            "claim_id": "invented-claim",
            "action": "喜欢",
            "object": "爵士乐",
            "evidence_event_id": "invented-event",
            "evidence_quote": "invented quote",
        },
        source_id="source-1",
        evidence=[{"evidence_event_id": "event-1", "text": "用户说：我喜欢爵士乐。"}],
    )

    assert bound == {
        "claim_id": "source-1",
        "action": "喜欢",
        "object": "爵士乐",
        "evidence_event_id": "event-1",
        "evidence_quote": "用户说：我喜欢爵士乐。",
    }


def test_authoritative_binding_marks_missing_evidence_without_inventing() -> None:
    bound = bind_authoritative_source_context(
        {
            "action": "喜欢",
            "object": "爵士乐",
            "evidence_event_id": "invented-event",
            "evidence_quote": "invented quote",
        },
        source_id="source-1",
        evidence=[{"evidence_event_id": "event-1", "text": "用户在听古典乐。"}],
    )

    assert "evidence_event_id" not in bound
    assert "evidence_quote" not in bound
    assert bound["_binding_reason"] == "evidence_not_found"


def test_relation_coverage_gate_rejects_required_case_without_edges(tmp_path) -> None:
    database = tmp_path / "required.db"
    _relation_db(database, 0)

    with pytest.raises(RuntimeError, match="required.*0"):
        validate_relation_coverage(
            [{"case_id": "case-required", "relation_coverage": "required"}],
            {"case-required": database},
        )


def test_relation_coverage_gate_reports_required_and_none_separately(tmp_path) -> None:
    required = tmp_path / "required.db"
    none = tmp_path / "none.db"
    _relation_db(required, 2)
    _relation_db(none, 0)

    summary = validate_relation_coverage(
        [
            {"case_id": "case-required", "relation_coverage": "required"},
            {"case_id": "case-none", "relation_coverage": "none"},
        ],
        {"case-required": required, "case-none": none},
    )

    assert summary["required_with_recallable_edges"] == 1
    assert summary["none_with_edges"] == 0
    assert summary["total_relations"] == 2


def test_packet_difference_smoke_rejects_equal_variant() -> None:
    packets = {
        "packets": [
            {
                "case_id": case_id,
                "repeat_index": 0,
                "variant_id": variant,
                "packet": (
                    [{"claim_id": case_id}]
                    if variant == "baseline" or case_id == "case-2"
                    else [
                        {"claim_id": case_id},
                        {"claim_id": f"{case_id}-expanded"},
                    ]
                ),
            }
            for case_id in ("case-1", "case-2", "case-3")
            for variant in ("baseline", "candidate")
        ]
    }

    with pytest.raises(RuntimeError, match="packet smoke.*case-2"):
        assert_packet_variants_differ(
            packets,
            ["case-1", "case-2", "case-3"],
            "frozen-run",
            baseline_variant="baseline",
            candidate_variant="candidate",
        )
