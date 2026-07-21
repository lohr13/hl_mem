from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from hl_mem.ingest.embeddings import cosine_similarity
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.observability.audit import current_audit
from hl_mem.recall.reranker import RerankResult
from hl_mem.recall.conflict import ConflictResolver, compute_conflict_key
from hl_mem.recall.dedup import Deduplicator
from hl_mem.recall.observation import ObservationBuilder
from hl_mem.recall.ranking import (
    DEFAULT_WEIGHTS, blend_reranker_score, memory_features, memory_score,
)
from hl_mem.storage.repository import ClaimRepository, DerivationRepository, EvidenceRepository


def new_id() -> str:
    return uuid.uuid4().hex


def claim_text(claim: dict[str, Any]) -> str:
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"


def _recorded_epoch(claim: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(claim.get("recorded_from") or "")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _access_count(claim: dict[str, Any]) -> int:
    try:
        return max(0, int(claim.get("access_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def compute_fact_hash(subject: str, predicate: str, value: Any) -> str:
    stable_value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = unicodedata.normalize("NFKC", subject).strip()
    raw += unicodedata.normalize("NFKC", predicate).strip() + stable_value
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _summary(claim: Any) -> dict[str, Any]:
    value = claim.get("value_json", getattr(claim, "value", None))
    return {"subject": claim.get("subject_entity_id", getattr(claim, "subject", None)),
            "predicate": claim.get("predicate", getattr(claim, "predicate", None)),
            "value_hash": hashlib.sha256(str(value).encode()).hexdigest(),
            "confidence": claim.get("confidence", getattr(claim, "confidence", None)),
            "status": claim.get("status")}


def store_extracted(
    connection: Any, extracted: Any, event: dict[str, Any], now: str, embedder: Any,
    authority: str | None = None, ttl_days: int = 7,
) -> str:
    audit = current_audit()
    claims, evidence = ClaimRepository(connection), EvidenceRepository(connection)
    namespace, subject = event.get("tenant_id", "default"), extracted.subject
    qualifiers = extracted.qualifiers or {}
    value_json = json.dumps(extracted.value, ensure_ascii=False, sort_keys=True)
    scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
    expires_at = ((datetime.fromisoformat(now) + timedelta(days=ttl_days)).isoformat()
                  if extracted.volatility == "ephemeral" and scope == "temporal" else None)
    try:
        importance = min(1.0, max(0.0, float(extracted.importance)))
    except (TypeError, ValueError):
        importance = 0.5
    claim = {
        "id": new_id(), "namespace_key": namespace, "subject_entity_id": subject,
        "predicate": extracted.predicate, "value_json": value_json,
        "fact_hash": compute_fact_hash(subject, extracted.predicate, extracted.value),
        "qualifiers_json": json.dumps(qualifiers, ensure_ascii=False, sort_keys=True),
        "conflict_key": compute_conflict_key(namespace, subject, extracted.predicate, qualifiers),
        "valid_from": event.get("occurred_at", now), "recorded_from": now,
        "observed_at": event.get("occurred_at", now),
        "expires_at": expires_at,
        "volatility": extracted.volatility, "status": "active", "confidence": extracted.confidence,
        "scope": scope, "importance": importance, "access_count": 0, "last_accessed_at": None,
        "source_authority": authority or ("low" if event.get("actor_type") == "assistant" else "medium"),
        "extractor_version": "llm-v1" if event.get("extractor") == "llm" else "fake-v1",
        "embedding_model": getattr(embedder, "model", "fake"), "embedding_dim": embedder.dim,
    }
    started = time.perf_counter_ns()
    exact = claims.find_by_fact_hash(namespace, claim["fact_hash"])
    audit.emit("dedup", "fact_hash_checked", "match" if exact else "new",
               event_id=event["id"], claim_id=claim["id"],
               related_claim_id=exact["id"] if exact else None,
               duration_us=(time.perf_counter_ns() - started) // 1000,
               detail={"fact_hash": claim["fact_hash"], "predicate": claim["predicate"]})
    if exact:
        _link_event(evidence, exact["id"], event["id"])
        return exact["id"]
    existing = claims.find_by_conflict_key(claim["conflict_key"])
    if existing:
        started = time.perf_counter_ns()
        resolution = ConflictResolver().resolve(existing[-1], {**claim, "qualifiers": qualifiers})
        audit.emit("conflict", "resolved", resolution, event_id=event["id"],
                   claim_id=claim["id"], related_claim_id=existing[-1]["id"],
                   duration_us=(time.perf_counter_ns() - started) // 1000,
                   detail={"conflict_key": claim["conflict_key"],
                           "candidate_count": len(existing), "old": _summary(existing[-1]),
                           "new": _summary(claim)})
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
    else:
        audit.emit("conflict", "not_applicable", "no_existing", event_id=event["id"],
                   claim_id=claim["id"], detail={"conflict_key": claim["conflict_key"]})
        claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
        started = time.perf_counter_ns()
        duplicate_id, _ = Deduplicator(claims, embedder).find_duplicate(claim)
        audit.emit("dedup", "semantic_checked", "match" if duplicate_id else "new",
                   event_id=event["id"], claim_id=claim["id"], related_claim_id=duplicate_id,
                   duration_us=(time.perf_counter_ns() - started) // 1000,
                   detail={"matched": duplicate_id is not None})
        if duplicate_id:
            _link_event(evidence, duplicate_id, event["id"])
            return duplicate_id
    if "embedding_dense" not in claim:
        claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
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


def hybrid_claims(
    repo: ClaimRepository, query: str, query_blob: bytes, limit: int,
    as_of: str | None, reranker: Any = None, now: str | None = None,
) -> list[dict[str, Any]]:
    audit = current_audit()
    total_started = time.perf_counter_ns()
    candidate_limit = min(200, max(limit * 5, 50))
    started = time.perf_counter_ns()
    fts = repo.search_claims_fts(query, candidate_limit, as_of)
    fts_us = (time.perf_counter_ns() - started) // 1000
    started = time.perf_counter_ns()
    if hasattr(repo, "search_claims_vector"):
        dense = repo.search_claims_vector(query_blob, candidate_limit, as_of)
    else:
        dense = sorted(repo.list_embedded(as_of),
                       key=lambda claim: cosine_similarity(query_blob, claim["embedding_dense"]),
                       reverse=True)[:candidate_limit]
    dense_us = (time.perf_counter_ns() - started) // 1000
    scores: dict[str, float] = {}
    by_id = {claim["id"]: claim for claim in fts + dense}
    for ranked in (fts, dense):
        for rank, claim in enumerate(ranked, 1):
            scores[claim["id"]] = scores.get(claim["id"], 0) + 1 / (60 + rank)
    ranking_now = now or datetime.now(timezone.utc).isoformat()
    max_access = max((_access_count(claim) for claim in by_id.values()), default=0)
    feature_by_id = {claim_id: memory_features(claim, score / (2 / 61), max_access, ranking_now)
                     for claim_id, claim in by_id.items() for score in [scores[claim_id]]}
    pre_scores = {claim_id: memory_score(features)
                  for claim_id, features in feature_by_id.items()}
    ranked_claims = sorted(by_id.values(), key=lambda claim: (
        -pre_scores[claim["id"]], -feature_by_id[claim["id"]]["semantic"],
        -_recorded_epoch(claim), str(claim["id"])))
    rerank_us = 0
    reranked: list[tuple[int, float]] = []
    rerank_scores: dict[str, float] = {}
    if reranker is None:
        outcome, final = "disabled", ranked_claims[:limit]
    elif len(ranked_claims) <= 1:
        outcome, final = "skipped", ranked_claims[:limit]
    else:
        candidates = ranked_claims[:candidate_limit]
        started = time.perf_counter_ns()
        returned = reranker.rerank(
            query, [claim_text(claim) for claim in candidates], top_n=candidate_limit)
        rerank_us = (time.perf_counter_ns() - started) // 1000
        if isinstance(returned, RerankResult):
            reranked, result_status = returned.results, returned.outcome
        else:
            reranked = returned
            last = getattr(reranker, "last_outcome", None)
            result_status = (getattr(last, "outcome", None) or last or
                             ("empty" if not reranked else "success"))
        if reranked:
            valid = [(candidates[index], score) for index, score in reranked
                     if 0 <= index < len(candidates)]
            raw_rerank_scores = {claim["id"]: float(score) for claim, score in valid}
            rerank_scores = {claim["id"]: blend_reranker_score(score, feature_by_id[claim["id"]])
                             for claim, score in valid}
            final = sorted((claim for claim, _ in valid), key=lambda claim: (
                -rerank_scores[claim["id"]], -raw_rerank_scores[claim["id"]],
                -feature_by_id[claim["id"]]["semantic"],
                -_recorded_epoch(claim), str(claim["id"])))[:limit]
            outcome = "applied"
        else:
            outcome = "error_fallback" if result_status == "error" else "empty_fallback"
            final = ranked_claims[:limit]
    audit.emit("recall", "ranked", outcome,
               duration_us=(time.perf_counter_ns() - total_started) // 1000,
               detail={"query_hash": hashlib.sha256(query.encode()).hexdigest(), "limit": limit,
                       "as_of": as_of, "candidate_limit": candidate_limit,
                       "fts_ids": [item["id"] for item in fts],
                       "dense_ids": [item["id"] for item in dense],
                       "rrf_ids": [item["id"] for item in ranked_claims],
                       "returned_ids": [item["id"] for item in final],
                       "weights": DEFAULT_WEIGHTS,
                       "scores": {item["id"]: {**feature_by_id[item["id"]],
                                   "pre_rank": pre_scores[item["id"]],
                                   "final": rerank_scores.get(item["id"], pre_scores[item["id"]])
                                   if reranked else pre_scores[item["id"]]}
                                  for item in final},
                       "timing_us": {"fts": fts_us, "dense": dense_us,
                                     "reranker": rerank_us}})
    return final


def stale_observations(connection: Any, claim_id: str) -> None:
    rows = connection.execute(
        "SELECT derived_id FROM evidence_links WHERE derived_type='observation' "
        "AND evidence_type='claim' AND evidence_id=?", (claim_id,),
    ).fetchall()
    for row in rows:
        DerivationRepository(connection).update_status(row["derived_id"], "stale")
