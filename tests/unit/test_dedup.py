import sqlite3

from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.domain.claims.dedup import Deduplicator
from hl_mem.storage.database import Database
from hl_mem.storage.claims import ClaimRepository


def test_exact_semantic_and_new_dedup(tmp_path) -> None:
    connection = Database(tmp_path / "dedup.db").open()
    repo, embedder = ClaimRepository(connection), FakeEmbedder(8)
    base = {"id": "one", "namespace_key": "default", "subject_entity_id": "用户",
            "predicate": "preference", "value": "深色", "conflict_key": "key",
            "recorded_from": "2026-01-01", "status": "active",
            "canonical_attribute": "preference.ui_theme",
            "embedding_dense": embedder.embed_one('用户 preference "深色"')}
    repo.insert_claim(base)
    dedup = Deduplicator(repo, embedder)
    assert dedup.find_duplicate({**base, "id": "two"}) == ("one", "exact")
    semantic = {**base, "id": "three", "conflict_key": "other"}
    assert dedup.find_duplicate(semantic) == ("one", "semantic")
    new = {**base, "id": "four", "conflict_key": "new", "value": "浅色",
           "canonical_attribute": "preference.ui_theme",
           "embedding_dense": embedder.embed_one("completely different")}
    assert dedup.find_duplicate(new) == (None, "new")
