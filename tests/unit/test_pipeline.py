import hl_mem.api.pipeline as pipeline_module
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


def test_store_extracted_does_not_build_observation(tmp_path) -> None:
    database = Database(tmp_path / "no-observation.db")
    connection = database.open()

    store_extracted(
        connection,
        ExtractedClaim("使用", "PostgreSQL"),
        {"id": "event-1", "actor_type": "user"},
        "2026-07-21T10:01:00+00:00",
        FakeEmbedder(8),
    )

    assert connection.execute("SELECT count(*) FROM derivations").fetchone()[0] == 0
    database.close()


def test_store_extracted_writes_canonical_attribute_and_v2_keys(tmp_path) -> None:
    database = Database(tmp_path / "v2-write.db")
    connection = database.open()
    claim_id = store_extracted(
        connection,
        ExtractedClaim("使用", "PostgreSQL", canonical_attribute="choice.database"),
        {"id": "event-v2", "actor_type": "user", "tenant_id": "default"},
        "2026-07-21T10:01:00+00:00",
        FakeEmbedder(8),
    )

    row = connection.execute(
        "SELECT canonical_attribute,conflict_key_version,conflict_key,legacy_conflict_key "
        "FROM claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    assert row["canonical_attribute"] == "choice.database"
    assert row["conflict_key_version"] == 2
    assert row["conflict_key"]
    assert row["legacy_conflict_key"]
    assert row["conflict_key"] != row["legacy_conflict_key"]
    database.close()
