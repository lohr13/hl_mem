from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from hl_mem.application.recall import RecallService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository

NOW = "2026-08-31T12:00:00+00:00"


class RecordingEmbedder:
    model = "fake"

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.texts: list[str] = []
        self.delegate = FakeEmbedder(dim)

    def embed_one(self, text: str) -> bytes:
        self.texts.append(text)
        return cast(bytes, self.delegate.embed_one(text))

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed_one(text) for text in texts]


def _settings(mode: str = "enforce", *, dense: bool = True) -> Settings:
    return replace(
        Settings.for_test(),
        embedding_dim=4,
        entity_constraint_mode=mode,
        recall_dense_enabled=dense,
        recall_candidate_floor=5,
        recall_vector_scan_limit=25,
        reranker_mode="off",
        query_expansion_mode="off",
        relation_expansion_mode="off",
        tag_boost_enabled=False,
        resurrection_mode="off",
        freshness_annotation_mode="off",
        recall_dedup_threshold=0.0,
        echo_suppression_mode="off",
    )


def _claim(
    claim_id: str,
    text: str,
    seed_embedder: FakeEmbedder,
    entity_id: str,
) -> dict[str, object]:
    return {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": entity_id,
        "subject_canonical_entity_id": entity_id,
        "predicate": "state",
        "value": text,
        "index_text": text,
        "canonical_attribute": "state.service",
        "assertion_kind": "observation",
        "scope": "permanent",
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "confidence": 0.95,
        "importance": 0.7,
        "embedding_dense": seed_embedder.embed_one(text),
    }


def _seed_service(
    tmp_path: Path, *, mode: str = "enforce", dense: bool = True
) -> tuple[RecallService, object, RecordingEmbedder]:
    connection = Database(tmp_path / f"entity-recall-{mode}-{dense}.db").open()
    entities = EntityRepository(connection)
    entities.create_entity("agent:pony", "agent", "pony", "Pony Agent", now="2026-01-01T00:00:00+00:00")
    alias = entities.create_alias(
        "Pony",
        "agent",
        "agent:pony",
        "user_explicit",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    entities.create_entity("agent:other", "agent", "other", "Other Agent", now="2026-01-01T00:00:00+00:00")
    repository = ClaimRepository(connection)
    seed_embedder = FakeEmbedder(4)
    for index in range(30):
        repository.insert_claim(
            _claim(
                f"claim:decoy-{index:02d}",
                "Pony deployment status",
                seed_embedder,
                "agent:other",
            )
        )
    repository.insert_claim(_claim("claim:pony-target", "deployment status", seed_embedder, "agent:pony"))
    connection.execute(
        "INSERT INTO events(id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES ('event:pony','default','message','user','{}',?,?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('proof:pony','claim','claim:pony-target','event','event:pony','supports')"
    )
    entities.link_claim(
        "claim:pony-target",
        "agent:pony",
        "subject",
        mention_text="Pony",
        alias_version=int(alias["version"]),
        proof_id="proof:pony",
    )
    connection.commit()
    embedder = RecordingEmbedder()
    return RecallService(connection, embedder, settings=_settings(mode, dense=dense)), connection, embedder


def test_high_confidence_recall_embeds_residual_once_and_returns_scoped_claim(tmp_path: Path) -> None:
    service, connection, embedder = _seed_service(tmp_path)

    result = service.recall("Pony deployment status", limit=5, debug=True, ranking_now=NOW)

    assert embedder.texts == ["deployment status"]
    assert result["results"][0]["id"] == "claim:pony-target"
    trace = result["search_trace"]
    assert trace["entity_filter_mode"] == "enforce"
    assert trace["entity_residual_term_count"] == 2
    assert trace["entity_scope_counts"] == {"fts": 1, "dense": 1}
    assert trace["entity_fallback_reason"] is None
    assert trace["entity_fallback_embedding_calls"] == 0
    connection.close()


def test_dense_off_never_embeds_and_empty_residual_embeds_original_once(tmp_path: Path) -> None:
    dense_off, dense_off_connection, dense_off_embedder = _seed_service(tmp_path / "dense-off", dense=False)
    dense_off.recall("Pony deployment status", limit=5, ranking_now=NOW)
    assert dense_off_embedder.texts == []
    dense_off_connection.close()

    empty, empty_connection, empty_embedder = _seed_service(tmp_path / "empty")
    empty.recall("Pony", limit=5, ranking_now=NOW)
    assert empty_embedder.texts == ["Pony"]
    empty_connection.close()


def test_no_entity_and_off_mode_keep_original_query_with_one_embedding(tmp_path: Path) -> None:
    service, connection, embedder = _seed_service(tmp_path / "no-entity")
    service.recall("ordinary deployment status", limit=5, ranking_now=NOW)
    assert embedder.texts == ["ordinary deployment status"]
    connection.close()

    off, off_connection, off_embedder = _seed_service(tmp_path / "off", mode="off")
    off.recall("Pony deployment status", limit=5, ranking_now=NOW)
    assert off_embedder.texts == ["Pony deployment status"]
    off_connection.close()


def test_scoped_storage_failure_retries_original_wide_query_with_one_fallback_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, connection, embedder = _seed_service(tmp_path)
    original = ClaimRepository.search_claims_fts

    def fail_scoped(self: ClaimRepository, *args: object, **kwargs: object):
        if kwargs.get("entity_id") is not None:
            raise sqlite3.OperationalError("forced scoped read failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ClaimRepository, "search_claims_fts", fail_scoped)

    result = service.recall("Pony deployment status", limit=5, debug=True, ranking_now=NOW)

    assert embedder.texts == ["deployment status", "Pony deployment status"]
    trace = result["search_trace"]
    assert trace["entity_filter_mode"] == "wide"
    assert trace["entity_fallback_reason"] == "storage_error"
    assert trace["entity_fallback_embedding_calls"] == 1
    connection.close()


def test_entity_trace_contains_no_query_or_residual_text(tmp_path: Path) -> None:
    service, connection, _embedder = _seed_service(tmp_path)
    query = "Pony confidential deployment status"

    trace = service.recall(query, limit=5, debug=True, ranking_now=NOW)["search_trace"]
    serialized = json.dumps(trace, ensure_ascii=False)

    assert query not in serialized
    assert "confidential deployment status" not in serialized
    assert "residual_query" not in serialized
    assert "search_query" not in serialized
    connection.close()
