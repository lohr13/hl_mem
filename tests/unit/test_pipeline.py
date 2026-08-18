import math

from hl_mem.application.ingest import IngestService
from hl_mem.core.vector import pack_vector
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.database import Database


def store_extracted(conn, claim, event, now, embedder, **kw):
    return IngestService.store_extracted(conn, claim, event, now, embedder, **kw)


class _PairSimilarityEmbedder(FakeEmbedder):
    def __init__(self, similarity: float) -> None:
        super().__init__(2)
        self._vectors = iter(
            (
                pack_vector([1.0, 0.0]),
                pack_vector([similarity, math.sqrt(1.0 - similarity * similarity)]),
            )
        )

    def embed_one(self, _text: str) -> bytes:
        return next(self._vectors)


def test_fact_hash_exact_duplicate_merges_evidence(tmp_path) -> None:
    database = Database(tmp_path / "fact-hash.db")
    connection = database.open()
    claim = ExtractedClaim("使用", "PostgreSQL", 0.9, "stable", "用户", {})
    base_event = {
        "tenant_id": "default",
        "actor_type": "user",
        "occurred_at": "2026-07-21T10:00:00+00:00",
    }
    first_id = store_extracted(
        connection,
        claim,
        {**base_event, "id": "event-1"},
        "2026-07-21T10:01:00+00:00",
        FakeEmbedder(8),
    ).claim_id
    second_id = store_extracted(
        connection,
        claim,
        {**base_event, "id": "event-2"},
        "2026-07-21T10:02:00+00:00",
        FakeEmbedder(8),
    ).claim_id
    assert second_id == first_id
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM evidence_links WHERE derived_id=?", (first_id,)).fetchone()[0] == 2
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
        ExtractedClaim(
            "使用",
            "PostgreSQL",
            canonical_attribute="choice.database",
            canonical_slot="choice.database",
            qualifiers={"project": "hl_mem"},
        ),
        {"id": "event-v2", "actor_type": "user", "tenant_id": "default"},
        "2026-07-21T10:01:00+00:00",
        FakeEmbedder(8),
    ).claim_id

    row = connection.execute(
        "SELECT canonical_attribute,conflict_key_version,conflict_key,legacy_conflict_key " "FROM claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    assert row["canonical_attribute"] == "choice.database"
    assert row["conflict_key_version"] == 3
    assert row["conflict_key"]
    assert row["legacy_conflict_key"]
    assert row["conflict_key"] != row["legacy_conflict_key"]
    database.close()


def test_store_extracted_persists_explicit_assertion_kind(tmp_path) -> None:
    database = Database(tmp_path / "assertion-kind.db")
    connection = database.open()
    claim_id = store_extracted(
        connection,
        ExtractedClaim(
            "state",
            "Tailscale is online",
            subject="host-a",
            canonical_attribute="state.service_health",
            assertion_kind="observation",
        ),
        {"id": "event-observation", "actor_type": "user", "tenant_id": "default"},
        "2026-08-18T01:00:00+00:00",
        FakeEmbedder(8),
    ).claim_id

    row = connection.execute("SELECT assertion_kind,status,valid_to FROM claims WHERE id=?", (claim_id,)).fetchone()
    assert tuple(row) == ("observation", "active", None)
    database.close()


def test_semantic_candidate_at_pair_similarity_floor_is_queued_for_async_judgment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hl_mem.application.ingest.cosine_similarity", lambda *_args: 0.88)
    database = Database(tmp_path / "semantic-candidate.db")
    connection = database.open()
    embedder = _PairSimilarityEmbedder(0.88)
    event = {
        "tenant_id": "default",
        "actor_type": "user",
        "occurred_at": "2026-07-21T10:00:00+00:00",
    }
    first = store_extracted(
        connection,
        ExtractedClaim(
            "事实",
            "supports offline recall",
            subject="hl_mem",
            canonical_attribute="fact.capability",
            canonical_slot="fact.capability",
        ),
        {**event, "id": "event-candidate-1"},
        "2026-07-21T10:01:00+00:00",
        embedder,
    )
    second = store_extracted(
        connection,
        ExtractedClaim(
            "事实",
            "can recall memories without a server",
            subject="hl_mem",
            canonical_attribute="fact.capability",
            canonical_slot="fact.capability",
        ),
        {**event, "id": "event-candidate-2"},
        "2026-07-21T10:02:00+00:00",
        embedder,
    )

    pair = connection.execute(
        "SELECT left_claim_id,right_claim_id,decision,policy_version,similarity FROM dedup_pairs"
    ).fetchone()

    assert second.reason == "inserted"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
    assert pair["left_claim_id"] == first.claim_id
    assert pair["right_claim_id"] == second.claim_id
    assert pair["decision"] is None
    assert pair["policy_version"] == "v2"
    assert pair["similarity"] == 0.88
    database.close()


def test_semantic_candidate_below_pair_similarity_floor_is_not_recorded(tmp_path) -> None:
    database = Database(tmp_path / "low-similarity-semantic-candidate.db")
    connection = database.open()
    embedder = _PairSimilarityEmbedder(0.87)
    event = {
        "tenant_id": "default",
        "actor_type": "user",
        "occurred_at": "2026-07-21T10:00:00+00:00",
    }
    store_extracted(
        connection,
        ExtractedClaim(
            "事实",
            "supports offline recall",
            subject="hl_mem",
            canonical_attribute="fact.capability",
            canonical_slot="fact.capability",
        ),
        {**event, "id": "event-low-similarity-1"},
        "2026-07-21T10:01:00+00:00",
        embedder,
    )
    second = store_extracted(
        connection,
        ExtractedClaim(
            "事实",
            "can recall memories without a server",
            subject="hl_mem",
            canonical_attribute="fact.capability",
            canonical_slot="fact.capability",
        ),
        {**event, "id": "event-low-similarity-2"},
        "2026-07-21T10:02:00+00:00",
        embedder,
    )

    assert second.reason == "inserted"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM dedup_pairs").fetchone()[0] == 0
    database.close()


def test_safe_near_copy_reuses_claim_and_merges_evidence_before_insert(tmp_path) -> None:
    class ConstantEmbedder(FakeEmbedder):
        def embed_one(self, _text: str) -> bytes:
            return super().embed_one("constant-vector")

    database = Database(tmp_path / "near-copy-ingest.db")
    connection = database.open()
    embedder = ConstantEmbedder(8)
    event = {
        "tenant_id": "default",
        "actor_type": "user",
        "occurred_at": "2026-07-21T10:00:00+00:00",
    }
    first = store_extracted(
        connection,
        ExtractedClaim(
            "事实",
            "User's tank is 20 gallons",
            subject="user",
            canonical_attribute="fact.other",
        ),
        {**event, "id": "event-near-copy-1"},
        "2026-07-21T10:01:00+00:00",
        embedder,
    )
    second = store_extracted(
        connection,
        ExtractedClaim(
            "事实",
            "The user's tank size is 20 gallons.",
            subject="user",
            canonical_attribute="fact.other",
        ),
        {**event, "id": "event-near-copy-2"},
        "2026-07-21T10:02:00+00:00",
        embedder,
    )

    assert second.claim_id == first.claim_id
    assert second.reason == "semantic_duplicate"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    assert (
        connection.execute("SELECT count(*) FROM evidence_links WHERE derived_id=?", (first.claim_id,)).fetchone()[0]
        == 2
    )
    assert connection.execute("SELECT count(*) FROM dedup_pairs").fetchone()[0] == 0
    database.close()


def test_fact_hash_match_does_not_merge_different_task_qualifiers(tmp_path) -> None:
    database = Database(tmp_path / "fact-hash-qualifier-guard.db")
    connection = database.open()
    embedder = FakeEmbedder(8)
    event = {"tenant_id": "default", "actor_type": "user"}
    first = store_extracted(
        connection,
        ExtractedClaim(
            "使用",
            "qwen3.7-plus",
            subject="user",
            canonical_attribute="choice.model",
            canonical_slot="choice.model",
            qualifiers={"task": "general_chat"},
        ),
        {**event, "id": "event-task-1"},
        "2026-07-21T10:01:00+00:00",
        embedder,
    )
    second = store_extracted(
        connection,
        ExtractedClaim(
            "使用",
            "qwen3.7-plus",
            subject="user",
            canonical_attribute="choice.model",
            canonical_slot="choice.model",
            qualifiers={"task": "data_cleaning"},
        ),
        {**event, "id": "event-task-2"},
        "2026-07-21T10:02:00+00:00",
        embedder,
    )

    assert second.claim_id != first.claim_id
    assert second.reason == "inserted"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 2
    database.close()
