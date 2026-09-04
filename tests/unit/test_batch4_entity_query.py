from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hl_mem.application.recall import _QueryExpansionSession
from hl_mem.ingest.embedder import pack_vector
from hl_mem.recall.entity_query import filter_entity_candidates, plan_query_entity, resolve_query_entity
from hl_mem.recall.staged_pipeline import RecallConfig, hybrid_claims
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE canonical_entities(id TEXT, namespace_key TEXT, entity_type TEXT, "
        "display_name TEXT, status TEXT);"
        "CREATE TABLE entity_aliases(id TEXT, namespace_key TEXT, alias_normalized TEXT, "
        "entity_type TEXT, canonical_entity_id TEXT, version INTEGER, source_kind TEXT, valid_to TEXT);"
        "CREATE TABLE claims(id TEXT, namespace_key TEXT, status TEXT, "
        "subject_canonical_entity_id TEXT, canonical_target_entity_id TEXT);"
        "CREATE TABLE claim_entity_links(claim_id TEXT, canonical_entity_id TEXT, proof_id TEXT);"
    )
    connection.execute("INSERT INTO canonical_entities VALUES ('agent:pony','default','agent','Local Pony','active')")
    connection.execute(
        "INSERT INTO entity_aliases VALUES "
        "('alias-pony','default','pony','agent','agent:pony',1,'user_explicit',NULL)"
    )
    connection.execute("INSERT INTO claims VALUES ('pony-claim','default','active','agent:pony',NULL)")
    connection.execute("INSERT INTO claim_entity_links VALUES ('pony-claim','agent:pony','proof-pony')")
    return connection


def _add_person_alias(
    connection: sqlite3.Connection,
    *,
    entity_id: str,
    display_name: str,
    alias: str,
) -> None:
    proof_id = f"alias-{entity_id}"
    claim_id = f"claim-{entity_id}"
    connection.execute(
        "INSERT INTO canonical_entities VALUES (?,?,?,?,?)",
        (entity_id, "default", "person", display_name, "active"),
    )
    connection.execute(
        "INSERT INTO entity_aliases VALUES (?,?,?,?,?,?,?,NULL)",
        (proof_id, "default", alias, "person", entity_id, 1, "user_explicit"),
    )
    connection.execute(
        "INSERT INTO claims VALUES (?,?,?,?,NULL)",
        (claim_id, "default", "active", entity_id),
    )
    connection.execute(
        "INSERT INTO claim_entity_links VALUES (?,?,?)",
        (claim_id, entity_id, proof_id),
    )


def test_query_entity_resolution_requires_unique_alias_and_complete_link_coverage() -> None:
    connection = _connection()

    resolved = resolve_query_entity(connection, "show Pony settings", "default")

    assert resolved.confidence_class == "high"
    assert resolved.filter_entity_id == "agent:pony"
    assert resolved.proof_ids == ("alias-pony",)
    connection.execute("DELETE FROM claim_entity_links")
    assert resolve_query_entity(connection, "pony settings", "default").confidence_class == "low"
    connection.execute(
        "INSERT INTO canonical_entities VALUES " "('environment:pony','default','environment','Pony Runtime','active')"
    )
    connection.execute(
        "INSERT INTO entity_aliases VALUES "
        "('alias-runtime','default','pony','environment','environment:pony',1,'user_explicit',NULL)"
    )
    ambiguous = resolve_query_entity(connection, "pony settings", "default")
    assert ambiguous.confidence_class == "ambiguous"
    assert ambiguous.filter_entity_id is None


def test_enforce_pronoun_only_query_fails_wide() -> None:
    for pronoun in ("我", "我自己"):
        connection = _connection()
        _add_person_alias(
            connection,
            entity_id="person:user",
            display_name="User",
            alias=pronoun,
        )

        plan = plan_query_entity(connection, f"{pronoun}参加的活动的地点", "default", "enforce")

        assert plan.resolution.confidence_class == "high"
        assert plan.resolution.mentions == (pronoun,)
        assert plan.scope_mode == "wide"
        assert plan.entity_id is None
        assert plan.search_query == f"{pronoun}参加的活动的地点"
        assert plan.fallback_reason == "pronoun_only"


def test_enforce_named_entity_remains_scoped() -> None:
    connection = _connection()
    _add_person_alias(
        connection,
        entity_id="person:xu_hui",
        display_name="许慧",
        alias="许慧",
    )

    plan = plan_query_entity(connection, "许慧参加的活动的地点", "default", "enforce")

    assert plan.scope_mode == "entity"
    assert plan.entity_id == "person:xu_hui"
    assert plan.search_query == "参加的活动的地点"
    assert plan.fallback_reason is None


