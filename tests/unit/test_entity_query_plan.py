from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import asdict

from hl_mem.recall.entity_query import plan_query_entity
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace


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
    return connection


def _add_entity(
    connection: sqlite3.Connection,
    entity_id: str,
    entity_type: str,
    alias: str,
    *,
    claim_id: str | None = None,
    active_alias: bool = True,
    linked: bool = True,
) -> None:
    normalized = unicodedata.normalize("NFKC", alias).casefold()
    connection.execute(
        "INSERT INTO canonical_entities VALUES (?,?,?,?, 'active')",
        (entity_id, "default", entity_type, f"Synthetic {entity_type}"),
    )
    connection.execute(
        "INSERT INTO entity_aliases VALUES (?,?,?,?,?,?, 'user_explicit',?)",
        (
            f"proof:{entity_id}:{normalized}",
            "default",
            normalized,
            entity_type,
            entity_id,
            1,
            None if active_alias else "2026-01-02T00:00:00+00:00",
        ),
    )
    resolved_claim_id = claim_id or f"claim:{entity_id}"
    connection.execute(
        "INSERT INTO claims VALUES (?, 'default', 'active', ?, NULL)",
        (resolved_claim_id, entity_id),
    )
    if linked:
        connection.execute(
            "INSERT INTO claim_entity_links VALUES (?,?,?)",
            (resolved_claim_id, entity_id, f"link:{resolved_claim_id}"),
        )


def _trace() -> SearchTrace:
    return SearchTrace(
        query_id="query-id",
        query_hash="query-hash",
        intent="current_state",
        limit=5,
        candidate_limit=25,
        candidates={},
        phases=SearchPhaseMetrics(),
    )


def test_high_confidence_plan_removes_only_the_normalized_entity_mention() -> None:
    connection = _connection()
    _add_entity(connection, "agent:pony", "agent", "Pony")

    plan = plan_query_entity(connection, "Pony deployment status", "default", "enforce")

    assert plan.entity_id == "agent:pony"
    assert plan.residual_query == "deployment status"
    assert plan.search_query == "deployment status"
    assert plan.scope_mode == "entity"
    assert plan.fallback_reason is None
    assert [(mention.start, mention.end, mention.alias) for mention in plan.resolution.mention_spans] == [
        (0, 4, "pony")
    ]


def test_ambiguous_and_overlapping_aliases_stay_on_the_original_query() -> None:
    ambiguous = _connection()
    _add_entity(ambiguous, "agent:pony", "agent", "Pony")
    _add_entity(ambiguous, "project:pony", "project", "Pony")

    ambiguous_plan = plan_query_entity(ambiguous, "Pony status", "default", "enforce")

    assert ambiguous_plan.entity_id is None
    assert ambiguous_plan.search_query == "Pony status"
    assert ambiguous_plan.scope_mode == "wide"
    assert ambiguous_plan.fallback_reason == "ambiguous_alias"

    overlapping = _connection()
    _add_entity(overlapping, "project:phoenix", "project", "Project Phoenix")
    _add_entity(overlapping, "agent:phoenix", "agent", "Phoenix")

    overlap_plan = plan_query_entity(overlapping, "Project Phoenix status", "default", "enforce")

    assert overlap_plan.entity_id is None
    assert overlap_plan.search_query == "Project Phoenix status"
    assert overlap_plan.scope_mode == "wide"
    assert overlap_plan.fallback_reason == "ambiguous_alias"


def test_multiple_mentions_of_one_entity_scope_but_multiple_entities_do_not() -> None:
    same = _connection()
    _add_entity(same, "agent:pony", "agent", "Pony")
    same.execute(
        "INSERT INTO entity_aliases VALUES "
        "('proof:pony-cn','default','小马','agent','agent:pony',1,'user_explicit',NULL)"
    )

    same_plan = plan_query_entity(same, "Pony and 小马 deployment status", "default", "enforce")

    assert same_plan.entity_id == "agent:pony"
    assert same_plan.residual_query == "and deployment status"
    assert same_plan.scope_mode == "entity"

    multiple = _connection()
    _add_entity(multiple, "agent:pony", "agent", "Pony")
    _add_entity(multiple, "project:phoenix", "project", "Phoenix")

    multiple_plan = plan_query_entity(multiple, "Pony Phoenix status", "default", "enforce")

    assert multiple_plan.entity_id is None
    assert multiple_plan.search_query == "Pony Phoenix status"
    assert multiple_plan.scope_mode == "wide"
    assert multiple_plan.fallback_reason == "multiple_entities"


