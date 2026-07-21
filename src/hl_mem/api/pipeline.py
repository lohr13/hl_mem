from __future__ import annotations

import hashlib
import json
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Any

from hl_mem.observability.audit import current_audit
from hl_mem.recall.attribute_map import validate_canonical_attribute
from hl_mem.recall.conflict import ConflictResolver, compute_conflict_key, compute_legacy_conflict_key
from hl_mem.recall.dedup import Deduplicator
from hl_mem.recall.observation import ObservationBuilder
from hl_mem.storage.repository import ClaimRepository, DerivationRepository, EvidenceRepository


def new_id() -> str:
    return uuid.uuid4().hex


def claim_text(claim: dict[str, Any]) -> str:
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"


def compute_fact_hash(subject: str, predicate: str, value: Any) -> str:
    stable_value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = unicodedata.normalize("NFKC", subject).strip()
    raw += unicodedata.normalize("NFKC", predicate).strip() + stable_value
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _summary(claim: Any) -> dict[str, Any]:
    value = claim.get("value_json", getattr(claim, "value", None))
    return {
        "subject": claim.get("subject_entity_id", getattr(claim, "subject", None)),
        "predicate": claim.get("predicate", getattr(claim, "predicate", None)),
        "value_hash": hashlib.sha256(str(value).encode()).hexdigest(),
        "confidence": claim.get("confidence", getattr(claim, "confidence", None)),
        "status": claim.get("status"),
    }


