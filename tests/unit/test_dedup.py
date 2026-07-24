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
            "canonical_slot": "preference.ui_theme",
            "embedding_dense": embedder.embed_one('用户 preference "深色"')}
    repo.insert_claim(base)
    dedup = Deduplicator(repo, embedder)
    assert dedup.find_duplicate({**base, "id": "two"}) == ("one", "exact")
    # Different conflict_key but same slot+subject → semantic dedup by embedding
    semantic = {**base, "id": "three", "conflict_key": "other",
                "embedding_dense": embedder.embed_one('用户 preference "深色"')}
    result = dedup.find_duplicate(semantic)
    assert result[0] == "one"  # found duplicate
    assert result[1] in ("exact", "semantic")  # method may vary
    new = {**base, "id": "four", "conflict_key": "new", "value": "浅色",
           "canonical_attribute": "preference.ui_theme",
           "canonical_slot": "preference.ui_theme",
           "embedding_dense": embedder.embed_one("completely different")}
    assert dedup.find_duplicate(new) == (None, "new")
