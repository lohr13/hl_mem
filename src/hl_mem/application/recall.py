"""记忆召回应用服务。执行 FTS + 向量 + reranker 混合召回，管理访问记录和反馈。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, cast

from hl_mem.application import recall_delivery as _recall_delivery
from hl_mem.application._procedure_recall_flow import ProcedureRecallFlow as _ProcedureRecallFlow
from hl_mem.application.answerability import Answerability
from hl_mem.application.context_packet import (
    ContextPacketAssembler,
    RetrievalBundle,
    apply_freshness_decisions,
    estimate_tokens,
    pack_retrieval_bundle,
    project_claim_relation,
    render_memory_text,
    retrieval_bundle_to_dict,
)
from hl_mem.application.ingest import new_id
from hl_mem.application.recall_access import is_access_recording_eligible
from hl_mem.application.recall_delivery import (
    assemble_context,
    bundle_from_context_items,
    context_candidates,
    context_from_packed_bundle,
)
from hl_mem.application.recall_enrichment import assemble_observations as assemble_recall_observations
from hl_mem.application.recall_enrichment import assemble_results as assemble_recall_results
from hl_mem.application.recall_side_effects import RecallSideEffectSink
from hl_mem.application.resurrection import ResurrectionService
from hl_mem.domain.recall import RecallIntent, route_recall_intent
from hl_mem.experience.service import ExperienceService
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import (
    EmbedderProtocol,
    IntentRouterProtocol,
    RerankerProtocol,
    WeightedQuery,
    embed_query,
)
from hl_mem.recall.echo_suppression import EchoRequest, EchoSuppressionPolicy
from hl_mem.recall.entity_query import prepare_entity_query, prepare_wide_query
from hl_mem.recall.freshness_annotation import (
    DEFAULT_FRESHNESS_ANNOTATION_METRICS,
    FreshnessAnnotationPolicy,
    FreshnessEvaluation,
    FreshnessItem,
    FreshnessRequest,
)
from hl_mem.recall.injection import InjectionContext
from hl_mem.recall.procedure_pipeline import recall_procedure as recall_procedure
from hl_mem.recall.query_expansion import QueryExpander
from hl_mem.recall.recall_pipeline import RecallConfig, hybrid_claims, matching_policies
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.recall.relevance import (
    enforce_relevance,
    evaluate_relevance,
    should_enforce_relevance,
)
from hl_mem.recall.staged_pipeline import EntityScopeFallback
from hl_mem.recall.trace import QueryExpansionTrace, SearchPhaseMetrics, SearchTrace, SearchTracer
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository

LOGGER = logging.getLogger(__name__)
budget_pack_by_type = _recall_delivery.budget_pack_by_type
_RESPONSE_FORMATS = frozenset({"legacy", "context_packet", "both", "retrieval_bundle"})
_SIDE_EFFECT_LOCK = threading.Lock()
_SIDE_EFFECT_HEALTH: dict[str, dict[str, int | str | None]] = {
    "access_record": {"failures": 0, "last_error": None},
    "feedback_record": {"failures": 0, "last_error": None},
    "audit_emit": {"failures": 0, "last_error": None},
}


@dataclass(frozen=True)
class RecallRequest:
    query: str
    limit: int | None
    as_of: str | None
    intent: RecallIntent | str | None
    known_as_of: str | None
    query_id: str | None
    token_budget: int | None
    context_mode: str | None
    namespace: str
    session_id: str | None
    debug: bool
    response_format: str
    ranking_now: str | None
    injection_context: InjectionContext | None


def auxiliary_context_is_current(*, as_of: str | None, known_as_of: str | None) -> bool:
    return as_of is None and known_as_of is None


@dataclass(frozen=True)
class _RecallSession:
    request: RecallRequest
    limit: int
    query_id: str
    selected_intent: RecallIntent
    injection_context: InjectionContext
    tracer: SearchTracer
    total_started: int


@dataclass(frozen=True)
class EnrichedSelection:
    claims: list[dict[str, Any]]
    results: list[dict[str, Any]]
    relevance_enforced: bool
    assembly_started: int
    observations: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    packet_candidates: list[dict[str, Any]] = field(default_factory=list)
    answerability: Answerability = "no_evidence"


_LowRecallExpander = Callable[[int, int], tuple[list[WeightedQuery], list[bytes]]]


def _freshness_item(memory_type: str, item_id: str, text: str, metadata: Mapping[str, Any]) -> FreshnessItem:
    raw_tags = metadata.get("topic_tags") or ()
    tags = tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, (list, tuple)) else ()
    return FreshnessItem(
        item_id=item_id,
        memory_type=memory_type,
        text=text,
        recorded_from=str(metadata["recorded_from"]) if metadata.get("recorded_from") else None,
        canonical_slot=str(metadata["canonical_slot"]) if metadata.get("canonical_slot") else None,
        canonical_attribute=(str(metadata["canonical_attribute"]) if metadata.get("canonical_attribute") else None),
        topic_tags=tags,
    )


def _record_freshness_evaluation(
    evaluation: FreshnessEvaluation,
    tracer: SearchTracer,
    request: FreshnessRequest,
) -> None:
    DEFAULT_FRESHNESS_ANNOTATION_METRICS.record(evaluation)
    tracer.trace.injection["freshness_annotation"] = evaluation.summary()
    for decision in evaluation.decisions:
        if not decision.eligible:
            continue
        current_audit().emit(
            "recall",
            "freshness_annotation",
            "rendered" if evaluation.mode == "render" else "would_render",
            query_id=tracer.trace.query_id,
            claim_id=decision.item_id,
            detail={
                "render_kind": decision.render_kind,
                "reason": decision.reason,
                "age_bucket": decision.age_bucket,
                "added_tokens": decision.added_token_estimate,
                "policy_version": decision.policy_version,
                "experiment_variant": request.experiment_variant,
            },
        )


def _freshness_pack_bundle(
    bundle: RetrievalBundle,
    items: list[FreshnessItem],
    *,
    policy: FreshnessAnnotationPolicy,
    request: FreshnessRequest,
    token_budget: int,
    tracer: SearchTracer,
) -> RetrievalBundle:
    evaluation = policy.evaluate(items, request)
    control = pack_retrieval_bundle(bundle, token_budget)
    hypothetical = pack_retrieval_bundle(
        apply_freshness_decisions(bundle, evaluation, force_render=True),
        token_budget,
    )
    control_keys = [(item.type, item.id) for item in control.items]
    hypothetical_keys = [(item.type, item.id) for item in hypothetical.items]
    evaluation = evaluation.with_truncation_changed(
        control_keys != hypothetical_keys or control.truncated != hypothetical.truncated
    )
    _record_freshness_evaluation(evaluation, tracer, request)
    return hypothetical if policy.mode == "render" else control


def _claim_index_text(claim: dict[str, Any]) -> str:
    """只暴露 Claim 持久化的索引文本。"""
    index_text = claim.get("index_text")
    return index_text if isinstance(index_text, str) else ""


def _context_text(memory_type: str, data: Mapping[str, Any]) -> str:
    """只从公开投影提取 packet/context 文本，Claim 不读取 value_json。"""
    if memory_type == "claim":
        text = data.get("text")
        return (
            render_memory_text(
                text,
                role=data.get("role"),
                action=data.get("action"),
                object_=data.get("object"),
            )
            if isinstance(text, str)
            else ""
        )
    value = data.get("text") or data.get("body") or data.get("procedure") or ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _claim_relation(claim: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Compatibility wrapper around the shared semantic relation projection."""

    return project_claim_relation(claim)


