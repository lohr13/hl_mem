from __future__ import annotations

from collections.abc import Sequence

import pytest

from hl_mem.application.conflicts import ResolutionService
from hl_mem.application.ingest import IngestService, compute_fact_hash
from hl_mem.domain.claims.conflicts import compute_claim_pair_key, compute_conflict_key
from hl_mem.errors import ConflictResolutionError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.consolidate import auto_resolve_conflicts
from hl_mem.workers.repair_active_claims import audit_active_claims, repair_active_claims
from tests.unit._conflict_fixture import seed_pre_041_history

NOW = "2026-08-15T08:00:00+00:00"
CONFLICT_KEY = compute_conflict_key(
    "default",
    "gateway",
    "配置",
    "config.port",
    {"service": "gateway"},
)
assert CONFLICT_KEY is not None


def _claim(repository: ClaimRepository, claim_id: str, value: str, *, status: str) -> None:
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
            "fact_hash": compute_fact_hash("gateway", "配置", value),
            "conflict_key": CONFLICT_KEY,
            "conflict_key_version": 3,
            "valid_from": NOW,
            "recorded_from": NOW,
            "observed_at": NOW,
            "status": status,
            "confidence": 0.9,
            "importance": 0.5,
            "scope": "permanent",
            "volatility": "stable",
            "source_authority": "medium",
        }
    )


def _case(
    repository: ClaimRepository,
    case_id: str,
    left_id: str,
    right_id: str,
    *,
    status: str = "manual_required",
    decision: str = "uncertain",
) -> None:
    assert repository.insert_conflict_case(
        {
            "id": case_id,
            "pair_key": compute_claim_pair_key(left_id, right_id),
            "left_claim_id": left_id,
            "right_claim_id": right_id,
            "status": status,
            "decision": decision,
            "rationale": "lifecycle-fixture",
            "created_at": NOW,
            "resolved_at": NOW if status in {"resolved", "rejected"} else None,
        }
    )


def _lifecycle_database(tmp_path, name: str):
    connection = Database(tmp_path / f"{name}.db").open()
    repository = ClaimRepository(connection)
    with seed_pre_041_history(connection):
        _claim(repository, "left", "8080", status="active")
        _claim(repository, "right", "8081", status="active")
        _claim(repository, "third", "9090", status="active")
    _case(repository, "case-left-right", "left", "right", status="resolved", decision="coexist")
    _case(repository, "case-left-third", "left", "third")
    _case(repository, "case-right-third", "right", "third")
    return connection, repository


def _ingest_exact(connection) -> None:
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="配置",
            value="8080",
            subject="gateway",
            qualifiers={"service": "gateway"},
            canonical_attribute="config.port",
            canonical_slot="config.port",
        ),
        {"id": "event-exact", "actor_type": "user", "tenant_id": "default"},
        NOW,
        FakeEmbedder(8),
    )
    assert result.claim_id == "left"


def _run_operations(connection, operations: Sequence[str]) -> None:
    for operation in operations:
        if operation == "repair":
            repair_active_claims(connection, apply=True, repaired_at=NOW)
        elif operation == "resolve":
            revision = connection.execute("SELECT revision FROM conflict_cases WHERE id='case-left-third'").fetchone()[
                0
            ]
            ResolutionService(connection).resolve(
                "case-left-third",
                "keep_left",
                resolved_at=NOW,
                expected_revision=revision,
            )
        elif operation == "ingest":
            _ingest_exact(connection)
        elif operation == "audit":
            audit_active_claims(connection)
        else:
            raise AssertionError(f"unexpected lifecycle operation: {operation}")


def _snapshot(connection) -> dict[str, object]:
    claims = [
        tuple(row) for row in connection.execute("SELECT id,status,superseded_by_id FROM claims ORDER BY id").fetchall()
    ]
    cases = [
        tuple(row)
        for row in connection.execute(
            "SELECT id,status,decision,resolved_at FROM conflict_cases ORDER BY id"
        ).fetchall()
    ]
    audit = audit_active_claims(connection)
    evidence_count = connection.execute(
        "SELECT count(*) FROM evidence_links WHERE derived_type='claim' AND derived_id='left' "
        "AND evidence_type='event' AND evidence_id='event-exact'"
    ).fetchone()[0]
    return {
        "claims": claims,
        "cases": cases,
        "active_invariants_healthy": audit["active_invariants_healthy"],
        "terminal_coexist_case_ids": [item["id"] for item in audit["terminal_coexist_conflicts"]["cases"]],
        "healthy": audit["healthy"],
        "evidence_count": evidence_count,
    }


