"""HL-Mem FastAPI 应用工厂与 REST 路由适配层。"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from hl_mem import __version__, components
from hl_mem.api.conflict_routes import add_conflict_routes
from hl_mem.api.request_limits import RequestSizeLimitMiddleware
from hl_mem.api.routes.experience import add_experience_routes
from hl_mem.api.routes.maintenance import add_maintenance_routes
from hl_mem.api.routes.memory import add_memory_routes
from hl_mem.api.routes.recall import add_recall_routes
from hl_mem.api.schemas import (
    RecallInput,
)
from hl_mem.application.recall import RecallService, recall_side_effect_health
from hl_mem.application.recall_side_effects import (
    DeferredAuditLogger,
    DeferredLLMSpanRecorder,
    RecallSideEffectDispatcher,
)
from hl_mem.compatibility import compatibility_manifest
from hl_mem.errors import ConflictError, NotFoundError, ValidationError
from hl_mem.http_utils import HL_MEM_VERSION_HEADER
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.observability.audit import NullAuditLogger
from hl_mem.recall.injection import InjectionContext
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.recall.reranker import FakeReranker
from hl_mem.recall.trace import SearchTracer
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

LOGGER = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录每个 HTTP 请求的开始、完成状态与单调时钟耗时。"""

    @staticmethod
    def _safe_query_id(value: str) -> str:
        """限制日志字段长度，并替换可能破坏单行日志结构的字符。"""
        return "".join(character if character.isalnum() or character in "-._:" else "_" for character in value)[:200]

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method
        path = request.url.path
        query_id = request.headers.get("X-Request-ID")
        if query_id:
            query_id = self._safe_query_id(query_id)
            LOGGER.info(
                "request_started method=%s path=%s query_id=%s",
                method,
                path,
                query_id,
            )
        else:
            LOGGER.info("request_started method=%s path=%s", method, path)

        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            LOGGER.info(
                "request_finished method=%s path=%s status=%d duration_ms=%.3f",
                method,
                path,
                status_code,
                duration_ms,
            )


