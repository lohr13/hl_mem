"""Deterministic query-entity resolution and pre-RRF candidate constraints."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from hl_mem.protocols import EmbedderProtocol, WeightedQuery, embed_query

ConfidenceClass = Literal["high", "low", "ambiguous"]
EntityScopeMode = Literal["entity", "observe", "wide", "off"]
EntityFallbackReason = Literal[
    "no_mention",
    "ambiguous_alias",
    "multiple_entities",
    "incomplete_links",
    "resolution_error",
    "storage_error",
    "mode_off",
]
_MAX_ACTIVE_ALIASES = 1024


@dataclass(frozen=True, slots=True)
class QueryEntityMention:
    start: int
    end: int
    alias: str
    entity_id: str
    entity_type: str
    proof_id: str


@dataclass(frozen=True, slots=True)
class QueryEntityResolution:
    mentions: tuple[str, ...] = ()
    resolved_ids: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()
    confidence_class: ConfidenceClass = "low"
    proof_ids: tuple[str, ...] = ()
    rewrite_terms: tuple[str, ...] = ()
    filter_entity_id: str | None = None
    mention_spans: tuple[QueryEntityMention, ...] = ()
    resolution_reason: EntityFallbackReason | None = "no_mention"


@dataclass(frozen=True, slots=True)
class QueryEntityPlan:
    resolution: QueryEntityResolution
    entity_id: str | None
    residual_query: str
    search_query: str
    scope_mode: EntityScopeMode
    fallback_reason: EntityFallbackReason | None

    @property
    def rewrite(self) -> str | None:
        """Compatibility view for the pre-1.1 query-expansion session."""

        if self.scope_mode != "entity":
            return None
        return self.residual_query or None

    @property
    def filter_mode(self) -> str:
        """Compatibility view for the existing staged-pipeline configuration."""

        return "enforce" if self.scope_mode == "entity" else self.scope_mode

    def record(self, trace: Any) -> None:
        trace.entity_mentions = list(self.resolution.mentions)
        trace.entity_resolution = {
            "confidence_class": self.resolution.confidence_class,
            "entity_types": list(self.resolution.entity_types),
            "resolved_ids": list(self.resolution.resolved_ids),
            "mention_count": len(self.resolution.mention_spans),
            "mention_spans": [
                {
                    "start": mention.start,
                    "end": mention.end,
                    "entity_id": mention.entity_id,
                    "entity_type": mention.entity_type,
                    "proof_id": mention.proof_id,
                }
                for mention in self.resolution.mention_spans
            ],
        }
        trace.entity_proof_ids = list(self.resolution.proof_ids)
        trace.entity_filter_mode = self.filter_mode
        trace.entity_residual_term_count = len(self.residual_query.split()) if self.residual_query else 0
        trace.entity_fallback_reason = self.fallback_reason


@dataclass(frozen=True, slots=True)
class EntityConstraintResult:
    items: list[dict[str, Any]]
    filtered_ids: frozenset[str]


def _mention_spans(query: str, alias: str) -> list[tuple[int, int]]:
    escaped = re.escape(alias)
    pattern = rf"(?<!\w){escaped}(?!\w)" if alias.isascii() else escaped
    return [(match.start(), match.end()) for match in re.finditer(pattern, query)]


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left != right and left[0] < right[1] and left[1] > right[0]


def _residual_query(normalized_query: str, mentions: tuple[QueryEntityMention, ...]) -> str:
    cursor = 0
    retained: list[str] = []
    for mention in mentions:
        retained.append(normalized_query[cursor : mention.start])
        cursor = mention.end
    retained.append(normalized_query[cursor:])
    without_mentions = "".join(retained)
    collapsed = "".join(
        " " if unicodedata.category(character)[0] in {"P", "Z"} else character for character in without_mentions
    )
    return " ".join(collapsed.split())


def _link_coverage_complete(connection: sqlite3.Connection, namespace: str, entity_id: str) -> bool:
    linked = connection.execute(
        "SELECT 1 FROM claim_entity_links AS link JOIN claims AS claim ON claim.id=link.claim_id "
        "WHERE claim.namespace_key=? AND claim.status='active' AND link.canonical_entity_id=? LIMIT 1",
        (namespace, entity_id),
    ).fetchone()
    if linked is None:
        return False
    missing = connection.execute(
        "SELECT 1 FROM claims AS claim WHERE claim.namespace_key=? AND claim.status='active' "
        "AND (claim.subject_canonical_entity_id=? OR claim.canonical_target_entity_id=?) "
        "AND NOT EXISTS (SELECT 1 FROM claim_entity_links AS link "
        "WHERE link.claim_id=claim.id AND link.canonical_entity_id=?) LIMIT 1",
        (namespace, entity_id, entity_id, entity_id),
    ).fetchone()
    return missing is None


def resolve_query_entity(
    connection: sqlite3.Connection,
    query: str,
    namespace: str = "default",
) -> QueryEntityResolution:
    """Resolve bounded active aliases; any overlap, type ambiguity, or incomplete coverage stays wide."""

    normalized = unicodedata.normalize("NFKC", query).casefold()
    try:
        rows = connection.execute(
            "SELECT alias.id,alias.alias_normalized,alias.entity_type,alias.canonical_entity_id,"
            "entity.display_name FROM entity_aliases AS alias JOIN canonical_entities AS entity "
            "ON entity.namespace_key=alias.namespace_key AND entity.id=alias.canonical_entity_id "
            "WHERE alias.namespace_key=? AND alias.valid_to IS NULL AND entity.status='active' "
            "ORDER BY length(alias.alias_normalized) DESC,alias.alias_normalized,alias.entity_type LIMIT ?",
            (namespace, _MAX_ACTIVE_ALIASES + 1),
        ).fetchall()
    except sqlite3.Error:
        return QueryEntityResolution(resolution_reason="resolution_error")
    if len(rows) > _MAX_ACTIVE_ALIASES:
        return QueryEntityResolution(resolution_reason="resolution_error")
    matches: list[tuple[int, int, sqlite3.Row]] = []
    for row in rows:
        for start, end in _mention_spans(normalized, str(row["alias_normalized"])):
            matches.append((start, end, row))
    if not matches:
        return QueryEntityResolution()
    grouped: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for start, end, row in matches:
        grouped.setdefault((start, end), []).append(row)
    spans = sorted(grouped)
    overlapping = any(_spans_overlap(left, right) for index, left in enumerate(spans) for right in spans[index + 1 :])
    selected = [row for span in spans for row in grouped[span]]
    entity_ids = tuple(sorted({str(row["canonical_entity_id"]) for row in selected}))
    same_span_ambiguous = any(
        len({(str(row["canonical_entity_id"]), str(row["entity_type"])) for row in group}) > 1
        for group in grouped.values()
    )
    proof_ids = tuple(sorted({str(row["id"]) for row in selected}))
    rewrite_terms = tuple(sorted({str(row["display_name"]) for row in selected}))
    mentions: list[QueryEntityMention] = []
    if not overlapping:
        for start, end in spans:
            group = grouped[(start, end)]
            targets = {(str(row["canonical_entity_id"]), str(row["entity_type"])): row for row in group}
            if len(targets) != 1:
                continue
            (entity_id, entity_type), row = next(iter(targets.items()))
            mentions.append(
                QueryEntityMention(
                    start,
                    end,
                    str(row["alias_normalized"]),
                    entity_id,
                    entity_type,
                    str(row["id"]),
                )
            )
    reason: EntityFallbackReason | None
    if overlapping or same_span_ambiguous:
        reason = "ambiguous_alias"
    elif len(entity_ids) != 1:
        reason = "multiple_entities"
    else:
        reason = "incomplete_links"
    confidence: ConfidenceClass = "ambiguous" if reason in {"ambiguous_alias", "multiple_entities"} else "low"
    filter_id = None
    if reason == "incomplete_links":
        try:
            complete = _link_coverage_complete(connection, namespace, entity_ids[0])
        except sqlite3.Error:
            return QueryEntityResolution(
                mentions=tuple(sorted({str(row["alias_normalized"]) for row in selected})),
                resolved_ids=entity_ids,
                entity_types=tuple(sorted({str(row["entity_type"]) for row in selected})),
                confidence_class="low",
                proof_ids=proof_ids,
                rewrite_terms=rewrite_terms,
                mention_spans=tuple(mentions),
                resolution_reason="resolution_error",
            )
        if complete:
            confidence = "high"
            filter_id = entity_ids[0]
            reason = None
    return QueryEntityResolution(
        mentions=tuple(sorted({str(row["alias_normalized"]) for row in selected})),
        resolved_ids=entity_ids,
        entity_types=tuple(sorted({str(row["entity_type"]) for row in selected})),
        confidence_class=confidence,
        proof_ids=proof_ids,
        rewrite_terms=rewrite_terms,
        filter_entity_id=filter_id,
        mention_spans=tuple(mentions),
        resolution_reason=reason,
    )


def plan_query_entity(
    connection: sqlite3.Connection,
    query: str,
    namespace: str,
    mode: str,
) -> QueryEntityPlan:
    """Resolve before expansion and produce a fail-wide immutable query plan."""

    if mode not in {"off", "observe", "enforce"}:
        raise ValueError("entity constraint mode must be off, observe, or enforce")
    if mode == "off":
        resolution = QueryEntityResolution(resolution_reason="mode_off")
        return QueryEntityPlan(resolution, None, "", query, "off", "mode_off")
    resolution = resolve_query_entity(connection, query, namespace)
    fallback_reason = resolution.resolution_reason
    normalized = unicodedata.normalize("NFKC", query).casefold()
    residual = _residual_query(normalized, resolution.mention_spans)
    if resolution.confidence_class != "high" or resolution.filter_entity_id is None:
        return QueryEntityPlan(resolution, None, residual, query, "wide", fallback_reason)
    if mode == "observe":
        return QueryEntityPlan(resolution, resolution.filter_entity_id, residual, query, "observe", None)
    search_query = residual or query
    return QueryEntityPlan(resolution, resolution.filter_entity_id, residual, search_query, "entity", None)


def prepare_entity_query(
    connection: sqlite3.Connection,
    embedder: EmbedderProtocol,
    query: str,
    namespace: str,
    mode: str,
    *,
    dense_enabled: bool,
) -> tuple[QueryEntityPlan, WeightedQuery, bytes]:
    """Plan first so a normal query creates exactly one search embedding."""

    plan = plan_query_entity(connection, query, namespace, mode)
    weighted = WeightedQuery(plan.search_query, "original", 1.0)
    blob = embed_query(embedder, plan.search_query) if dense_enabled else b""
    return plan, weighted, blob


def prepare_wide_query(
    embedder: EmbedderProtocol,
    original_query: str,
    current_query: WeightedQuery,
    current_blob: bytes,
    *,
    dense_enabled: bool,
) -> tuple[WeightedQuery, bytes, int]:
    """Prepare one original-query retry and count only a newly required embedding."""

    if dense_enabled and current_query.text != original_query:
        return WeightedQuery(original_query, "original", 1.0), embed_query(embedder, original_query), 1
    return WeightedQuery(original_query, "original", 1.0), current_blob, 0


def filter_entity_candidates(
    connection: sqlite3.Connection,
    claims: list[dict[str, Any]],
    entity_id: str,
) -> list[dict[str, Any]]:
    """Filter an existing channel without changing candidate order or scores."""

    hot_ids = {
        str(claim["id"])
        for claim in claims
        if entity_id in {claim.get("subject_canonical_entity_id"), claim.get("canonical_target_entity_id")}
    }
    claim_ids = [str(claim["id"]) for claim in claims if str(claim["id"]) not in hot_ids]
    if claim_ids:
        placeholders = ",".join("?" for _ in claim_ids)
        try:
            linked = connection.execute(
                f"SELECT claim_id FROM claim_entity_links WHERE canonical_entity_id=? AND claim_id IN ({placeholders})",
                (entity_id, *claim_ids),
            ).fetchall()
        except sqlite3.Error:
            return claims
        hot_ids.update(str(row[0]) for row in linked)
    return [claim for claim in claims if str(claim["id"]) in hot_ids]


def apply_entity_constraint(
    connection: sqlite3.Connection | None,
    claims: list[dict[str, Any]],
    mode: str,
    entity_id: str | None,
) -> EntityConstraintResult:
    if connection is None or entity_id is None or mode not in {"observe", "enforce"}:
        return EntityConstraintResult(claims, frozenset())
    shadow = filter_entity_candidates(connection, claims, entity_id)
    shadow_ids = {str(claim["id"]) for claim in shadow}
    removed = frozenset(str(claim["id"]) for claim in claims if str(claim["id"]) not in shadow_ids)
    return EntityConstraintResult(shadow if mode == "enforce" else claims, removed)
