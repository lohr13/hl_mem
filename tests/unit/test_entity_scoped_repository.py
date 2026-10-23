from __future__ import annotations

from pathlib import Path
from typing import Any

from hl_mem.domain.temporal import RecallIntent
from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository

NOW = "2026-07-01T00:00:00+00:00"
VECTOR = pack_vector([1.0, 0.0])


def _repository(tmp_path: Path) -> tuple[ClaimRepository, Any]:
    connection = Database(tmp_path / "entity-scope.db").open()
    entities = EntityRepository(connection)
    for entity_id, entity_type, key in (
        ("agent:target", "agent", "target"),
        ("agent:other", "agent", "other"),
    ):
        entities.create_entity(entity_id, entity_type, key, f"Synthetic {key}", now="2026-01-01T00:00:00+00:00")
    connection.commit()
    return ClaimRepository(connection, vector_batch_size=3), connection


def _claim(
    claim_id: str,
    *,
    namespace: str = "default",
    subject: str | None = None,
    target: str | None = None,
    status: str = "active",
    valid_from: str = "2026-01-01T00:00:00+00:00",
    valid_to: str | None = None,
    recorded_from: str = "2026-01-01T00:00:00+00:00",
    vector: bytes | None = VECTOR,
) -> dict[str, object]:
    return {
        "id": claim_id,
        "namespace_key": namespace,
        "subject_entity_id": subject or "unscoped",
        "subject_canonical_entity_id": subject,
        "canonical_target_entity_id": target,
        "predicate": "state",
        "value": "deployment status",
        "index_text": "deployment status",
        "status": status,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "recorded_from": recorded_from,
        "embedding_dense": vector,
    }


def _link_only_claim(repository: ClaimRepository, connection: Any, claim_id: str, *, duplicate: bool = False) -> None:
    entities = EntityRepository(connection)
    alias = entities.create_alias(
        "Target",
        "agent",
        "agent:target",
        "user_explicit",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    repository.insert_claim(_claim(claim_id))
    event_id = f"event:{claim_id}"
    connection.execute(
        "INSERT INTO events(id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES (?, 'default','message','user','{}',?,?)",
        (event_id, NOW, NOW),
    )
    proof_id = f"proof:{claim_id}"
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES (?,'claim',?,'event',?,'supports')",
        (proof_id, claim_id, event_id),
    )
    for role in (("subject", "actor") if duplicate else ("subject",)):
        entities.link_claim(
            claim_id,
            "agent:target",
            role,
            mention_text="Target",
            alias_version=int(alias["version"]),
            proof_id=proof_id,
        )
    connection.commit()


def test_entity_scope_finds_claim_beyond_wide_fts_and_dense_limits(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    for index in range(8):
        repository.insert_claim(_claim(f"claim:a-decoy-{index}", subject="agent:other"))
    repository.insert_claim(_claim("claim:z-target", subject="agent:target"))

    assert "claim:z-target" not in [row["id"] for row in repository.search_claims_fts("deployment", 5, NOW)]
    assert "claim:z-target" not in [row["id"] for row in repository.search_claims_vector(VECTOR, 5, NOW)]
    assert [row["id"] for row in repository.search_claims_fts("deployment", 5, NOW, entity_id="agent:target")] == [
        "claim:z-target"
    ]
    assert [row["id"] for row in repository.search_claims_vector(VECTOR, 5, NOW, entity_id="agent:target")] == [
        "claim:z-target"
    ]
    connection.close()


def test_entity_scope_accepts_subject_target_and_link_without_duplicate_rows(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.insert_claim(_claim("claim:subject", subject="agent:target"))
    repository.insert_claim(_claim("claim:target", target="agent:target"))
    _link_only_claim(repository, connection, "claim:link")
    _link_only_claim(repository, connection, "claim:duplicate-link", duplicate=True)

    expected = {"claim:subject", "claim:target", "claim:link", "claim:duplicate-link"}
    fts = repository.search_claims_fts("deployment", 10, NOW, entity_id="agent:target")
    dense = repository.search_claims_vector(VECTOR, 10, NOW, entity_id="agent:target")

    assert {str(row["id"]) for row in fts} == expected
    assert {str(row["id"]) for row in dense} == expected
    assert len(fts) == len(expected)
    connection.close()


def test_entity_scope_preserves_namespace_status_valid_and_recorded_time(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    EntityRepository(connection).create_entity(
        "agent:target",
        "agent",
        "target",
        "Other Namespace Target",
        namespace_key="other",
        now="2026-01-01T00:00:00+00:00",
    )
    repository.insert_claim(_claim("claim:visible", subject="agent:target"))
    repository.insert_claim(_claim("claim:expired", subject="agent:target", valid_to="2026-02-01T00:00:00+00:00"))
    repository.insert_claim(_claim("claim:future", subject="agent:target", valid_from="2026-12-01T00:00:00+00:00"))
    repository.insert_claim(_claim("claim:candidate", subject="agent:target", status="candidate"))
    repository.insert_claim(
        _claim("claim:recorded-late", subject="agent:target", recorded_from="2026-06-15T00:00:00+00:00")
    )
    repository.insert_claim(_claim("claim:other-namespace", namespace="other", subject="agent:target"))

    expected = ["claim:visible"]
    fts = repository.search_claims_fts(
        "deployment",
        10,
        NOW,
        RecallIntent.CURRENT_STATE,
        "2026-06-01T00:00:00+00:00",
        entity_id="agent:target",
    )
    dense = repository.search_claims_vector(
        VECTOR,
        10,
        NOW,
        RecallIntent.CURRENT_STATE,
        "2026-06-01T00:00:00+00:00",
        entity_id="agent:target",
    )

    assert [row["id"] for row in fts] == expected
    assert [row["id"] for row in dense] == expected
    connection.close()


def test_scoped_vector_scan_bypasses_backend_but_wide_search_still_delegates(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.insert_claim(_claim("claim:target", subject="agent:target"))

    class RecordingBackend:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            self.calls += 1
            return [{"id": "backend-result"}]

    backend = RecordingBackend()
    repository.vector_backend = backend  # type: ignore[assignment]

    assert repository.search_claims_vector(VECTOR, 5, NOW) == [{"id": "backend-result"}]
    assert [row["id"] for row in repository.search_claims_vector(VECTOR, 5, NOW, entity_id="agent:target")] == [
        "claim:target"
    ]
    assert backend.calls == 1
    assert repository.search_claims_vector(VECTOR, 0, NOW, entity_id="agent:target") == []
    connection.close()


def test_entity_without_vectors_has_no_dense_candidates(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.insert_claim(_claim("claim:no-vector", subject="agent:target", vector=None))

    assert repository.search_claims_vector(VECTOR, 5, NOW, entity_id="agent:target") == []
    connection.close()
