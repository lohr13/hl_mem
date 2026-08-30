import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.recall import auxiliary_context_is_current
from hl_mem.experience.service import ExperienceService
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.evidence import DerivationRepository, EvidenceRepository


@pytest.mark.parametrize(
    ("as_of", "known_as_of", "expected"),
    [
        (None, None, True),
        ("2026-01-01T00:00:00Z", None, False),
        (None, "2026-01-01T00:00:00Z", False),
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", False),
    ],
)
def test_auxiliary_context_requires_current_time(as_of, known_as_of, expected) -> None:
    assert auxiliary_context_is_current(as_of=as_of, known_as_of=known_as_of) is expected


def test_historical_recall_omits_current_only_auxiliary_context(tmp_path) -> None:
    app = create_app(tmp_path / "historical-auxiliary.db")
    try:
        with TestClient(app) as client:
            connection = app.state.db.open()
            claims = ClaimRepository(connection)
            claims.insert_claim(
                {
                    "id": "claim-historical",
                    "status": "active",
                    "subject_entity_id": "service",
                    "predicate": "state",
                    "value": "historical baseline",
                    "index_text": "service outage historical baseline",
                    "valid_from": "2025-01-01T00:00:00+00:00",
                    "recorded_from": "2025-01-01T00:00:00+00:00",
                }
            )
            claims.insert_claim(
                {
                    "id": "claim-valid-time-future",
                    "status": "active",
                    "subject_entity_id": "service",
                    "predicate": "state",
                    "value": "future valid-time migration",
                    "index_text": "service outage future valid-time migration",
                    "valid_from": "2026-02-01T00:00:00+00:00",
                    "recorded_from": "2025-12-01T00:00:00+00:00",
                }
            )
            claims.insert_claim(
                {
                    "id": "claim-recorded-time-future",
                    "status": "active",
                    "subject_entity_id": "service",
                    "predicate": "state",
                    "value": "future recorded-time migration",
                    "index_text": "service outage future recorded-time migration",
                    "valid_from": "2025-12-01T00:00:00+00:00",
                    "recorded_from": "2026-02-01T00:00:00+00:00",
                }
            )
            DerivationRepository(connection).insert_observation(
                {
                    "id": "observation-current",
                    "body": "Current-only derived service context",
                    "status": "active",
                    "updated_at": "2026-08-30T00:00:00+00:00",
                }
            )
            EvidenceRepository(connection).add_link(
                {
                    "id": "observation-current-evidence",
                    "derived_type": "observation",
                    "derived_id": "observation-current",
                    "evidence_type": "claim",
                    "evidence_id": "claim-historical",
                    "relation": "supports",
                }
            )
            experience = ExperienceService(connection, min_support=2)
            for episode_id in ("episode-1", "episode-2"):
                experience.record_episode(
                    episode_id,
                    "recover service",
                    "success",
                    1.0,
                    "2026-08-30T00:00:00+00:00",
                )
            policy_id = experience.induce_policy(
                "service outage",
                {"steps": ["inspect logs"]},
                ["episode-1", "episode-2"],
                "2026-08-30T00:00:00+00:00",
            )

            current = client.post(
                "/v1/recall",
                json={"query": "service outage", "intent": "current_state", "limit": 10},
            )
            as_of = client.post(
                "/v1/recall",
                json={
                    "query": "service outage",
                    "intent": "current_state",
                    "limit": 10,
                    "as_of": "2026-01-15T00:00:00+00:00",
                },
            )
            known_as_of = client.post(
                "/v1/recall",
                json={
                    "query": "service outage",
                    "intent": "current_state",
                    "limit": 10,
                    "known_as_of": "2026-01-15T00:00:00+00:00",
                },
            )

        assert current.status_code == 200
        current_payload = current.json()
        assert {item["id"] for item in current_payload["results"]} == {
            "claim-historical",
            "claim-valid-time-future",
            "claim-recorded-time-future",
        }
        assert [item["id"] for item in current_payload["observations"]] == ["observation-current"]
        assert [item["id"] for item in current_payload["policies"]] == [policy_id]

        assert as_of.status_code == 200
        as_of_payload = as_of.json()
        assert {item["id"] for item in as_of_payload["results"]} == {
            "claim-historical",
            "claim-recorded-time-future",
        }
        assert as_of_payload["policies"] == []
        assert as_of_payload["observations"] == []

        assert known_as_of.status_code == 200
        known_as_of_payload = known_as_of.json()
        assert {item["id"] for item in known_as_of_payload["results"]} == {
            "claim-historical",
            "claim-valid-time-future",
        }
        assert known_as_of_payload["policies"] == []
        assert known_as_of_payload["observations"] == []
    finally:
        app.state.db.close()


@pytest.mark.parametrize(
    ("intent", "historical_fields"),
    [
        ("procedure", {"as_of": "2026-01-15T00:00:00+00:00"}),
        ("procedure", {"known_as_of": "2026-01-15T00:00:00+00:00"}),
        ("tool", {"as_of": "2026-01-15T00:00:00+00:00"}),
        ("tool", {"known_as_of": "2026-01-15T00:00:00+00:00"}),
    ],
)
def test_historical_experience_recall_omits_current_policies(
    tmp_path,
    intent: str,
    historical_fields: dict[str, str],
) -> None:
    app = create_app(tmp_path / f"historical-{intent}.db")
    try:
        with TestClient(app) as client:
            connection = app.state.db.open()
            ClaimRepository(connection).insert_claim(
                {
                    "id": "claim-historical-procedure",
                    "status": "active",
                    "subject_entity_id": "service",
                    "predicate": "state",
                    "value": "historical outage procedure",
                    "index_text": "service outage historical procedure",
                    "valid_from": "2025-01-01T00:00:00+00:00",
                    "recorded_from": "2025-01-01T00:00:00+00:00",
                }
            )
            experience = ExperienceService(connection, min_support=2)
            for episode_id in ("episode-1", "episode-2"):
                experience.record_episode(
                    episode_id,
                    f"unrelated support {episode_id}",
                    "success",
                    1.0,
                    "2025-01-01T00:00:00+00:00",
                )
            policy_ids = {
                experience.induce_policy(
                    f"service outage policy {index}",
                    {"steps": [f"inspect logs {index}"]},
                    ["episode-1", "episode-2"],
                    f"2026-08-30T00:00:0{index}+00:00",
                )
                for index in range(4)
            }

            current = client.post(
                "/v1/recall",
                json={"query": "service outage", "intent": intent, "limit": 1},
            )
            historical = client.post(
                "/v1/recall",
                json={
                    "query": "service outage",
                    "intent": intent,
                    "limit": 1,
                    **historical_fields,
                },
            )

        assert current.status_code == 200
        current_payload = current.json()
        assert len(current_payload["policies"]) == 1
        assert current_payload["policies"][0]["id"] in policy_ids
        assert [item["memory_type"] for item in current_payload["results"]] == ["policy"]

        assert historical.status_code == 200
        historical_payload = historical.json()
        assert historical_payload["policies"] == []
        assert [item["id"] for item in historical_payload["results"]] == ["claim-historical-procedure"]
        assert historical_payload["total"] == 1
        assert historical_payload["answerability"] == "supported"
    finally:
        app.state.db.close()
