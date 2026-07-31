"""记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from hl_mem.application.context_packet import (
    Answerability,
    ContextPacketAssembler,
    MemoryType,
    RetrievalBundle,
    RetrievalBundleItem,
    estimate_tokens,
    pack_retrieval_bundle,
)
from hl_mem.application.ingest import new_id
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
from hl_mem.recall.relevance import (
    enforce_relevance,
    evaluate_relevance,
    should_enforce_relevance,
)
from hl_mem.recall.trace import (
    ExperienceCandidateTrace,
    QueryExpansionTrace,
    SearchPhaseMetrics,
    SearchTrace,
    SearchTracer,
)
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import DerivationRepository, EvidenceRepository

LOGGER = logging.getLogger(__name__)
_RESPONSE_FORMATS = frozenset({"legacy", "context_packet", "both"})
_SIDE_EFFECT_LOCK = threading.Lock()
_SIDE_EFFECT_HEALTH: dict[str, dict[str, int | str | None]] = {
    "access_record": {"failures": 0, "last_error": None},
    "feedback_record": {"failures": 0, "last_error": None},
    "audit_emit": {"failures": 0, "last_error": None},
}


def _claim_index_text(claim: dict[str, Any]) -> str:
    """只暴露 Claim 持久化的索引文本。"""
    index_text = claim.get("index_text")
    return index_text if isinstance(index_text, str) else ""


def _context_text(memory_type: str, data: Mapping[str, Any]) -> str:
    """只从公开投影提取 packet/context 文本，Claim 不读取 value_json。"""
    if memory_type == "claim":
        text = data.get("text")
        return text if isinstance(text, str) else ""
    value = data.get("text") or data.get("body") or data.get("procedure") or ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        memory_type = str(item.get("type") or data.get("memory_type") or data.get("type") or "")
        text = _context_text(memory_type, data)
        cost = estimate_tokens(text)
        if used + cost > token_budget:
            continue
        packed.append(item)
        used += cost
        if used >= token_budget:
            break
    return packed


def _session_context(
    connection: sqlite3.Connection,
    namespace: str,
    session_id: str,
    *,
    max_events: int,
    token_budget: int,
) -> tuple[tuple[tuple[str, str], ...], bool, str | None, str]:
    """读取并按粗略 token 预算装入同命名空间会话的用户/助手文本。"""
    before = {"occurred_at": "\U0010ffff", "id": "\U0010ffff"}
    events = EventRepository(connection).get_recent_events(
        namespace,
        session_id,
        before,
        max_events + 1,
        ("user", "assistant"),
    )
    truncated = len(events) > max_events
    selected: list[tuple[str, str]] = []
    used = 0
    for event in events[:max_events]:
        role = str(event.get("actor_type") or "")
        if role not in {"user", "assistant"}:
            continue
        try:
            content = json.loads(str(event.get("content_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        text = content if isinstance(content, str) else content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str) or not text.strip():
            continue
        normalized = text.strip()
        cost = max(1, (len(normalized) + 1) // 2)
        if used + cost > token_budget:
            truncated = True
            continue
        selected.append((role, normalized))
        used += cost
    selected.reverse()
    context = tuple(selected)
    if not context:
        return (), truncated, None, "empty"
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return context, truncated, hashlib.sha256(serialized.encode("utf-8")).hexdigest(), "ok"


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
        limit: int | None = None,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        query_id: str | None = None,
        token_budget: int | None = None,
        context_mode: str | None = None,
        namespace: str = "default",
        session_id: str | None = None,
        debug: bool = False,
        response_format: str = "legacy",
    ) -> dict[str, Any]:
        """执行混合召回并返回 claim、策略、证据及查询标识。"""
        if response_format not in _RESPONSE_FORMATS:
            raise ValueError(f"unsupported response_format: {response_format}")
        limit = self.settings.recall_default_limit if limit is None else limit
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
                    allowed=(
                        RecallIntent.CURRENT_STATE,
                        RecallIntent.TOOL,
                        RecallIntent.PROCEDURE,
                    ),
                    timeout_seconds=self.settings.procedure_router_timeout_seconds,
                )
                if (
                    isinstance(decision.intent, RecallIntent)
                    and decision.intent
                    in {
                        RecallIntent.CURRENT_STATE,
                        RecallIntent.TOOL,
                        RecallIntent.PROCEDURE,
                    }
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
                    self.settings.recall_vector_scan_limit,
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
            tracer.trace.expansion_trigger = trigger
            if self.query_expander is None or time.monotonic() >= expansion_deadline:
                tracer.trace.expansions.append(QueryExpansionTrace.from_text("", trace_source, 0.6, outcome="timeout"))
                return [], []
            session_context: tuple[tuple[str, str], ...] = ()
            tracer.trace.context_outcome = "disabled"
            if trigger == "coreference" and self.settings.query_context_mode == "coreference":
                if not session_id:
                    tracer.trace.context_outcome = "missing_session"
                    return [], []
                if time.monotonic() >= expansion_deadline:
                    tracer.trace.context_outcome = "deadline_exhausted"
                    return [], []
                try:
                    session_context, context_truncated, context_hash, context_outcome = _session_context(
                        self.connection,
                        namespace,
                        session_id,
                        max_events=self.settings.query_context_max_events,
                        token_budget=self.settings.query_context_token_budget,
                    )
                except Exception as error:
                    LOGGER.warning("session context read failed: %s", type(error).__name__)
                    tracer.trace.context_outcome = "read_error"
                    return [], []
                tracer.trace.context_event_count = len(session_context)
                tracer.trace.context_truncated = context_truncated
                tracer.trace.context_hash = context_hash
                tracer.trace.context_outcome = context_outcome
                if not session_context:
                    return [], []
                if time.monotonic() >= expansion_deadline:
                    tracer.trace.context_outcome = "deadline_exhausted"
                    return [], []
            remaining = max(0.001, expansion_deadline - time.monotonic())
            result = self.query_expander.expand(
                query,
                intent=selected_intent,
                max_expansions=self.settings.query_expansion_max,
                timeout_seconds=min(self.settings.query_expansion_timeout_seconds, remaining),
                token_ceiling=self.settings.query_expansion_token_ceiling,
                source=trigger,
                session_context=session_context,
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
                        error_class=result.error_class,
                        attempts=result.attempts,
                        http_status=result.http_status,
                        provider_code=result.provider_code,
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

            def low_recall_expander(
                candidate_count: int,
            ) -> tuple[list[WeightedQuery], list[bytes]]:
                trigger = QueryExpander.trigger_for(
                    query,
                    "auto",
                    candidate_count=candidate_count,
                    candidate_floor=self.settings.query_expansion_candidate_floor,
                )
                return expand_for(trigger) if trigger is not None else ([], [])

        claims = hybrid_claims(
            ClaimRepository(
                self.connection,
                vector_batch_size=self.settings.vector_batch_size,
                settings=self.settings,
            ),
            query,
            query_blobs[0],
            limit,
            as_of,
            self.reranker,
            intent=selected_intent,
            known_as_of=known_as_of,
            namespace=namespace,
            recall_config=RecallConfig(
                vector_scan_limit=self.settings.recall_vector_scan_limit,
                candidate_floor=self.settings.recall_candidate_floor,
                tag_boost_enabled=self.settings.tag_boost_enabled,
                tag_boost_weight=self.settings.tag_boost_weight,
                tag_channel_enabled=self.settings.tag_channel_enabled,
                tag_channel_weight=self.settings.tag_channel_weight,
                tag_candidate_limit=self.settings.tag_candidate_limit,
                preference_recency_boost=self.settings.preference_recency_boost,
                dedup_threshold=self.settings.recall_dedup_threshold,
                dedup_candidate_limit=self.settings.recall_dedup_candidate_limit,
                feedback_min_samples=self.settings.feedback_min_samples,
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
        enforce_enabled = False
        if self.settings.relevance_gate_mode in {"observe", "enforce"}:
            enforce_enabled = should_enforce_relevance(
                self.settings.relevance_gate_mode,
                selected_intent.value,
                self.settings.relevance_intents,
            )
            if enforce_enabled:
                claims = enforce_relevance(
                    claims,
                    tracer,
                    reranker_floor=self.settings.relevance_reranker_floor,
                    dense_floor=self.settings.relevance_dense_floor,
                    relative_drop_threshold=self.settings.relevance_relative_drop,
                    keep_top1=self.settings.relevance_keep_top1,
                )
            else:
                evaluate_relevance(
                    [str(claim["id"]) for claim in claims],
                    tracer,
                    reranker_floor=self.settings.relevance_reranker_floor,
                    dense_floor=self.settings.relevance_dense_floor,
                    relative_drop_threshold=self.settings.relevance_relative_drop,
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
                response_format=response_format,
                tracer=tracer,
                total_started=total_started,
            )
        tracer.trace.phases.assembly_us = (time.perf_counter_ns() - assembly_started) // 1000
        observations = self._assemble_observations([claim["id"] for claim in claims])
        policies = matching_policies(
            ExperienceService(self.connection).list_policies("active", namespace=namespace),
            query,
        )
        policy_evidence = EvidenceRepository(self.connection).batch_get_links_for_derived(
            "policy",
            [str(policy["id"]) for policy in policies],
        )
        for policy in policies:
            policy["evidence"] = policy_evidence.get(str(policy["id"]), [])
        answerability = self._answerability(
            claims,
            tracer,
            relevance_enforced=enforce_enabled,
        )
        packet_candidates = self._context_candidates(results, observations, policies)
        retrieval_bundle = self._bundle_from_context_items(
            query_id,
            cast(Answerability, answerability),
            packet_candidates,
        )
        if response_format != "legacy" or context_mode == "packed":
            budget = token_budget or self.settings.packed_context_token_budget
            bundle = pack_retrieval_bundle(retrieval_bundle, budget)
            packet_context = self._assemble_context(
                results,
                observations,
                policies,
                budget,
            )
        else:
            used = sum(estimate_tokens(item.text) for item in retrieval_bundle.items)
            bundle = RetrievalBundle(
                query_id=retrieval_bundle.query_id,
                answerability=retrieval_bundle.answerability,
                items=retrieval_bundle.items,
                used_tokens_estimate=used,
                truncated=False,
            )
            packet_context = {
                "context_items": packet_candidates,
                "used_tokens_estimate": used,
                "truncated": False,
            }
        materialized_packet = self._materialize_context_packet(bundle)
        if response_format != "context_packet":
            self._attach_packet_feedback(packet_context, materialized_packet)
        context_packet = materialized_packet if response_format != "legacy" else None
        response = {
            "results": results,
            "observations": observations,
            "policies": policies,
            "total": len(results),
            "query_id": query_id,
            "answerability": answerability,
        }
        if context_mode == "packed":
            response["context"] = packet_context
        if debug:
            tracer.trace.phases.total_us = (time.perf_counter_ns() - total_started) // 1000
            response["search_trace"] = tracer.to_dict()
        if response_format == "context_packet":
            return {"context_packet": context_packet}
        if response_format == "both":
            response["context_packet"] = context_packet
        return response

    @staticmethod
    def _answerability(
        claims: list[dict[str, Any]],
        tracer: SearchTracer,
        *,
        relevance_enforced: bool = False,
    ) -> str:
        """按最终候选及 relevance gate 结果给出 answerability。"""
        if not claims:
            tracer.trace.answerability = "no_evidence"
            return "no_evidence"
        top = claims[0]
        trace = tracer.trace.candidates.get(str(top["id"]))
        if relevance_enforced and (trace is None or trace.relevance_decision != "relevant"):
            tracer.trace.answerability = "low_confidence"
            return "low_confidence"
        fts_hit = bool(trace and "fts" in trace.channels)
        dense_score = float(trace.channel_scores.get("dense", 0.0)) if trace else 0.0
        slot = str(top.get("canonical_slot") or "")
        high_confidence_slot = slot.startswith(("identity.", "config.", "preference."))
        scores = [float(item.get("_score", 0.0)) for item in claims[:2]]
        margin_ok = len(scores) == 1 or scores[0] - scores[1] > 0.05
        reranker_ok = trace is None or trace.rerank_score is None or trace.rerank_score > 0.4
        has_signal = fts_hit or dense_score > 0.3 or high_confidence_slot
        answerability = "supported" if has_signal and margin_ok and reranker_ok else "low_confidence"
        tracer.trace.answerability = answerability
        return answerability

    def _assemble_observations(self, claim_ids: list[str]) -> list[dict[str, Any]]:
        """查询与召回 Claim 相关的活跃派生记忆。"""
        observations = DerivationRepository(self.connection).list_active_for_claims(claim_ids)
        if not observations:
            return []
        evidence_repo = EvidenceRepository(self.connection)
        by_kind: dict[str, list[str]] = {}
        for observation in observations:
            by_kind.setdefault(str(observation.get("kind") or "observation"), []).append(str(observation["id"]))
        evidence: dict[str, list[dict[str, str]]] = {}
        for kind, derived_ids in by_kind.items():
            evidence.update(evidence_repo.batch_get_links_for_derived(kind, derived_ids))
        for observation in observations:
            observation["evidence"] = evidence.get(str(observation["id"]), [])
        return observations

    @staticmethod
    def _context_candidates(
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按稳定优先级返回尚未预算裁剪的 context candidates。"""
        all_items: list[dict[str, Any]] = (
            [{"type": "claim", "data": item, "priority": 2} for item in claims]
            + [{"type": "observation", "data": item, "priority": 1} for item in observations]
            + [{"type": "policy", "data": item, "priority": 0} for item in policies]
        )
        all_items.sort(key=lambda item: -item["priority"] if isinstance(item.get("priority"), int) else 0)
        return all_items

    @staticmethod
    def _assemble_context(
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
        token_budget: int,
    ) -> dict[str, Any]:
        """按优先级跨类型组装受 token 预算约束的上下文。"""
        all_items = RecallService._context_candidates(claims, observations, policies)
        packed = budget_pack(all_items, token_budget)
        used = 0
        for item in packed:
            data = item.get("data", item)
            memory_type = str(item.get("type") or data.get("memory_type") or data.get("type") or "")
            used += estimate_tokens(_context_text(memory_type, data))
        return {
            "context_items": packed,
            "used_tokens_estimate": used,
            "truncated": len(packed) < len(all_items),
        }

    @staticmethod
    def _bundle_from_context_items(
        query_id: str,
        answerability: Answerability,
        context_items: list[dict[str, Any]],
    ) -> RetrievalBundle:
        """把有序 context candidates 投影为可缓存、无 receipt 的 RetrievalBundle。"""
        items: list[RetrievalBundleItem] = []
        for wrapped in context_items:
            data = wrapped.get("data", wrapped)
            memory_type = str(wrapped.get("type") or data.get("memory_type") or data.get("type") or "")
            raw_evidence = data.get("evidence") or []
            evidence = tuple(reference for reference in raw_evidence if isinstance(reference, Mapping))
            raw_score = data.get("_score", data.get("score"))
            score: float | None
            try:
                score = float(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                score = None
            items.append(
                RetrievalBundleItem(
                    cast(MemoryType, memory_type),
                    str(data["id"]),
                    _context_text(memory_type, data),
                    evidence,
                    score,
                )
            )
        return RetrievalBundle(
            query_id=query_id,
            answerability=answerability,
            items=tuple(items),
        )

    def _materialize_context_packet(self, bundle: RetrievalBundle) -> dict[str, Any]:
        """有限重试 exposure 批量落库，并把最终失败收敛为 degraded packet。"""
        service = ExperienceService(self.connection)
        assembler = ContextPacketAssembler(
            service,
            persist_exposures=lambda exposures: self._run_side_effect_with_retry(
                lambda: service.record_exposure_batch(exposures)
            ),
        )
        packet = assembler.assemble(bundle)
        if assembler.last_error is not None:
            _record_side_effect_failure("feedback_record", assembler.last_error)
            self._emit_failure(
                "feedback_record",
                "feedback_record_failed",
                assembler.last_error,
                len(bundle.items),
            )
        return packet

    @staticmethod
    def _attach_packet_feedback(
        context: Mapping[str, Any],
        packet: Mapping[str, Any],
    ) -> None:
        """both 模式仅在 exposure 已确认时把同一 receipt 附到 legacy item。"""
        if packet.get("feedback_state") != "available":
            return
        context_items = context.get("context_items") or []
        packet_items = packet.get("items") or []
        for wrapped, packet_item in zip(context_items, packet_items):
            data = wrapped.get("data", wrapped)
            data["feedback_id"] = packet_item["feedback_id"]

    def _record_access(self, claims: list[dict[str, Any]]) -> None:
        try:
            self._run_side_effect_with_retry(
                lambda: ClaimRepository(self.connection).record_access([claim["id"] for claim in claims], _now())
            )
        except Exception as error:
            _record_side_effect_failure("access_record", error)
            LOGGER.exception("recall side effect failed: access_record")
            self._emit_failure("access_record", "access_record_failed", error, len(claims))

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
                detail={
                    "error_class": type(error).__name__,
                    "claim_count": claim_count,
                },
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
            superseded_by_id = claim.get("superseded_by_id")
            replacement = replacement_map.get(str(superseded_by_id)) if superseded_by_id else None
            result: dict[str, Any] = {
                "type": "claim",
                "memory_type": "claim",
                "id": claim["id"],
                "text": _claim_index_text(claim),
                "score": float(claim.get("_score", 0.0)),
                "score_path": str(claim.get("_score_path", "reranker_fallback")),
                "reranker_raw_score": claim.get("_reranker_raw_score"),
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
        response_format: str,
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
        packet_selected = packed[:limit]
        selected = packet_selected if context_mode == "packed" else candidates[:limit]
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
        answerability = self._answerability([], tracer) if not candidates else "supported"
        materialized_selection = packet_selected if response_format != "legacy" else selected
        bundle = RetrievalBundle(
            query_id=query_id,
            answerability=cast(Answerability, answerability),
            items=tuple(
                RetrievalBundleItem(
                    cast(MemoryType, item.memory_type),
                    item.memory_id,
                    item.text,
                    tuple(reference for reference in item.evidence if isinstance(reference, Mapping)),
                    item.score,
                )
                for item in materialized_selection
            ),
            used_tokens_estimate=sum(estimate_tokens(item.text) for item in materialized_selection),
            truncated=len(materialized_selection) < len(candidates),
        )
        materialized_packet = self._materialize_context_packet(bundle)
        if response_format != "context_packet" and materialized_packet["feedback_state"] == "available":
            feedback_by_memory = {
                (item["type"], item["id"]): item["feedback_id"] for item in materialized_packet["items"]
            }
            for result in results:
                feedback_id = feedback_by_memory.get((result["memory_type"], result["id"]))
                if feedback_id is not None:
                    result["feedback_id"] = feedback_id
        context_packet = materialized_packet if response_format != "legacy" else None
        tracer.trace.candidate_counts = {
            kind: sum(item.memory_type == kind for item in candidates)
            for kind in ("policy", "episode", "trace", "claim")
        }
        tracer.trace.quota_tokens = quotas
        tracer.trace.reflow_tokens = reflow
        traced_selection = materialized_selection
        selected_keys = {(item.memory_type, item.memory_id): rank for rank, item in enumerate(traced_selection, 1)}
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
            "answerability": answerability,
        }
        if context_mode == "packed":
            used = sum(estimate_tokens(item.text) for item in selected)
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
        if response_format == "context_packet":
            return {"context_packet": context_packet}
        if response_format == "both":
            response["context_packet"] = context_packet
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
                "text": _claim_index_text(claim),
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
