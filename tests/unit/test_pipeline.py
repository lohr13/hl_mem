from hl_mem.api.pipeline import store_extracted
from hl_mem.ingest.embeddings import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.database import Database


def test_fact_hash_exact_duplicate_merges_evidence(tmp_path) -> None:
    database = Database(tmp_path / "fact-hash.db")
    connection = database.open()
    claim = ExtractedClaim("使用", "PostgreSQL", 0.9, "stable", "用户", {})
    base_event = {
        "tenant_id": "default", "actor_type": "user",
        "occurred_at": "2026-07-21T10:00:00+00:00",
    }
    first_id = store_extracted(
        connection, claim, {**base_event, "id": "event-1"},
        "2026-07-21T10:01:00+00:00", FakeEmbedder(8),
    )
    second_id = store_extracted(
        connection, claim, {**base_event, "id": "event-2"},
        "2026-07-21T10:02:00+00:00", FakeEmbedder(8),
    )
    assert second_id == first_id
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM evidence_links WHERE derived_id=?", (first_id,)
    ).fetchone()[0] == 2
    database.close()