def recall_side_effect_health(
    connection: sqlite3.Connection | None = None,
    dispatcher: Any = None,
) -> dict[str, dict[str, int | str | None]]:
    """返回召回副作用的进程级降级计数与最近错误类型。"""
    with _SIDE_EFFECT_LOCK:
        fallback = {name: dict(status) for name, status in _SIDE_EFFECT_HEALTH.items()}
    if dispatcher is None:
        return fallback
    result = cast(dict[str, dict[str, int | str | None]], dispatcher.health(connection))
    for operation, status in fallback.items():
        result[operation]["failures"] = int(result[operation]["failures"] or 0) + int(status["failures"] or 0)
        if status["last_error"] is not None:
            result[operation]["last_error"] = status["last_error"]
    return result


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _QueryExpansionSession:
    """一次 recall 的 query expansion 状态与 deadline。"""

    def __init__(self, service: RecallService, recall: _RecallSession) -> None:
        self.service = service
        self.recall = recall
        self.deadline = time.monotonic() + service.settings.query_expansion_total_timeout_seconds
        self.tracer = recall.tracer
        self.session_context: tuple[tuple[str, str], ...] = ()
        request = recall.request
        self.entity_plan, weighted_query, query_blob = prepare_entity_query(
            service.connection,
            service.embedder,
            request.query,
            request.namespace,
            service.settings.entity_constraint_mode,
            dense_enabled=service.settings.recall_dense_enabled,
        )
        self.entity_plan.record(self.tracer.trace)
        self.weighted_queries = [weighted_query]
        self.query_blobs = [query_blob]
        self.low_recall_expander: _LowRecallExpander | None = None
        self.entity_fallback_reason: str | None = None

    def prepare(self) -> _QueryExpansionSession:
        request = self.recall.request
        service = self.service
        context_available = service.settings.query_context_mode != "off"
        initial_trigger = QueryExpander.trigger_for(
            request.query,
            service.settings.query_expansion_mode,
            context_available=context_available,
        )
        additions_made = False
        if initial_trigger is not None and service.query_expander is not None:
            additions, blobs = self.expand_for(initial_trigger)
            additions_made = bool(additions)
            self.weighted_queries.extend(additions)
            self.query_blobs.extend(blobs)
        if (
            service.settings.query_expansion_mode == "auto"
            and not additions_made
            and service.query_expander is not None
        ):
            self.low_recall_expander = self._expand_for_low_recall
        return self

    def prepare_wide_fallback(self, reason: str) -> None:
        request = self.recall.request
        weighted, blob, calls = prepare_wide_query(
            self.service.embedder,
            request.query,
            self.weighted_queries[0],
            self.query_blobs[0],
            dense_enabled=self.service.settings.recall_dense_enabled,
        )
        self.weighted_queries, self.query_blobs = [weighted], [blob]
        self.tracer.trace.entity_fallback_embedding_calls += calls
        self.low_recall_expander = None
        self.entity_fallback_reason = reason
        self.tracer.trace.entity_fallback_reason = reason
        self.tracer.trace.entity_filter_mode = "wide"

    def expand_for(self, trigger: str) -> tuple[list[WeightedQuery], list[bytes]]:
        request = self.recall.request
        service = self.service
        trace_source = {
            "short_query": "llm_short",
            "coreference": "llm_coreference",
            "low_recall": "llm_low_recall",
            "low_fts_recall": "llm_low_recall",
            "always": "llm_short",
        }.get(trigger, "llm_short")
        self.tracer.trace.expansion_trigger = trigger
        if service.query_expander is None or time.monotonic() >= self.deadline:
            self.tracer.trace.expansions.append(QueryExpansionTrace.from_text("", trace_source, 0.6, outcome="timeout"))
            return [], []
        self.session_context = ()
        self.tracer.trace.context_outcome = "disabled"
        if trigger == "coreference" and service.settings.query_context_mode == "coreference":
            if not request.session_id:
                self.tracer.trace.context_outcome = "missing_session"
                return [], []
            if time.monotonic() >= self.deadline:
                self.tracer.trace.context_outcome = "deadline_exhausted"
                return [], []
            try:
                self.session_context, context_truncated, context_hash, context_outcome = _session_context(
                    service.connection,
                    request.namespace,
                    request.session_id,
                    max_events=service.settings.query_context_max_events,
                    token_budget=service.settings.query_context_token_budget,
                )
            except Exception as error:
                LOGGER.warning("session context read failed: %s", type(error).__name__)
                self.tracer.trace.context_outcome = "read_error"
                return [], []
            self.tracer.trace.context_event_count = len(self.session_context)
            self.tracer.trace.context_truncated = context_truncated
            self.tracer.trace.context_hash = context_hash
            self.tracer.trace.context_outcome = context_outcome
            if not self.session_context:
                return [], []
            if time.monotonic() >= self.deadline:
                self.tracer.trace.context_outcome = "deadline_exhausted"
                return [], []
        remaining = max(0.001, self.deadline - time.monotonic())
        result = service.query_expander.expand(
            request.query,
            intent=self.recall.selected_intent,
            max_expansions=service.settings.query_expansion_max,
            timeout_seconds=min(service.settings.query_expansion_timeout_seconds, remaining),
            token_ceiling=service.settings.query_expansion_token_ceiling,
            source=trigger,
            session_context=self.session_context,
        )
        self.tracer.trace.expansion_total_tokens += result.input_tokens + result.output_tokens
        if not result.expansions:
            self.tracer.trace.expansions.append(
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
            self.tracer.trace.expansions.append(
                QueryExpansionTrace.from_text(
                    item.text,
                    item.source,
                    item.weight,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms,
                )
            )
        return additions, [
            embed_query(service.embedder, item.text) if service.settings.recall_dense_enabled else b""
            for item in additions
        ]

    def _expand_for_low_recall(
        self,
        candidate_count: int,
        fts_candidate_count: int,
    ) -> tuple[list[WeightedQuery], list[bytes]]:
        service = self.service
        trigger = QueryExpander.trigger_for(
            self.recall.request.query,
            "auto",
            candidate_count=candidate_count,
            candidate_floor=service.settings.query_expansion_candidate_floor,
            fts_candidate_count=fts_candidate_count,
            context_available=service.settings.query_context_mode != "off",
        )
        return self.expand_for(trigger) if trigger is not None else ([], [])


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
        side_effect_sink: RecallSideEffectSink | None = None,
    ) -> None:
        self.connection = connection
        self.embedder = embedder
        self.reranker = reranker
        self.relation_config = relation_config or RelationExpansionConfig()
        self.settings = settings or Settings()
        self.query_expander = query_expander
        self.intent_router = intent_router
        self.side_effect_sink = side_effect_sink

    def _resolve_recall_request(self, request: RecallRequest) -> _RecallSession:
        if request.response_format not in _RESPONSE_FORMATS:
            raise ValueError(f"unsupported response_format: {request.response_format}")
        limit = self.settings.recall_default_limit if request.limit is None else request.limit
        total_started = time.perf_counter_ns()
        query_id = request.query_id or new_id()
        intent_source = "explicit" if request.intent is not None else "keyword"
        inferred_intent = route_recall_intent(request.query, request.as_of)
        if (
            request.intent is None
            and self.settings.procedure_recall_mode == "off"
            and inferred_intent in {RecallIntent.TOOL, RecallIntent.PROCEDURE}
        ):
            inferred_intent = RecallIntent.CURRENT_STATE
            intent_source = "fallback"
        elif (
            request.intent is None
            and inferred_intent is RecallIntent.CURRENT_STATE
            and self.settings.procedure_recall_mode == "auto"
            and self.intent_router is not None
        ):
            try:
                decision = self.intent_router.route(
                    request.query,
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
        selected_intent = RecallIntent(request.intent or inferred_intent)
        injection_context = request.injection_context or InjectionContext.create(
            delivery_purpose="api",
            rendering_now=request.ranking_now or _now(),
        )
        tracer = SearchTracer(
            SearchTrace(
                query_id=query_id,
                query_hash=hashlib.sha256(request.query.encode()).hexdigest(),
                intent=selected_intent.value,
                limit=limit,
                candidate_limit=min(
                    self.settings.recall_vector_scan_limit,
                    max(limit * 5, self.settings.recall_candidate_floor),
                ),
                candidates={},
                phases=SearchPhaseMetrics(),
                intent_source=intent_source,
                injection=injection_context.envelope(),
            )
        )
        return _RecallSession(request, limit, query_id, selected_intent, injection_context, tracer, total_started)

    def _prepare_queries(self, session: _RecallSession) -> _QueryExpansionSession:
        return _QueryExpansionSession(self, session).prepare()

    def _run_claim_pipeline(
        self,
        session: _RecallSession,
        expansion: _QueryExpansionSession,
    ) -> list[dict[str, Any]]:
        request = session.request
        planned_query_changed = expansion.weighted_queries[0].text != request.query
        expansion_enabled = len(expansion.weighted_queries) > 1 or planned_query_changed
        scope_mode = "wide" if expansion.entity_fallback_reason is not None else expansion.entity_plan.scope_mode
        scope_id = None if expansion.entity_fallback_reason is not None else expansion.entity_plan.entity_id
        return hybrid_claims(
            ClaimRepository(
                self.connection,
                vector_batch_size=self.settings.vector_batch_size,
                settings=self.settings,
            ),
            request.query,
            expansion.query_blobs[0],
            session.limit,
            request.as_of,
            self.reranker,
            now=request.ranking_now,
            intent=session.selected_intent,
            known_as_of=request.known_as_of,
            namespace=request.namespace,
            recall_config=RecallConfig(
                vector_scan_limit=self.settings.recall_vector_scan_limit,
                dense_enabled=self.settings.recall_dense_enabled,
                candidate_floor=self.settings.recall_candidate_floor,
                tag_boost_enabled=self.settings.tag_boost_enabled,
                tag_boost_weight=self.settings.tag_boost_weight,
                preference_recency_boost=self.settings.preference_recency_boost,
                dedup_threshold=self.settings.recall_dedup_threshold,
                dedup_candidate_limit=self.settings.recall_dedup_candidate_limit,
                feedback_min_samples=self.settings.feedback_min_samples,
                decay_model=self.settings.decay_model,
                entity_scope_mode=scope_mode,
                entity_scope_id=scope_id,
            ),
            relation_connection=self.connection,
            relation_config=self.relation_config,
            tracer=session.tracer,
            weighted_queries=expansion.weighted_queries if expansion_enabled else None,
            query_blobs=expansion.query_blobs if expansion_enabled else None,
            low_recall_expander=expansion.low_recall_expander,
            echo_policy=EchoSuppressionPolicy(
                mode=self.settings.echo_suppression_mode,
                session_window_seconds=self.settings.echo_session_window_seconds,
                pending_review_enabled=self.settings.echo_pending_review_enabled,
                pending_similarity_threshold=self.settings.echo_pending_similarity_threshold,
                pending_max_seconds=self.settings.echo_pending_max_seconds,
            ),
            echo_request=EchoRequest(
                delivery_purpose=session.injection_context.delivery_purpose,
                session_id=request.session_id,
                namespace=request.namespace,
                intent=session.selected_intent.value,
                as_of=request.as_of,
                known_as_of=request.known_as_of,
                request_now=session.injection_context.rendering_now,
                experiment_variant=session.injection_context.experiment_variant,
                policy_version=dict(session.injection_context.policy_versions)["echo"],
            ),
            echo_signal_loader=lambda claim_ids: EvidenceRepository(self.connection).batch_get_echo_signals(
                claim_ids,
                namespace=request.namespace,
                session_id=request.session_id or "",
            ),
        )

    def _postprocess_selection(
        self,
        session: _RecallSession,
        claims: list[dict[str, Any]],
    ) -> EnrichedSelection:
        request = session.request
        enforce_enabled = False
        if self.settings.relevance_gate_mode in {"observe", "enforce"}:
            enforce_enabled = should_enforce_relevance(
                self.settings.relevance_gate_mode,
                session.selected_intent.value,
                self.settings.relevance_intents,
            )
            if enforce_enabled:
                claims = enforce_relevance(
                    claims,
                    session.tracer,
                    reranker_floor=self.settings.relevance_reranker_floor,
                    dense_floor=self.settings.relevance_dense_floor,
                    relative_drop_threshold=self.settings.relevance_relative_drop,
                    keep_top1=self.settings.relevance_keep_top1,
                )
            else:
                evaluate_relevance(
                    [str(claim["id"]) for claim in claims],
                    session.tracer,
                    reranker_floor=self.settings.relevance_reranker_floor,
                    dense_floor=self.settings.relevance_dense_floor,
                    relative_drop_threshold=self.settings.relevance_relative_drop,
                )
        provisional_answerability = self._answerability(
            claims,
            session.tracer,
            relevance_enforced=enforce_enabled,
        )
        if self.settings.resurrection_mode == "auto" and provisional_answerability != "supported":
            defer_activation = None
            if self.side_effect_sink is not None:
                sink = self.side_effect_sink

                def submit_resurrection(
                    claim_id: str,
                    embedding: bytes,
                    model: str,
                    dim: int,
                    target_namespace: str,
                    target_as_of: str,
                    target_known: str | None,
                ) -> bool:
                    return sink.submit_resurrection(
                        session.query_id,
                        claim_id,
                        embedding,
                        model,
                        dim,
                        namespace=target_namespace,
                        as_of=target_as_of,
                        known_as_of=target_known,
                    )

                defer_activation = submit_resurrection
            resurrected = ResurrectionService(
                self.connection,
                self.embedder,
                self.settings,
                defer_activation=defer_activation,
            ).try_resurrect(
                request.query,
                namespace=request.namespace,
                as_of=request.as_of or _now(),
                known_as_of=request.known_as_of,
                intent=session.selected_intent,
            )
            if resurrected is not None:
                resurrected["_score"] = max(
                    1.0,
                    max((float(claim.get("_score", 0.0)) for claim in claims), default=0.0) + 0.01,
                )
                claims = [resurrected, *(claim for claim in claims if claim["id"] != resurrected["id"])][
                    : session.limit
                ]
                session.tracer.record_channel("cold_fts", [resurrected])
                session.tracer.record_final(claims)
        if is_access_recording_eligible(
            intent=session.selected_intent,
            as_of=request.as_of,
            known_as_of=request.known_as_of,
        ):
            if self.side_effect_sink is not None:
                self._submit_access(session.query_id, claims)
            else:
                self._record_access(claims)
        assembly_started = time.perf_counter_ns()
        return EnrichedSelection(
            claims,
            self._assemble_results(claims, request.namespace),
            enforce_enabled,
            assembly_started,
        )

    def _enrich_standard_results(
        self,
        session: _RecallSession,
        selection: EnrichedSelection,
    ) -> EnrichedSelection:
        session.tracer.trace.phases.assembly_us = (time.perf_counter_ns() - selection.assembly_started) // 1000
        request = session.request
        if auxiliary_context_is_current(as_of=request.as_of, known_as_of=request.known_as_of):
            observations = self._assemble_observations([claim["id"] for claim in selection.claims])
            policies = matching_policies(
                ExperienceService(self.connection).list_policies("active", namespace=request.namespace),
                request.query,
            )
            policy_evidence = EvidenceRepository(self.connection).batch_get_links_for_derived(
                "policy",
                [str(policy["id"]) for policy in policies],
            )
            for policy in policies:
                policy["evidence"] = policy_evidence.get(str(policy["id"]), [])
        else:
            observations = []
            policies = []
        packet_candidates = self._context_candidates(selection.results, observations, policies)
        answerability = self._answerability(
            selection.claims,
            session.tracer,
            relevance_enforced=selection.relevance_enforced,
            has_auxiliary_candidates=bool(packet_candidates),
        )
        return replace(
            selection,
            observations=observations,
            policies=policies,
            packet_candidates=packet_candidates,
            answerability=answerability,
        )

    def _assemble_delivery(
        self,
        session: _RecallSession,
        selection: EnrichedSelection,
    ) -> dict[str, Any]:
        request = session.request
        retrieval_bundle = self._bundle_from_context_items(
            session.query_id,
            selection.answerability,
            selection.packet_candidates,
        )
        freshness_items = [
            _freshness_item(
                bundle_item.type,
                bundle_item.id,
                bundle_item.text,
                wrapped.get("data", wrapped),
            )
            for bundle_item, wrapped in zip(retrieval_bundle.items, selection.packet_candidates, strict=True)
        ]
        if request.response_format != "legacy" or request.context_mode == "packed":
            budget = request.token_budget or self.settings.packed_context_token_budget
            bundle = _freshness_pack_bundle(
                retrieval_bundle,
                freshness_items,
                policy=FreshnessAnnotationPolicy(mode=self.settings.freshness_annotation_mode),
                request=FreshnessRequest(
                    delivery_purpose=session.injection_context.delivery_purpose,
                    intent=session.selected_intent.value,
                    as_of=request.as_of,
                    known_as_of=request.known_as_of,
                    rendering_now=session.injection_context.rendering_now,
                    experiment_variant=session.injection_context.experiment_variant,
                    policy_version=dict(session.injection_context.policy_versions)["freshness"],
                ),
                token_budget=budget,
                tracer=session.tracer,
            )
            if self.settings.freshness_annotation_mode == "render":
                packet_context = self._context_from_packed_bundle(selection.packet_candidates, bundle)
            else:
                packet_context = self._assemble_context(
                    selection.results,
                    selection.observations,
                    selection.policies,
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
                "context_items": selection.packet_candidates,
                "used_tokens_estimate": used,
                "truncated": False,
            }
        if request.response_format == "retrieval_bundle":
            return {"retrieval_bundle": retrieval_bundle_to_dict(bundle)}
        materialized_packet = self._materialize_context_packet(bundle)
        if request.response_format != "context_packet":
            self._attach_packet_feedback(packet_context, materialized_packet)
        context_packet = materialized_packet if request.response_format != "legacy" else None
        response = {
            "results": selection.results,
            "observations": selection.observations,
            "policies": selection.policies,
            "total": len(selection.results),
            "query_id": session.query_id,
            "answerability": selection.answerability,
        }
        if request.context_mode == "packed":
            response["context"] = packet_context
        if request.debug:
            session.tracer.trace.phases.total_us = (time.perf_counter_ns() - session.total_started) // 1000
            response["search_trace"] = session.tracer.to_dict()
        if request.response_format == "context_packet":
            return {"context_packet": context_packet}
        if request.response_format == "both":
            response["context_packet"] = context_packet
        return response

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
        ranking_now: str | None = None,
        injection_context: InjectionContext | None = None,
    ) -> dict[str, Any]:
        """执行混合召回；ranking_now 仅控制排序时钟，不改变时间可见性。"""
        request = RecallRequest(
            query=query,
            limit=limit,
            as_of=as_of,
            intent=intent,
            known_as_of=known_as_of,
            query_id=query_id,
            token_budget=token_budget,
            context_mode=context_mode,
            namespace=namespace,
            session_id=session_id,
            debug=debug,
            response_format=response_format,
            ranking_now=ranking_now,
            injection_context=injection_context,
        )
        session = self._resolve_recall_request(request)
        limit = session.limit
        query_id = session.query_id
        selected_intent = session.selected_intent
        resolved_injection_context = session.injection_context
        tracer = session.tracer
        total_started = session.total_started
        expansion = self._prepare_queries(session)

        try:
            claims = self._run_claim_pipeline(session, expansion)
        except EntityScopeFallback as fallback:
            expansion.prepare_wide_fallback(fallback.reason)
            try:
                claims = self._run_claim_pipeline(session, expansion)
            except sqlite3.Error:
                raise fallback.original_error
        selection = self._postprocess_selection(session, claims)
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
                claim_results=selection.results,
                claim_scores={str(item["id"]): float(item.get("_score", 0.0)) for item in selection.claims},
                token_budget=token_budget,
                context_mode=context_mode,
                debug=debug,
                response_format=response_format,
                tracer=tracer,
                total_started=total_started,
                injection_context=resolved_injection_context,
                as_of=as_of,
                known_as_of=known_as_of,
            )
        selection = self._enrich_standard_results(session, selection)
        return self._assemble_delivery(session, selection)

    @staticmethod
    def _answerability(
        claims: list[dict[str, Any]],
        tracer: SearchTracer,
        *,
        relevance_enforced: bool = False,
        has_auxiliary_candidates: bool = False,
    ) -> Answerability:
        """按最终候选及 relevance gate 结果给出 answerability。"""
        if not claims:
            answerability: Answerability = "low_confidence" if has_auxiliary_candidates else "no_evidence"
            tracer.trace.answerability = answerability
            return answerability
        top = claims[0]
        trace = tracer.trace.candidates.get(str(top["id"]))
        if relevance_enforced and (trace is None or trace.relevance_decision != "relevant"):
            tracer.trace.answerability = "low_confidence"
            return "low_confidence"
        fts_hit = bool(trace and ({"fts", "cold_fts"} & trace.channels.keys()))
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
        return assemble_recall_observations(self.connection, claim_ids)

    @staticmethod
    def _context_candidates(
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return context_candidates(claims, observations, policies)

    @staticmethod
    def _assemble_context(
        claims: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        policies: list[dict[str, Any]],
        token_budget: int,
    ) -> dict[str, Any]:
        return assemble_context(
            claims,
            observations,
            policies,
            token_budget,
            packer=budget_pack,
            text_for=_context_text,
        )

    @staticmethod
    def _bundle_from_context_items(
        query_id: str,
        answerability: Answerability,
        context_items: list[dict[str, Any]],
    ) -> RetrievalBundle:
        return bundle_from_context_items(
            query_id,
            answerability,
            context_items,
            text_for=_context_text,
        )

    @staticmethod
    def _context_from_packed_bundle(
        context_items: list[dict[str, Any]],
        bundle: RetrievalBundle,
    ) -> dict[str, Any]:
        return context_from_packed_bundle(context_items, bundle)

    def _materialize_context_packet(self, bundle: RetrievalBundle) -> dict[str, Any]:
        """有限重试 exposure 批量落库，并把最终失败收敛为 degraded packet。"""
        service = ExperienceService(self.connection)

        def persist_exposures(exposures: list[tuple[Any, ...]]) -> int:
            if self.side_effect_sink is not None:
                return self._submit_exposures(bundle.query_id, exposures)
            return cast(int, self._run_side_effect_with_retry(lambda: service.record_exposure_batch(exposures)))

        assembler = ContextPacketAssembler(
            service,
            persist_exposures=persist_exposures,
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

    def materialize_context_packet(self, bundle: RetrievalBundle) -> dict[str, Any]:
        """为 adapter 已缓存的最终 bundle 创建本次 delivery packet。"""
        return self._materialize_context_packet(bundle)

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

    def _submit_exposures(self, query_id: str, exposures: list[tuple[Any, ...]]) -> int:
        if self.side_effect_sink is None or not self.side_effect_sink.submit_exposures(query_id, exposures):
            raise RuntimeError("recall exposure submission rejected")
        return len(exposures)

    def _submit_access(self, query_id: str, claims: list[dict[str, Any]]) -> None:
        try:
            claim_ids = [claim["id"] for claim in claims]
            accessed_at = _now()
            assert self.side_effect_sink is not None
            if not self.side_effect_sink.submit_access(query_id, claim_ids, accessed_at):
                raise RuntimeError("recall access submission rejected")
        except Exception as error:
            _record_side_effect_failure("access_record", error)
            LOGGER.exception("recall side effect failed: access_record")

    def _record_access(self, claims: list[dict[str, Any]]) -> None:
        try:
            claim_ids = [claim["id"] for claim in claims]
            self._run_side_effect_with_retry(lambda: ClaimRepository(self.connection).record_access(claim_ids, _now()))
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
        return assemble_recall_results(
            self.connection,
            claims,
            namespace,
            claim_text=_claim_index_text,
            claim_relation=_claim_relation,
        )

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
        injection_context: InjectionContext,
        as_of: str | None,
        known_as_of: str | None,
    ) -> dict[str, Any]:
        """执行 Experience 专用排序、统一 packing 与多类型 exposure。"""
        request = RecallRequest(
            query=query,
            limit=limit,
            as_of=as_of,
            intent=selected_intent,
            known_as_of=known_as_of,
            query_id=query_id,
            token_budget=token_budget,
            context_mode=context_mode,
            namespace=namespace,
            session_id=None,
            debug=debug,
            response_format=response_format,
            ranking_now=None,
            injection_context=injection_context,
        )
        return _ProcedureRecallFlow(
            self,
            request,
            claim_results,
            claim_scores,
            tracer,
            total_started,
        ).run()