def test_enforce_mixed_pronoun_and_name_keeps_existing_multi_entity_fail_wide() -> None:
    connection = _connection()
    _add_person_alias(
        connection,
        entity_id="person:user",
        display_name="User",
        alias="我",
    )
    _add_person_alias(
        connection,
        entity_id="person:xu_hui",
        display_name="许慧",
        alias="许慧",
    )

    plan = plan_query_entity(connection, "我和许慧参加的活动", "default", "enforce")

    assert plan.resolution.mentions == ("我", "许慧")
    assert plan.scope_mode == "wide"
    assert plan.entity_id is None
    assert plan.search_query == "我和许慧参加的活动"
    assert plan.fallback_reason == "multiple_entities"


def _claim(claim_id: str, entity_id: str | None) -> dict[str, object]:
    return {
        "id": claim_id,
        "subject_canonical_entity_id": entity_id,
        "canonical_target_entity_id": None,
        "subject_entity_id": claim_id,
        "predicate": "configures",
        "value": claim_id,
        "index_text": claim_id,
        "embedding_dense": pack_vector([1.0]),
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "recorded_to": None,
        "confidence": 1.0,
        "importance": 0.5,
        "access_count": 0,
    }


class _Repo:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.claims = [_claim("other", None), _claim("pony-claim", "agent:pony")]

    def search_claims_fts(self, *_args, **_kwargs):
        entity_id = _kwargs.get("entity_id")
        return [
            claim
            for claim in self.claims
            if entity_id is None
            or entity_id in {claim.get("subject_canonical_entity_id"), claim.get("canonical_target_entity_id")}
        ]

    def search_claims_vector(self, *_args, **_kwargs):
        entity_id = _kwargs.get("entity_id")
        return [
            claim
            for claim in self.claims
            if entity_id is None
            or entity_id in {claim.get("subject_canonical_entity_id"), claim.get("canonical_target_entity_id")}
        ]

    def helpful_rates(self, _claim_ids, _min_samples):
        return {}


def _tracer() -> SearchTracer:
    return SearchTracer(
        SearchTrace(
            query_id="query",
            query_hash="hash",
            intent="current_state",
            limit=5,
            candidate_limit=50,
            candidates={},
            phases=SearchPhaseMetrics(),
        )
    )


def test_entity_constraint_scopes_channels_before_limit_only_in_enforce_mode() -> None:
    connection = _connection()
    assert [item["id"] for item in filter_entity_candidates(connection, _Repo(connection).claims, "agent:pony")] == [
        "pony-claim"
    ]
    enforce_trace = _tracer()
    enforced = hybrid_claims(
        _Repo(connection),
        "pony",
        pack_vector([1.0]),
        5,
        None,
        now="2026-01-02T00:00:00+00:00",
        tracer=enforce_trace,
        recall_config=RecallConfig(entity_scope_mode="entity", entity_scope_id="agent:pony"),
    )
    assert [item["id"] for item in enforced] == ["pony-claim"]
    assert enforce_trace.trace.entity_filter_mode == "enforce"
    assert enforce_trace.trace.entity_filtered_count == 0
    assert {channel for candidate in enforce_trace.trace.candidates.values() for channel in candidate.channels} <= {
        "fts",
        "dense",
    }

    observe_trace = _tracer()
    observed = hybrid_claims(
        _Repo(connection),
        "pony",
        pack_vector([1.0]),
        5,
        None,
        now="2026-01-02T00:00:00+00:00",
        tracer=observe_trace,
        recall_config=RecallConfig(entity_scope_mode="observe", entity_scope_id="agent:pony"),
    )
    assert {item["id"] for item in observed} == {"other", "pony-claim"}
    assert observe_trace.trace.entity_filter_mode == "observe"
    assert observe_trace.trace.entity_filtered_count == 1


def test_low_confidence_entity_keeps_original_without_extra_rrf_channels() -> None:
    connection = _connection()
    connection.execute("DELETE FROM claim_entity_links")
    recall = SimpleNamespace(
        request=SimpleNamespace(query="pony settings", namespace="default"),
        tracer=_tracer(),
    )
    service = SimpleNamespace(
        connection=connection,
        embedder=None,
        settings=SimpleNamespace(
            entity_constraint_mode="enforce",
            query_expansion_total_timeout_seconds=1.0,
            recall_dense_enabled=False,
        ),
    )

    expansion = _QueryExpansionSession(service, recall)

    assert len(expansion.weighted_queries) == 1
    assert expansion.weighted_queries[0].text == "pony settings"