ORDERS = (
    ("repair", "resolve", "ingest", "audit"),
    ("ingest", "audit", "repair", "resolve"),
    ("resolve", "ingest", "repair", "audit"),
    ("audit", "ingest", "resolve", "repair"),
)


@pytest.mark.parametrize("operations", ORDERS)
def test_conflict_lifecycle_operations_are_order_independent(tmp_path, operations) -> None:
    connection, _ = _lifecycle_database(tmp_path, "-".join(operations))

    _run_operations(connection, operations)

    assert _snapshot(connection) == {
        "claims": [
            ("left", "active", None),
            ("right", "superseded", "left"),
            ("third", "superseded", "left"),
        ],
        "cases": [
            ("case-left-right", "resolved", "coexist", NOW),
            ("case-left-third", "resolved", "keep_left", NOW),
            ("case-right-third", "resolved", "group_winner", NOW),
        ],
        "active_invariants_healthy": True,
        "terminal_coexist_case_ids": ["case-left-right"],
        "healthy": False,
        "evidence_count": 1,
    }


def test_conflict_lifecycle_operations_are_idempotent_on_rerun(tmp_path) -> None:
    connection, _ = _lifecycle_database(tmp_path, "idempotent")

    _run_operations(connection, ORDERS[0])
    first = _snapshot(connection)
    _run_operations(connection, ORDERS[0])

    assert _snapshot(connection) == first


def _insert_dangling_case(connection) -> None:
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,created_at"
        ") VALUES ('dangling','dangling-pair','missing-left','missing-right','resolved','reject',?)",
        (NOW,),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")


def test_resolution_postcondition_violation_rolls_back_group_mutations(tmp_path) -> None:
    connection = Database(tmp_path / "resolve-postcondition.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", "8080", status="disputed")
    _claim(repository, "right", "8081", status="disputed")
    _case(repository, "case", "left", "right")
    _insert_dangling_case(connection)

    with pytest.raises(ConflictResolutionError, match="dangling conflict reference"):
        ResolutionService(connection).resolve("case", "keep_left", resolved_at=NOW)

    assert repository.get_claim("left")["status"] == "disputed"
    assert repository.get_claim("right")["status"] == "disputed"
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case'").fetchone()[0] == "manual_required"


def test_repair_postcondition_violation_rolls_back_group_mutations(tmp_path) -> None:
    connection = Database(tmp_path / "repair-postcondition.db").open()
    repository = ClaimRepository(connection)
    with seed_pre_041_history(connection):
        _claim(repository, "left", "8080", status="active")
        _claim(repository, "right", "8081", status="active")
    _insert_dangling_case(connection)

    with pytest.raises(ConflictResolutionError, match="dangling conflict reference"):
        repair_active_claims(connection, apply=True, repaired_at=NOW)

    assert repository.get_claim("left")["status"] == "active"
    assert repository.get_claim("right")["status"] == "active"
    assert connection.execute("SELECT count(*) FROM conflict_cases WHERE status='manual_required'").fetchone()[0] == 0


def test_auto_resolution_uses_local_postconditions_before_batch_global_audit(tmp_path) -> None:
    connection = Database(tmp_path / "auto-resolve-postcondition.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", "8080", status="disputed")
    _claim(repository, "right", "8081", status="disputed")
    connection.execute("UPDATE claims SET source_authority='high' WHERE id='left'")
    connection.commit()
    _case(repository, "case", "left", "right")
    _insert_dangling_case(connection)

    with pytest.raises(ConflictResolutionError, match="dangling conflict reference"):
        auto_resolve_conflicts(connection, NOW)

    assert repository.get_claim("left")["status"] == "active"
    assert repository.get_claim("right")["status"] == "superseded"
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case'").fetchone()[0] == "resolved"
