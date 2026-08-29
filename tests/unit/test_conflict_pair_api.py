from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-30T10:00:00+00:00"


def _seed_pair(path: Path) -> str:
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    for claim_id, value in (("left", "SQLite"), ("right", "PostgreSQL")):
        assert repository.insert_claim(
            {
                "id": claim_id,
                "namespace_key": "default",
                "subject_entity_id": "project",
                "predicate": "uses",
                "value": value,
                "recorded_from": NOW,
                "status": "disputed",
                "scope": "permanent",
                "source_authority": "medium",
            }
        )
    assert repository.insert_conflict_case(
        {
            "id": "pair-case",
            "pair_key": "left:right",
            "left_claim_id": "left",
            "right_claim_id": "right",
            "status": "manual_required",
            "decision": "uncertain",
            "created_at": NOW,
        }
    )
    connection.close()
    return "pair-case"


def _database_state(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = Database(path).open()
    state = {
        "claims": [
            tuple(row)
            for row in connection.execute(
                "SELECT id,status,superseded_by_id,valid_to,recorded_to FROM claims ORDER BY id"
            )
        ],
        "cases": [
            tuple(row)
            for row in connection.execute(
                "SELECT id,status,decision,resolved_at,revision FROM conflict_cases ORDER BY id"
            )
        ],
        "actions": [tuple(row) for row in connection.execute("SELECT * FROM governance_actions ORDER BY id")],
    }
    connection.close()
    return state


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_winner", "expected_claim_statuses"),
    [
        ("keep_left", "resolved", "left", {"left": "active", "right": "superseded"}),
        ("keep_right", "resolved", "right", {"left": "superseded", "right": "active"}),
        ("coexist", "resolved", None, {"left": "active", "right": "active"}),
        ("reject", "rejected", None, {"left": "active", "right": "active"}),
    ],
)
def test_pair_rest_supports_full_action_vocabulary_and_one_audit(
    tmp_path: Path,
    decision: str,
    expected_status: str,
    expected_winner: str | None,
    expected_claim_statuses: dict[str, str],
) -> None:
    path = tmp_path / f"pair-{decision}.db"
    case_id = _seed_pair(path)

    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": decision,
                "expected_revision": review["revision"],
                "expected_fingerprint": review["fingerprint"],
                "rationale": f"host chose {decision}",
                "resolver": "agent:host-test",
            },
        )

    assert response.status_code == 200
    body = response.json()
    connection = Database(path).open()
    case = connection.execute(
        "SELECT revision,resolved_at FROM conflict_cases WHERE id=?",
        (case_id,),
    ).fetchone()
    assert int(case["revision"]) > review["revision"]
    assert body == {
        "case_id": case_id,
        "generation": 1,
        "revision": int(case["revision"]),
        "status": expected_status,
        "decision": decision,
        "winner_id": expected_winner,
        "resolved_at": str(case["resolved_at"]),
        "closed_case_ids": [case_id],
    }
    assert {
        str(row["id"]): str(row["status"]) for row in connection.execute("SELECT id,status FROM claims ORDER BY id")
    } == expected_claim_statuses
    actions = connection.execute(
        "SELECT decision,resolver_model FROM governance_actions WHERE subject_ref=?",
        (case_id,),
    ).fetchall()
    assert [(row["decision"], row["resolver_model"]) for row in actions] == [(decision, "agent:host-test")]


def test_pair_rest_stale_revision_returns_409_without_mutation_or_audit(tmp_path: Path) -> None:
    path = tmp_path / "pair-stale-revision.db"
    case_id = _seed_pair(path)
    before = _database_state(path)

    with TestClient(server.create_app(path)) as client:
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={"action": "keep_left", "expected_revision": 7},
        )

    assert response.status_code == 409
    assert "stale conflict revision" in response.json()["detail"]
    assert _database_state(path) == before


def test_pair_rest_stale_fingerprint_returns_409_without_mutation_or_audit(tmp_path: Path) -> None:
    path = tmp_path / "pair-stale-fingerprint.db"
    case_id = _seed_pair(path)
    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        connection = Database(path).open()
        connection.execute("UPDATE claims SET confidence=0.42 WHERE id='left'")
        connection.commit()
        connection.close()
        before = _database_state(path)

        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "keep_left",
                "expected_revision": review["revision"],
                "expected_fingerprint": review["fingerprint"],
            },
        )

    assert response.status_code == 409
    assert "stale conflict fingerprint" in response.json()["detail"]
    assert _database_state(path) == before


def test_pair_rest_blind_retry_is_stale_and_keeps_single_audit(tmp_path: Path) -> None:
    path = tmp_path / "pair-blind-retry.db"
    case_id = _seed_pair(path)
    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        payload = {
            "action": "keep_left",
            "expected_revision": review["revision"],
            "expected_fingerprint": review["fingerprint"],
            "resolver": "agent:host-test",
        }
        first = client.post(f"/v1/conflicts/{case_id}/resolve", json=payload)
        retry = client.post(f"/v1/conflicts/{case_id}/resolve", json=payload)

    assert first.status_code == 200
    assert retry.status_code == 409
    connection = Database(path).open()
    assert (
        connection.execute("SELECT count(*) FROM governance_actions WHERE subject_ref=?", (case_id,)).fetchone()[0] == 1
    )


def test_conflict_resolve_openapi_uses_action_discriminator_and_pair_group_responses(tmp_path: Path) -> None:
    operation = server.create_app(tmp_path / "openapi.db").openapi()["paths"]["/v1/conflicts/{case_id}/resolve"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]

    assert request_schema["discriminator"]["propertyName"] == "action"
    assert set(request_schema["discriminator"]["mapping"]) == {
        "keep_left",
        "keep_right",
        "coexist",
        "reject",
        "select_candidate",
        "reject_candidate",
    }
    assert len(request_schema["oneOf"]) == 2
    assert len(response_schema["anyOf"]) == 2
