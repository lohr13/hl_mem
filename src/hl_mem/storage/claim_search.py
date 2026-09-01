"""Shared SQL and vector-scan implementation for Claim retrieval reads."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, cast

from hl_mem.core.vector import batch_cosine_similarity
from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.protocols import ClaimRow
from hl_mem.recall.lexicalizer import prepare_fts_query
from hl_mem.storage._shared import is_fts_syntax_error
from hl_mem.storage.candidate_materializer import materialize_candidates

_CURRENT_STATUS_SQL = "('active','superseded','expired')"
_HISTORICAL_STATUS_SQL = "('active','archived','superseded','expired')"


def recall_statuses_sql(intent: RecallIntent) -> str:
    return _HISTORICAL_STATUS_SQL if intent is RecallIntent.HISTORICAL else _CURRENT_STATUS_SQL


def valid_time_sql(intent: RecallIntent, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    started = f"AND ({prefix}valid_from IS NULL OR {prefix}valid_from<=?) "
    if intent is RecallIntent.HISTORICAL:
        return started
    return started + f"AND ({prefix}valid_to IS NULL OR {prefix}valid_to>?) "


def valid_time_parameters(intent: RecallIntent, reference: str) -> tuple[str, ...]:
    return (reference,) if intent is RecallIntent.HISTORICAL else (reference, reference)


def _entity_scope(alias: str, namespace: str, entity_id: str | None) -> tuple[str, tuple[str, ...]]:
    if entity_id is None:
        return "", ()
    prefix = f"{alias}." if alias else ""
    sql = (
        f"AND {prefix}id IN ("
        "SELECT subject_claim.id FROM claims AS subject_claim "
        "WHERE subject_claim.namespace_key=? AND subject_claim.subject_canonical_entity_id=? "
        "UNION SELECT target_claim.id FROM claims AS target_claim "
        "WHERE target_claim.namespace_key=? AND target_claim.canonical_target_entity_id=? "
        "UNION SELECT entity_link.claim_id FROM claim_entity_links AS entity_link "
        "JOIN claims AS linked_claim ON linked_claim.id=entity_link.claim_id "
        "WHERE linked_claim.namespace_key=? AND entity_link.canonical_entity_id=?) "
    )
    return sql, (namespace, entity_id, namespace, entity_id, namespace, entity_id)


def search_claims_vector_scan(
    repository: Any,
    query_blob: bytes,
    limit: int | None = None,
    as_of: str | None = None,
    intent: RecallIntent | str | None = None,
    known_as_of: str | None = None,
    namespace: str = "default",
    *,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Scan visible embedded Claims, optionally inside one canonical entity scope."""

    limit = repository.recall_vector_scan_limit if limit is None else limit
    if limit <= 0:
        return []
    reference = as_of or datetime.now(timezone.utc).isoformat()
    selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
    statuses = recall_statuses_sql(selected_intent)
    scope_sql, scope_parameters = _entity_scope("", namespace, entity_id)
    cursor = repository.connection.execute(
        f"SELECT id, embedding_dense FROM claims WHERE embedding_dense IS NOT NULL AND status IN {statuses} "
        "AND namespace_key=? "
        f"{valid_time_sql(selected_intent)}"
        f"{scope_sql}",
        (namespace, *valid_time_parameters(selected_intent, reference), *scope_parameters),
    )
    scored_claims: list[tuple[str, float]] = []
    while rows := cursor.fetchmany(repository.vector_batch_size):
        scores = batch_cosine_similarity(
            query_blob,
            [row["embedding_dense"] for row in rows],
            repository.vector_batch_size,
        )
        scored_claims.extend((str(row["id"]), score) for row, score in zip(rows, scores))
    scored_claims.sort(key=lambda item: (-item[1], item[0]))
    return materialize_candidates(repository, scored_claims, limit, reference, known_as_of, selected_intent)


def search_claims_fts(
    repository: Any,
    query: str,
    limit: int | None = None,
    as_of: str | None = None,
    intent: RecallIntent | str | None = None,
    known_as_of: str | None = None,
    namespace: str = "default",
    *,
    entity_id: str | None = None,
) -> list[ClaimRow]:
    """Read the tokenized FTS index with the same optional entity predicate."""

    limit = repository.recall_default_limit if limit is None else limit
    reference = as_of or datetime.now(timezone.utc).isoformat()
    selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
    statuses = recall_statuses_sql(selected_intent)
    scope_sql, scope_parameters = _entity_scope("c", namespace, entity_id)
    match_sql = (
        "SELECT c.* FROM claims_fts_v2 f JOIN claims c ON c.rowid=f.rowid "
        f"WHERE claims_fts_v2 MATCH ? AND c.status IN {statuses} "
        "AND c.namespace_key=? "
        f"{valid_time_sql(selected_intent, 'c')}"
        f"{scope_sql}"
        "ORDER BY bm25(claims_fts_v2) LIMIT ?"
    )

    def execute_match(match_query: str) -> list[sqlite3.Row]:
        return cast(
            list[sqlite3.Row],
            repository.connection.execute(
                match_sql,
                (
                    match_query,
                    namespace,
                    *valid_time_parameters(selected_intent, reference),
                    *scope_parameters,
                    limit,
                ),
            ).fetchall(),
        )

    match_query = prepare_fts_query(query, language=repository.fts_language)
    if not match_query:
        return []
    try:
        rows = execute_match(match_query)
    except sqlite3.OperationalError as error:
        if not is_fts_syntax_error(error):
            raise
        return []
    return cast(
        list[ClaimRow],
        [
            claim
            for claim in repository._decode_rows(rows)
            if claim_is_visible(claim, reference, known_as_of, selected_intent)
        ],
    )
