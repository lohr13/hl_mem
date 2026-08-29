from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-18T08:00:00+00:00"


def _claim(repository: ClaimRepository, claim_id: str, value: str) -> dict[str, object]:
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
            "conflict_key": "gateway-port",
            "conflict_key_version": 3,
            "recorded_from": NOW,
            "status": "disputed",
            "source_authority": "medium",
            "scope": "permanent",
            "volatility": "stable",
        }
    )
    claim = repository.get_claim(claim_id)
    assert claim is not None
    return claim


def _seed(path: Path) -> tuple[str, str]:
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    left = _claim(repository, "left", "8080")
    right = _claim(repository, "right", "8081")
    created = repository.ensure_group_conflict_case(
        [left, right],
        created_at=NOW,
        decision="uncertain",
        rationale="api_test",
    )
    database.close()
    return str(created["case_id"]), '"8080"'


def _snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    database = Database(path)
    connection = database.open()
    snapshot = {
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
        "candidates": [
            tuple(row)
            for row in connection.execute(
                "SELECT case_id,candidate_key,representative_claim_id,support_count "
                "FROM conflict_case_candidates ORDER BY case_id,candidate_key"
            )
        ],
    }
    database.close()
    return snapshot


def test_review_returns_generation_revision_and_all_candidates(tmp_path: Path) -> None:
    path = tmp_path / "review.db"
    case_id, _ = _seed(path)

    with TestClient(server.create_app(path)) as client:
        response = client.get(f"/v1/conflicts/{case_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["case_id"] == case_id
    assert body["generation"] == 1
    assert body["revision"] == 2
    assert body["candidate_count"] == 2
    assert [candidate["canonical_value"] for candidate in body["candidates"]] == ["8080", "8081"]
    assert {candidate["representative_claim_id"] for candidate in body["candidates"]} == {"left", "right"}


def test_stale_group_resolution_returns_409_without_mutating_any_state(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    case_id, winner_key = _seed(path)
    with TestClient(server.create_app(path)) as client:
        stale_revision = client.get(f"/v1/conflicts/{case_id}").json()["revision"]

        database = Database(path)
        connection = database.open()
        repository = ClaimRepository(connection)
        third = _claim(repository, "third", "8082")
        repository.ensure_group_conflict_case(
            [third],
            created_at=NOW,
            decision="uncertain",
            rationale="new_candidate",
        )
        database.close()
        refreshed = client.get(f"/v1/conflicts/{case_id}").json()
        assert refreshed["revision"] == stale_revision + 1
        assert refreshed["candidate_count"] == 3
        assert {candidate["canonical_value"] for candidate in refreshed["candidates"]} == {
            "8080",
            "8081",
            "8082",
        }
        before = _snapshot(path)

        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "select_candidate",
                "candidate_key": winner_key,
                "expected_revision": stale_revision,
            },
        )

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert _snapshot(path) == before


def test_current_revision_selects_candidate_and_closes_group(tmp_path: Path) -> None:
    path = tmp_path / "select.db"
    case_id, winner_key = _seed(path)
    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "select_candidate",
                "candidate_key": winner_key,
                "expected_revision": review["revision"],
            },
        )

    assert response.status_code == 200
    assert response.json()["winner_id"] == "left"
    database = Database(path)
    connection = database.open()
    assert connection.execute("SELECT status FROM conflict_cases WHERE id=?", (case_id,)).fetchone()[0] == "resolved"
    assert connection.execute("SELECT count(*) FROM claims WHERE status='active'").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM claims WHERE status='superseded'").fetchone()[0] == 1
    database.close()


def test_reject_candidate_removes_only_that_candidate_and_keeps_case_open(tmp_path: Path) -> None:
    path = tmp_path / "reject-candidate.db"
    case_id, _ = _seed(path)
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    third = _claim(repository, "third", "8082")
    repository.ensure_group_conflict_case(
        [third],
        created_at=NOW,
        decision="uncertain",
        rationale="third",
    )
    database.close()

    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "reject_candidate",
                "candidate_key": '"8082"',
                "expected_revision": review["revision"],
                "rationale": "operator rejected stale port",
                "resolver": "agent:rest-reviewer",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "manual_required"
    assert response.json()["revision"] == review["revision"] + 1
    database = Database(path)
    connection = database.open()
    assert connection.execute("SELECT status FROM claims WHERE id='third'").fetchone()[0] == "retracted"
    assert connection.execute("SELECT status FROM conflict_cases WHERE id=?", (case_id,)).fetchone()[0] == (
        "manual_required"
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM conflict_case_candidates WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        == 2
    )
    action = connection.execute("SELECT * FROM governance_actions WHERE subject_ref=?", (case_id,)).fetchone()
    assert action is not None
    assert (action["decision"], action["resolution_rule"], action["resolver_model"]) == (
        "reject_candidate",
        "operator rejected stale port",
        "agent:rest-reviewer",
    )
    before = json.loads(action["before_json"])
    after = json.loads(action["after_json"])
    assert before["candidate_key"] == '"8082"'
    assert before["rationale"] == "operator rejected stale port"
    assert after["revision"] == review["revision"] + 1
    assert after["revision"] > before["revision"]
    database.close()


def test_terminal_group_resolution_retry_preserves_state_and_audit_count(tmp_path: Path) -> None:
    path = tmp_path / "terminal-retry.db"
    case_id, winner_key = _seed(path)
    payload = {
        "action": "select_candidate",
        "candidate_key": winner_key,
        "expected_revision": 2,
        "rationale": "reviewed once",
        "resolver": "agent:rest-reviewer",
    }
    with TestClient(server.create_app(path)) as client:
        first = client.post(f"/v1/conflicts/{case_id}/resolve", json=payload)
        assert first.status_code == 200
        payload["expected_revision"] = first.json()["revision"]

        database = Database(path)
        connection = database.open()
        before_case = tuple(
            connection.execute(
                "SELECT status,decision,rationale,resolved_at,revision FROM conflict_cases WHERE id=?",
                (case_id,),
            ).fetchone()
        )
        before_actions = connection.execute(
            "SELECT count(*) FROM governance_actions WHERE subject_ref=?",
            (case_id,),
        ).fetchone()[0]
        database.close()

        retry = client.post(f"/v1/conflicts/{case_id}/resolve", json=payload)

    assert retry.status_code == 200
    database = Database(path)
    connection = database.open()
    after_case = tuple(
        connection.execute(
            "SELECT status,decision,rationale,resolved_at,revision FROM conflict_cases WHERE id=?",
            (case_id,),
        ).fetchone()
    )
    after_actions = connection.execute(
        "SELECT count(*) FROM governance_actions WHERE subject_ref=?",
        (case_id,),
    ).fetchone()[0]
    assert after_case == before_case
    assert after_actions == before_actions == 1
    connection.close()
