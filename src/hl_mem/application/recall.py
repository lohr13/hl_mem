"""记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.ingest import new_id
from hl_mem.config import RECALL_DEFAULT_LIMIT, RECALL_VECTOR_SCAN_LIMIT
from hl_mem.domain.recall import RecallIntent, route_recall_intent
from hl_mem.experience.service import ExperienceService
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import (
    EmbedderProtocol,
    IntentRouterProtocol,
    RerankerProtocol,
    WeightedQuery,
)
from hl_mem.recall.procedure_pipeline import MemoryCandidate, recall_procedure
from hl_mem.recall.query_expansion import QueryExpander
from hl_mem.recall.recall_pipeline import RecallConfig, hybrid_claims, matching_policies
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.recall.trace import (
    ExperienceCandidateTrace,
    QueryExpansionTrace,
    SearchPhaseMetrics,
    SearchTrace,
    SearchTracer,
)
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.evidence import DerivationRepository, EvidenceRepository

LOGGER = logging.getLogger(__name__)
_SIDE_EFFECT_LOCK = threading.Lock()
_SIDE_EFFECT_HEALTH: dict[str, dict[str, int | str | None]] = {
    "access_record": {"failures": 0, "last_error": None},
    "feedback_record": {"failures": 0, "last_error": None},
    "audit_emit": {"failures": 0, "last_error": None},
}


def recall_side_effect_health() -> dict[str, dict[str, int | str | None]]:
    """返回召回副作用的进程级降级计数与最近错误类型。"""
    with _SIDE_EFFECT_LOCK:
        return {name: dict(status) for name, status in _SIDE_EFFECT_HEALTH.items()}


def _record_side_effect_failure(operation: str, error: Exception) -> None:
    """原子累计副作用失败状态。"""
    with _SIDE_EFFECT_LOCK:
        status = _SIDE_EFFECT_HEALTH[operation]
        status["failures"] = int(status["failures"] or 0) + 1
        status["last_error"] = type(error).__name__


def budget_pack(items: list[dict[str, Any]], token_budget: int) -> list[dict[str, Any]]:
    """按粗略中文 token 估算将候选顺序装入预算。"""
    if token_budget < 1:
        return []
    packed: list[dict[str, Any]] = []
    used = 0
    for item in items:
        data = item.get("data", item)
        text = str(data.get("text") or data.get("body") or data.get("procedure") or "")
        cost = max(1, (len(text) + 1) // 2)
        if used + cost > token_budget:
            continue
        packed.append(item)
        used += cost
        if used >= token_budget:
            break
    return packed


def budget_pack_by_type(
    candidates: list[MemoryCandidate],
    intent: RecallIntent,
    token_budget: int,
) -> tuple[list[MemoryCandidate], dict[str, int], int]:
    """按 Tool/Procedure 类型配额装箱，并将未使用预算按固定顺序回流。"""
    ratios = (
        {"policy": 0.35, "episode": 0.25, "trace": 0.15, "claim": 0.25}
        if intent is RecallIntent.TOOL
        else {"policy": 0.40, "episode": 0.20, "trace": 0.25, "claim": 0.15}
    )
    quotas = {kind: int(token_budget * ratio) for kind, ratio in ratios.items()}
    grouped = {kind: [item for item in candidates if item.memory_type == kind] for kind in ratios}
    packed: list[MemoryCandidate] = []
    used_by_type = {kind: 0 for kind in ratios}
    remaining = {kind: list(items) for kind, items in grouped.items()}

    def take(kind: str, allowance: int) -> int:
        used = 0
        retained: list[MemoryCandidate] = []
        for item in remaining[kind]:
            cost = max(1, (len(item.text) + 1) // 2)
            if used + cost <= allowance:
                packed.append(item)
                used += cost
            else:
                retained.append(item)
        remaining[kind] = retained
        used_by_type[kind] += used
        return used

    for kind, quota in quotas.items():
        take(kind, quota)
    total_used = sum(used_by_type.values())
    reflow_budget = max(0, token_budget - total_used)
    reflow_used = 0
    for kind in ("policy", "episode", "claim", "trace"):
        used = take(kind, reflow_budget)
        reflow_used += used
        reflow_budget -= used
    order = {id(item): index for index, item in enumerate(candidates)}
    packed.sort(key=lambda item: order[id(item)])
    return packed, quotas, reflow_used


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecallService:
    """记忆召回应用服务。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        embedder: EmbedderProtocol,
        reranker: RerankerProtocol | None = None,
        relation_config: RelationExpansionConfig | None = None,
        settings: Settings | None = None,
        query_expander: QueryExpander | None = None,
        intent_router: IntentRouterProtocol | None = None,
    ) -> None:
        self.connection = connection
        self.embedder = embedder
        self.reranker = reranker
        self.relation_config = relation_config or RelationExpansionConfig()
        self.settings = settings or Settings()
        self.query_expander = query_expander
        self.intent_router = intent_router

    def recall(
        self,
        query: str,
        limit: int = RECALL_DEFAULT_LIMIT,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        query_id: str | None = None,
        token_budget: int | None = None,
        context_mode: str | None = None,
        namespace: str = "default",
        debug: bool = False,
    ) -> dict[str, Any]:
        """执行混合召回并返回 claim、策略、证据及查询标识。"""
        total_started = time.perf_counter_ns()
        query_id = query_id or new_id()
        intent_source = "explicit" if intent is not None else "keyword"
        inferred_intent = route_recall_intent(query, as_of)
        if (
            intent is None
            and self.settings.procedure_recall_mode == "off"
            and inferred_intent
            in {
                RecallIntent.TOOL,
                RecallIntent.PROCEDURE,
            }
        ):
            inferred_intent = RecallIntent.CURRENT_STATE
            intent_source = "fallback"
        elif (
            intent is None
            and inferred_intent is RecallIntent.CURRENT_STATE
            and self.settings.procedure_recall_mode == "auto"
            and self.intent_router is not None
        ):
            try:
                decision = self.intent_router.route(
                    query,
                    allowed=(RecallIntent.CURRENT_STATE, RecallIntent.TOOL, RecallIntent.PROCEDURE),
                    timeout_seconds=self.settings.procedure_router_timeout_seconds,
                )
                if (
                    isinstance(decision.intent, RecallIntent)
                    and decision.intent in {RecallIntent.CURRENT_STATE, RecallIntent.TOOL, RecallIntent.PROCEDURE}
                    and decision.confidence >= self.settings.procedure_llm_threshold
                ):
                    inferred_intent = decision.intent
                    intent_source = "llm"
                else:
                    intent_source = "fallback"
            except (TimeoutError, ValueError, TypeError):
                intent_source = "fallback"
        selected_intent = RecallIntent(intent or inferred_intent)
        tracer = SearchTracer(
            SearchTrace(
                query_id=query_id,
                query_hash=hashlib.sha256(query.encode()).hexdigest(),
                intent=selected_intent.value,
                limit=limit,
                candidate_limit=min(
                    RECALL_VECTOR_SCAN_LIMIT,
                    max(limit * 5, self.settings.recall_candidate_floor),
                ),
                candidates={},
                phases=SearchPhaseMetrics(),
                intent_source=intent_source,
            )
        )
        expansion_deadline = time.monotonic() + self.settings.query_expansion_total_timeout_seconds
        weighted_queries = [WeightedQuery(query, "original", 1.0)]
        query_blobs = [self.embedder.embed_one(query)]

        def expand_for(trigger: str) -> tuple[list[WeightedQuery], list[bytes]]:
            trace_source = {
                "short_query": "llm_short",
                "coreference": "llm_coreference",
                "low_recall": "llm_low_recall",
                "always": "llm_short",
            }.get(trigger, "llm_short")
            if self.query_expander is None or time.monotonic() >= expansion_deadline:
                tracer.trace.expansion_trigger = trigger
                tracer.trace.expansions.append(QueryExpansionTrace.from_text("", trace_source, 0.6, outcome="timeout"))
                return [], []
            tracer.trace.expansion_trigger = trigger
            remaining = max(0.001, expansion_deadline - time.monotonic())
            result = self.query_expander.expand(
                query,
                intent=selected_intent,
                max_expansions=self.settings.query_expansion_max,
                timeout_seconds=min(self.settings.query_expansion_timeout_seconds, remaining),
                token_ceiling=self.settings.query_expansion_token_ceiling,
                source=trigger,
            )
            tracer.trace.expansion_total_tokens += result.input_tokens + result.output_tokens
            if not result.expansions:
                tracer.trace.expansions.append(
                    QueryExpansionTrace.from_text(
                        "",
                        trace_source,
                        0.6,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        latency_ms=result.latency_ms,
                        outcome=result.outcome,
                    )
                )
                return [], []
            additions = [WeightedQuery(item.text, item.source, item.weight) for item in result.expansions]
            for item in additions:
                tracer.trace.expansions.append(
                    QueryExpansionTrace.from_text(
                        item.text,
                        item.source,
                        item.weight,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        latency_ms=result.latency_ms,
                    )
                )
            return additions, [self.embedder.embed_one(item.text) for item in additions]

        initial_trigger = QueryExpander.trigger_for(query, self.settings.query_expansion_mode)
        if initial_trigger is not None and self.query_expander is not None:
            additions, blobs = expand_for(initial_trigger)
            weighted_queries.extend(additions)
            query_blobs.extend(blobs)

        low_recall_expander = None
        if self.settings.query_expansion_mode == "auto" and initial_trigger is None and self.query_expander is not None:

            def low_recall_expander(candidate_count: int) -> tuple[list[WeightedQuery], list[bytes]]:
                trigger = QueryExpander.trigger_for(
                    query,
                    "auto",
                    candidate_count=candidate_count,
                    candidate_floor=self.settings.query_expansion_candidate_floor,
                )
                return expand_for(trigger) if trigger is not None else ([], [])

        claims = hybrid_claims(
            ClaimRepository(self.connection),
            query,
            query_blobs[0],
            limit,
            as_of,
            self.reranker,
            intent=selected_intent,
            known_as_of=known_as_of,
            namespace=namespace,
            recall_config=RecallConfig(
                candidate_floor=self.settings.recall_candidate_floor,
                tag_boost_enabled=self.settings.tag_boost_enabled,
                tag_boost_weight=self.settings.tag_boost_weight,
                tag_channel_enabled=self.settings.tag_channel_enabled,
                tag_channel_weight=self.settings.tag_channel_weight,
                tag_candidate_limit=self.settings.tag_candidate_limit,
                preference_recency_boost=self.settings.preference_recency_boost,
                dedup_threshold=self.settings.recall_dedup_threshold,
                dedup_candidate_limit=self.settings.recall_dedup_candidate_limit,
            ),
            relation_connection=self.connection,
            relation_config=self.relation_config,
            tracer=tracer,
            weighted_queries=(
                weighted_queries
                if self.query_expander is not None and self.settings.query_expansion_mode != "off"
                else None
            ),
            query_blobs=(
                query_blobs if self.query_expander is not None and self.settings.query_expansion_mode != "off" else None
            ),
            low_recall_expander=low_recall_expander,
        )
        self._record_access(claims)
        assembly_started = time.perf_counter_ns()
        results = self._assemble_results(claims, namespace)
        if (
            selected_intent in {RecallIntent.TOOL, RecallIntent.PROCEDURE}
            and self.settings.procedure_recall_mode != "off"
        ):
            return self._recall_experience(
                query=query,
                selected_intent=selected_intent,
                namespace=namespace,
                limit=limit,
                query_id=query_id,
                claim_results=results,
                claim_scores={str(item["id"]): float(item.get("_score", 0.0)) for item in claims},
                token_budget=token_budget,
                context_mode=context_mode,
                debug=debug,
                tracer=tracer,
                total_started=total_started,
            )
        tracer.trace.phases.assembly_us = (time.perf_counter_ns() - assembly_started) // 1000
        observations = self._assemble_observations([claim["id"] for claim in claims])
        policies = matching_policies(
            ExperienceService(self.connection).list_policies("active", namespace=namespace),
            query,
        )
        self._record_feedback(results, observations, policies, query_id)
        response = {
            "results": results,
            "observations": observations,
            "policies": policies,
            "total": len(results),
            "query_id": query_id,
        }
        if context_mode == "packed":
            response["context"] = self._assemble_context(
                results,
                observations,
                policies,
                token_budget or self.settings.packed_context_token_budget,
            )
        if debug:
            tracer.trace.phases.total_us = (time.perf_counter_ns() - total_started) // 1000
            response["search_trace"] = tracer.to_dict()
        return response

    def _assemble_observations(self, claim_ids: list[str]) -> list[dict[str, Any]]:
        """查询与召回 Claim 相关的活跃派生记忆。"""
        return DerivationRepository(self.connection).list_active_for_claims(claim_ids)

    @staticmethod
    def _assemble_context(
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
        token_budget: int,
    ) -> dict[str, Any]:
        """按优先级跨类型组装受 token 预算约束的上下文。"""
        all_items: list[dict[str, Any]] = (
            [{"type": "claim", "data": item, "priority": 2} for item in claims]
            + [{"type": "observation", "data": item, "priority": 1} for item in observations]
            + [{"type": "policy", "data": item, "priority": 0} for item in policies]
        )
        all_items.sort(key=lambda item: -item["priority"] if isinstance(item.get("priority"), int) else 0)
        packed = budget_pack(all_items, token_budget)
        used = 0
        for item in packed:
            data = item.get("data", item)
            text = str(data.get("text") or data.get("body") or data.get("procedure") or "")
            used += max(1, (len(text) + 1) // 2)
        return {
            "context_items": packed,
            "used_tokens_estimate": used,
            "truncated": len(packed) < len(all_items),
        }

    def _record_access(self, claims: list[dict[str, Any]]) -> None:
        try:
            self._run_side_effect_with_retry(
                lambda: ClaimRepository(self.connection).record_access([claim["id"] for claim in claims], _now())
            )
        except Exception as error:
            _record_side_effect_failure("access_record", error)
            LOGGER.exception("recall side effect failed: access_record")
            self._emit_failure("access_record", "access_record_failed", error, len(claims))

    def _record_feedback(
        self,
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
        query_id: str,
    ) -> None:
        """为实际返回的三类记忆创建唯一 exposure，并把主键返回给调用方。"""
        try:
            recorded_at = _now()
            feedback: list[tuple[Any, ...]] = []
            for default_memory_type, items in (
                ("claim", claims),
                ("observation", observations),
                ("policy", policies),
            ):
                for rank, item in enumerate(items, 1):
                    memory_type = str(item.get("memory_type") or default_memory_type)
                    feedback_id = new_id()
                    item["feedback_id"] = feedback_id
                    feedback.append(
                        (
                            feedback_id,
                            query_id,
                            memory_type,
                            item["id"],
                            rank,
                            float(item.get("_score", item.get("score", 0.0))),
                            0,
                            None,
                            None,
                            recorded_at,
                        )
                    )
            self._run_side_effect_with_retry(lambda: ExperienceService(self.connection).record_feedback_batch(feedback))
        except Exception as error:
            _record_side_effect_failure("feedback_record", error)
            LOGGER.exception("recall side effect failed: feedback_record")
            self._emit_failure(
                "feedback_record", "feedback_record_failed", error, len(claims) + len(observations) + len(policies)
            )

    def _run_side_effect_with_retry(self, operation: Any) -> Any:
        """仅对 SQLite busy/locked 做 Settings 控制的有限退避重试。"""
        attempts = self.settings.recall_side_effect_max_attempts
        for attempt in range(attempts):
            try:
                return operation()
            except sqlite3.OperationalError as error:
                busy = "busy" in str(error).lower() or "locked" in str(error).lower()
                if not busy or attempt + 1 >= attempts:
                    raise
                time.sleep(self.settings.recall_side_effect_backoff_seconds * (attempt + 1))
        raise RuntimeError("unreachable recall side-effect retry state")

    @staticmethod
    def _emit_failure(operation: str, outcome: str, error: Exception, claim_count: int) -> None:
        try:
            current_audit().emit(
                "recall",
                operation,
                outcome,
                detail={"error_class": type(error).__name__, "claim_count": claim_count},
            )
        except Exception as audit_error:
            _record_side_effect_failure("audit_emit", audit_error)
            LOGGER.exception("recall failure audit emission failed")

    def _assemble_results(
        self,
        claims: list[dict[str, Any]],
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        if not claims:
            return []
        evidence_repo = EvidenceRepository(self.connection)
        claim_repo = ClaimRepository(self.connection)
        claim_ids = [claim["id"] for claim in claims]
        all_evidence = self._batch_evidence(evidence_repo, claim_ids)
        superseded_ids = [claim["superseded_by_id"] for claim in claims if claim.get("superseded_by_id")]
        replacement_map = self._batch_replacements(claim_repo, superseded_ids)
        relations_map = self._batch_relations(claim_ids)
        rivals_map = self._batch_rivals(claims, namespace)
        results: list[dict[str, Any]] = []
        for claim in claims:
            evidence = all_evidence.get(claim["id"], [])
            decoded = claim.get("value")
            text = (
                decoded.get("old_value")
                if isinstance(decoded, dict) and decoded.get("_type") == "superseded_value"
                else decoded
            )
            superseded_by_id = claim.get("superseded_by_id")
            replacement = replacement_map.get(str(superseded_by_id)) if superseded_by_id else None
            result: dict[str, Any] = {
                "type": "claim",
                "memory_type": "claim",
                "id": claim["id"],
                "text": text,
                "score": float(claim.get("_score", 0.0)),
                "features": dict(claim.get("_features") or {}),
                "status": claim["status"],
                "confidence": claim["confidence"],
                "canonical_attribute": claim.get("canonical_attribute"),
                "canonical_slot": claim.get("canonical_slot"),
                "topic_tags": list(claim.get("topic_tags") or []),
                "valid_from": claim["valid_from"],
                "replacement": replacement,
                "evidence": evidence,
                "relations": relations_map.get(claim["id"], []),
            }
            for field in ("occurred_start", "occurred_end", "entities"):
                if claim.get(field):
                    result[field] = claim[field]
            if claim["status"] == "disputed" and claim.get("conflict_key"):
                result["conflicts"] = rivals_map.get(claim["id"], [])
            results.append(result)
        return results

    def _recall_experience(
        self,
        *,
        query: str,
        selected_intent: RecallIntent,
        namespace: str,
        limit: int,
        query_id: str,
        claim_results: list[dict[str, Any]],
        claim_scores: dict[str, float],
        token_budget: int | None,
        context_mode: str | None,
        debug: bool,
        tracer: SearchTracer,
        total_started: int,
    ) -> dict[str, Any]:
        """执行 Experience 专用排序、统一 packing 与多类型 exposure。"""
        claim_candidates = [
            MemoryCandidate(
                "claim",
                str(item["id"]),
                str(item.get("text") or ""),
                claim_scores.get(str(item["id"]), 0.0),
                tuple(item.get("evidence") or ()),
                {"claim_score": claim_scores.get(str(item["id"]), 0.0)},
            )
            for item in claim_results
        ]
        candidates = recall_procedure(
            ExperienceService(self.connection),
            query,
            selected_intent,
            namespace,
            limit,
            candidate_limit=self.settings.procedure_candidate_limit,
            recent_outcome_window=self.settings.procedure_recent_outcome_window,
            outcome_half_life_days=self.settings.procedure_outcome_half_life_days,
            claim_candidates=claim_candidates,
        )
        budget = token_budget or self.settings.packed_context_token_budget
        packed, quotas, reflow = budget_pack_by_type(candidates, selected_intent, budget)
        selected = packed[:limit] if context_mode == "packed" else candidates[:limit]
        results = [
            {
                "type": item.memory_type,
                "memory_type": item.memory_type,
                "id": item.memory_id,
                "text": item.text,
                "score": item.score,
                "evidence": list(item.evidence),
                "features": item.features,
            }
            for item in selected
        ]
        self._record_feedback(results, [], [], query_id)
        tracer.trace.candidate_counts = {
            kind: sum(item.memory_type == kind for item in candidates)
            for kind in ("policy", "episode", "trace", "claim")
        }
        tracer.trace.quota_tokens = quotas
        tracer.trace.reflow_tokens = reflow
        selected_keys = {(item.memory_type, item.memory_id): rank for rank, item in enumerate(selected, 1)}
        tracer.trace.experience_candidates = [
            ExperienceCandidateTrace(
                memory_type=item.memory_type,
                memory_id=item.memory_id,
                source_rank=rank,
                features=item.features,
                final_rank=selected_keys.get((item.memory_type, item.memory_id)),
                included=(item.memory_type, item.memory_id) in selected_keys,
                filter_reasons=[] if (item.memory_type, item.memory_id) in selected_keys else ["limit_or_budget"],
            )
            for rank, item in enumerate(candidates, 1)
        ]
        response: dict[str, Any] = {
            "results": results,
            "observations": [],
            "policies": [item for item in results if item["memory_type"] == "policy"],
            "total": len(results),
            "query_id": query_id,
        }
        if context_mode == "packed":
            used = sum(max(1, (len(item.text) + 1) // 2) for item in selected)
            response["context"] = {
                "context_items": [
                    {"type": item.memory_type, "data": result} for item, result in zip(selected, results)
                ],
                "used_tokens_estimate": used,
                "truncated": len(selected) < len(candidates),
                "quota_tokens": quotas,
                "reflow_tokens": reflow,
            }
        if debug:
            tracer.trace.phases.total_us = (time.perf_counter_ns() - total_started) // 1000
            response["search_trace"] = tracer.to_dict()
        return response

    @staticmethod
    def _batch_evidence(
        evidence_repo: EvidenceRepository,
        claim_ids: list[str],
    ) -> dict[str, list[dict[str, str]]]:
        """批量加载 claim 的证据链接。"""
        return evidence_repo.batch_get_links_for_derived("claim", claim_ids)

    @staticmethod
    def _batch_replacements(
        claim_repo: ClaimRepository,
        superseded_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """批量加载被替代 claim 的替代项。"""
        claims = claim_repo.batch_get_claims(superseded_ids)
        return {
            claim_id: {
                "id": claim["id"],
                "text": claim["value"],
                "valid_from": claim["valid_from"],
            }
            for claim_id, claim in claims.items()
        }

    def _batch_relations(self, claim_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """批量加载 claim 的关系。"""
        from hl_mem.domain.relations import get_relations_batch

        return get_relations_batch(self.connection, claim_ids)

    def _batch_rivals(
        self,
        claims: list[dict[str, Any]],
        namespace: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """批量加载 disputed claim 的同 namespace 冲突项并精确映射。"""
        disputed_claims = [claim for claim in claims if claim["status"] == "disputed" and claim.get("conflict_key")]
        if not disputed_claims:
            return {}
        unique_keys = list(dict.fromkeys(claim["conflict_key"] for claim in disputed_claims))
        rivals_by_key = ClaimRepository(self.connection).find_disputed_rivals(unique_keys, namespace)
        return {
            claim["id"]: [rival for rival in rivals_by_key[claim["conflict_key"]] if rival["id"] != claim["id"]]
            for claim in disputed_claims
        }
