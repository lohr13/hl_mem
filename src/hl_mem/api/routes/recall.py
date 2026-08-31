"""Domain route registration extracted from the application factory."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from hl_mem.api.routes import utc_now
from hl_mem.api.schemas import (
    ContextPacketRecallOutput,
    RecallInput,
    RecallOutput,
    RetrievalBundleInput,
)
from hl_mem.application.context_packet import UnknownSchemaMajorError, retrieval_bundle_from_dict
from hl_mem.application.ingest import new_id
from hl_mem.experience.service import ExperienceService
from hl_mem.observability.audit import audit_scope
from hl_mem.recall.injection import InjectionContext


def add_recall_routes(
    app: FastAPI,
    *,
    get_connection: Callable[..., Any],
    get_read_connection: Callable[..., Any],
    make_recall_service: Callable[..., Any],
    execute_recall: Callable[..., dict[str, Any]],
    deferred_audit: Any,
    settings: Any,
    recall_side_effects: Any,
) -> None:
    @app.post(
        "/v1/recall",
        response_model=RecallOutput | ContextPacketRecallOutput,
        response_model_exclude_none=True,
    )
    def recall(
        payload: RecallInput,
        request: Request,
        connection: sqlite3.Connection = Depends(get_read_connection),
    ) -> dict[str, Any]:
        query_id = request.headers.get("X-Request-ID") or new_id()
        with audit_scope(
            deferred_audit,
            trace_id=query_id,
            query_id=query_id,
            tenant_id=payload.effective_namespace,
        ):
            return execute_recall(
                payload,
                query_id=query_id,
                connection=connection,
                response_format=payload.response_format,
                injection_context=InjectionContext.create(delivery_purpose="api", rendering_now=utc_now()),
            )

    @app.post(
        "/v1/internal/retrieval-bundles",
        include_in_schema=False,
    )
    def retrieve_bundle(
        payload: RetrievalBundleInput,
        request: Request,
        connection: sqlite3.Connection = Depends(get_read_connection),
    ) -> dict[str, Any]:
        """为 Hermes 预取 receipt-free bundle；旧 daemon 会安全返回 404。"""
        query_id = request.headers.get("X-Request-ID") or new_id()
        with audit_scope(
            deferred_audit,
            trace_id=query_id,
            query_id=query_id,
            tenant_id=payload.effective_namespace,
        ):
            return execute_recall(
                payload,
                query_id=query_id,
                connection=connection,
                response_format="retrieval_bundle",
                injection_context=InjectionContext.create(
                    delivery_purpose=payload.delivery_purpose,
                    experiment_variant=payload.experiment_variant,
                    echo_variant=payload.echo_variant,
                    freshness_variant=payload.freshness_variant,
                    policy_versions=payload.policy_versions,
                    rendering_now=payload.rendering_now or utc_now(),
                ),
            )

    @app.post(
        "/v1/internal/context-packets/materialize",
        include_in_schema=False,
    )
    def materialize_context_packet(
        payload: dict[str, Any],
        connection: sqlite3.Connection = Depends(get_read_connection),
    ) -> dict[str, Any]:
        """为 Hermes 缓存的 receipt-free bundle 创建本次 delivery receipt。"""
        raw_bundle = payload.get("retrieval_bundle", payload)
        if not isinstance(raw_bundle, Mapping):
            raise HTTPException(422, "retrieval_bundle must be an object")
        try:
            bundle = retrieval_bundle_from_dict(raw_bundle)
        except UnknownSchemaMajorError as error:
            raise HTTPException(409, str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        packet = make_recall_service(connection).materialize_context_packet(bundle)
        return {"context_packet": packet}

    @app.post(
        "/v1/internal/retrieval-feedback/injected",
        include_in_schema=False,
    )
    def mark_retrieval_feedback_injected(
        payload: dict[str, Any],
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, int]:
        """在 Hermes delivery 边界后原子标记 injected，不写 outcome。"""
        feedback_ids = payload.get("feedback_ids")
        if (
            not isinstance(feedback_ids, list)
            or not feedback_ids
            or any(not isinstance(feedback_id, str) or not feedback_id.strip() for feedback_id in feedback_ids)
        ):
            raise HTTPException(422, "feedback_ids must be a non-empty string array")
        try:
            updated = ExperienceService(
                connection,
                settings=settings,
                pending_exposure_check=recall_side_effects.has_pending_exposures,
            ).mark_feedback_injected_eventually(
                feedback_ids,
                utc_now(),
            )
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return {"updated": updated}
