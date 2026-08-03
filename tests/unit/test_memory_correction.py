from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.correction import CorrectionService
from hl_mem.errors import ConflictError, ValidationError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.mcp.server import McpMemoryServer, get_tool_schemas
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.evidence import EvidenceRepository
from hl_mem.storage.experience import ExperienceRepository


def _settings(database_path: Path) -> Settings:
    return replace(Settings.for_test(), database_path=str(database_path), embedding_dim=8)


def _insert_claim(connection, claim_id: str = "claim-old") -> None:
    assert ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": "project-a",
            "subject_entity_id": "Alice",
            "predicate": "uses_language",
            "value": "Python 3.11",
            "qualifiers": {"project": "hl_mem"},
            "canonical_attribute": "preference.language",
            "canonical_slot": "preference.programming_language",
            "topic_tags_json": '["python","backend"]',
            "fact_hash": "old-fact-hash",
            "conflict_key": "same-conflict-key",
            "conflict_key_version": 3,
            "legacy_conflict_key": "same-legacy-key",
            "valid_from": "2026-07-01T00:00:00+00:00",
            "recorded_from": "2026-07-01T00:00:01+00:00",
            "observed_at": "2026-07-01T00:00:00+00:00",
            "expires_at": "2026-07-15T00:00:00+00:00",
            "volatility": "stable",
            "status": "active",
            "confidence": 0.72,
            "importance": 0.8,
            "scope": "temporal",
            "source_authority": "high",
            "extractor_version": "llm-v1",
            "embedding_dense": FakeEmbedder(8).embed_one(
                "Alice uses_language Python 3.11 preference.programming_language python backend"
            ),
            "embedding_model": "fake",
            "embedding_dim": 8,
        }
    )


def test_correct_memory_api_replaces_only_content_and_rebuilds_derived_fields(tmp_path: Path) -> None:
    database_path = tmp_path / "correct.db"
    database = Database(database_path)
    connection = database.open()
    _insert_claim(connection)
    connection.execute(
        "INSERT INTO derivations(id,kind,body,status,updated_at) VALUES(?,?,?,?,?)",
        ("observation-old", "observation", "基于旧值", "active", "2026-07-01T00:00:02+00:00"),
    )
    assert EvidenceRepository(connection).add_link(
        {
            "id": "observation-link",
            "derived_type": "observation",
            "derived_id": "observation-old",
            "evidence_type": "claim",
            "evidence_id": "claim-old",
            "relation": "derived_from",
            "weight": 1.0,
        }
    )
    connection.execute(
        "INSERT INTO derivations(id,kind,body,status,updated_at) VALUES(?,?,?,?,?)",
        ("observation-already-stale", "observation", "已失效派生", "stale", "2026-07-01T00:00:03+00:00"),
    )
    assert EvidenceRepository(connection).add_link(
        {
            "id": "observation-stale-link",
            "derived_type": "observation",
            "derived_id": "observation-already-stale",
            "evidence_type": "claim",
            "evidence_id": "claim-old",
            "relation": "derived_from",
            "weight": 1.0,
        }
    )
    old_embedding = ClaimRepository(connection).get_claim("claim-old")["embedding_dense"]
    database.close()

    with TestClient(create_app(_settings(database_path))) as client:
        response = client.post(
            "/v1/memories/claim-old/correct",
            json={"corrected_text": "Python 3.13", "idempotency_key": "correction-1"},
        )
        duplicate = client.post(
            "/v1/memories/claim-old/correct",
            json={"corrected_text": "Python 3.13", "idempotency_key": "correction-1"},
        )

    assert response.status_code == 200
    result = response.json()
    assert result["correction_event_id"]
    assert result["new_claim_id"]
    assert result["created"] is True
    assert duplicate.json() == {**result, "created": False}

    database = Database(database_path)
    connection = database.open()
    old_claim = ClaimRepository(connection).get_claim("claim-old")
    new_claim = ClaimRepository(connection).get_claim(result["new_claim_id"])
    assert old_claim["status"] == "superseded"
    assert old_claim["superseded_by_id"] == result["new_claim_id"]
    assert old_claim["valid_to"] == new_claim["valid_from"]
    assert old_claim["value"] == "Python 3.11"
    assert new_claim["status"] == "active"
    assert new_claim["value"] == "Python 3.13"
    assert {
        key: new_claim[key]
        for key in (
            "namespace_key",
            "subject_entity_id",
            "predicate",
            "qualifiers",
            "canonical_attribute",
            "canonical_slot",
            "topic_tags",
            "scope",
            "importance",
            "confidence",
        )
    } == {
        "namespace_key": "project-a",
        "subject_entity_id": "Alice",
        "predicate": "uses_language",
        "qualifiers": {"project": "hl_mem"},
        "canonical_attribute": "preference.language",
        "canonical_slot": "preference.programming_language",
        "topic_tags": ["python", "backend"],
        "scope": "temporal",
        "importance": 0.8,
        "confidence": 0.72,
    }
    assert new_claim["fact_hash"] == "0788a2c5836ae50f"
    assert new_claim["index_text"] == ("Alice uses_language Python 3.13 preference.programming_language python backend")
    assert new_claim["embedding_dense"] == FakeEmbedder(8).embed_one(new_claim["index_text"])
    assert new_claim["embedding_dense"] != old_embedding
    valid_from = datetime.fromisoformat(new_claim["valid_from"])
    assert datetime.fromisoformat(new_claim["expires_at"]) == valid_from + timedelta(days=14)
    assert new_claim["supersedes_id"] == "claim-old"

    links = connection.execute(
        "SELECT evidence_type,evidence_id,relation FROM evidence_links WHERE derived_id=? ORDER BY relation",
        (result["new_claim_id"],),
    ).fetchall()
    assert [tuple(link) for link in links] == [
        ("event", result["correction_event_id"], "derived_from"),
        ("claim", "claim-old", "supersedes"),
    ]
    event = connection.execute(
        "SELECT event_type,idempotency_key FROM events WHERE id=?",
        (result["correction_event_id"],),
    ).fetchone()
    assert tuple(event) == ("correction", "correction-1")
    assert connection.execute("SELECT count(*) FROM jobs WHERE job_type='extract_event'").fetchone()[0] == 0
    assert connection.execute("SELECT status FROM derivations WHERE id='observation-old'").fetchone()[0] == "stale"
    assert (
        connection.execute("SELECT status FROM derivations WHERE id='observation-already-stale'").fetchone()[0]
        == "stale"
    )
    database.close()


