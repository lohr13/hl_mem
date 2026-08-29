from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.application.conflict_queries import ConflictQueryService
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository

NOW = "2026-08-29T20:00:00+00:00"


def _claim(
    repository: ClaimRepository,
    claim_id: str,
    value: str,
    *,
    status: str = "disputed",
) -> dict[str, object]:
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
            "valid_from": "2026-08-01T00:00:00+00:00",
            "valid_to": None,
            "recorded_from": NOW,
            "observed_at": "2026-08-29T19:30:00+00:00",
            "status": status,
            "assertion_kind": "observation",
            "confidence": 0.91,
            "source_authority": "high",
            "scope": "permanent",
            "volatility": "stable",
        }
    )
    claim = repository.get_claim(claim_id)
    assert claim is not None
    return claim


def _pair_case(path: Path, *, left_value: str = "8080") -> str:
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", left_value)
    _claim(repository, "right", "8081")
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
    database.close()
    return "pair-case"


def _add_event_evidence(path: Path, claim_id: str, count: int = 1) -> None:
    database = Database(path)
    connection = database.open()
    events = EventRepository(connection)
    evidence = EvidenceRepository(connection)
    for index in range(count):
        event_id = f"event-{index}"
        assert events.insert_event(
            {
                "id": event_id,
                "event_type": "message",
                "actor_type": "user",
                "content": {"text": f"evidence-{index}-" + "证" * 600},
                "occurred_at": f"2026-08-29T19:{index:02d}:00+00:00",
                "recorded_at": NOW,
            }
        )
        assert evidence.add_link(
            {
                "id": f"link-{index}",
                "derived_type": "claim",
                "derived_id": claim_id,
                "evidence_type": "event",
                "evidence_id": event_id,
                "relation": "derived_from",
                "weight": 1.0,
            }
        )
    database.close()


def test_pair_dossier_returns_full_claims_and_bounded_event_evidence(tmp_path: Path) -> None:
    path = tmp_path / "pair-dossier.db"
    case_id = _pair_case(path, left_value="8" * 700)
    _add_event_evidence(path, "left", count=6)

    with TestClient(server.create_app(path)) as client:
        response = client.get(f"/v1/conflicts/{case_id}/dossier")

    assert response.status_code == 200
    body = response.json()
    assert {
        "case_id": body["case_id"],
        "pair_key": body["pair_key"],
        "status": body["status"],
        "created_at": body["created_at"],
        "revision": body["revision"],
        "namespace_key": body["namespace_key"],
        "group_key": body["group_key"],
        "overflow": body["overflow"],
    } == {
        "case_id": "pair-case",
        "pair_key": "left:right",
        "status": "manual_required",
        "created_at": NOW,
        "revision": 0,
        "namespace_key": None,
        "group_key": None,
        "overflow": False,
    }
    left = body["left_claim"]
    assert left["value"] == "8" * 700
    assert {
        "id": left["id"],
        "canonical_slot": left["canonical_slot"],
        "subject_entity_id": left["subject_entity_id"],
        "assertion_kind": left["assertion_kind"],
        "confidence": left["confidence"],
        "source_authority": left["source_authority"],
        "valid_from": left["valid_from"],
        "valid_to": left["valid_to"],
        "observed_at": left["observed_at"],
        "status": left["status"],
    } == {
        "id": "left",
        "canonical_slot": "config.port",
        "subject_entity_id": "gateway",
        "assertion_kind": "observation",
        "confidence": 0.91,
        "source_authority": "high",
        "valid_from": "2026-08-01T00:00:00+00:00",
        "valid_to": None,
        "observed_at": "2026-08-29T19:30:00+00:00",
        "status": "disputed",
    }
    assert [link["id"] for link in left["evidence_links"]] == [f"link-{index}" for index in range(5)]
    assert left["evidence_links"][0] == {
        "id": "link-0",
        "evidence_type": "event",
        "evidence_id": "event-0",
        "relation": "derived_from",
        "weight": 1.0,
        "event_type": "message",
        "occurred_at": "2026-08-29T19:00:00+00:00",
        "content_json": left["evidence_links"][0]["content_json"],
    }
    assert left["evidence_links"][0]["content_json"].startswith('{"text": "evidence-0-')
    assert all(len(link["content_json"]) == 500 for link in left["evidence_links"])
    assert body["right_claim"]["id"] == "right"
    assert body["candidates"] == []


