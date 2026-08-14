"""Audit and repair tests for active-claim invariants."""

from __future__ import annotations

import json

import pytest

from hl_mem.domain.claims.conflicts import compute_claim_pair_key
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.evidence import EvidenceRepository
from hl_mem.workers.repair_active_claims import (
    _parse_args,
    audit_active_claims,
    repair_active_claims,
)
from tests.unit._conflict_fixture import seed_pre_041_history

NOW = "2026-08-14T08:00:00+00:00"


def _claim(
    connection,
    claim_id: str,
    *,
    value: str,
    fact_hash: str,
    slot: str | None = None,
    conflict_key: str | None = None,
    recorded_from: str = NOW,
) -> None:
    assert ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "user",
            "predicate": "配置" if slot and slot.startswith("config.") else "使用",
            "value": value,
            "qualifiers": {},
            "canonical_attribute": slot or "fact.other",
            "canonical_slot": slot,
            "fact_hash": fact_hash,
            "conflict_key": conflict_key,
            "conflict_key_version": 3,
            "valid_from": recorded_from,
            "recorded_from": recorded_from,
            "observed_at": recorded_from,
            "status": "active",
            "confidence": 0.9,
            "importance": 0.5,
            "scope": "permanent",
            "volatility": "stable",
            "source_authority": "medium",
        }
    )


def _dirty_database(tmp_path):
    connection = Database(tmp_path / "dirty-claims.db").open()
    _claim(connection, "exact-a", value="same fact", fact_hash="same-hash")
    _claim(connection, "exact-b", value="same fact", fact_hash="same-hash")
    with seed_pre_041_history(connection):
        for index, value in enumerate(("8080", "8081", "9090"), start=1):
            _claim(
                connection,
                f"port-{index}",
                value=value,
                fact_hash=f"port-hash-{index}",
                slot="config.port",
                conflict_key="port-conflict",
            )
        for index, value in enumerate(("Orion-7B", "Orion-14B"), start=1):
            _claim(
                connection,
                f"model-{index}",
                value=value,
                fact_hash=f"model-hash-{index}",
                slot="choice.model",
                conflict_key="model-conflict",
            )
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,"
        "confidence,created_at,resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "resolved-model-case",
            compute_claim_pair_key("model-1", "model-2"),
            "model-1",
            "model-2",
            "resolved",
            "coexist",
            "historical_resolution",
            0.9,
            NOW,
            NOW,
        ),
    )
    connection.commit()
    evidence = EvidenceRepository(connection)
    assert evidence.add_link(
        {
            "id": "exact-a-event",
            "derived_type": "claim",
            "derived_id": "exact-a",
            "evidence_type": "event",
            "evidence_id": "event-a",
            "relation": "derived_from",
            "weight": 1.0,
        }
    )
    assert evidence.add_link(
        {
            "id": "exact-a-event-2",
            "derived_type": "claim",
            "derived_id": "exact-a",
            "evidence_type": "event",
            "evidence_id": "event-a-2",
            "relation": "derived_from",
            "weight": 1.0,
        }
    )
    assert evidence.add_link(
        {
            "id": "exact-b-event",
            "derived_type": "claim",
            "derived_id": "exact-b",
            "evidence_type": "event",
            "evidence_id": "event-b",
            "relation": "derived_from",
            "weight": 1.0,
        }
    )
    return connection


def test_audit_reports_exact_duplicates_and_exclusive_groups_by_slot(tmp_path) -> None:
    connection = _dirty_database(tmp_path)

    report = audit_active_claims(connection)

    assert report["healthy"] is False
    assert report["exact_duplicates"]["group_count"] == 1
    assert report["exact_duplicates"]["claim_count"] == 2
    assert report["exclusive_conflicts"]["group_count"] == 2
    assert report["exclusive_conflicts"]["claim_count"] == 5
    assert report["exclusive_conflicts"]["by_slot"] == {
        "choice.model": {"groups": 1, "claims": 2},
        "config.port": {"groups": 1, "claims": 3},
    }


