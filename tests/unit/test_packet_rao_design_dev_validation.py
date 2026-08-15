"""Packet RAO 52 题快速验证 runner 的离线契约。"""

from __future__ import annotations

import importlib


def _runner():
    return importlib.import_module("evaluation.tools.run_packet_rao_design_dev_validation")


def test_build_tasks_covers_each_case_arm_reader_once() -> None:
    tasks = _runner().build_tasks(
        [{"case_id": "case-1"}, {"case_id": "case-2"}],
        preregistration_id="packet-rao-test",
    )

    keys = {(task["case_id"], task["arm_id"], task["reader_id"]) for task in tasks}
    assert len(tasks) == 8
    assert keys == {
        (case_id, arm, reader) for case_id in ("case-1", "case-2") for arm in ("C0", "C4") for reader in ("qwen", "glm")
    }


def test_upgrade_packet_adds_reader_visible_relation_and_final_token_cost() -> None:
    upgraded = _runner().upgrade_packet(
        [
            {
                "claim_id": "claim-1",
                "text": "团队后来采用海风看板",
                "role": "团队",
                "action": "采用",
                "object": "海风看板",
                "token_count": 5,
            }
        ]
    )

    rendered = "团队后来采用海风看板\nrelation: 团队 → 采用 → 海风看板"
    assert upgraded[0]["rendered_text"] == rendered
    assert upgraded[0]["token_count"] == (len(rendered) + 1) // 2


def test_upgrade_packet_keeps_incomplete_relation_text_only() -> None:
    upgraded = _runner().upgrade_packet([{"claim_id": "claim-1", "text": "团队后来采用海风看板", "role": "团队"}])

    assert upgraded[0]["rendered_text"] == "团队后来采用海风看板"
    assert upgraded[0]["token_count"] == 5


def test_enrich_packet_relations_projects_stored_claim_fields() -> None:
    enriched = _runner().enrich_packet_relations(
        [{"claim_id": "claim-1", "text": "团队后来采用海风看板"}],
        {
            "claim-1": {
                "subject_entity_id": "团队",
                "predicate": "采用",
                "value": "海风看板",
                "qualifiers": {},
            }
        },
    )

    assert enriched[0].get("role") == "团队"
    assert enriched[0].get("action") == "采用"
    assert enriched[0].get("object") == "海风看板"


def test_legacy_projection_removes_structured_relation_without_mutating_source() -> None:
    source = [
        {
            "claim_id": "claim-1",
            "text": "团队后来采用海风看板",
            "rendered_text": "rendered",
            "role": "团队",
            "action": "采用",
            "object": "海风看板",
        }
    ]

    legacy = _runner().legacy_packet(source)

    assert legacy == [{"claim_id": "claim-1", "text": "团队后来采用海风看板"}]
    assert source[0]["role"] == "团队"
