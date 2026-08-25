"""Deterministic query-entity resolution and pre-RRF candidate constraints."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

ConfidenceClass = Literal["high", "low", "ambiguous"]


@dataclass(frozen=True, slots=True)
class QueryEntityResolution:
    mentions: tuple[str, ...] = ()
    resolved_ids: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()
    confidence_class: ConfidenceClass = "low"
    proof_ids: tuple[str, ...] = ()
    rewrite_terms: tuple[str, ...] = ()
    filter_entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class QueryEntityPlan:
    resolution: QueryEntityResolution
    rewrite: str | None
    filter_mode: str

    def record(self, trace: Any) -> None:
        trace.entity_mentions = list(self.resolution.mentions)
        trace.entity_resolution = {
            "confidence_class": self.resolution.confidence_class,
            "entity_types": list(self.resolution.entity_types),
            "resolved_ids": list(self.resolution.resolved_ids),
            "rewrite_terms": list(self.resolution.rewrite_terms),
        }
        trace.entity_proof_ids = list(self.resolution.proof_ids)
        trace.entity_filter_mode = self.filter_mode


@dataclass(frozen=True, slots=True)
class EntityConstraintResult:
    items: list[dict[str, Any]]
    filtered_ids: frozenset[str]


def _mention_spans(query: str, alias: str) -> list[tuple[int, int]]:
    escaped = re.escape(alias)
    pattern = rf"(?<!\w){escaped}(?!\w)" if alias.isascii() else escaped
    return [(match.start(), match.end()) for match in re.finditer(pattern, query)]


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
            "ORDER BY length(alias.alias_normalized) DESC,alias.alias_normalized,alias.entity_type LIMIT 1024",
            (namespace,),
        ).fetchall()
    except sqlite3.Error:
        return QueryEntityResolution()
    matches: list[tuple[int, int, sqlite3.Row]] = []
    for row in rows:
        for start, end in _mention_spans(normalized, str(row["alias_normalized"])):
            matches.append((start, end, row))
    if not matches:
        return QueryEntityResolution()
    longest: dict[tuple[int, int], list[sqlite3.Row]] = {}
    occupied: list[tuple[int, int]] = []
    for start, end, row in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0], item[1])):
        same_span = (start, end) in longest
        if not same_span and any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        if not same_span:
            occupied.append((start, end))
        longest.setdefault((start, end), []).append(row)
    selected = [row for span in sorted(longest) for row in longest[span]]
    entity_ids = tuple(sorted({str(row["canonical_entity_id"]) for row in selected}))
    ambiguous = any(len({str(row["canonical_entity_id"]) for row in group}) > 1 for group in longest.values())
    ambiguous = ambiguous or len(entity_ids) != 1
    proof_ids = tuple(sorted({str(row["id"]) for row in selected}))
    rewrite_terms = tuple(sorted({str(row["display_name"]) for row in selected}))
    confidence: ConfidenceClass = "ambiguous" if ambiguous else "low"
    filter_id = None
    if not ambiguous and _link_coverage_complete(connection, namespace, entity_ids[0]):
        confidence = "high"
        filter_id = entity_ids[0]
    return QueryEntityResolution(
        mentions=tuple(sorted({str(row["alias_normalized"]) for row in selected})),
        resolved_ids=entity_ids,
        entity_types=tuple(sorted({str(row["entity_type"]) for row in selected})),
        confidence_class=confidence,
        proof_ids=proof_ids,
        rewrite_terms=rewrite_terms,
        filter_entity_id=filter_id,
    )


def plan_query_entity(
    connection: sqlite3.Connection,
    query: str,
    namespace: str,
    mode: str,
) -> QueryEntityPlan:
    """Resolve before expansion and produce a deterministic wide-search rewrite when needed."""

    try:
        resolution = resolve_query_entity(connection, query, namespace) if mode != "off" else QueryEntityResolution()
    except sqlite3.Error:
        resolution = QueryEntityResolution()
    filter_mode = "off"
    rewrite = None
    if resolution.mentions:
        if mode == "observe":
            filter_mode = "observe"
        elif resolution.confidence_class == "high":
            filter_mode = mode
        elif resolution.rewrite_terms:
            filter_mode = "rewrite"
            existing = query.casefold()
            additions = [term for term in resolution.rewrite_terms if term.casefold() not in existing]
            rewritten = " ".join((query, *additions)).strip()
            rewrite = rewritten if rewritten != query else None
        else:
            filter_mode = "wide"
    return QueryEntityPlan(resolution, rewrite, filter_mode)


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
