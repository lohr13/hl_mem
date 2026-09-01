"""Recall query planning without application-service dependencies."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from hl_mem.domain.recall import RecallIntent
from hl_mem.protocols import EmbedderProtocol, WeightedQuery, embed_query
from hl_mem.recall.entity_query import QueryEntityPlan, prepare_entity_query, prepare_wide_query
from hl_mem.recall.query_expansion import QueryExpander
from hl_mem.recall.trace import QueryExpansionTrace, SearchTracer
from hl_mem.storage.events import EventRepository

LOGGER = logging.getLogger(__name__)

LowRecallExpander = Callable[[int, int], tuple[list[WeightedQuery], list[bytes]]]


class RecallPlanningSettings(Protocol):
    @property
    def query_expansion_total_timeout_seconds(self) -> float: ...

    @property
    def entity_constraint_mode(self) -> str: ...

    @property
    def recall_dense_enabled(self) -> bool: ...

    @property
    def query_context_mode(self) -> str: ...

    @property
    def query_expansion_mode(self) -> str: ...

    @property
    def query_context_max_events(self) -> int: ...

    @property
    def query_context_token_budget(self) -> int: ...

    @property
    def query_expansion_max(self) -> int: ...

    @property
    def query_expansion_timeout_seconds(self) -> float: ...

    @property
    def query_expansion_token_ceiling(self) -> int: ...

    @property
    def query_expansion_candidate_floor(self) -> int: ...


class RecallPlanningRequest(Protocol):
    @property
    def query(self) -> str: ...

    @property
    def namespace(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...


class RecallPlanningInput(Protocol):
    @property
    def request(self) -> RecallPlanningRequest: ...

    @property
    def selected_intent(self) -> RecallIntent: ...

    @property
    def tracer(self) -> SearchTracer: ...


class RecallPlanningService(Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    @property
    def embedder(self) -> EmbedderProtocol: ...

    @property
    def settings(self) -> RecallPlanningSettings: ...

    @property
    def query_expander(self) -> QueryExpander | None: ...


@dataclass(frozen=True, slots=True)
class PreparedQueries:
    weighted_queries: tuple[WeightedQuery, ...]
    query_blobs: tuple[bytes, ...]
    entity_plan: QueryEntityPlan
    low_recall_expander: LowRecallExpander | None


def load_session_context(
    connection: sqlite3.Connection,
    namespace: str,
    session_id: str,
    *,
    max_events: int,
    token_budget: int,
) -> tuple[tuple[tuple[str, str], ...], bool, str | None, str]:
    """Load bounded user/assistant text for one namespace and session."""

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


class QueryPlanningSession:
    """Own one recall request's entity, embedding, and expansion plan."""

    def __init__(
        self,
        service: RecallPlanningService,
        recall: RecallPlanningInput,
        *,
        monotonic: Callable[[], float] | None = None,
        context_reader: Callable[..., tuple[tuple[tuple[str, str], ...], bool, str | None, str]] | None = None,
    ) -> None:
        self.service = service
        self.recall = recall
        self._monotonic = monotonic or time.monotonic
        self._context_reader = context_reader or load_session_context
        self.deadline = self._monotonic() + service.settings.query_expansion_total_timeout_seconds
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
        self.low_recall_expander: LowRecallExpander | None = None
        self.entity_fallback_reason: str | None = None

    def snapshot(self) -> PreparedQueries:
        return PreparedQueries(
            tuple(self.weighted_queries),
            tuple(self.query_blobs),
            self.entity_plan,
            self.low_recall_expander,
        )

    def prepare(self) -> PreparedQueries:
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
        return self.snapshot()

    def prepare_wide_fallback(self, reason: str) -> PreparedQueries:
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
        return self.snapshot()

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
        if service.query_expander is None or self._monotonic() >= self.deadline:
            self.tracer.trace.expansions.append(QueryExpansionTrace.from_text("", trace_source, 0.6, outcome="timeout"))
            return [], []
        self.session_context = ()
        self.tracer.trace.context_outcome = "disabled"
        if trigger == "coreference" and service.settings.query_context_mode == "coreference":
            if not request.session_id:
                self.tracer.trace.context_outcome = "missing_session"
                return [], []
            if self._monotonic() >= self.deadline:
                self.tracer.trace.context_outcome = "deadline_exhausted"
                return [], []
            try:
                self.session_context, context_truncated, context_hash, context_outcome = self._context_reader(
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
            if self._monotonic() >= self.deadline:
                self.tracer.trace.context_outcome = "deadline_exhausted"
                return [], []
        remaining = max(0.001, self.deadline - self._monotonic())
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
