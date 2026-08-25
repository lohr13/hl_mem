import json

import hl_mem.domain.claims.dedup as dedup_domain
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.llm.types import LLMResponse
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository
from hl_mem.workers.deduplicate import deduplicate_claims


def test_dedup_slot_strategy_migration_defaults_fail_closed(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-slot.db").open()

    columns = {
        row["name"]: (row["type"], row["notnull"], row["dflt_value"])
        for row in connection.execute("PRAGMA table_info(dedup_pairs)")
    }

    assert columns["candidate_strategy"] == ("TEXT", 1, "'legacy_no_slot'")
    assert columns["bucket_key"] == ("TEXT", 0, None)
    assert columns["entity_proof_id"] == ("TEXT", 0, None)
    assert columns["auto_apply_eligible"] == ("INTEGER", 1, "0")


def _slot_fixture(tmp_path):
    connection = Database(tmp_path / "slot-candidates.db").open()
    claims = ClaimRepository(connection)
    entities = EntityRepository(connection)
    embedder = FakeEmbedder(8)
    now = "2026-08-25T00:00:00+00:00"
    entities.create_entity("agent:local_pony", "agent", "local_pony", "Local Pony", now=now)
    aliases = {
        alias: entities.create_alias(alias, "agent", "agent:local_pony", "user_explicit", valid_from=now)
        for alias in ("pony", "local pony")
    }
    vector = embedder.embed_one("same model selection")
    common = {
        "namespace_key": "default",
        "predicate": "使用",
        "value": "uses qwen for chat",
        "qualifiers": {"task": "chat"},
        "canonical_attribute": "choice.model",
        "canonical_slot": "choice.model",
        "subject_canonical_entity_id": "agent:local_pony",
        "recorded_from": now,
        "valid_from": now,
        "status": "active",
        "source_authority": "low",
        "assertion_kind": "unknown",
        "embedding_dense": vector,
    }
    for claim_id, alias in (("left", "pony"), ("right", "local pony")):
        authority = "high" if claim_id == "right" else "low"
        claims.insert_claim(
            {
                **common,
                "id": claim_id,
                "subject_entity_id": alias,
                "source_authority": authority,
                "entities_json": json.dumps([alias]),
            }
        )
        proof_id = f"proof-{claim_id}"
        connection.execute(
            "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
            "VALUES (?,'claim',?,'event',?,'supports',1.0)",
            (proof_id, claim_id, f"event-{claim_id}"),
        )
        entities.link_claim(
            claim_id,
            "agent:local_pony",
            "subject",
            mention_text=alias,
            alias_version=aliases[alias]["version"],
            proof_id=proof_id,
        )
    connection.commit()
    return connection, claims, embedder


def test_slot_cross_subject_candidates_require_one_typed_entity_with_proof(tmp_path) -> None:
    connection, claims, embedder = _slot_fixture(tmp_path)

    candidates = claims.find_cross_subject_dedup_candidates("default", embedder, threshold=0.9, limit=20)

    assert len(candidates) == 1
    assert candidates[0]["candidate_strategy"] == "slot_cross_subject_v1"
    assert candidates[0]["auto_apply_eligible"] is True
    assert candidates[0]["bucket_key"]
    assert candidates[0]["entity_proof_id"]


def test_auto_apply_structure_gate_precedes_confidence_and_protects_atoms() -> None:
    common = {
        "namespace_key": "default",
        "subject_entity_id": "pony",
        "subject_canonical_entity_id": "agent:local_pony",
        "predicate": "计划",
        "value": "Buy 10 shares of ACME v1.2 on Monday",
        "canonical_attribute": "plan.other",
        "canonical_slot": "plan.other",
        "qualifiers": {
            "action_family": "open",
            "assertion_phase": "plan",
            "direction": "long",
            "quantity": "10",
            "quantity_mode": "exact",
            "quantity_unit": "share",
        },
        "assertion_kind": "plan",
        "status": "active",
        "valid_from": "2026-08-25T00:00:00+00:00",
        "valid_to": None,
    }
    near = {**common, "subject_entity_id": "local pony", "value": "Buy 10 shares of ACME v1.2 on Monday."}

    assert dedup_domain.dedup_structural_gate(common, near, allow_cross_subject=True).safe
    unsafe = (
        {**near, "value": "Buy 11 shares of ACME v1.3 on Monday"},
        {**near, "subject_canonical_entity_id": "agent:other"},
        {**near, "assertion_kind": "observation"},
        {**near, "qualifiers": {**near["qualifiers"], "quantity": "11"}},
    )
    for changed in unsafe:
        result = dedup_domain.dedup_structural_gate(common, changed, allow_cross_subject=True)
        assert not result.safe
    assert not dedup_domain.dedup_structural_gate(
        {**common, "canonical_attribute": "memory.explicit"},
        {**near, "canonical_attribute": "memory.explicit"},
        allow_cross_subject=True,
    ).safe


class _EquivalentClient:
    model = "offline-e2-fixture"

    def complete(self, _request):
        return LLMResponse(
            content={"decision": "equivalent", "confidence": 0.99, "reason": "same typed fact"},
            finish_reason="stop",
            usage_total_tokens=10,
        )


def test_auto_apply_selects_authoritative_survivor_and_rolls_back_exact_links(tmp_path) -> None:
    connection, claims, embedder = _slot_fixture(tmp_path)
    evidence_before = connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0]

    result = deduplicate_claims(
        connection,
        _EquivalentClient(),
        embedder,
        audit_only=False,
        auto_merge_min_confidence=0.98,
    )

    assert result["applied"] == 1
    assert claims.get_claim("right")["status"] == "active"
    assert claims.get_claim("left")["superseded_by_id"] == "right"
    pair = connection.execute("SELECT candidate_strategy,auto_apply_eligible,applied_at FROM dedup_pairs").fetchone()
    assert tuple(pair[:2]) == ("slot_cross_subject_v1", 1)
    assert pair["applied_at"] is not None
    action = connection.execute("SELECT id,status FROM governance_actions WHERE domain='dedup'").fetchone()
    assert action["status"] == "applied"
    assert connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0] > evidence_before

    from hl_mem.workers.deduplicate import rollback_dedup_action

    rollback_dedup_action(
        connection,
        action["id"],
        rolled_back_at="2026-08-25T01:00:00+00:00",
        reason="e2 rollback replay",
    )

    assert claims.get_claim("left")["status"] == "active"
    assert claims.get_claim("left")["superseded_by_id"] is None
    assert connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == evidence_before
    assert connection.execute("SELECT status FROM governance_actions WHERE id=?", (action["id"],)).fetchone()[0] == (
        "rolled_back"
    )


def test_auto_apply_rejects_entity_proof_closed_after_review(tmp_path) -> None:
    connection, claims, embedder = _slot_fixture(tmp_path)
    reviewed = deduplicate_claims(connection, _EquivalentClient(), embedder, audit_only=True)
    assert reviewed["equivalent"] == 1
    connection.execute("UPDATE entity_aliases SET valid_to='2026-08-25T00:30:00+00:00'")
    connection.commit()

    applied = deduplicate_claims(connection, _EquivalentClient(), embedder, audit_only=False)

    assert applied["applied"] == 0
    assert claims.get_claim("left")["status"] == "active"
    assert claims.get_claim("right")["status"] == "active"
    assert connection.execute("SELECT auto_apply_eligible FROM dedup_pairs").fetchone()[0] == 0