def test_group_dossier_returns_candidates_and_full_member_claims(tmp_path: Path) -> None:
    path = tmp_path / "group-dossier.db"
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    left = _claim(repository, "left", "8080")
    right = _claim(repository, "right", "8081")
    created = repository.ensure_group_conflict_case(
        [left, right],
        created_at=NOW,
        decision="uncertain",
        rationale="delegated",
    )
    database.close()

    with TestClient(server.create_app(path)) as client:
        response = client.get(f"/v1/conflicts/{created['case_id']}/dossier")

    assert response.status_code == 200
    body = response.json()
    assert body["namespace_key"] == "default"
    assert body["group_key"] == "gateway-port"
    assert body["revision"] == 2
    assert [candidate["candidate_key"] for candidate in body["candidates"]] == ['"8080"', '"8081"']
    assert [candidate["canonical_value_json"] for candidate in body["candidates"]] == ['"8080"', '"8081"']
    assert {candidate["representative_claim_id"] for candidate in body["candidates"]} == {"left", "right"}
    assert all(candidate["support_count"] == 1 for candidate in body["candidates"])
    assert {member["id"] for candidate in body["candidates"] for member in candidate["member_claims"]} == {
        "left",
        "right",
    }
    assert all(
        member["canonical_slot"] == "config.port"
        for candidate in body["candidates"]
        for member in candidate["member_claims"]
    )
    left_member = next(
        member for candidate in body["candidates"] for member in candidate["member_claims"] if member["id"] == "left"
    )
    assert left_member == {
        "id": "left",
        "canonical_slot": "config.port",
        "value": "8080",
        "subject_entity_id": "gateway",
        "assertion_kind": "observation",
        "confidence": 0.91,
        "source_authority": "high",
        "valid_from": "2026-08-01T00:00:00+00:00",
        "valid_to": None,
        "observed_at": "2026-08-29T19:30:00+00:00",
        "status": "disputed",
        "evidence_links": [],
    }


def test_large_group_dossier_uses_bounded_batch_queries(tmp_path: Path) -> None:
    path = tmp_path / "large-group.db"
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index:02d}", str(8000 + index)) for index in range(25)]
    created = repository.ensure_group_conflict_case(
        members,
        created_at=NOW,
        decision="uncertain",
        rationale="large_group",
    )
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    dossier = ConflictQueryService(connection).dossier(str(created["case_id"]))

    connection.set_trace_callback(None)
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(dossier["candidates"]) == 25
    assert sum(len(candidate["member_claims"]) for candidate in dossier["candidates"]) == 25
    assert len(selects) <= 6


def test_conflict_list_filters_open_statuses_and_paginates(tmp_path: Path) -> None:
    path = tmp_path / "list.db"
    database = Database(path)
    connection = database.open()
    repository = ClaimRepository(connection)
    for index, status in enumerate(("manual_required", "pending", "auto_resolved", "resolved")):
        left_id = f"left-{index}"
        right_id = f"right-{index}"
        _claim(repository, left_id, f"left-{index}")
        _claim(repository, right_id, f"right-{index}")
        assert repository.insert_conflict_case(
            {
                "id": f"case-{index}",
                "pair_key": f"{left_id}:{right_id}",
                "left_claim_id": left_id,
                "right_claim_id": right_id,
                "status": status,
                "created_at": f"2026-08-29T20:0{index}:00+00:00",
                "resolved_at": NOW if status == "resolved" else None,
            }
        )
    database.close()

    with TestClient(server.create_app(path)) as client:
        unfiltered = client.get("/v1/conflicts")
        filtered = client.get(
            "/v1/conflicts",
            params={"status": "manual_required,pending", "limit": 1, "offset": 1},
        )

    assert unfiltered.status_code == 200
    assert unfiltered.json()["total"] == 3
    assert [item["case_id"] for item in unfiltered.json()["cases"]] == ["case-0", "case-1", "case-2"]
    assert filtered.status_code == 200
    assert filtered.json() == {
        "cases": [
            {
                "case_id": "case-1",
                "status": "pending",
                "created_at": "2026-08-29T20:01:00+00:00",
                "namespace": "default",
                "group_key": None,
                "slot": "config.port",
                "revision": 0,
            }
        ],
        "total": 2,
        "limit": 1,
        "offset": 1,
    }


def test_dossier_missing_case_returns_resource_specific_404(tmp_path: Path) -> None:
    with TestClient(server.create_app(tmp_path / "missing.db")) as client:
        response = client.get("/v1/conflicts/missing/dossier")

    assert response.status_code == 404
    assert response.json() == {"detail": "conflict case not found: missing"}


def test_dossier_rejects_serialized_response_larger_than_one_mibibyte(tmp_path: Path) -> None:
    path = tmp_path / "oversized.db"
    case_id = _pair_case(path, left_value="x" * 1_100_000)

    with TestClient(server.create_app(path)) as client:
        response = client.get(f"/v1/conflicts/{case_id}/dossier")

    assert response.status_code == 413
    assert response.json() == {"detail": "conflict dossier exceeds 1048576 bytes: pair-case"}


def test_dossier_openapi_declares_structured_404_and_413_errors(tmp_path: Path) -> None:
    operation = server.create_app(tmp_path / "openapi.db").openapi()["paths"]["/v1/conflicts/{case_id}/dossier"]["get"]

    assert operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorOutput")
    assert operation["responses"]["413"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorOutput")
