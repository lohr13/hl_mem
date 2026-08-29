from __future__ import annotations

import copy
from pathlib import Path

import pytest

from hl_mem.application.conflict_snapshot import (
    conflict_docket_fingerprint,
    load_conflict_docket,
    resolve_claim_lineage,
)
from hl_mem.errors import ConflictResolutionError
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-30T08:00:00+00:00"


def _claim(
    repository: ClaimRepository,
    claim_id: str,
    value: str | None = None,
    *,
    conflict_key: str | None = None,
) -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "gateway",
            "predicate": "uses",
            "value": value or claim_id,
            "qualifiers": {"service": "gateway"},
            "canonical_attribute": "config.port",
            "canonical_slot": "config.port",
            "fact_hash": f"hash-{claim_id}",
            "conflict_key": conflict_key or f"key-{claim_id}",
            "conflict_key_version": 3,
            "recorded_from": NOW,
            "status": "disputed",
            "source_authority": "medium",
            "scope": "permanent",
            "volatility": "stable",
        }
    )


def _link(connection, source_id: str, target_id: str) -> None:
    connection.execute(
        "UPDATE claims SET status='superseded',superseded_by_id=?,valid_to=?,recorded_to=? WHERE id=?",
        (target_id, NOW, NOW, source_id),
    )


def _pair_with_lineages(path: Path):
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    for claim_id in ("left-0", "left-1", "left-tip", "right-0", "right-tip"):
        _claim(repository, claim_id)
    _link(connection, "left-0", "left-1")
    _link(connection, "left-1", "left-tip")
    _link(connection, "right-0", "right-tip")
    assert repository.insert_conflict_case(
        {
            "id": "case",
            "pair_key": "left-0:right-0",
            "left_claim_id": "left-0",
            "right_claim_id": "right-0",
            "status": "manual_required",
            "decision": "uncertain",
            "rationale": "needs review",
            "created_at": NOW,
        }
    )
    connection.commit()
    return connection, repository


def test_resolve_claim_lineage_returns_every_claim_edge_and_tip(tmp_path: Path) -> None:
    connection, repository = _pair_with_lineages(tmp_path / "lineage.db")

    lineage = resolve_claim_lineage(repository, "left-0")

    assert [claim["id"] for claim in lineage.claims] == ["left-0", "left-1", "left-tip"]
    assert lineage.edges == (("left-0", "left-1"), ("left-1", "left-tip"))
    assert lineage.tip_id == "left-tip"
    assert lineage.tip["status"] == "disputed"
    connection.close()


def test_resolve_claim_lineage_rejects_cycle(tmp_path: Path) -> None:
    connection = Database(tmp_path / "cycle.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "a")
    _claim(repository, "b")
    _link(connection, "a", "b")
    _link(connection, "b", "a")
    connection.commit()

    with pytest.raises(ConflictResolutionError, match="supersession cycle.*a"):
        resolve_claim_lineage(repository, "a")


def test_resolve_claim_lineage_rejects_missing_claim(tmp_path: Path) -> None:
    connection = Database(tmp_path / "missing.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "start")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    _link(connection, "start", "missing")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(ConflictResolutionError, match="supersession claim is missing: missing"):
        resolve_claim_lineage(repository, "start")


def test_resolve_claim_lineage_allows_32_edges_and_rejects_33(tmp_path: Path) -> None:
    allowed_connection = Database(tmp_path / "depth-32.db").open()
    allowed_repository = ClaimRepository(allowed_connection)
    for index in range(33):
        _claim(allowed_repository, f"claim-{index:02d}")
    for index in range(32):
        _link(allowed_connection, f"claim-{index:02d}", f"claim-{index + 1:02d}")
    allowed_connection.commit()

    assert resolve_claim_lineage(allowed_repository, "claim-00").tip_id == "claim-32"

    denied_connection = Database(tmp_path / "depth-33.db").open()
    denied_repository = ClaimRepository(denied_connection)
    for index in range(34):
        _claim(denied_repository, f"claim-{index:02d}")
    for index in range(33):
        _link(denied_connection, f"claim-{index:02d}", f"claim-{index + 1:02d}")
    denied_connection.commit()

    with pytest.raises(ConflictResolutionError, match="supersession depth exceeds 32: claim-00"):
        resolve_claim_lineage(denied_repository, "claim-00")


def test_docket_rejects_missing_nonrepresentative_candidate_member(tmp_path: Path) -> None:
    connection = Database(tmp_path / "missing-member.db").open()
    repository = ClaimRepository(connection)
    for claim_id, value in (("a1", "8080"), ("a2", "8080"), ("b", "8081")):
        _claim(repository, claim_id, value, conflict_key="gateway-port")
    members = [repository.get_claim(claim_id) for claim_id in ("a1", "a2", "b")]
    created = repository.ensure_group_conflict_case(
        [member for member in members if member is not None],
        created_at=NOW,
        decision="uncertain",
        rationale="missing member fixture",
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM claims WHERE id='a2'")
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(ConflictResolutionError, match="conflict candidate member is missing: a2"):
        load_conflict_docket(connection, str(created["case_id"]))


def test_docket_uses_tips_and_retains_complete_lineages(tmp_path: Path) -> None:
    connection, _ = _pair_with_lineages(tmp_path / "docket.db")

    docket = load_conflict_docket(connection, "case")

    assert [claim["id"] for claim in docket["claims"]] == ["left-tip", "right-tip"]
    assert docket["context"]["left_tip_id"] == "left-tip"
    assert docket["context"]["right_tip_id"] == "right-tip"
    assert [claim["id"] for claim in docket["lineages"]["left"]["claims"]] == [
        "left-0",
        "left-1",
        "left-tip",
    ]
    assert docket["lineages"]["left"]["edges"] == [
        {"source_id": "left-0", "target_id": "left-1"},
        {"source_id": "left-1", "target_id": "left-tip"},
    ]


def test_v2_fingerprint_tracks_tips_edges_and_decision_but_not_query_order(tmp_path: Path) -> None:
    connection, _ = _pair_with_lineages(tmp_path / "fingerprint.db")
    docket = load_conflict_docket(connection, "case")
    docket["evidence"] = [{"id": "evidence-b"}, {"id": "evidence-a"}]
    baseline = conflict_docket_fingerprint(docket)

    reordered = copy.deepcopy(docket)
    reordered["candidates"].reverse()
    reordered["evidence"].reverse()
    reordered["resolution_scope"]["claims"].reverse()
    reordered["resolution_scope"]["cases"].reverse()
    assert conflict_docket_fingerprint(reordered) == baseline

    changed_tip = copy.deepcopy(docket)
    changed_tip["claims"][0]["value"] = "changed"
    assert conflict_docket_fingerprint(changed_tip) != baseline

    changed_edge = copy.deepcopy(docket)
    changed_edge["lineages"]["left"]["edges"][0]["target_id"] = "other"
    assert conflict_docket_fingerprint(changed_edge) != baseline

    changed_decision = copy.deepcopy(docket)
    changed_decision["case"]["rationale"] = "reviewed elsewhere"
    assert conflict_docket_fingerprint(changed_decision) != baseline
