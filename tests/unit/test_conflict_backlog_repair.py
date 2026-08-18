from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.application.conflict_backlog import (
    inspect_invalid_conflict_groups,
    repair_invalid_conflict_groups,
)
from hl_mem.application.conflict_invariants import (
    find_dangling_conflict_references,
    find_orphan_disputed_claims,
)
from hl_mem.cli import main
from hl_mem.errors import ConflictError
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

NOW = "2026-08-18T08:00:00+00:00"
INVALID_RATIONALE = "ingest_dirty_active_group"


def _insert_claim(
    connection,
    claim_id: str,
    *,
    status: str,
    slot: str,
    conflict_key: str,
    namespace: str = "default",
) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,value_json,recorded_from,status,"
        "canonical_slot,canonical_attribute,conflict_key"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            namespace,
            "repair-fixture",
            "配置",
            f'"{claim_id}"',
            NOW,
            status,
            slot,
            slot,
            conflict_key,
        ),
    )


def _insert_case(
    connection,
    case_id: str,
    left_id: str,
    right_id: str,
    *,
    rationale: str = INVALID_RATIONALE,
    status: str = "manual_required",
) -> None:
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,created_at,resolved_at"
        ") VALUES (?,?,?,?,?,'uncertain',?,?,?)",
        (
            case_id,
            f"pair-{case_id}",
            left_id,
            right_id,
            status,
            rationale,
            NOW,
            NOW if status in {"resolved", "rejected"} else None,
        ),
    )


def _small_fixture(path: Path):
    connection = Database(path).open()
    _insert_claim(connection, "disputed", status="disputed", slot="config.path", conflict_key="path-group")
    _insert_claim(connection, "superseded", status="superseded", slot="config.path", conflict_key="path-group")
    _insert_claim(connection, "active", status="active", slot="config.path", conflict_key="path-group")
    _insert_case(connection, "invalid-a", "disputed", "superseded")
    _insert_case(connection, "invalid-b", "disputed", "active", rationale="ingest_group_resolution")
    _insert_claim(connection, "port-a", status="disputed", slot="config.port", conflict_key="port-group")
    _insert_claim(connection, "port-b", status="disputed", slot="config.port", conflict_key="port-group")
    _insert_case(connection, "valid-exclusive", "port-a", "port-b")
    connection.commit()
    return connection


def test_dry_run_classifies_targets_without_mutating_state(tmp_path: Path) -> None:
    connection = _small_fixture(tmp_path / "dry-run.db")
    before = connection.total_changes

    report = inspect_invalid_conflict_groups(connection)

    assert report["candidate_case_count"] == 2
    assert report["cases_by_slot"] == {"config.path": 2}
    assert report["endpoint_count"] == 3
    assert report["endpoint_status_counts"] == {"active": 1, "disputed": 1, "superseded": 1}
    assert report["disputed_to_activate"] == 1
    assert report["outside_open_endpoint_count"] == 0
    assert report["remaining_open_count"] == 1
    assert connection.total_changes == before
    assert connection.execute("SELECT count(*) FROM conflict_cases WHERE status='manual_required'").fetchone()[0] == 3


def test_expected_count_mismatch_fails_closed(tmp_path: Path) -> None:
    connection = _small_fixture(tmp_path / "expected-count.db")
    before = [tuple(row) for row in connection.execute("SELECT id,status,rationale FROM conflict_cases ORDER BY id")]

    with pytest.raises(ConflictError, match="expected 3.*found 2"):
        repair_invalid_conflict_groups(connection, expected_count=3, repaired_at=NOW)

    assert [tuple(row) for row in connection.execute("SELECT id,status,rationale FROM conflict_cases ORDER BY id")] == before


def test_apply_refuses_target_endpoint_used_by_another_open_case(tmp_path: Path) -> None:
    connection = _small_fixture(tmp_path / "shared-endpoint.db")
    _insert_case(
        connection,
        "outside-open",
        "disputed",
        "active",
        rationale="semantic_consolidation",
    )
    connection.commit()

    with pytest.raises(ConflictError, match="outside the repair target"):
        repair_invalid_conflict_groups(connection, expected_count=2, repaired_at=NOW)

    assert connection.execute(
        "SELECT count(*) FROM conflict_cases WHERE id LIKE 'invalid-%' AND status='manual_required'"
    ).fetchone()[0] == 2
    assert connection.execute("SELECT status FROM claims WHERE id='disputed'").fetchone()[0] == "disputed"


