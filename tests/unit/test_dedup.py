from hl_mem.domain.claims.dedup import (
    Deduplicator,
    compute_dedup_pair_key,
    is_safe_near_duplicate,
)
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.deduplicate import _apply_equivalent_pair, deduplicate_claims


def _base_claim(embedder: FakeEmbedder) -> dict:
    vector = embedder.embed_one("same semantic surface")
    return {
        "id": "one",
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "preference",
        "value": "dark theme",
        "qualifiers": {"task": "general_chat"},
        "conflict_key": "key",
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "status": "active",
        "canonical_attribute": "preference.workflow",
        "canonical_slot": "preference.workflow",
        "embedding_dense": vector,
    }


def test_exact_semantic_candidate_and_new_dedup(tmp_path) -> None:
    connection = Database(tmp_path / "dedup.db").open()
    repo, embedder = ClaimRepository(connection), FakeEmbedder(8)
    base = _base_claim(embedder)
    repo.insert_claim(base)
    dedup = Deduplicator(repo, embedder)

    assert dedup.find_duplicate({**base, "id": "two"}) == ("one", "exact")

    semantic = {
        **base,
        "id": "three",
        "value": "prefers a dark interface",
        "conflict_key": "other",
    }
    assert dedup.find_duplicate(semantic) == ("one", "semantic_candidate")

    new = {
        **base,
        "id": "four",
        "value": "light theme",
        "conflict_key": "new",
        "embedding_dense": embedder.embed_one("completely different"),
    }
    assert dedup.find_duplicate(new) == (None, "new")
    connection.close()


def test_deterministic_check_rejects_subject_slot_predicate_and_qualifier_mismatches() -> None:
    base = {
        "subject_entity_id": "hl_mem",
        "predicate": "uses",
        "value": "same",
        "canonical_attribute": "choice.model",
        "canonical_slot": "choice.model",
        "qualifiers": {"task": "general_chat"},
    }

    assert Deduplicator._deterministic_check(base, {**base, "subject_entity_id": "hl_agent"}) == "distinct"
    assert Deduplicator._deterministic_check(base, {**base, "namespace_key": "other"}) == "distinct"
    assert Deduplicator._deterministic_check(base, {**base, "canonical_slot": "choice.tool"}) == "distinct"
    assert (
        Deduplicator._deterministic_check(
            {**base, "canonical_slot": None},
            {**base, "canonical_slot": None, "predicate": "prefers"},
        )
        == "distinct"
    )
    assert (
        Deduplicator._deterministic_check(
            base,
            {**base, "qualifiers": {"task": "data_cleaning"}},
        )
        == "distinct"
    )
    assert Deduplicator._deterministic_check(base, dict(base)) == "equivalent"


def test_safe_near_duplicate_requires_structure_lexical_similarity_and_protected_atoms() -> None:
    base = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "事实",
        "value": "User's tank is 20 gallons on Monday",
        "canonical_attribute": "fact.other",
        "canonical_slot": None,
        "qualifiers": {},
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
    }
    near_copy = {
        **base,
        "value": "The user's tank size is 20 gallons on Monday.",
    }

    assert is_safe_near_duplicate(
        base,
        near_copy,
        similarity=0.97,
        semantic_threshold=0.92,
    )
    assert not is_safe_near_duplicate(
        base,
        {**near_copy, "value": "The user's tank size is 30 gallons on Monday."},
        similarity=0.99,
        semantic_threshold=0.92,
    )
    assert not is_safe_near_duplicate(
        base,
        {**near_copy, "qualifiers": {"tank": "office"}},
        similarity=0.99,
        semantic_threshold=0.92,
    )
    assert not is_safe_near_duplicate(
        base,
        {**near_copy, "subject_entity_id": "assistant"},
        similarity=0.99,
        semantic_threshold=0.92,
    )
    assert is_safe_near_duplicate(
        base,
        {**near_copy, "subject_entity_id": "user's tank"},
        similarity=0.99,
        semantic_threshold=0.92,
        allow_subject_mismatch=True,
    )
    assert not is_safe_near_duplicate(
        base,
        {**near_copy, "status": "disputed"},
        similarity=0.99,
        semantic_threshold=0.92,
    )
    assert not is_safe_near_duplicate(
        {**base, "valid_to": "2026-02-01T00:00:00+00:00"},
        {
            **near_copy,
            "valid_from": "2026-02-01T00:00:00+00:00",
            "valid_to": None,
        },
        similarity=0.99,
        semantic_threshold=0.92,
    )


