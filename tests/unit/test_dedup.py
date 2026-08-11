from hl_mem.domain.claims.dedup import (
    Deduplicator,
    compute_dedup_pair_key,
    is_safe_near_duplicate,
)
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.deduplicate import (
    _apply_equivalent_pair,
    deduplicate_claims,
    review_pending_near_duplicates,
)


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


def test_safe_near_duplicate_preserves_atom_order_negation_and_cjk_time() -> None:
    common = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "fact",
        "canonical_attribute": "fact.other",
        "canonical_slot": None,
        "qualifiers": {},
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
    }
    distinct_pairs = [
        (
            "Take 1 tablet at 2 pm every weekday after breakfast and record the result in the health journal.",
            "Take 2 tablets at 1 pm every weekday after breakfast and record the result in the health journal.",
        ),
        (
            "Alice reports to Bob regarding this project and sends the same detailed weekly status update every Friday.",
            "Bob reports to Alice regarding this project and sends the same detailed weekly status update every Friday.",
        ),
        (
            "The policy does allow this operation under normal conditions for all standard production deployments.",
            "The policy doesn't allow this operation under normal conditions for all standard production deployments.",
        ),
        (
            "用户周一不服用抗菌药，并在早餐后把结果详细记录到健康日志中。",
            "用户周二服用抗菌药，并在早餐后把结果详细记录到健康日志中。",
        ),
    ]

    for left_value, right_value in distinct_pairs:
        assert not is_safe_near_duplicate(
            {**common, "value": left_value},
            {**common, "value": right_value},
            similarity=0.99,
            semantic_threshold=0.92,
        )


def test_cross_subject_near_duplicate_requires_verified_user_projection() -> None:
    common = {
        "namespace_key": "default",
        "predicate": "fact",
        "value": "Prefers decaf coffee for the regular morning order",
        "canonical_attribute": "preference.food",
        "canonical_slot": None,
        "qualifiers": {},
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
    }

    assert not is_safe_near_duplicate(
        {**common, "subject_entity_id": "Alice", "entities": ["Alice"]},
        {**common, "subject_entity_id": "Bob", "entities": ["Bob"]},
        similarity=0.99,
        semantic_threshold=0.92,
        allow_subject_mismatch=True,
    )
    assert not is_safe_near_duplicate(
        {**common, "subject_entity_id": "user", "entities": ["user"]},
        {**common, "subject_entity_id": "user's friend", "entities": ["user's friend"]},
        similarity=0.99,
        semantic_threshold=0.92,
        allow_subject_mismatch=True,
    )


def test_review_pending_near_duplicates_marks_only_safe_pair_equivalent(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-maintenance.db").open()
    repo = ClaimRepository(connection)
    recorded_from = "2026-01-01T00:00:00+00:00"
    common = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "fact",
        "qualifiers": {},
        "recorded_from": recorded_from,
        "valid_from": recorded_from,
        "status": "active",
        "canonical_attribute": "fact.other",
        "canonical_slot": None,
    }
    repo.insert_claim({**common, "id": "left", "value": "User's tank is 20 gallons"})
    repo.insert_claim({**common, "id": "near", "value": "The user's tank size is 20 gallons."})
    repo.insert_claim({**common, "id": "different", "value": "The user's tank size is 30 gallons."})
    connection.executemany(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,policy_version,created_at"
        ") VALUES (?,?,?,?,?,?,?)",
        [
            (
                "safe-pair",
                compute_dedup_pair_key("left", "near"),
                "left",
                "near",
                0.99,
                "v2",
                recorded_from,
            ),
            (
                "protected-atom-pair",
                compute_dedup_pair_key("left", "different"),
                "left",
                "different",
                0.995,
                "v2",
                recorded_from,
            ),
        ],
    )
    connection.commit()

    result = review_pending_near_duplicates(
        connection,
        threshold=0.92,
        limit=2,
        reviewed_at="2026-08-12T00:00:00+00:00",
    )
    rows = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,decision,judge_confidence,judge_reason,judge_model,reviewed_at " "FROM dedup_pairs ORDER BY id"
        ).fetchall()
    }

    assert result == {"scanned": 2, "equivalent": 1, "deferred": 1, "missing": 0}
    assert rows["safe-pair"]["decision"] == "equivalent"
    assert rows["safe-pair"]["judge_confidence"] == 0.99
    assert rows["safe-pair"]["judge_reason"] == "deterministic_near_copy_v1"
    assert rows["safe-pair"]["judge_model"] is None
    assert rows["safe-pair"]["reviewed_at"] == "2026-08-12T00:00:00+00:00"
    assert rows["protected-atom-pair"]["decision"] is None
    assert rows["protected-atom-pair"]["reviewed_at"] == "2026-08-12T00:00:00+00:00"
    assert repo.get_claim("left")["status"] == "active"
    assert repo.get_claim("near")["status"] == "active"
    assert not _apply_equivalent_pair(
        connection,
        "safe-pair",
        repo.get_claim("left"),
        repo.get_claim("near"),
        applied_at="2026-08-12T00:01:00+00:00",
        min_confidence=0.95,
    )
    connection.close()


def test_review_pending_near_duplicates_rotates_deferred_pairs(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-maintenance-rotation.db").open()
    repo = ClaimRepository(connection)
    recorded_from = "2026-01-01T00:00:00+00:00"
    common = {
        "namespace_key": "default",
        "subject_entity_id": "user",
        "predicate": "fact",
        "qualifiers": {},
        "recorded_from": recorded_from,
        "valid_from": recorded_from,
        "status": "active",
        "canonical_attribute": "fact.other",
        "canonical_slot": None,
    }
    repo.insert_claim({**common, "id": "base", "value": "The tank size is 10 gallons"})
    repo.insert_claim({**common, "id": "twenty", "value": "The tank size is 20 gallons"})
    repo.insert_claim({**common, "id": "thirty", "value": "The tank size is 30 gallons"})
    connection.executemany(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,policy_version,created_at"
        ") VALUES (?,?,?,?,?,?,?)",
        [
            (
                "higher-similarity",
                compute_dedup_pair_key("base", "twenty"),
                "base",
                "twenty",
                0.999,
                "v2",
                recorded_from,
            ),
            (
                "lower-similarity",
                compute_dedup_pair_key("base", "thirty"),
                "base",
                "thirty",
                0.998,
                "v2",
                recorded_from,
            ),
        ],
    )
    connection.commit()

    first = review_pending_near_duplicates(
        connection,
        threshold=0.92,
        limit=1,
        reviewed_at="2026-08-12T00:00:00+00:00",
    )
    second = review_pending_near_duplicates(
        connection,
        threshold=0.92,
        limit=1,
        reviewed_at="2026-08-12T00:01:00+00:00",
    )
    reviewed = {
        row["id"]: row["reviewed_at"]
        for row in connection.execute("SELECT id,reviewed_at FROM dedup_pairs ORDER BY id").fetchall()
    }

    assert first == {"scanned": 1, "equivalent": 0, "deferred": 1, "missing": 0}
    assert second == {"scanned": 1, "equivalent": 0, "deferred": 1, "missing": 0}
    assert reviewed == {
        "higher-similarity": "2026-08-12T00:00:00+00:00",
        "lower-similarity": "2026-08-12T00:01:00+00:00",
    }
    connection.close()


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
