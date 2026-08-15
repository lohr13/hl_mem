"""C-series 独立 scorer 的硬安全回归。"""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.tools.score_c_series_relation_experiment import (
    audit_evidence_provenance,
    audit_leakage,
    score_visible_case,
)

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "tests" / "eval" / "fixtures" / "c_series_relation_design_dev.json"


def _gold() -> dict:
    return {
        "answerability": "answerable",
        "answer_entities": ["海风看板"],
        "role_action_object": [{"role": "团队", "action": "采用", "object": "海风看板"}],
        "forbidden_entities": [],
        "forbidden_assertions": ["团队采用了青石笔记"],
    }


def test_visible_score_requires_all_entities_and_rao_semantics() -> None:
    packet = [
        {
            "claim_id": "answer",
            "entities": ["海风看板"],
            "role": "团队",
            "action": "采用",
            "object": "海风看板",
            "seed_rank": 1,
            "expanded_from_seed_ranks": [],
        }
    ]
    assert score_visible_case("团队采用了海风看板", packet, _gold())["answer_correct"] is True
    assert score_visible_case("海风看板", packet, _gold())["answer_correct"] is False
    packet[0]["action"] = "推荐"
    assert score_visible_case("团队采用了海风看板", packet, _gold())["answer_correct"] is False


def test_score_separates_role_confusion_from_evidence_modality_and_structural_leakage() -> None:
    packet = [
        {
            "claim_id": "recommendation",
            "entities": ["青石笔记"],
            "role": "顾问",
            "action": "推荐",
            "object": "青石笔记",
            "seed_rank": 1,
            "expanded_from_seed_ranks": [],
        }
    ]
    scored = score_visible_case("团队采用了青石笔记", packet, _gold())
    assert scored["negative_violation"] is True
    assert scored["role_modality_confusion"] is True
    assert scored["modality_violation"] is False
    assert audit_leakage({"question": "x", "gold": {"answer": "y"}})
    assert audit_leakage({"question": "x", "indirect_gold_ref": "sha256:deadbeef"})
    assert not audit_leakage({"question": "x", "namespace": "n"})


def test_evidence_audit_enforces_modality_visibility_and_frozen_source() -> None:
    case = {
        "namespace": "wanted",
        "question_at": "2026-06-30T00:00:00+00:00",
        "known_as_of": "2026-06-01T00:00:00+00:00",
        "allowed_modalities": ["text"],
        "source_cache_identity": "D:/cache/case.db",
        "source_cache_sha256": "a" * 64,
        "source_corpora": [{"id": "visible_relation_dev", "sha256": "b" * 64}],
    }
    prereg = {
        "cache_files": {"D:/cache/case.db": "a" * 64},
        "corpora": {"visible_relation_dev": "b" * 64},
    }
    provenance = {
        "event_id": "e1",
        "namespace": "wanted",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "recorded_at": "2026-01-02T00:00:00+00:00",
        "modality": "text",
        "content_kind": "message",
        "source_cache_identity": "D:/cache/case.db",
        "source_cache_sha256": "a" * 64,
        "source_corpora": [{"id": "visible_relation_dev", "sha256": "b" * 64}],
    }
    clean = audit_evidence_provenance([{"evidence_provenance": [provenance]}], case, prereg)
    assert clean == {"modality": [], "provenance": []}
    bad = dict(
        provenance,
        modality="image",
        namespace="other",
        occurred_at="2027-01-01T00:00:00+00:00",
        recorded_at="2027-01-01T00:00:00+00:00",
        source_cache_sha256="c" * 64,
        source_corpora=[{"id": "not_frozen", "sha256": "d" * 64}],
    )
    violations = audit_evidence_provenance([{"evidence_provenance": [bad]}], case, prereg)
    assert violations["modality"]
    assert violations["provenance"]
    assert any("source_cache" in path for path in violations["provenance"])
    assert any("source_corpus" in path for path in violations["provenance"])


def test_enumeration_rao_can_be_satisfied_by_multiple_claims_but_not_wrong_relation() -> None:
    cases = json.loads(DESIGN.read_text(encoding="utf-8"))["cases"]
    enumeration = [case for case in cases if case["category"] == "enumeration_completeness"]
    assert len(enumeration) == 2
    for case in enumeration:
        packet = [
            {
                "claim_id": claim["claim_id"],
                "entities": claim["entities"],
                "role": claim["role"],
                "action": claim["action"],
                "object": claim["object"],
            }
            for claim in case["claims"]
        ]
        assert score_visible_case(case["answer"], packet, case["gold"])["answer_correct"] is True
        wrong = [{**claim, "action": "推荐"} for claim in packet]
        assert score_visible_case(case["answer"], wrong, case["gold"])["answer_correct"] is False
