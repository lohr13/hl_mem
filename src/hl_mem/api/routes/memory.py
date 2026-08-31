"""Domain route registration extracted from the application factory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from hl_mem.api.routes import resolve_namespace_alias
from hl_mem.api.schemas import (
    DryRunExtractionInput,
    EventBatchInput,
    EventInput,
    MemoryCorrectionInput,
    MemoryCorrectionOutput,
    MemoryDetailOutput,
    MemoryInput,
    MemoryListOutput,
    MemorySaveOutput,
)
from hl_mem.application.correction import CorrectionService
from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService
from hl_mem.application.memories import MemoryQueryService


def add_memory_routes(
    app: FastAPI,
    *,
    get_connection: Callable[..., Any],
    settings: Any,
    audit: Any,
    provider_runtime: Any,
    embedder: Any,
    make_extractor: Callable[..., Any],
) -> None:
    @app.post("/v1/events")
    def post_event(
        payload: EventInput,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        key = idempotency_key or payload.idempotency_key
        content = payload.content if isinstance(payload.content, dict) else {"text": payload.content}
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        event = payload.model_dump(exclude={"namespace", "tenant_id"})
        event["tenant_id"] = payload.effective_namespace
        service = IngestService(connection)
        result = service.ingest_event(event, key)
        event_id, created = result["id"], result["created"]
        audit.emit(
            "ingest",
            "accepted",
            "queued" if created else "duplicate",
            trace_id=event_id,
            event_id=event_id,
            tenant_id=payload.effective_namespace,
            detail={
                "event_type": payload.event_type,
                "actor_type": payload.actor_type,
                "content_chars": len(content_json),
                "content_hash": hashlib.sha256(content_json.encode()).hexdigest(),
                "sensitivity": payload.sensitivity,
            },
        )
        return result

    @app.post("/v1/events/batch")
    def post_event_batch(
        payload: EventBatchInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """原子写入一个有界 Event 集合；现有单 Event API 保持不变。"""
        events: list[dict[str, Any]] = []
        for item in payload.events:
            event = item.model_dump(exclude={"namespace", "tenant_id"})
            event["tenant_id"] = item.effective_namespace
            events.append(event)
        results = IngestService(connection).ingest_events(events)
        for item, result in zip(payload.events, results, strict=True):
            content = item.content if isinstance(item.content, dict) else {"text": item.content}
            content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
            audit.emit(
                "ingest",
                "accepted",
                "queued" if result["created"] else "duplicate",
                trace_id=result["id"],
                event_id=result["id"],
                tenant_id=item.effective_namespace,
                detail={
                    "event_type": item.event_type,
                    "actor_type": item.actor_type,
                    "content_chars": len(content_json),
                    "content_hash": hashlib.sha256(content_json.encode()).hexdigest(),
                    "sensitivity": item.sensitivity,
                    "batch_size": len(payload.events),
                },
            )
        return {"events": results}

    @app.post("/v1/extract/dry-run")
    def dry_run_extract(
        payload: DryRunExtractionInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """提取候选 claims 与 token 用量，但不持久化记忆数据。"""
        extractor = make_extractor(
            settings,
            require_real=True,
            connection=connection,
            runtime=provider_runtime,
        )
        return IngestService.dry_run_extract(
            extractor,
            payload.text,
            payload.context,
            payload.custom_instructions,
        )

    _add_memory_crud_routes(
        app,
        get_connection=get_connection,
        settings=settings,
        audit=audit,
        embedder=embedder,
    )


def _add_memory_crud_routes(
    app: FastAPI,
    *,
    get_connection: Callable[..., Any],
    settings: Any,
    audit: Any,
    embedder: Any,
) -> None:
    @app.get("/v1/memories", response_model=MemoryListOutput)
    def list_memories(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        status: str = Query(
            default="active",
            pattern="^(active|candidate|disputed|superseded|expired|archived|retracted)$",
        ),
        namespace: str | None = Query(default=None, min_length=1, max_length=100),
        tenant_id: str | None = Query(default=None, min_length=1, max_length=100, deprecated=True),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """按 namespace/status 分页列出 Claim 记忆。"""
        return MemoryQueryService(connection).list_memories(
            namespace=resolve_namespace_alias(namespace, tenant_id),
            status=status,
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/memories/{memory_id}", response_model=MemoryDetailOutput)
    def get_memory(
        memory_id: str,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """返回单条 Claim 的完整公开详情。"""
        return MemoryQueryService(connection).get_memory(memory_id)

    @app.post("/v1/memories/{memory_id}/correct", response_model=MemoryCorrectionOutput)
    def correct_memory(
        memory_id: str,
        payload: MemoryCorrectionInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """按既定契约仅替换 Claim 内容，并原子建立替代证据链。"""
        return CorrectionService(connection, embedder, settings=settings).correct(
            memory_id,
            payload.corrected_text,
            payload.idempotency_key,
        )

    @app.post(
        "/v1/memories",
        response_model=MemorySaveOutput,
        responses={409: {"description": "Idempotency key payload conflict"}},
    )
    def save_memory(
        payload: MemoryInput,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        text = payload.text or payload.content
        if not text:
            raise HTTPException(422, "text or content is required")
        service = IngestService(connection)
        result = service.save_explicit_memory(
            text,
            payload.subject,
            payload.predicate,
            payload.qualifiers,
            idempotency_key=idempotency_key or payload.idempotency_key,
            namespace=payload.effective_namespace,
            session_id=payload.session_id,
        )
        event_id = result["id"]
        content_json = json.dumps(
            {
                "text": text,
                "memory": {
                    "text": text,
                    "subject": payload.subject,
                    "predicate": payload.predicate,
                    "qualifiers": payload.qualifiers,
                },
            },
            ensure_ascii=False,
        )
        audit.emit(
            "ingest",
            "accepted",
            "queued" if result["created"] else "duplicate",
            trace_id=event_id,
            event_id=event_id,
            tenant_id=payload.effective_namespace,
            detail={
                "event_type": "explicit_memory",
                "actor_type": "user",
                "content_chars": len(content_json),
                "sensitivity": "normal",
            },
        )
        return result

    @app.delete("/v1/memories/{memory_id}")
    def forget(memory_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        try:
            return ForgetService(connection).forget(memory_id)
        except ValueError as error:
            if str(error).startswith("memory not found"):
                raise HTTPException(404, "memory not found") from error
            raise