def store_extracted(
    connection: Any,
    extracted: Any,
    event: dict[str, Any],
    now: str,
    embedder: Any,
    authority: str | None = None,
    ttl_days: int = 7,
) -> str:
    audit = current_audit()
    claims, evidence = ClaimRepository(connection), EvidenceRepository(connection)
    namespace, subject = event.get("tenant_id", "default"), extracted.subject
    qualifiers = extracted.qualifiers or {}
    canonical_attribute = validate_canonical_attribute(
        extracted.predicate, getattr(extracted, "canonical_attribute", None)
    )
    value_json = json.dumps(extracted.value, ensure_ascii=False, sort_keys=True)
    scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
    expires_at = (
        (datetime.fromisoformat(now) + timedelta(days=ttl_days)).isoformat()
        if extracted.volatility == "ephemeral" and scope == "temporal"
        else None
    )
    try:
        importance = min(1.0, max(0.0, float(extracted.importance)))
    except (TypeError, ValueError):
        importance = 0.5
    claim = {
        "id": new_id(),
        "namespace_key": namespace,
        "subject_entity_id": subject,
        "predicate": extracted.predicate,
        "value_json": value_json,
        "canonical_attribute": canonical_attribute,
        "fact_hash": compute_fact_hash(subject, extracted.predicate, extracted.value),
        "qualifiers_json": json.dumps(qualifiers, ensure_ascii=False, sort_keys=True),
        "conflict_key": compute_conflict_key(namespace, subject, canonical_attribute, qualifiers),
        "conflict_key_version": 2,
        "legacy_conflict_key": compute_legacy_conflict_key(namespace, subject, extracted.predicate, qualifiers),
        "valid_from": event.get("occurred_at", now),
        "recorded_from": now,
        "observed_at": event.get("occurred_at", now),
        "expires_at": expires_at,
        "volatility": extracted.volatility,
        "status": "active",
        "confidence": extracted.confidence,
        "scope": scope,
        "importance": importance,
        "access_count": 0,
        "last_accessed_at": None,
        "source_authority": authority or ("low" if event.get("actor_type") == "assistant" else "medium"),
        "extractor_version": "llm-v1" if event.get("extractor") == "llm" else "fake-v1",
        "embedding_model": getattr(embedder, "model", "fake"),
        "embedding_dim": embedder.dim,
    }
    started = time.perf_counter_ns()
    exact = claims.find_by_fact_hash(namespace, claim["fact_hash"])
    audit.emit(
        "dedup",
        "fact_hash_checked",
        "match" if exact else "new",
        event_id=event["id"],
        claim_id=claim["id"],
        related_claim_id=exact["id"] if exact else None,
        duration_us=(time.perf_counter_ns() - started) // 1000,
        detail={"fact_hash": claim["fact_hash"], "predicate": claim["predicate"]},
    )
    if exact:
        _link_event(evidence, exact["id"], event["id"])
        return exact["id"]
    existing = claims.find_by_conflict_key(claim["conflict_key"])
    superseded_old_id: str | None = None
    if existing:
        started = time.perf_counter_ns()
        resolution = ConflictResolver().resolve(existing[-1], {**claim, "qualifiers": qualifiers})
        audit.emit(
            "conflict",
            "resolved",
            resolution,
            event_id=event["id"],
            claim_id=claim["id"],
            related_claim_id=existing[-1]["id"],
            duration_us=(time.perf_counter_ns() - started) // 1000,
            detail={
                "conflict_key": claim["conflict_key"],
                "candidate_count": len(existing),
                "old": _summary(existing[-1]),
                "new": _summary(claim),
            },
        )
        if resolution == "entails":
            _link_event(evidence, existing[-1]["id"], event["id"])
            return existing[-1]["id"]
        if resolution == "state_change":
            claim["supersedes_id"] = existing[-1]["id"]
            superseded_old_id = existing[-1]["id"]
        elif resolution == "contradicts":
            claims.update_status(existing[-1]["id"], "disputed")
            claim["status"] = "disputed"
        elif resolution == "uncertain":
            claim["status"] = "candidate"
    else:
        audit.emit(
            "conflict",
            "not_applicable",
            "no_existing",
            event_id=event["id"],
            claim_id=claim["id"],
            detail={"conflict_key": claim["conflict_key"]},
        )
        claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
        started = time.perf_counter_ns()
        duplicate_id, _ = Deduplicator(claims, embedder).find_duplicate(claim)
        audit.emit(
            "dedup",
            "semantic_checked",
            "match" if duplicate_id else "new",
            event_id=event["id"],
            claim_id=claim["id"],
            related_claim_id=duplicate_id,
            duration_us=(time.perf_counter_ns() - started) // 1000,
            detail={"matched": duplicate_id is not None},
        )
        if duplicate_id:
            _link_event(evidence, duplicate_id, event["id"])
            return duplicate_id
    if "embedding_dense" not in claim:
        claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
    if superseded_old_id:
        connection.execute("BEGIN IMMEDIATE")
    try:
        claims.insert_claim(claim, commit=not superseded_old_id)
        if superseded_old_id:
            claims.supersede_with_inline(superseded_old_id, claim["id"], extracted.value, claim["valid_from"], now)
        _link_event(evidence, claim["id"], event["id"], commit=not superseded_old_id)
        if superseded_old_id:
            connection.commit()
    except Exception:
        if superseded_old_id:
            connection.rollback()
        raise
    return claim["id"]


def _link_event(repo: EvidenceRepository, claim_id: str, event_id: str, commit: bool = True) -> None:
    repo.add_link(
        {
            "id": new_id(),
            "derived_type": "claim",
            "derived_id": claim_id,
            "evidence_type": "event",
            "evidence_id": event_id,
            "relation": "derived_from",
            "weight": 1.0,
        },
        commit=commit,
    )


def _build_observation(connection: Any, conflict_key: str, now: str) -> None:
    claims = ClaimRepository(connection).find_by_conflict_key(conflict_key)
    evidence = EvidenceRepository(connection)
    for claim in claims:
        claim["evidence"] = evidence.get_links_for_derived("claim", claim["id"])
    built = ObservationBuilder().try_build(claims)
    if not built:
        return
    observation_id = new_id()
    DerivationRepository(connection).insert_observation(
        {
            "id": observation_id,
            "body": built["body"],
            "confidence": built["confidence"],
            "scope_json": json.dumps({"conflict_key": conflict_key}),
            "updated_at": now,
        }
    )
    for claim_id in built["claim_ids"]:
        evidence.add_link(
            {
                "id": new_id(),
                "derived_type": "observation",
                "derived_id": observation_id,
                "evidence_type": "claim",
                "evidence_id": claim_id,
                "relation": "supports",
                "weight": 1.0,
            }
        )
