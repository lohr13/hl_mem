from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from hl_mem import __version__, components
from hl_mem.api.schemas import (
    ConsolidationScopeInput,
    DryRunExtractionInput,
    EpisodeInput,
    EpisodeUpdate,
    EventInput,
    FeedbackInput,
    MemoryInput,
    RecallInput,
    RecallOutput,
    TraceInput,
)
from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService, new_id
from hl_mem.application.recall import RecallService
from hl_mem.errors import ConflictError, NotFoundError, ValidationError
from hl_mem.experience.service import ExperienceService, InvalidStateTransitionError, backprop_episode_reward
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.observability.audit import NullAuditLogger, audit_scope
from hl_mem.observability.llm_spans import llm_span_stats
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.recall.reranker import FakeReranker
from hl_mem.recall.trace import SearchTracer
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.jobs import JobRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """根据 Content-Length 拒绝超过配置上限的请求体，并返回 HTTP 413。"""

    def __init__(self, app: Any, max_request_body: int) -> None:
        super().__init__(app)
        self.max_request_body = max_request_body

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """检查请求体声明长度，并继续处理未超限的请求。"""
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_request_body:
            return Response(status_code=413, content="Request body too large")
        return await call_next(request)


def create_app(database_path: str | Path | None = None, audit: Any = None) -> FastAPI:
    settings = Settings.from_env()
    database = Database(database_path or settings.database_path)
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
    app.state.db, app.state.token_budget, app.state.reranker = database, budget, reranker
    app.state.settings = settings
    app.state.audit = audit
    app.add_middleware(RequestSizeLimitMiddleware, max_request_body=settings.max_request_body)

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

    @app.get("/healthz")
    def healthz(connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        connection.execute("SELECT 1").fetchone()
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        operations = llm_span_stats(connection, since)["operations"]
        return {
            "status": "ok",
            "version": __version__,
            "embedder": "fake" if isinstance(embedder, FakeEmbedder) else "real",
            "reranker": ("off" if reranker is None else "fake" if isinstance(reranker, FakeReranker) else "real"),
            "settings": settings.snapshot(),
            "llm_stats": {
                "calls": sum(item["count"] for item in operations),
                "total_tokens": sum(item["total_tokens"] for item in operations),
            },
            "vector_search": SearchTracer.vector_search_metrics(),
        }

    @app.post("/v1/events")
    def post_event(
        payload: EventInput,
        idempotency_key: str | None = Header(default=None),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        key = idempotency_key or payload.idempotency_key
        content = payload.content if isinstance(payload.content, dict) else {"text": payload.content}
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        event = payload.model_dump()
        service = IngestService(connection)
        result = service.ingest_event(event, key)
        event_id, created = result["id"], result["created"]
        audit.emit(
            "ingest",
            "accepted",
            "queued" if created else "duplicate",
            trace_id=event_id,
            event_id=event_id,
            tenant_id=payload.tenant_id,
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

    @app.post("/v1/recall", response_model=RecallOutput, response_model_exclude_none=True)
    def recall(
        payload: RecallInput,
        request: Request,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        query_id = request.headers.get("X-Request-ID") or new_id()
        with audit_scope(audit, trace_id=query_id, query_id=query_id, tenant_id="default"):
            return RecallService(
                connection,
                embedder,
                reranker,
                RelationExpansionConfig(
                    enabled=settings.relation_expansion_mode == "on",
                    max_depth=settings.relation_expansion_max_depth,
                ),
                settings,
            ).recall(
                payload.query,
                payload.limit,
                payload.as_of,
                intent=payload.intent,
                known_as_of=payload.known_as_of,
                query_id=query_id,
                token_budget=payload.token_budget,
                context_mode=payload.context_mode,
                namespace=payload.namespace,
                debug=payload.debug,
            )

    @app.post("/v1/episodes")
    def create_episode(
        payload: EpisodeInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        episode_id = new_id()
        service = ExperienceService(connection)
        service.create_episode(episode_id, payload.goal, _now(), payload.session_id, payload.task_type)
        return service.get_episode(episode_id)

    @app.post("/v1/feedback")
    def post_feedback(
        payload: FeedbackInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, bool]:
        return ExperienceService(connection).submit_retrieval_feedback(
            payload.query_id, payload.memory_id, payload.helpful, payload.task_outcome, _now()
        )

    @app.post("/v1/episodes/{episode_id}/traces")
    def add_episode_trace(
        episode_id: str, payload: TraceInput, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        service = ExperienceService(connection)
        try:
            trace_id = service.add_trace(
                episode_id, payload.action, payload.observation, payload.error_signature, payload.value
            )
        except InvalidStateTransitionError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return {"id": trace_id, "episode_id": episode_id}

    @app.patch("/v1/episodes/{episode_id}")
    def update_episode(
        episode_id: str, payload: EpisodeUpdate, connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        service = ExperienceService(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = service.update_episode(
                episode_id, _now(), payload.status, payload.reward, payload.outcome_summary, commit=False
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
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit must be between 1 and 100")
        return {"episodes": ExperienceService(connection).list_episodes(limit, status)}

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(episode_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        try:
            return ExperienceService(connection).get_episode(episode_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/policies")
    def list_policies(
        status: str = "active", connection: sqlite3.Connection = Depends(get_connection)
    ) -> dict[str, Any]:
        return {"policies": ExperienceService(connection).list_policies(status)}

    @app.post("/v1/memories")
    def save_memory(payload: MemoryInput, connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, str]:
        text = payload.text or payload.content
        if not text:
            raise HTTPException(422, "text or content is required")
        service = IngestService(connection)
        result = service.save_explicit_memory(text, payload.subject, payload.predicate, payload.qualifiers)
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
            "queued",
            trace_id=event_id,
            event_id=event_id,
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
    def stats(connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        token_stats = budget.get_stats()
        return {
            "events": connection.execute("SELECT count(*) FROM events").fetchone()[0],
            "claims": connection.execute("SELECT count(*) FROM claims").fetchone()[0],
            "tokens_today": token_stats["used_tokens"],
            "jobs_pending": connection.execute("SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0],
        }

    @app.get("/v1/jobs")
    def jobs(connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        repository = JobRepository(connection)
        return {**repository.counts(), "jobs": repository.list_jobs()}

    return app


app = create_app()
