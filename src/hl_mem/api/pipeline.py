from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hl_mem.ingest.embeddings import cosine_similarity
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.recall.conflict import ConflictResolver, compute_conflict_key
from hl_mem.recall.dedup import Deduplicator
from hl_mem.recall.observation import ObservationBuilder
from hl_mem.storage.repository import ClaimRepository, DerivationRepository, EvidenceRepository


def new_id() -> str:
    return uuid.uuid4().hex


def claim_text(claim: dict[str, Any]) -> str:
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"


def store_extracted(
    connection: Any, extracted: Any, event: dict[str, Any], now: str, embedder: Any,
    authority: str | None = None,
) -> str:
    claims, evidence = ClaimRepository(connection), EvidenceRepository(connection)
    namespace, subject = event.get("tenant_id", "default"), extracted.subject
    qualifiers = extracted.qualifiers or {}
    value_json = json.dumps(extracted.value, ensure_ascii=False, sort_keys=True)
    claim = {
        "id": new_id(), "namespace_key": namespace, "subject_entity_id": subject,
        "predicate": extracted.predicate, "value_json": value_json,
        "qualifiers_json": json.dumps(qualifiers, ensure_ascii=False, sort_keys=True),
        "conflict_key": compute_conflict_key(namespace, subject, extracted.predicate, qualifiers),
        "valid_from": event.get("occurred_at", now), "recorded_from": now,
        "observed_at": event.get("occurred_at", now),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        if extracted.volatility == "ephemeral" else None,
        "volatility": extracted.volatility, "status": "active", "confidence": extracted.confidence,
        "source_authority": authority or ("low" if event.get("actor_type") == "assistant" else "medium"),
        "extractor_version": "llm-v1" if event.get("extractor") == "llm" else "fake-v1",
        "embedding_model": getattr(embedder, "model", "fake"), "embedding_dim": embedder.dim,
    }
    claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
    duplicate_id, _ = Deduplicator(claims, embedder).find_duplicate(claim)
    if duplicate_id:
        _link_event(evidence, duplicate_id, event["id"])
        return duplicate_id
    existing = claims.find_by_conflict_key(claim["conflict_key"])
    if existing:
        resolution = ConflictResolver().resolve(existing[-1], {**claim, "qualifiers": qualifiers})
        if resolution == "entails":
            _link_event(evidence, existing[-1]["id"], event["id"])
            return existing[-1]["id"]
        if resolution == "state_change":
            claims.supersede(existing[-1]["id"], claim["valid_from"])
            claim["supersedes_id"] = existing[-1]["id"]
        elif resolution == "contradicts":
            claims.update_status(existing[-1]["id"], "disputed")
            claim["status"] = "disputed"
        elif resolution == "uncertain":
            claim["status"] = "candidate"
    claims.insert_claim(claim)
    _link_event(evidence, claim["id"], event["id"])
    _build_observation(connection, claim["conflict_key"], now)
    return claim["id"]


def _link_event(repo: EvidenceRepository, claim_id: str, event_id: str) -> None:
    repo.add_link({"id": new_id(), "derived_type": "claim", "derived_id": claim_id,
                   "evidence_type": "event", "evidence_id": event_id,
                   "relation": "derived_from", "weight": 1.0})


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
        {"id": observation_id, "body": built["body"], "confidence": built["confidence"],
         "scope_json": json.dumps({"conflict_key": conflict_key}), "updated_at": now}
    )
    for claim_id in built["claim_ids"]:
        evidence.add_link({"id": new_id(), "derived_type": "observation", "derived_id": observation_id,
                           "evidence_type": "claim", "evidence_id": claim_id,
                           "relation": "supports", "weight": 1.0})


def hybrid_claims(repo: ClaimRepository, query: str, query_blob: bytes, limit: int, as_of: str | None):
    fts = repo.search_claims_fts(query, limit, as_of)
    dense = sorted(repo.list_embedded(as_of),
                   key=lambda claim: cosine_similarity(query_blob, claim["embedding_dense"]), reverse=True)[:limit]
    scores: dict[str, float] = {}
    by_id = {claim["id"]: claim for claim in fts + dense}
    for ranked in (fts, dense):
        for rank, claim in enumerate(ranked, 1):
            scores[claim["id"]] = scores.get(claim["id"], 0) + 1 / (60 + rank)
    return [by_id[key] for key in sorted(scores, key=scores.get, reverse=True)[:limit]]


def stale_observations(connection: Any, claim_id: str) -> None:
    rows = connection.execute(
        "SELECT derived_id FROM evidence_links WHERE derived_type='observation' "
        "AND evidence_type='claim' AND evidence_id=?", (claim_id,),
    ).fetchall()
    for row in rows:
        DerivationRepository(connection).update_status(row["derived_id"], "stale")
