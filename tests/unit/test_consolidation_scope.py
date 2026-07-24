"""ConsolidationScope 定向归并测试。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.domain.consolidation_scope import ConsolidationScope
from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.consolidate import ConflictConsolidator, ConsolidationDecision


class _Judge:
    """返回固定兼容结论的测试判定器。"""

    def judge(self, _left: dict, _right: dict) -> ConsolidationDecision:
        """返回固定判定。"""
        return ConsolidationDecision("compatible", 1.0, "test")


def _claim(
    connection,
    claim_id: str,
    vector: list[float],
    *,
    slot: str,
    tags: list[str],
) -> None:
    row = {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "user",
        "canonical_attribute": slot,
        "canonical_slot": slot,
        "topic_tags_json": json.dumps(tags),
        "predicate": "fact",
        "value_json": json.dumps(claim_id),
        "status": "active",
        "scope": "permanent",
        "valid_from": "2026-01-01T00:00:00Z",
        "recorded_from": "2026-01-01T00:00:00Z",
        "embedding_dense": pack_vector(vector),
        "embedding_model": "fake-v1",
    }
    assert ClaimRepository(connection).insert_claim(row)


def _pair_ids(pairs) -> set[frozenset[str]]:
    return {frozenset((pair.left["id"], pair.right["id"])) for pair in pairs}


def test_scope_filters_by_slot(tmp_path) -> None:
    connection = Database(tmp_path / "slot.db").open()
    _claim(connection, "a", [1.0, 0.0], slot="choice.database", tags=["database"])
    _claim(connection, "b", [0.8, 0.6], slot="choice.database", tags=["database"])
    _claim(connection, "c", [0.8, 0.6], slot="choice.editor", tags=["tooling"])

    pairs = ConflictConsolidator(connection, _Judge()).scan_candidates(
        scope=ConsolidationScope(slot_filter="choice.database")
    )

    assert _pair_ids(pairs) == {frozenset(("a", "b"))}


def test_scope_filters_by_tags(tmp_path) -> None:
    connection = Database(tmp_path / "tags.db").open()
    _claim(connection, "a", [1.0, 0.0], slot="fact.other", tags=["database", "python"])
    _claim(connection, "b", [0.8, 0.6], slot="fact.other", tags=["database", "python"])
    _claim(connection, "c", [0.8, 0.6], slot="fact.other", tags=["tooling"])

    pairs = ConflictConsolidator(connection, _Judge()).scan_candidates(
        scope=ConsolidationScope(tag_filter=["python"])
    )

    assert _pair_ids(pairs) == {frozenset(("a", "b"))}


def test_scope_limits_max_pairs(tmp_path) -> None:
    connection = Database(tmp_path / "limit.db").open()
    _claim(connection, "a", [1.0, 0.0, 0.0], slot="fact.other", tags=["python"])
    _claim(connection, "b", [0.8, 0.6, 0.0], slot="fact.other", tags=["python"])
    _claim(connection, "c", [0.8, 0.2666667, 0.5374838], slot="fact.other", tags=["python"])

    pairs = ConflictConsolidator(connection, _Judge()).scan_candidates(scope=ConsolidationScope(max_pairs=2))

    assert len(pairs) == 2


def test_default_scope_matches_all(tmp_path) -> None:
    connection = Database(tmp_path / "default.db").open()
    _claim(connection, "a", [1.0, 0.0], slot="fact.other", tags=["python"])
    _claim(connection, "b", [0.8, 0.6], slot="fact.other", tags=["tooling"])

    pairs = ConflictConsolidator(connection, _Judge()).scan_candidates(scope=ConsolidationScope())

    assert _pair_ids(pairs) == {frozenset(("a", "b"))}


def test_consolidate_api_stores_scope_payload(tmp_path) -> None:
    database_path = tmp_path / "api.db"
    with TestClient(create_app(database_path)) as client:
        response = client.post(
            "/v1/consolidate",
            json={"namespace": "project", "slot_filter": "choice.database", "tag_filter": ["database"], "max_pairs": 12},
        )
        assert response.status_code == 200
        job_id = response.json()["id"]
    database = Database(database_path)
    connection = database.open()
    job = dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    assert job["job_type"] == "consolidate_conflicts"
    assert json.loads(job["payload_json"]) == {
        "namespace": "project",
        "slot_filter": "choice.database",
        "tag_filter": ["database"],
        "max_pairs": 12,
        "similarity_threshold": 0.72,
        "similarity_ceiling": 0.95,
    }
