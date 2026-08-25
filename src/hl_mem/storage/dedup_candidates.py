"""Typed slot-aware candidate generation for cross-subject deduplication."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.dedup import dedup_slot_bucket_key, governing_canonical_entity_id

_ELIGIBLE_ALIAS_SOURCES = frozenset({"builtin", "config_explicit", "user_explicit", "migration_exact"})


def find_legacy_cross_subject_candidates(claims: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Preserve the legacy no-slot audit branch without making it auto-eligible."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        groups.setdefault(str(claim["predicate"]), []).append(claim)
    candidates: list[dict[str, Any]] = []
    for group in groups.values():
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                if left.get("subject_entity_id") == right.get("subject_entity_id"):
                    continue
                similarity = cosine_similarity(left["embedding_dense"], right["embedding_dense"])
                if similarity >= threshold:
                    candidates.append(
                        {
                            "left": left,
                            "right": right,
                            "similarity": similarity,
                            "candidate_strategy": "legacy_no_slot",
                            "bucket_key": None,
                            "entity_proof_id": None,
                            "auto_apply_eligible": False,
                        }
                    )
    return candidates


def _proof_id(connection: sqlite3.Connection, claim: dict[str, Any], namespace: str) -> str | None:
    entity_id = governing_canonical_entity_id(claim)
    role = "target" if claim.get("canonical_target_entity_id") == entity_id else "subject"
    row = connection.execute(
        "SELECT link.proof_id,alias.source_kind FROM claim_entity_links AS link "
        "JOIN entity_aliases AS alias ON alias.canonical_entity_id=link.canonical_entity_id "
        "AND alias.alias_normalized=link.mention_text AND alias.version=link.alias_version "
        "WHERE link.claim_id=? AND link.canonical_entity_id=? AND link.role=? "
        "AND alias.namespace_key=? AND alias.valid_to IS NULL LIMIT 1",
        (claim["id"], entity_id, role, namespace),
    ).fetchone()
    if row is None or row["source_kind"] not in _ELIGIBLE_ALIAS_SOURCES:
        return None
    return str(row["proof_id"])


def find_slot_cross_subject_candidates(
    connection: sqlite3.Connection,
    claims: list[dict[str, Any]],
    namespace: str,
    threshold: float,
) -> list[dict[str, Any]]:
    """Return typed candidates only when both claims carry an explicit active proof."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        bucket_key = dedup_slot_bucket_key(claim)
        if bucket_key is not None:
            groups.setdefault(bucket_key, []).append(claim)
    candidates: list[dict[str, Any]] = []
    for bucket_key, group in groups.items():
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                if left.get("subject_entity_id") == right.get("subject_entity_id"):
                    continue
                proof_ids = [_proof_id(connection, claim, namespace) for claim in (left, right)]
                if any(proof_id is None for proof_id in proof_ids):
                    continue
                similarity = cosine_similarity(left["embedding_dense"], right["embedding_dense"])
                if similarity < threshold:
                    continue
                proof_key = hashlib.sha256(
                    "\x1f".join(sorted(str(proof_id) for proof_id in proof_ids)).encode("utf-8")
                ).hexdigest()
                candidates.append(
                    {
                        "left": left,
                        "right": right,
                        "similarity": similarity,
                        "candidate_strategy": "slot_cross_subject_v1",
                        "bucket_key": bucket_key,
                        "entity_proof_id": proof_key,
                        "auto_apply_eligible": True,
                    }
                )
    return candidates