def test_repair_dry_run_reports_plan_without_mutating_database(tmp_path) -> None:
    connection = _dirty_database(tmp_path)
    before = [tuple(row) for row in connection.execute("SELECT id,status FROM claims ORDER BY id")]

    result = repair_active_claims(connection, apply=False)

    assert result["dry_run"] is True
    assert result["plan"] == {
        "exact_duplicate_groups": 1,
        "exact_duplicate_claims_to_supersede": 1,
        "conflict_groups_to_dispute": 2,
        "claims_to_dispute": 5,
        "manual_conflict_cases_to_create": 3,
        "manual_conflict_cases_to_reopen": 0,
        "terminal_conflict_cases_to_preserve": 1,
    }
    assert [tuple(row) for row in connection.execute("SELECT id,status FROM claims ORDER BY id")] == before
    assert connection.execute("SELECT status FROM conflict_cases").fetchone()[0] == "resolved"


def test_repair_apply_reuses_exact_claim_and_quarantines_uncertain_groups(tmp_path) -> None:
    connection = _dirty_database(tmp_path)

    result = repair_active_claims(connection, apply=True, repaired_at=NOW)

    assert result["dry_run"] is False
    assert result["applied"] == {
        "exact_duplicate_claims_superseded": 1,
        "claims_disputed": 5,
        "manual_conflict_cases_created": 3,
        "manual_conflict_cases_reopened": 0,
        "terminal_conflict_cases_preserved": 1,
    }
    assert result["after"]["healthy"] is True
    exact = connection.execute(
        "SELECT id,status,superseded_by_id FROM claims WHERE id IN ('exact-a','exact-b') ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in exact] == [
        ("exact-a", "active", None),
        ("exact-b", "superseded", "exact-a"),
    ]
    winner_events = connection.execute(
        "SELECT evidence_id FROM evidence_links WHERE derived_type='claim' AND derived_id='exact-a' "
        "AND evidence_type='event' ORDER BY evidence_id"
    ).fetchall()
    assert [row["evidence_id"] for row in winner_events] == ["event-a", "event-a-2", "event-b"]
    assert {
        row["status"]
        for row in connection.execute(
            "SELECT status FROM claims WHERE conflict_key IN ('port-conflict','model-conflict')"
        )
    } == {"disputed"}
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 4
    assert {tuple(row) for row in connection.execute("SELECT status,decision,rationale FROM conflict_cases")} == {
        ("manual_required", "uncertain", "active_claim_invariant_repair"),
        ("resolved", "coexist", "historical_resolution"),
    }
    historical = connection.execute(
        "SELECT confidence,resolved_at FROM conflict_cases WHERE id='resolved-model-case'"
    ).fetchone()
    assert tuple(historical) == (0.9, NOW)


def test_repair_is_idempotent_after_invariants_converge(tmp_path) -> None:
    connection = _dirty_database(tmp_path)
    repair_active_claims(connection, apply=True, repaired_at=NOW)

    second = repair_active_claims(connection, apply=True, repaired_at=NOW)

    assert second["plan"] == {
        "exact_duplicate_groups": 0,
        "exact_duplicate_claims_to_supersede": 0,
        "conflict_groups_to_dispute": 0,
        "claims_to_dispute": 0,
        "manual_conflict_cases_to_create": 0,
        "manual_conflict_cases_to_reopen": 0,
        "terminal_conflict_cases_to_preserve": 0,
    }
    assert second["after"]["healthy"] is True


def test_cli_requires_explicit_repair_mode() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["repair"])
    with pytest.raises(SystemExit):
        _parse_args(["repair", "--dry-run", "--apply"])

    assert _parse_args(["audit"]).command == "audit"
    assert _parse_args(["repair", "--dry-run"]).dry_run is True
    assert _parse_args(["repair", "--apply"]).apply is True


def test_audit_report_is_json_serializable(tmp_path) -> None:
    report = audit_active_claims(_dirty_database(tmp_path))

    assert json.loads(json.dumps(report, ensure_ascii=False))["healthy"] is False
