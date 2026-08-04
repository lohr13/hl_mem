"""HL-Mem FastAPI 应用工厂与 REST 路由适配层。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from hl_mem import __version__, components
from hl_mem.api.schemas import (
    ConsolidationScopeInput,
    ContextPacketRecallOutput,
    DryRunExtractionInput,
    EpisodeInput,
    EpisodeUpdate,
    EventInput,
    FeedbackInput,
    MemoryCorrectionInput,
    MemoryCorrectionOutput,
    MemoryDetailOutput,
    MemoryInput,
    MemoryListOutput,
    MemorySaveOutput,
    RecallInput,
    RecallOutput,
    TraceInput,
)
from hl_mem.application.context_packet import (
    UnknownSchemaMajorError,
    retrieval_bundle_from_dict,
)
from hl_mem.application.correction import CorrectionService
from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService, new_id
from hl_mem.application.memories import MemoryQueryService
from hl_mem.application.recall import RecallService, recall_side_effect_health
from hl_mem.errors import ConflictError, NotFoundError, ValidationError
from hl_mem.experience.service import (
    ExperienceService,
    InvalidStateTransitionError,
    backprop_episode_reward,
)
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.observability.audit import NullAuditLogger, audit_scope
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.recall.reranker import FakeReranker
from hl_mem.recall.trace import SearchTracer
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.jobs import JobRepository

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_namespace_alias(namespace: str | None, tenant_id: str | None) -> str:
    """解析查询参数中的 deprecated tenant_id alias。"""
    if namespace is not None and tenant_id is not None and namespace != tenant_id:
        raise HTTPException(422, "namespace and deprecated tenant_id must match")
    return namespace or tenant_id or "default"


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """根据 Content-Length 拒绝超过配置上限的请求体，并返回 HTTP 413。"""

    def __init__(self, app: Any, max_request_body: int) -> None:
        super().__init__(app)
        self.max_request_body = max_request_body

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """检查请求体声明长度，并继续处理未超限的请求。"""
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_request_body:
            return Response(status_code=413, content="Request body too large")
        return await call_next(request)


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


def create_app(settings: Settings | str | Path, audit: Any = None) -> FastAPI:
    """使用已加载的统一配置组装数据库、应用服务、审计和全部 REST 路由。"""
    if not isinstance(settings, Settings):
        settings = replace(Settings.for_test(), database_path=str(settings))
    components.initialize_process(settings)
    database = Database(settings=settings)
    embedder = components.make_embedder(settings)
    reranker = components.make_reranker(settings)
    budget = TokenBudget(settings.daily_token_limit, Path(database.path).with_suffix(".budget.db"))
    audit = audit or NullAuditLogger()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = database
        database.open_worker()
        try:
            yield
        finally:
            audit.close()
            database.close()

    app = FastAPI(title="HL-Mem", lifespan=lifespan)
    app.state.db, app.state.token_budget, app.state.reranker = (
        database,
        budget,
        reranker,
    )
    app.state.settings = settings
    app.state.audit = audit
    app.add_middleware(RequestSizeLimitMiddleware, max_request_body=settings.max_request_body)
    app.add_middleware(RequestLoggingMiddleware)

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
            components.make_query_expander(settings, connection),
        )

    def execute_recall(
        payload: RecallInput,
        *,
        query_id: str,
        connection: sqlite3.Connection,
        response_format: str,
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
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        from hl_mem.application.health import monitoring_snapshot

        connection = database.connection or database.open_worker()
        conflict_open_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM conflict_cases "
                "WHERE status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL"
            ).fetchone()[0]
        )
        return {
            "status": "ok",
            "version": __version__,
            "conflict_open_count": conflict_open_count,
            "embedder": "fake" if isinstance(embedder, FakeEmbedder) else "real",
            "reranker": ("off" if reranker is None else "fake" if isinstance(reranker, FakeReranker) else "real"),
            "settings": settings.snapshot(),
            "components": components.component_health(),
            "vector_search": SearchTracer.vector_search_metrics(),
            "recall_side_effects": recall_side_effect_health(),
            "monitoring": monitoring_snapshot(),
        }

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

    @app.post("/v1/extract/dry-run")
    def dry_run_extract(
        payload: DryRunExtractionInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """提取候选 claims 与 token 用量，但不持久化记忆数据。"""
        extractor = components.make_extractor(settings, require_real=True, connection=connection)
        return IngestService.dry_run_extract(
            extractor,
            payload.text,
            payload.context,
            payload.custom_instructions,
        )

    @app.post("/v1/consolidate")
    def consolidate(
        payload: ConsolidationScopeInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, str]:
        """创建带显式作用域的冲突归并任务。"""
        job_id = new_id()
        now = _now()
        JobRepository(connection).insert_job(
            {
                "id": job_id,
                "job_type": "consolidate_conflicts",
                "payload": payload.model_dump(),
                "created_at": now,
                "updated_at": now,
            }
        )
        return {"id": job_id}

    @app.post(
        "/v1/recall",
        response_model=RecallOutput | ContextPacketRecallOutput,
        response_model_exclude_none=True,
    )
    def recall(
        payload: RecallInput,
        request: Request,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        query_id = request.headers.get("X-Request-ID") or new_id()
        with audit_scope(
            audit,
            trace_id=query_id,
            query_id=query_id,
            tenant_id=payload.effective_namespace,
        ):
            return execute_recall(
                payload,
                query_id=query_id,
                connection=connection,
                response_format=payload.response_format,
            )

    @app.post(
        "/v1/internal/retrieval-bundles",
        include_in_schema=False,
    )
    def retrieve_bundle(
        payload: RecallInput,
        request: Request,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        """为 Hermes 预取 receipt-free bundle；旧 daemon 会安全返回 404。"""
        query_id = request.headers.get("X-Request-ID") or new_id()
        with audit_scope(
            audit,
            trace_id=query_id,
            query_id=query_id,
            tenant_id=payload.effective_namespace,
        ):
            return execute_recall(
                payload,
                query_id=query_id,
                connection=connection,
                response_format="retrieval_bundle",
            )

    @app.post(
        "/v1/internal/context-packets/materialize",
        include_in_schema=False,
    )
    def materialize_context_packet(
        payload: dict[str, Any],
        connection: sqlite3.Connection = Depends(get_connection),
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
            updated = ExperienceService(connection).mark_feedback_injected_batch(feedback_ids)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return {"updated": updated}

    @app.post("/v1/episodes")
    def create_episode(
        payload: EpisodeInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        episode_id = new_id()
        service = ExperienceService(connection)
        service.create_episode(
            episode_id,
            payload.goal,
            _now(),
            payload.session_id,
            payload.task_type,
            namespace=payload.effective_namespace,
        )
        return service.get_episode(episode_id)

    @app.post("/v1/feedback")
    def post_feedback(
        payload: FeedbackInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        try:
            result: dict[str, Any] = ExperienceService(connection, settings=settings).submit_retrieval_feedback(
                payload.feedback_id, payload.helpful, payload.task_outcome, _now()
            )
        except ValueError as error:
            if str(error).startswith("feedback exposure not found:"):
                raise HTTPException(404, str(error)) from error
            raise
        correction = payload.correction
        if correction is None:
            return result
        correction_result = CorrectionService(connection, embedder, settings=settings).apply(
            correction.memory_id,
            action=correction.action,
            corrected_text=correction.corrected_text,
            idempotency_key=correction.idempotency_key,
        )
        result["correction"] = correction_result
        result["correction_event_id"] = correction_result["correction_event_id"]
        return result

    @app.post("/v1/episodes/{episode_id}/traces")
    def add_episode_trace(
        episode_id: str,
        payload: TraceInput,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        service = ExperienceService(connection)
        try:
            trace_id = service.add_trace(
                episode_id,
                payload.action,
                payload.observation,
                payload.error_signature,
                payload.value,
            )
        except InvalidStateTransitionError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return {"id": trace_id, "episode_id": episode_id}

    @app.patch("/v1/episodes/{episode_id}")
    def update_episode(
        episode_id: str,
        payload: EpisodeUpdate,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        service = ExperienceService(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = service.update_episode(
                episode_id,
                _now(),
                payload.status,
                payload.reward,
                payload.outcome_summary,
                commit=False,
            )
            if payload.reward is not None:
                backprop_episode_reward(connection, episode_id, payload.reward, commit=False)
                updated = service.get_episode(episode_id)
            connection.commit()
            return updated
        except InvalidStateTransitionError as error:
            connection.rollback()
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            connection.rollback()
            raise HTTPException(404, str(error)) from error
        except Exception:
            connection.rollback()
            raise

    @app.get("/v1/episodes")
    def list_episodes(
        limit: int = 20,
        status: str | None = None,
        namespace: str | None = Query(default=None, min_length=1, max_length=100),
        tenant_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=100,
            deprecated=True,
        ),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit must be between 1 and 100")
        effective_namespace = _resolve_namespace_alias(namespace, tenant_id)
        return {
            "episodes": ExperienceService(connection).list_episodes(
                limit,
                status,
                namespace=effective_namespace,
            )
        }

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(episode_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        try:
            return ExperienceService(connection).get_episode(episode_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/policies")
    def list_policies(
        status: str = "active",
        namespace: str | None = Query(default=None, min_length=1, max_length=100),
        tenant_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=100,
            deprecated=True,
        ),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        effective_namespace = _resolve_namespace_alias(namespace, tenant_id)
        return {
            "policies": ExperienceService(connection).list_policies(
                status,
                namespace=effective_namespace,
            )
        }

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
            namespace=_resolve_namespace_alias(namespace, tenant_id),
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

    @app.get("/v1/stats")
    def stats(
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        token_stats = budget.get_stats()
        return {
            "events": connection.execute("SELECT count(*) FROM events").fetchone()[0],
            "claims": connection.execute("SELECT count(*) FROM claims").fetchone()[0],
            "tokens_today": token_stats["used_tokens"],
            "jobs_pending": connection.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0],
        }

    @app.get("/v1/jobs")
    def jobs(
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        repository = JobRepository(connection)
        return {**repository.counts(), "jobs": repository.list_jobs()}

    return app