def test_apply_closes_invalid_cases_restores_only_disputed_and_writes_one_audit(tmp_path: Path) -> None:
    connection = _small_fixture(tmp_path / "apply.db")
    audit_before = connection.execute("SELECT count(*) FROM audit_log").fetchone()[0]

    result = repair_invalid_conflict_groups(connection, expected_count=2, repaired_at=NOW)

    assert result["applied_case_count"] == 2
    assert result["activated_claim_count"] == 1
    assert result["remaining_open_count"] == 1
    assert result["invalid_open_count"] == 0
    assert connection.execute("SELECT status FROM claims WHERE id='disputed'").fetchone()[0] == "active"
    assert connection.execute("SELECT status FROM claims WHERE id='superseded'").fetchone()[0] == "superseded"
    assert connection.execute("SELECT status FROM claims WHERE id='active'").fetchone()[0] == "active"
    rows = connection.execute(
        "SELECT status,decision,resolved_at,rationale FROM conflict_cases WHERE id LIKE 'invalid-%' ORDER BY id"
    ).fetchall()
    assert all(row["status"] == "rejected" and row["decision"] == "reject" for row in rows)
    assert all(row["resolved_at"] == NOW for row in rows)
    assert all(row["rationale"].endswith(";v0.28.9_invalid_nonexclusive_group") for row in rows)
    assert connection.execute(
        "SELECT count(*) FROM conflict_review_state WHERE case_id LIKE 'invalid-%'"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audit_before + 1
    assert inspect_invalid_conflict_groups(connection)["candidate_case_count"] == 0


def test_cli_repair_invalid_groups_is_dry_run_by_default_and_apply_requires_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cli-repair.db"
    connection = _small_fixture(path)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    main(["--db", str(path), "conflicts", "repair-invalid-groups", "--expected-count", "2"])

    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["candidate_case_count"] == 2
    assert preview["applied_case_count"] == 0
    assert connection.execute("SELECT count(*) FROM conflict_cases WHERE status='manual_required'").fetchone()[0] == 3

    main(
        [
            "--db",
            str(path),
            "conflicts",
            "repair-invalid-groups",
            "--apply",
            "--expected-count",
            "2",
        ]
    )

    applied = json.loads(capsys.readouterr().out)
    assert applied["dry_run"] is False
    assert applied["applied_case_count"] == 2
    assert applied["remaining_open_count"] == 1


def test_production_shaped_5841_repair_leaves_three_open_cases(tmp_path: Path) -> None:
    connection = Database(tmp_path / "production-shape.db").open()
    invalid_claim_ids = [f"invalid-{index:03d}" for index in range(116)]
    claim_rows = []
    for index, claim_id in enumerate(invalid_claim_ids):
        status = "disputed" if index < 82 else ("superseded" if index < 115 else "active")
        slot = "config.path" if index < 112 else "config.network"
        conflict_key = "path-group" if slot == "config.path" else "network-group"
        claim_rows.append(
            (
                claim_id,
                "default",
                "repair-fixture",
                "配置",
                f'"{claim_id}"',
                NOW,
                status,
                slot,
                slot,
                conflict_key,
            )
        )
    connection.executemany(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,value_json,recorded_from,status,"
        "canonical_slot,canonical_attribute,conflict_key"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        claim_rows,
    )
    path_pairs = list(combinations(invalid_claim_ids[:112], 2))[:5835]
    network_pairs = list(combinations(invalid_claim_ids[112:], 2))
    case_rows = [
        (
            f"invalid-case-{index:04d}",
            f"invalid-pair-{index:04d}",
            left_id,
            right_id,
            "manual_required",
            "uncertain",
            INVALID_RATIONALE,
            NOW,
        )
        for index, (left_id, right_id) in enumerate([*path_pairs, *network_pairs])
    ]
    connection.executemany(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        case_rows,
    )
    for index in range(3):
        left_id = f"valid-{index}-left"
        right_id = f"valid-{index}-right"
        conflict_key = f"valid-port-{index}"
        _insert_claim(connection, left_id, status="disputed", slot="config.port", conflict_key=conflict_key)
        _insert_claim(connection, right_id, status="disputed", slot="config.port", conflict_key=conflict_key)
        _insert_case(
            connection,
            f"valid-case-{index}",
            left_id,
            right_id,
            rationale="deterministic_ingest_resolution",
        )
    connection.commit()

    preview = inspect_invalid_conflict_groups(connection)
    assert preview["candidate_case_count"] == 5841
    assert preview["cases_by_slot"] == {"config.network": 6, "config.path": 5835}
    assert preview["endpoint_count"] == 116
    assert preview["endpoint_status_counts"] == {"active": 1, "disputed": 82, "superseded": 33}

    result = repair_invalid_conflict_groups(connection, expected_count=5841, repaired_at=NOW)

    assert result["applied_case_count"] == 5841
    assert result["activated_claim_count"] == 82
    assert result["remaining_open_count"] == 3
    assert result["invalid_open_count"] == 0
    assert find_orphan_disputed_claims(connection) == []
    assert find_dangling_conflict_references(connection) == []
    assert connection.execute(
        "SELECT count(*) FROM conflict_cases WHERE status IN ('pending','auto_resolved','manual_required') "
        "AND resolved_at IS NULL"
    ).fetchone()[0] == 3
