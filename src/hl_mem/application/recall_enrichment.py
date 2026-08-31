"""Repository-backed enrichment for recall results and derived memories."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from hl_mem.domain.relations import get_relations_batch
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.evidence import DerivationRepository, EvidenceRepository

ClaimText = Callable[[dict[str, Any]], str]
ClaimRelation = Callable[[Mapping[str, Any]], tuple[str, str, str] | None]


def assemble_observations(connection: sqlite3.Connection, claim_ids: list[str]) -> list[dict[str, Any]]:
    """Load active derived memories and their evidence for recalled claims."""
    observations = DerivationRepository(connection).list_active_for_claims(claim_ids)
    if not observations:
        return []
    evidence_repo = EvidenceRepository(connection)
    by_kind: dict[str, list[str]] = {}
    for observation in observations:
        by_kind.setdefault(str(observation.get("kind") or "observation"), []).append(str(observation["id"]))
    evidence: dict[str, list[dict[str, str]]] = {}
    for kind, derived_ids in by_kind.items():
        evidence.update(evidence_repo.batch_get_links_for_derived(kind, derived_ids))
    for observation in observations:
        observation["evidence"] = evidence.get(str(observation["id"]), [])
    return observations


def assemble_results(
    connection: sqlite3.Connection,
    claims: list[dict[str, Any]],
    namespace: str,
    *,
    claim_text: ClaimText,
    claim_relation: ClaimRelation,
) -> list[dict[str, Any]]:
    """Attach evidence, replacement, relation, and conflict projections."""
    if not claims:
        return []
    evidence_repo = EvidenceRepository(connection)
    claim_repo = ClaimRepository(connection)
    claim_ids = [claim["id"] for claim in claims]
    evidence_claim_ids = [
        str(claim_id) for claim in claims for claim_id in [claim["id"], *(claim.get("_equivalent_claim_ids") or [])]
    ]
    all_evidence = evidence_repo.batch_get_links_for_derived("claim", evidence_claim_ids)
    superseded_ids = [claim["superseded_by_id"] for claim in claims if claim.get("superseded_by_id")]
    replacements = _replacement_map(claim_repo, superseded_ids, claim_text=claim_text)
    relations = get_relations_batch(connection, claim_ids)
    rivals = _rivals_by_claim(connection, claims, namespace)
    results: list[dict[str, Any]] = []
    for claim in claims:
        evidence: list[dict[str, str]] = []
        evidence_keys: set[tuple[str, str]] = set()
        for claim_id in [claim["id"], *(claim.get("_equivalent_claim_ids") or [])]:
            for item in all_evidence.get(str(claim_id), []):
                key = (item["type"], item["id"])
                if key not in evidence_keys:
                    evidence_keys.add(key)
                    evidence.append(item)
        superseded_by_id = claim.get("superseded_by_id")
        replacement = replacements.get(str(superseded_by_id)) if superseded_by_id else None
        result: dict[str, Any] = {
            "type": "claim",
            "memory_type": "claim",
            "id": claim["id"],
            "text": claim_text(claim),
            "score": float(claim.get("_score", 0.0)),
            "score_path": str(claim.get("_score_path", "reranker_fallback")),
            "reranker_raw_score": claim.get("_reranker_raw_score"),
            "features": dict(claim.get("_features") or {}),
            "equivalent_claim_ids": list(claim.get("_equivalent_claim_ids") or []),
            "status": claim["status"],
            "assertion_kind": claim.get("assertion_kind") or "unknown",
            "confidence": claim["confidence"],
            "canonical_attribute": claim.get("canonical_attribute"),
            "canonical_slot": claim.get("canonical_slot"),
            "topic_tags": list(claim.get("topic_tags") or []),
            "valid_from": claim["valid_from"],
            "valid_to": claim.get("valid_to"),
            "recorded_from": claim.get("recorded_from"),
            "recorded_to": claim.get("recorded_to"),
            "replacement": replacement,
            "evidence": evidence,
            "relations": relations.get(claim["id"], []),
        }
        relation = claim_relation(claim)
        if relation is not None:
            result.update(zip(("role", "action", "object"), relation, strict=True))
        for field_name in ("occurred_start", "occurred_end", "entities"):
            if claim.get(field_name):
                result[field_name] = claim[field_name]
        if claim["status"] == "disputed" and claim.get("conflict_key"):
            result["conflicts"] = rivals.get(claim["id"], [])
        results.append(result)
    return results


def _replacement_map(
    repository: ClaimRepository,
    superseded_ids: list[str],
    *,
    claim_text: ClaimText,
) -> dict[str, dict[str, Any]]:
    claims = repository.batch_get_claims(superseded_ids)
    return {
        claim_id: {"id": claim["id"], "text": claim_text(claim), "valid_from": claim["valid_from"]}
        for claim_id, claim in claims.items()
    }


def _rivals_by_claim(
    connection: sqlite3.Connection,
    claims: list[dict[str, Any]],
    namespace: str,
) -> dict[str, list[dict[str, Any]]]:
    disputed = [claim for claim in claims if claim["status"] == "disputed" and claim.get("conflict_key")]
    if not disputed:
        return {}
    unique_keys = list(dict.fromkeys(claim["conflict_key"] for claim in disputed))
    rivals_by_key = ClaimRepository(connection).find_disputed_rivals(unique_keys, namespace)
    return {
        claim["id"]: [rival for rival in rivals_by_key[claim["conflict_key"]] if rival["id"] != claim["id"]]
        for claim in disputed
    }
