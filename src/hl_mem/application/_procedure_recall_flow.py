"""Procedure/experience recall flow kept separate from the public orchestrator."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol, cast

from hl_mem.application.answerability import Answerability
from hl_mem.application.context_packet import (
    MemoryType,
    RetrievalBundle,
    RetrievalBundleItem,
    estimate_tokens,
    render_memory_text,
    retrieval_bundle_to_dict,
)
from hl_mem.domain.recall import RecallIntent
from hl_mem.experience.service import ExperienceService
from hl_mem.recall.procedure_pipeline import MemoryCandidate
from hl_mem.recall.trace import ExperienceCandidateTrace, SearchTracer
from hl_mem.settings import Settings


class _ProcedureRequest(Protocol):
    @property
    def query(self) -> str: ...

    @property
    def limit(self) -> int | None: ...

    @property
    def as_of(self) -> str | None: ...

    @property
    def intent(self) -> RecallIntent | str | None: ...

    @property
    def known_as_of(self) -> str | None: ...

    @property
    def query_id(self) -> str | None: ...

    @property
    def token_budget(self) -> int | None: ...

    @property
    def context_mode(self) -> str | None: ...

    @property
    def namespace(self) -> str: ...

    @property
    def debug(self) -> bool: ...

    @property
    def response_format(self) -> str: ...

    @property
    def injection_context(self) -> Any: ...


class _ProcedureService(Protocol):
    connection: Any
    settings: Settings

    def _answerability(
        self,
        claims: list[dict[str, Any]],
        tracer: SearchTracer,
        *,
        relevance_enforced: bool = False,
        has_auxiliary_candidates: bool = False,
    ) -> Answerability: ...

    def _materialize_context_packet(self, bundle: RetrievalBundle) -> dict[str, Any]: ...


class ProcedureRecallFlow:
    """Preserve procedure-specific packing, tracing, and response semantics."""

    def __init__(
        self,
        service: _ProcedureService,
        request: _ProcedureRequest,
        claim_results: list[dict[str, Any]],
        claim_scores: dict[str, float],
        tracer: SearchTracer,
        total_started: int,
    ) -> None:
        self.service = service
        self.request = request
        self.claim_results = claim_results
        self.claim_scores = claim_scores
        self.tracer = tracer
        self.total_started = total_started
        self.intent = cast(RecallIntent, request.intent)
        self.limit = cast(int, request.limit)
        self.query_id = cast(str, request.query_id)
        self.injection_context = request.injection_context

    def run(self) -> dict[str, Any]:
        self._recall_and_pack()
        self._select_and_build_bundle()
        if self.request.response_format == "retrieval_bundle":
            return {"retrieval_bundle": retrieval_bundle_to_dict(self.bundle)}
        materialized_packet = self.service._materialize_context_packet(self.bundle)
        if self.request.response_format != "context_packet" and materialized_packet["feedback_state"] == "available":
            feedback_by_memory = {
                (item["type"], item["id"]): item["feedback_id"] for item in materialized_packet["items"]
            }
            for result in self.results:
                feedback_id = feedback_by_memory.get((result["memory_type"], result["id"]))
                if feedback_id is not None:
                    result["feedback_id"] = feedback_id
        context_packet = materialized_packet if self.request.response_format != "legacy" else None
        self._record_trace()
        return self._assemble_response(context_packet)

    def _recall_and_pack(self) -> None:
        from hl_mem.application import recall as recall_module

        claim_candidates = [
            MemoryCandidate(
                "claim",
                str(item["id"]),
                str(item.get("text") or ""),
                self.claim_scores.get(str(item["id"]), 0.0),
                tuple(item.get("evidence") or ()),
                {"claim_score": self.claim_scores.get(str(item["id"]), 0.0)},
                role=str(item["role"]) if item.get("role") else None,
                action=str(item["action"]) if item.get("action") else None,
                object=str(item["object"]) if item.get("object") else None,
            )
            for item in self.claim_results
        ]
        claim_metadata_by_id = {str(item["id"]): item for item in self.claim_results}
        candidates = recall_module.recall_procedure(
            ExperienceService(self.service.connection).repository,
            self.request.query,
            self.intent,
            self.request.namespace,
            self.limit,
            candidate_limit=self.service.settings.procedure_candidate_limit,
            recent_outcome_window=self.service.settings.procedure_recent_outcome_window,
            outcome_half_life_days=self.service.settings.procedure_outcome_half_life_days,
            claim_candidates=claim_candidates,
        )
        budget = self.request.token_budget or self.service.settings.packed_context_token_budget
        freshness_request = recall_module._freshness_request(
            self.injection_context,
            self.intent,
            self.request.as_of,
            self.request.known_as_of,
        )
        freshness_evaluation = recall_module._freshness_policy(self.service.settings).evaluate(
            [
                recall_module._freshness_item(
                    item.memory_type,
                    item.memory_id,
                    item.text,
                    claim_metadata_by_id.get(item.memory_id, {}),
                )
                for item in candidates
            ],
            freshness_request,
        )
        rendered_by_key = {
            (decision.memory_type, decision.item_id): decision.rendered_text
            for decision in freshness_evaluation.decisions
            if decision.eligible
        }
        decorated_candidates = [
            (
                replace(item, text=rendered_by_key[(item.memory_type, item.memory_id)])
                if (item.memory_type, item.memory_id) in rendered_by_key
                else item
            )
            for item in candidates
        ]
        control_packed, control_quotas, control_reflow = recall_module.budget_pack_by_type(
            candidates,
            self.intent,
            budget,
        )
        if rendered_by_key:
            treatment_packed, treatment_quotas, treatment_reflow = recall_module.budget_pack_by_type(
                decorated_candidates,
                self.intent,
                budget,
            )
        else:
            treatment_packed, treatment_quotas, treatment_reflow = (
                control_packed,
                control_quotas,
                control_reflow,
            )
        freshness_evaluation = freshness_evaluation.with_truncation_changed(
            [(item.memory_type, item.memory_id) for item in control_packed]
            != [(item.memory_type, item.memory_id) for item in treatment_packed]
        )
        recall_module._record_freshness_evaluation(freshness_evaluation, self.tracer, freshness_request)
        if self.service.settings.freshness_annotation_mode == "render":
            self.candidates = decorated_candidates
            self.packed, self.quotas, self.reflow = treatment_packed, treatment_quotas, treatment_reflow
        else:
            self.candidates = candidates
            self.packed, self.quotas, self.reflow = control_packed, control_quotas, control_reflow

    def _select_and_build_bundle(self) -> None:
        packet_selected = self.packed[: self.limit]
        self.selected = packet_selected if self.request.context_mode == "packed" else self.candidates[: self.limit]
        self.results = []
        for item in self.selected:
            result: dict[str, Any] = {
                "type": item.memory_type,
                "memory_type": item.memory_type,
                "id": item.memory_id,
                "text": item.text,
                "score": item.score,
                "evidence": list(item.evidence),
                "features": item.features,
            }
            if item.memory_type == "claim" and item.role is not None:
                result.update(role=item.role, action=item.action, object=item.object)
            self.results.append(result)
        self.answerability = self.service._answerability([], self.tracer) if not self.candidates else "supported"
        self.materialized_selection = packet_selected if self.request.response_format != "legacy" else self.selected
        self.bundle = RetrievalBundle(
            query_id=self.query_id,
            answerability=self.answerability,
            items=tuple(
                RetrievalBundleItem(
                    cast(MemoryType, item.memory_type),
                    item.memory_id,
                    item.text,
                    tuple(reference for reference in item.evidence if isinstance(reference, Mapping)),
                    item.score,
                    item.role if item.memory_type == "claim" else None,
                    item.action if item.memory_type == "claim" else None,
                    item.object if item.memory_type == "claim" else None,
                )
                for item in self.materialized_selection
            ),
            used_tokens_estimate=sum(
                estimate_tokens(
                    render_memory_text(item.text, role=item.role, action=item.action, object_=item.object)
                    if item.memory_type == "claim"
                    else item.text
                )
                for item in self.materialized_selection
            ),
            truncated=len(self.materialized_selection) < len(self.candidates),
        )

    def _record_trace(self) -> None:
        self.tracer.trace.candidate_counts = {
            kind: sum(item.memory_type == kind for item in self.candidates)
            for kind in ("policy", "episode", "trace", "claim")
        }
        self.tracer.trace.quota_tokens = self.quotas
        self.tracer.trace.reflow_tokens = self.reflow
        selected_keys = {
            (item.memory_type, item.memory_id): rank for rank, item in enumerate(self.materialized_selection, 1)
        }
        self.tracer.trace.experience_candidates = [
            ExperienceCandidateTrace(
                memory_type=item.memory_type,
                memory_id=item.memory_id,
                source_rank=rank,
                features=item.features,
                final_rank=selected_keys.get((item.memory_type, item.memory_id)),
                included=(item.memory_type, item.memory_id) in selected_keys,
                filter_reasons=[] if (item.memory_type, item.memory_id) in selected_keys else ["limit_or_budget"],
            )
            for rank, item in enumerate(self.candidates, 1)
        ]

    def _assemble_response(self, context_packet: dict[str, Any] | None) -> dict[str, Any]:
        response: dict[str, Any] = {
            "results": self.results,
            "observations": [],
            "policies": [item for item in self.results if item["memory_type"] == "policy"],
            "total": len(self.results),
            "query_id": self.query_id,
            "answerability": self.answerability,
        }
        if self.request.context_mode == "packed":
            used = sum(
                estimate_tokens(
                    render_memory_text(item.text, role=item.role, action=item.action, object_=item.object)
                    if item.memory_type == "claim"
                    else item.text
                )
                for item in self.selected
            )
            response["context"] = {
                "context_items": [
                    {"type": item.memory_type, "data": result} for item, result in zip(self.selected, self.results)
                ],
                "used_tokens_estimate": used,
                "truncated": len(self.selected) < len(self.candidates),
                "quota_tokens": self.quotas,
                "reflow_tokens": self.reflow,
            }
        if self.request.debug:
            self.tracer.trace.phases.total_us = (time.perf_counter_ns() - self.total_started) // 1000
            response["search_trace"] = self.tracer.to_dict()
        if self.request.response_format == "context_packet":
            return {"context_packet": context_packet}
        if self.request.response_format == "both":
            response["context_packet"] = context_packet
        return response
