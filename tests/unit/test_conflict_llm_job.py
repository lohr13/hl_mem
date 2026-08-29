from __future__ import annotations

from pathlib import Path

import pytest

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.auto_resolve_conflicts import auto_resolve_conflicts
from hl_mem.workers.job_handlers import JOB_HANDLERS

NOW = "2026-08-25T08:00:00+00:00"


def _manual_pair(
    connection,
    *,
    status: str = "manual_required",
    left_authority: str = "medium",
    right_authority: str = "medium",
) -> None:
    repository = ClaimRepository(connection)
    for claim_id, value, authority in (
        ("left", "8080", left_authority),
        ("right", "8081", right_authority),
    ):
        assert repository.insert_claim(
            {
                "id": claim_id,
                "namespace_key": "default",
                "subject_entity_id": "gateway",
                "predicate": "配置",
                "value": value,
                "qualifiers": {"service": "gateway"},
                "canonical_attribute": "config.port",
                "canonical_slot": "config.port",
                "fact_hash": f"hash-{claim_id}",
                "conflict_key": "service-port",
                "conflict_key_version": 3,
                "recorded_from": NOW,
                "status": "disputed",
                "source_authority": authority,
                "confidence": 0.9,
                "scope": "permanent",
                "volatility": "stable",
            }
        )
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,created_at) "
        "VALUES ('case-1','pair-1','left','right',?,'uncertain',?)",
        (status, NOW),
    )
    connection.commit()


@pytest.mark.parametrize("initial_status", ("pending", "auto_resolved", "manual_required"))
def test_gray_case_becomes_manual_without_l2_job(tmp_path: Path, initial_status: str) -> None:
    connection = Database(tmp_path / "manual.db").open()
    _manual_pair(connection, status=initial_status)

    result = auto_resolve_conflicts(connection, NOW)

    assert result["manual_required"] == 1
    assert "l2_queued" not in result
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-1'").fetchone()[0] == (
        "manual_required"
    )
    assert [row[0] for row in connection.execute("SELECT status FROM claims ORDER BY id")] == [
        "disputed",
        "disputed",
    ]
    assert tuple(connection.execute("SELECT tier,status,resolution_rule FROM governance_actions").fetchone()) == (
        "L3",
        "applied",
        "l0_only_manual_required",
    )


def test_l0_decisive_case_still_applies(tmp_path: Path) -> None:
    connection = Database(tmp_path / "l0.db").open()
    _manual_pair(connection, left_authority="high")

    result = auto_resolve_conflicts(connection, NOW)

    assert result["resolved"] == 1
    assert [row[0] for row in connection.execute("SELECT status FROM claims ORDER BY id")] == [
        "active",
        "superseded",
    ]
    assert tuple(connection.execute("SELECT tier,status FROM governance_actions").fetchone()) == ("L0", "applied")


def test_retired_l2_job_handler_is_removed_from_active_registry() -> None:
    assert "resolve_conflict_llm" not in JOB_HANDLERS