def test_safe_near_duplicate_preserves_distinct_named_entities() -> None:
    common = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "参加",
        "canonical_attribute": "event.attendance",
        "canonical_slot": None,
        "qualifiers": {},
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
    }

    assert not is_safe_near_duplicate(
        {**common, "value": "User attended Emily's wedding"},
        {**common, "value": "User attended Emma's wedding"},
        similarity=0.99,
        semantic_threshold=0.92,
    )


def test_apply_equivalent_pair_rechecks_qualifiers(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-qualifier-guard.db").open()
    repo = ClaimRepository(connection)
    recorded_from = "2026-01-01T00:00:00+00:00"
    common = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "fact",
        "value": "same",
        "recorded_from": recorded_from,
        "status": "active",
        "canonical_attribute": "fact.capability",
        "canonical_slot": None,
    }
    repo.insert_claim({**common, "id": "left", "qualifiers": {"task": "general_chat"}})
    repo.insert_claim({**common, "id": "right", "qualifiers": {"task": "data_cleaning"}})
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,policy_version,decision,"
        "judge_confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "pair",
            compute_dedup_pair_key("left", "right"),
            "left",
            "right",
            0.99,
            "v2",
            "equivalent",
            0.99,
            recorded_from,
        ),
    )
    connection.commit()

    left = repo.get_claim("left")
    right = repo.get_claim("right")
    assert left is not None and right is not None
    assert not _apply_equivalent_pair(connection, "pair", left, right, recorded_from)
    assert repo.get_claim("right")["status"] == "active"
    connection.close()


def test_worker_rechecks_old_pending_pair_with_v2_policy_without_calling_llm(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-policy-v2.db").open()
    repo, embedder = ClaimRepository(connection), FakeEmbedder(8)
    vector = embedder.embed_one("same")
    common = {
        "namespace_key": "default",
        "predicate": "fact",
        "value": "same",
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "status": "active",
        "canonical_slot": None,
        "embedding_dense": vector,
    }
    repo.insert_claim({**common, "id": "left", "subject_entity_id": "hl_mem"})
    repo.insert_claim({**common, "id": "right", "subject_entity_id": "hl_agent"})
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,policy_version,predicate,created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            "old-pending",
            compute_dedup_pair_key("left", "right"),
            "left",
            "right",
            0.99,
            "v1",
            "fact",
            common["recorded_from"],
        ),
    )
    connection.commit()

    class NoCallClient:
        model = "must-not-run"

        def complete(self, _request):
            raise AssertionError("deterministic distinct pair must not call the LLM")

    result = deduplicate_claims(connection, NoCallClient(), embedder, audit_only=True)
    row = connection.execute(
        "SELECT policy_version,decision,judge_reason,judge_model " "FROM dedup_pairs WHERE id='old-pending'"
    ).fetchone()

    assert result["distinct"] == 1
    assert tuple(row) == ("v2", "distinct", "deterministic_safety_gate", None)
    connection.close()


def test_apply_equivalent_pair_rejects_v1_policy_decision(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-v1-apply-guard.db").open()
    repo = ClaimRepository(connection)
    recorded_from = "2026-01-01T00:00:00+00:00"
    common = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "fact",
        "value": "same",
        "qualifiers": {},
        "recorded_from": recorded_from,
        "status": "active",
        "canonical_attribute": "fact.capability",
        "canonical_slot": None,
    }
    repo.insert_claim({**common, "id": "left"})
    repo.insert_claim({**common, "id": "right"})
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,policy_version,decision,"
        "judge_confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "v1-pair",
            compute_dedup_pair_key("left", "right"),
            "left",
            "right",
            0.99,
            "v1",
            "equivalent",
            0.99,
            recorded_from,
        ),
    )
    connection.commit()

    left = repo.get_claim("left")
    right = repo.get_claim("right")
    assert left is not None and right is not None
    assert not _apply_equivalent_pair(connection, "v1-pair", left, right, recorded_from)
    assert repo.get_claim("right")["status"] == "active"
    connection.close()