def test_alias_scan_overflow_fails_wide_instead_of_hiding_a_second_entity() -> None:
    connection = _connection()
    _add_entity(connection, "agent:alpha", "agent", "VeryLongAlpha")
    _add_entity(connection, "project:z", "project", "Z")
    connection.execute(
        "INSERT INTO canonical_entities VALUES ('agent:filler','default','agent','Synthetic filler','active')"
    )
    connection.executemany(
        "INSERT INTO entity_aliases VALUES (?, 'default', ?, 'agent', 'agent:filler', 1, 'user_explicit', NULL)",
        [(f"proof:filler:{index}", f"filler-{index:04d}") for index in range(1023)],
    )

    plan = plan_query_entity(connection, "VeryLongAlpha Z status", "default", "enforce")

    assert plan.entity_id is None
    assert plan.search_query == "VeryLongAlpha Z status"
    assert plan.scope_mode == "wide"
    assert plan.fallback_reason == "resolution_error"


def test_nfkc_boundaries_empty_residual_and_chinese_mentions_are_deterministic() -> None:
    connection = _connection()
    _add_entity(connection, "agent:pony", "agent", "Pony")
    connection.execute(
        "INSERT INTO entity_aliases VALUES "
        "('proof:pony-cn','default','小马','agent','agent:pony',1,'user_explicit',NULL)"
    )

    nfkc = plan_query_entity(connection, "ＰＯＮＹ deployment status", "default", "enforce")
    boundary = plan_query_entity(connection, "Ponytail deployment status", "default", "enforce")
    empty = plan_query_entity(connection, "Pony", "default", "enforce")
    chinese = plan_query_entity(connection, "查看小马的部署状态", "default", "enforce")

    assert nfkc.residual_query == "deployment status"
    assert boundary.scope_mode == "wide"
    assert boundary.fallback_reason == "no_mention"
    assert empty.residual_query == ""
    assert empty.search_query == "Pony"
    assert empty.scope_mode == "entity"
    assert chinese.residual_query == "查看的部署状态"


def test_historical_alias_incomplete_links_off_mode_and_storage_failure_fall_back_wide() -> None:
    historical = _connection()
    _add_entity(historical, "agent:legacy", "agent", "Legacy", active_alias=False)
    historical_plan = plan_query_entity(historical, "Legacy status", "default", "enforce")
    assert historical_plan.scope_mode == "wide"
    assert historical_plan.fallback_reason == "no_mention"

    incomplete = _connection()
    _add_entity(incomplete, "project:cedar", "project", "Cedar")
    incomplete.execute("INSERT INTO claims VALUES ('claim:cedar:missing','default','active','project:cedar',NULL)")
    incomplete_plan = plan_query_entity(incomplete, "Cedar status", "default", "enforce")
    assert incomplete_plan.scope_mode == "wide"
    assert incomplete_plan.fallback_reason == "incomplete_links"

    closed = _connection()
    closed.close()
    off_plan = plan_query_entity(closed, "Pony status", "default", "off")
    assert off_plan.scope_mode == "off"
    assert off_plan.fallback_reason == "mode_off"

    broken = _connection()
    broken.execute("DROP TABLE entity_aliases")
    broken_plan = plan_query_entity(broken, "Pony status", "default", "enforce")
    assert broken_plan.scope_mode == "wide"
    assert broken_plan.fallback_reason == "resolution_error"


def test_observe_keeps_the_original_query_and_trace_never_records_query_or_residual_text() -> None:
    connection = _connection()
    _add_entity(connection, "agent:pony", "agent", "Pony")
    original = "Pony confidential deployment status"
    plan = plan_query_entity(connection, original, "default", "observe")
    trace = _trace()

    plan.record(trace)
    serialized = json.dumps(asdict(trace), ensure_ascii=False)

    assert plan.entity_id == "agent:pony"
    assert plan.scope_mode == "observe"
    assert plan.search_query == original
    assert "confidential deployment status" not in serialized
    assert "residual_query" not in serialized
    assert "search_query" not in serialized