def test_correct_memory_api_generates_idempotency_key_when_omitted(tmp_path: Path) -> None:
    database_path = tmp_path / "correct-generated-key.db"
    database = Database(database_path)
    connection = database.open()
    _insert_claim(connection)
    database.close()

    with TestClient(create_app(_settings(database_path))) as client:
        response = client.post(
            "/v1/memories/claim-old/correct",
            json={"corrected_text": "Python 3.13"},
        )

    assert response.status_code == 200
    database = Database(database_path)
    connection = database.open()
    key = connection.execute("SELECT idempotency_key FROM events WHERE event_type='correction'").fetchone()[0]
    assert UUID(key).version == 4
    database.close()


class _ClassificationChangingEmbedder:
    model = "fake"
    dim = 8

    def __init__(self, connection: sqlite3.Connection, claim_id: str) -> None:
        self.connection = connection
        self.claim_id = claim_id

    def embed_one(self, text: str) -> bytes:
        self.connection.execute("UPDATE claims SET importance=0.95 WHERE id=?", (self.claim_id,))
        self.connection.commit()
        return FakeEmbedder(self.dim).embed_one(text)


def test_correction_rejects_classification_change_during_embedding(tmp_path: Path) -> None:
    database = Database(tmp_path / "correction-race.db")
    connection = database.open()
    _insert_claim(connection, "claim-race")
    service = CorrectionService(
        connection,
        _ClassificationChangingEmbedder(connection, "claim-race"),
        settings=replace(Settings.for_test(), embedding_dim=8),
    )

    with pytest.raises(ConflictError, match="changed during correction"):
        service.correct("claim-race", "Python 3.13", "correction-race")

    claim = ClaimRepository(connection).get_claim("claim-race")
    assert claim["status"] == "active"
    assert claim["importance"] == 0.95
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    database.close()


def test_correction_rolls_back_event_claim_supersede_and_stale_on_evidence_failure(tmp_path: Path) -> None:
    database = Database(tmp_path / "correction-rollback.db")
    connection = database.open()
    _insert_claim(connection, "claim-rollback")
    connection.execute(
        "INSERT INTO derivations(id,kind,body,status,updated_at) VALUES(?,?,?,?,?)",
        ("observation-rollback", "observation", "旧派生", "active", "2026-07-01"),
    )
    assert EvidenceRepository(connection).add_link(
        {
            "id": "observation-rollback-link",
            "derived_type": "observation",
            "derived_id": "observation-rollback",
            "evidence_type": "claim",
            "evidence_id": "claim-rollback",
            "relation": "derived_from",
            "weight": 1.0,
        }
    )
    connection.execute(
        "CREATE TRIGGER fail_correction_evidence BEFORE INSERT ON evidence_links "
        "WHEN NEW.derived_type='claim' AND NEW.relation='supersedes' "
        "BEGIN SELECT RAISE(ABORT, 'forced correction evidence failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced correction evidence failure"):
        CorrectionService(
            connection,
            FakeEmbedder(8),
            settings=replace(Settings.for_test(), embedding_dim=8),
        ).correct("claim-rollback", "Python 3.13", "correction-rollback")

    old_claim = ClaimRepository(connection).get_claim("claim-rollback")
    assert old_claim["status"] == "active"
    assert old_claim["superseded_by_id"] is None
    assert old_claim["valid_to"] is None
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert (
        connection.execute("SELECT status FROM derivations WHERE id='observation-rollback'").fetchone()[0] == "active"
    )
    database.close()


