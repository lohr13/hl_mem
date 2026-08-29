from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.application.conflicts import ResolutionService
from hl_mem.errors import ConflictResolutionError
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
    assert body["fingerprint_version"] == "v2"
    assert len(body["fingerprint"]) == 64
    assert body["candidate_count"] == 2
    assert [candidate["canonical_value"] for candidate in body["candidates"]] == ["8080", "8081"]
    assert {candidate["representative_claim_id"] for candidate in body["candidates"]} == {"left", "right"}
    assert {candidate["representative_tip_id"] for candidate in body["candidates"]} == {"left", "right"}


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


def test_stale_group_fingerprint_returns_409_when_revision_still_matches(tmp_path: Path) -> None:
    path = tmp_path / "stale-fingerprint.db"
    case_id, winner_key = _seed(path)
    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        database = Database(path)
        connection = database.open()
        connection.execute("UPDATE claims SET confidence=0.42 WHERE id='left'")
        connection.commit()
        assert (
            connection.execute("SELECT revision FROM conflict_cases WHERE id=?", (case_id,)).fetchone()[0]
            == review["revision"]
        )
        database.close()
        before = _snapshot(path)

        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "select_candidate",
                "candidate_key": winner_key,
                "expected_revision": review["revision"],
                "expected_fingerprint": review["fingerprint"],
            },
        )

    assert response.status_code == 409
    assert "stale conflict fingerprint" in response.json()["detail"]
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


def test_group_resolution_selects_representative_tip_and_returns_tip_winner(tmp_path: Path) -> None:
    path = tmp_path / "select-tip.db"
    case_id, winner_key = _seed(path)
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    _claim(repository, "left-middle", "8080")
    _claim(repository, "left-tip", "8080")
    connection.execute(
        "UPDATE claims SET status='superseded',superseded_by_id='left-middle',valid_to=?,recorded_to=? "
        "WHERE id='left'",
        (NOW, NOW),
    )
    connection.execute(
        "UPDATE claims SET status='superseded',superseded_by_id='left-tip',valid_to=?,recorded_to=? "
        "WHERE id='left-middle'",
        (NOW, NOW),
    )
    connection.commit()
    database.close()

    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        winner = next(candidate for candidate in review["candidates"] if candidate["candidate_key"] == winner_key)
        assert winner["representative_claim_id"] == "left"
        assert winner["representative_tip_id"] == "left-tip"
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "select_candidate",
                "candidate_key": winner_key,
                "expected_revision": review["revision"],
                "expected_fingerprint": review["fingerprint"],
            },
        )

    assert response.status_code == 200
    assert response.json()["winner_id"] == "left-tip"


def test_group_native_terminal_replay_rejects_rationale_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "group-terminal-rationale.db"
    case_id, winner_key = _seed(path)
    connection = Database(path).open()
    service = ResolutionService(connection)
    review = service.review(case_id)
    service.resolve_group(
        case_id,
        "select_candidate",
        candidate_key=winner_key,
        expected_revision=review["revision"],
        expected_fingerprint=review["fingerprint"],
        rationale="original rationale",
    )
    current = service.review(case_id)

    with pytest.raises(ConflictResolutionError, match="terminal conflict rationale is immutable"):
        service.resolve_group(
            case_id,
            "select_candidate",
            candidate_key=winner_key,
            expected_revision=current["revision"],
            expected_fingerprint=current["fingerprint"],
            rationale="rewritten rationale",
        )

    assert (
        connection.execute("SELECT rationale FROM conflict_cases WHERE id=?", (case_id,)).fetchone()[0]
        == "original rationale"
    )


def test_reject_candidate_requires_api_confirmation_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "reject-confirmation-api.db"
    case_id, candidate_key = _seed(path)
    before = _snapshot(path)

    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "reject_candidate",
                "candidate_key": candidate_key,
                "expected_revision": review["revision"],
            },
        )

    assert response.status_code == 422
    assert "confirm_retraction=true" in response.text
    assert _snapshot(path) == before


def test_reject_candidate_requires_service_confirmation_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "reject-confirmation-service.db"
    case_id, candidate_key = _seed(path)
    before = _snapshot(path)
    database = Database(path)
    connection = database.open()
    revision = int(connection.execute("SELECT revision FROM conflict_cases WHERE id=?", (case_id,)).fetchone()[0])

    with pytest.raises(ConflictResolutionError, match="confirm_retraction=true"):
        ResolutionService(connection).resolve_group(
            case_id,
            "reject_candidate",
            candidate_key=candidate_key,
            expected_revision=revision,
        )

    database.close()
    assert _snapshot(path) == before


@pytest.mark.parametrize("open_status", ("pending", "auto_resolved", "manual_required"))
def test_reject_candidate_removes_only_that_candidate_and_keeps_case_open(
    tmp_path: Path,
    open_status: str,
) -> None:
    path = tmp_path / f"reject-candidate-{open_status}.db"
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
    connection.execute("UPDATE conflict_cases SET status=? WHERE id=?", (open_status, case_id))
    connection.commit()
    database.close()

    with TestClient(server.create_app(path)) as client:
        review = client.get(f"/v1/conflicts/{case_id}").json()
        assert review["status"] == open_status
        response = client.post(
            f"/v1/conflicts/{case_id}/resolve",
            json={
                "action": "reject_candidate",
                "candidate_key": '"8082"',
                "expected_revision": review["revision"],
                "rationale": "operator rejected stale port",
                "resolver": "agent:rest-reviewer",
                "confirm_retraction": True,
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
    assert before["case_status"] == open_status
    assert after["case_status"] == "manual_required"
    for snapshot in (before, after):
        assert snapshot["retracted_claim_count"] == 1
        assert snapshot["retracted_claim_ids"] == ["third"]
        assert snapshot["retracted_claim_ids_sha256"] == (
            "53f244a25fd6e7eea4f0526aa053d02d6686f1d8e5a0eeaa55079e7e2d9e93fd"
        )
        assert snapshot["retracted_claim_ids_truncated"] is False
    assert after["revision"] == review["revision"] + 1
    assert after["revision"] > before["revision"]
    database.close()


def test_reject_candidate_audit_bounds_retracted_member_ids(tmp_path: Path) -> None:
    path = tmp_path / "reject-candidate-bounded-audit.db"
    case_id, _ = _seed(path)
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"extra-{index:03d}", "8082") for index in range(66)]
    repository.ensure_group_conflict_case(
        members,
        created_at=NOW,
        decision="uncertain",
        rationale="bounded_audit",
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
                "confirm_retraction": True,
            },
        )

    assert response.status_code == 200
    connection = Database(path).open()
    action = connection.execute(
        "SELECT before_json,after_json FROM governance_actions WHERE subject_ref=?", (case_id,)
    ).fetchone()
    assert action is not None
    for raw_snapshot in action:
        snapshot = json.loads(raw_snapshot)
        assert snapshot["retracted_claim_count"] == 66
        assert len(snapshot["retracted_claim_ids"]) == 64
        assert snapshot["retracted_claim_ids"][0] == "extra-000"
        assert snapshot["retracted_claim_ids"][-1] == "extra-063"
        assert snapshot["retracted_claim_ids_sha256"] == (
            "5d1d017237fbdb337b40516adfbb571067e899adddbe013847122b302d2ba1d0"
        )
        assert snapshot["retracted_claim_ids_truncated"] is True
    connection.close()


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