class VersionHeaderMiddleware(BaseHTTPMiddleware):
    """在成功与验证失败响应上暴露 daemon 版本，供跨版本诊断。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers[HL_MEM_VERSION_HEADER] = __version__
        return response


def create_app(settings: Settings | str | Path, audit: Any = None) -> FastAPI:
    """使用已加载的统一配置组装数据库、应用服务、审计和全部 REST 路由。"""
    if not isinstance(settings, Settings):
        settings = replace(Settings.for_test(), database_path=str(settings))
    components.initialize_process(settings)
    database = Database(settings=settings)
    recall_side_effects = RecallSideEffectDispatcher(database, settings=settings)
    has_provider_line = bool(
        settings.llm_api_key
        or settings.query_expansion_api_key
        or (settings.embedder_mode == "real" and settings.embedding_api_key)
        or (settings.reranker_mode in {"on", "real"} and settings.reranker_api_key)
    )
    provider_runtime = (
        components.create_provider_runtime(settings, create_usage=has_provider_line)
        if has_provider_line or settings.plugins_enabled
        else None
    )
    embedder = components.make_embedder(settings, runtime=provider_runtime)
    reranker = components.make_reranker(settings, runtime=provider_runtime)
    audit = audit or NullAuditLogger()
    deferred_audit = DeferredAuditLogger(audit, recall_side_effects)
    deferred_llm_spans = DeferredLLMSpanRecorder(recall_side_effects)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = database
        database.open_worker()
        try:
            yield
        finally:
            side_effects_closed = recall_side_effects.close(recall_side_effects.recommended_shutdown_timeout)
            audit.close()
            if provider_runtime is not None:
                provider_runtime.close()
            if side_effects_closed:
                database.close()
            else:
                LOGGER.error(
                    "recall side-effect dispatcher did not stop; database connections remain open for thread safety"
                )

    app = FastAPI(title="HL-Mem", version=__version__, lifespan=lifespan)
    app.state.db, app.state.provider_runtime, app.state.reranker = (
        database,
        provider_runtime,
        reranker,
    )
    app.state.settings = settings
    app.state.audit = audit
    app.state.recall_side_effects = recall_side_effects
    app.add_middleware(RequestSizeLimitMiddleware, max_request_body=settings.max_request_body)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(VersionHeaderMiddleware)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        """将资源不存在异常映射为 HTTP 404。"""
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        """将应用验证异常映射为 HTTP 422。"""
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        """将应用状态冲突映射为 HTTP 409。"""
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def get_connection() -> Iterator[sqlite3.Connection]:
        with database.connect() as connection:
            yield connection

    def get_read_connection() -> Iterator[sqlite3.Connection]:
        with database.connect_readonly() as connection:
            yield connection

    def make_recall_service(connection: sqlite3.Connection) -> RecallService:
        """组装 REST recall 与 Hermes 内部 materializer 共用的应用服务。"""
        return RecallService(
            connection,
            embedder,
            reranker,
            RelationExpansionConfig(
                enabled=settings.relation_expansion_mode == "on",
                max_depth=settings.relation_expansion_max_depth,
            ),
            settings,
            components.make_query_expander(
                settings,
                span_recorder=deferred_llm_spans,
                runtime=provider_runtime,
            ),
            side_effect_sink=recall_side_effects,
        )

    def execute_recall(
        payload: RecallInput,
        *,
        query_id: str,
        connection: sqlite3.Connection,
        response_format: str,
        injection_context: InjectionContext,
    ) -> dict[str, Any]:
        """把公开 recall 与内部 receipt-free retrieval 接到同一应用服务。"""
        return make_recall_service(connection).recall(
            query=payload.query,
            limit=payload.limit,
            as_of=payload.as_of,
            intent=payload.intent,
            known_as_of=payload.known_as_of,
            query_id=query_id,
            token_budget=payload.token_budget,
            context_mode=payload.context_mode,
            response_format=response_format,
            namespace=payload.effective_namespace,
            session_id=payload.session_id,
            debug=payload.debug,
            injection_context=injection_context,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        from hl_mem.application.conflict_repairs import count_dangling_conflicts
        from hl_mem.application.health import conflict_backlog_snapshot, monitoring_snapshot, provider_usage_snapshot

        connection = database.connection or database.open_worker()
        conflict_backlog = conflict_backlog_snapshot(connection)
        conflict_open_count = sum(conflict_backlog["conflict_counts_by_status"].values())
        conflict_dangling = count_dangling_conflicts(connection)
        return {
            "status": "ok",
            "version": __version__,
            "compatibility": compatibility_manifest(),
            "vector_backend": str(settings.vector_backend),
            "conflict_open_count": conflict_open_count,
            **conflict_backlog,
            "conflict_dangling": conflict_dangling,
            "embedder": "fake" if isinstance(embedder, FakeEmbedder) else "real",
            "reranker": ("off" if reranker is None else "fake" if isinstance(reranker, FakeReranker) else "real"),
            "settings": settings.snapshot(),
            "components": components.component_health(),
            "providers": ([] if provider_runtime is None else list(provider_runtime.registry.health_snapshot())),
            "provider_usage": provider_usage_snapshot(provider_runtime),
            "vector_search": SearchTracer.vector_search_metrics(),
            "recall_side_effects": recall_side_effect_health(connection, recall_side_effects),
            "monitoring": monitoring_snapshot(
                echo_mode=settings.echo_suppression_mode,
                echo_session_window_seconds=settings.echo_session_window_seconds,
                echo_pending_review_enabled=settings.echo_pending_review_enabled,
                freshness_mode=settings.freshness_annotation_mode,
            ),
        }

    add_conflict_routes(
        app,
        get_connection=get_connection,
        get_read_connection=get_read_connection,
    )

    add_memory_routes(
        app,
        get_connection=get_connection,
        settings=settings,
        audit=audit,
        provider_runtime=provider_runtime,
        embedder=embedder,
        make_extractor=components.make_extractor,
    )
    add_recall_routes(
        app,
        get_connection=get_connection,
        get_read_connection=get_read_connection,
        make_recall_service=make_recall_service,
        execute_recall=execute_recall,
        deferred_audit=deferred_audit,
        settings=settings,
        recall_side_effects=recall_side_effects,
    )
    add_experience_routes(
        app,
        get_connection=get_connection,
        settings=settings,
        recall_side_effects=recall_side_effects,
        embedder=embedder,
    )
    add_maintenance_routes(
        app,
        get_connection=get_connection,
        settings=settings,
        provider_runtime=provider_runtime,
    )

    return app