def test_mcp_memory_get_and_correct_share_application_services(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "mcp-correct.db")
    server = McpMemoryServer(settings)
    with server.database.connect() as connection:
        _insert_claim(connection)

    get_schema = next(tool for tool in get_tool_schemas() if tool["name"] == "memory_get")
    correct_schema = next(tool for tool in get_tool_schemas() if tool["name"] == "memory_correct")
    assert get_schema["inputSchema"]["required"] == ["id"]
    assert correct_schema["inputSchema"]["required"] == ["id", "corrected_text"]

    detail = server.call_tool("memory_get", {"id": "claim-old"})
    corrected = server.call_tool(
        "memory_correct",
        {"id": "claim-old", "corrected_text": "Python 3.13"},
    )

    assert detail["id"] == "claim-old"
    assert detail["text"] == "Python 3.11"
    assert corrected["correction_event_id"]
    assert corrected["new_claim_id"]
    assert corrected["created"] is True
    with server.database.connect() as connection:
        key = connection.execute("SELECT idempotency_key FROM events WHERE event_type='correction'").fetchone()[0]
        assert UUID(key).version == 4
    server.database.close()


def test_mcp_memory_correct_rejects_non_string_and_oversized_inputs(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "mcp-correct-validation.db")
    server = McpMemoryServer(settings)
    with server.database.connect() as connection:
        _insert_claim(connection)

    with pytest.raises(ValidationError, match="memory_id must be a string"):
        server.call_tool(
            "memory_correct",
            {"id": 123, "corrected_text": "Python 3.13", "idempotency_key": "invalid-id"},
        )
    with pytest.raises(ValidationError, match="at most 50000"):
        server.call_tool(
            "memory_correct",
            {"id": "claim-old", "corrected_text": "x" * 50001, "idempotency_key": "oversized-text"},
        )

    with server.database.connect() as connection:
        assert ClaimRepository(connection).get_claim("claim-old")["status"] == "active"
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    server.database.close()


def test_mcp_feedback_correction_returns_shared_correction_event_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "mcp-feedback-correct.db")
    server = McpMemoryServer(settings)
    with server.database.connect() as connection:
        _insert_claim(connection, "feedback-claim")
        assert (
            ExperienceRepository(connection, settings=settings).record_exposure_batch(
                [("feedback-1", "query-1", "claim", "feedback-claim", 1, 0.8, "2026-08-01T00:00:00+00:00")]
            )
            == 1
        )

    result = server.call_tool(
        "memory_feedback",
        {
            "feedback_id": "feedback-1",
            "helpful": False,
            "correction": {
                "memory_id": "feedback-claim",
                "action": "replace",
                "corrected_text": "Python 3.13",
            },
        },
    )

    assert result["correction_event_id"] == result["correction"]["correction_event_id"]
    assert result["correction"]["new_claim_id"]
    assert result["correction"]["id"] == "feedback-claim"
    assert result["correction"]["replacement_event_id"] == result["correction_event_id"]
    server.database.close()


def test_rest_feedback_correction_returns_shared_correction_event_id(tmp_path: Path) -> None:
    database_path = tmp_path / "rest-feedback-correct.db"
    settings = _settings(database_path)
    database = Database(database_path)
    connection = database.open()
    _insert_claim(connection, "rest-feedback-claim")
    assert (
        ExperienceRepository(connection, settings=settings).record_exposure_batch(
            [("feedback-rest", "query-rest", "claim", "rest-feedback-claim", 1, 0.7, "2026-08-01T00:00:00+00:00")]
        )
        == 1
    )
    database.close()

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/feedback",
            json={
                "feedback_id": "feedback-rest",
                "helpful": False,
                "correction": {
                    "memory_type": "claim",
                    "memory_id": "rest-feedback-claim",
                    "action": "replace",
                    "corrected_text": "Python 3.13",
                },
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["correction_event_id"] == result["correction"]["correction_event_id"]
    assert result["correction"]["new_claim_id"]
    assert result["correction"]["id"] == "rest-feedback-claim"
    assert result["correction"]["replacement_event_id"] == result["correction_event_id"]


def test_mcp_feedback_retract_preserves_legacy_result_fields(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "mcp-feedback-retract.db")
    server = McpMemoryServer(settings)
    with server.database.connect() as connection:
        _insert_claim(connection, "retract-claim")
        assert (
            ExperienceRepository(connection, settings=settings).record_exposure_batch(
                [("feedback-retract", "query-retract", "claim", "retract-claim", 1, 0.5, "2026-08-01")]
            )
            == 1
        )

    result = server.call_tool(
        "memory_feedback",
        {
            "feedback_id": "feedback-retract",
            "helpful": False,
            "correction": {
                "memory_id": "retract-claim",
                "action": "retract",
                "idempotency_key": "feedback-retract-correction",
            },
        },
    )

    assert result["correction_event_id"] == result["correction"]["correction_event_id"]
    assert result["correction"]["id"] == "retract-claim"
    assert result["correction"]["forgotten"] is True
    server.database.close()


def test_database_open_creates_missing_parent_directories(tmp_path: Path) -> None:
    database_path = tmp_path / "var" / "nested" / "hl_mem.db"

    database = Database(database_path)
    connection = database.open()

    assert database_path.is_file()
    assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] > 0
    database.close()
