from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from hl_mem import __version__
from hl_mem.api.pipeline import new_id
from hl_mem.experience.service import ExperienceService, InvalidStateTransitionError, backprop_episode_reward
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embeddings import Embedder, FakeEmbedder
from hl_mem.observability.audit import NullAuditLogger, audit_scope
from hl_mem.recall.policy import RecallIntent, route_recall_intent
from hl_mem.recall.recall_pipeline import hybrid_claims, matching_policies, stale_observations
from hl_mem.recall.reranker import FakeReranker, Reranker
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository, EventRepository, EvidenceRepository, JobRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventInput(BaseModel):
    id: str | None = None
    idempotency_key: str | None = None
    tenant_id: str = "default"
    user_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    event_type: str = "message"
    actor_type: str = "user"
    actor_id: str | None = None
    content: dict[str, Any] | str = Field(default_factory=dict)
    occurred_at: str | None = None
    source_uri: str | None = None
    sensitivity: str = "normal"


class RecallInput(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=100)
    as_of: str | None = None
    session_id: str | None = None
    intent: RecallIntent | None = None
    known_as_of: str | None = None


class MemoryInput(BaseModel):
    text: str | None = None
    content: str | None = None
    subject: str = "用户"
    predicate: str = "explicit_memory"
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class EpisodeInput(BaseModel):
    """创建 Episode 的请求。"""

    goal: str = Field(min_length=1)
    session_id: str | None = None
    task_type: str | None = None


class TraceInput(BaseModel):
    """追加 Episode Trace 的请求。"""

    action: str = Field(min_length=1)
    observation: str | None = None
    error_signature: str | None = None
    value: float = 0.0


class EpisodeUpdate(BaseModel):
    """更新 Episode 结果的请求。"""

    status: str | None = None
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    outcome_summary: str | None = None


class FeedbackInput(BaseModel):
    """检索结果反馈请求。"""

    query_id: str = Field(min_length=1)
    memory_id: str = Field(min_length=1)
    helpful: bool
    task_outcome: str | None = None


def _make_embedder() -> Any:
    dim = int(os.getenv("EMBEDDING_DIM", "2048"))
    production = os.getenv("HL_MEM_ENV", "dev").lower() == "production"
    mode = os.getenv("HL_MEM_EMBEDDER", "real" if production else "fake").lower()
    if production and mode != "real":
        raise RuntimeError("HL_MEM_EMBEDDER must be 'real' in production")
    if mode == "fake":
        return FakeEmbedder(dim)
    if mode != "real":
        raise ValueError("HL_MEM_EMBEDDER must be 'fake' or 'real'")
    api_key = os.getenv("EMBEDDING_API_KEY")
    if not api_key:
        if production:
            raise RuntimeError("EMBEDDING_API_KEY is required in production")
        return FakeEmbedder(dim)
    return Embedder(
        api_key,
        os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        dim,
        float(os.getenv("EMBEDDING_CONNECT_TIMEOUT", "5")),
        float(os.getenv("EMBEDDING_READ_TIMEOUT", "30")),
        int(os.getenv("EMBEDDING_MAX_ATTEMPTS", "3")),
    )


def _make_reranker() -> Any:
    production = os.getenv("HL_MEM_ENV", "dev").lower() == "production"
    mode = os.getenv("HL_MEM_RERANKER", "real" if production else "off").lower()
    if production and mode not in {"on", "real"}:
        raise RuntimeError("HL_MEM_RERANKER must be enabled in production")
    if mode == "off":
        return None
    if mode == "fake":
        return FakeReranker()
    if mode not in {"on", "real"}:
        raise ValueError("HL_MEM_RERANKER must be 'off', 'fake', 'on', or 'real'")
    api_key = os.getenv("RERANKER_API_KEY") or os.getenv("EMBEDDING_API_KEY")
    if not api_key:
        if production:
            raise RuntimeError("RERANKER_API_KEY or EMBEDDING_API_KEY is required in production")
        return None
    try:
        return Reranker(
            api_key,
            os.getenv("RERANKER_BASE_URL", "https://dashscope.aliyuncs.com"),
            os.getenv("RERANKER_MODEL", "gte-rerank-v2"),
        )
    except Exception:
        if production:
            raise
        return None


def create_app(database_path: str | Path | None = None, audit: Any = None) -> FastAPI:
    database, embedder, reranker = Database(database_path), _make_embedder(), _make_reranker()
    budget = TokenBudget(
        int(os.getenv("HL_MEM_DAILY_TOKEN_LIMIT", "500000")), Path(database.path).with_suffix(".budget.db")
    )
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
    app.state.audit = audit

    def get_connection() -> Iterator[sqlite3.Connection]:
        with database.connect() as connection:
            yield connection

    @app.get("/healthz")
    def healthz(connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, str]:
        connection.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "version": __version__,
            "embedder": "fake" if isinstance(embedder, FakeEmbedder) else "real",
            "reranker": ("off" if reranker is None else "fake" if isinstance(reranker, FakeReranker) else "real"),
        }

    @app.post("/v1/events")
    def post_event(
        payload: EventInput,
        idempotency_key: str | None = Header(default=None),
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        events = EventRepository(connection)
        key = idempotency_key or payload.idempotency_key
        existing = (
            connection.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone() if key else None
        )
        if existing:
            audit.emit(
                "ingest",
                "accepted",
                "duplicate",
                trace_id=existing["id"],
                event_id=existing["id"],
                tenant_id=payload.tenant_id,
                detail={"event_type": payload.event_type, "actor_type": payload.actor_type},
            )
            return {"id": existing["id"], "created": False}
        event_id, timestamp = payload.id or new_id(), _now()
        content = payload.content if isinstance(payload.content, dict) else {"text": payload.content}
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        event = payload.model_dump(exclude={"content", "id"})
        event.update(
            id=event_id,
            idempotency_key=key,
            content_json=content_json,
            occurred_at=payload.occurred_at or timestamp,
            recorded_at=timestamp,
            content_hash=hashlib.sha256(content_json.encode()).hexdigest(),
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            created = events.insert_event(event, commit=False)
            if created:
                _queue_event(connection, event_id, timestamp, commit=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
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
                "content_hash": event["content_hash"],
                "sensitivity": payload.sensitivity,
            },
        )
        return {"id": event_id, "created": created}

    @app.post("/v1/recall")
    def recall(
        payload: RecallInput,
        request: Request,
        connection: sqlite3.Connection = Depends(get_connection),
    ) -> dict[str, Any]:
        query_id = request.headers.get("X-Request-ID") or new_id()
        with audit_scope(audit, trace_id=query_id, query_id=query_id, tenant_id="default"):
            intent = payload.intent or route_recall_intent(payload.query, payload.as_of)
            claims = hybrid_claims(
                ClaimRepository(connection),
                payload.query,
                embedder.embed_one(payload.query),
                payload.limit,
                payload.as_of,
                reranker,
                intent=intent,
                known_as_of=payload.known_as_of,
            )
            try:
                ClaimRepository(connection).record_access([claim["id"] for claim in claims], _now())
            except Exception as error:
                try:
                    audit.emit(
                        "recall",
                        "access_record",
                        "access_record_failed",
                        detail={"error_class": type(error).__name__, "claim_count": len(claims)},
                    )
                except Exception:
                    pass
            try:
                recorded_at = _now()
                ExperienceService(connection).record_feedback_batch(
                    [
                        (
                            new_id(),
                            query_id,
                            "claim",
                            claim["id"],
                            rank,
                            float(claim.get("_score", 0.0)),
                            0,
                            None,
                            None,
                            recorded_at,
                        )
                        for rank, claim in enumerate(claims, 1)
                    ]
                )
            except Exception as error:
                try:
                    audit.emit(
                        "recall",
                        "feedback_record",
                        "feedback_record_failed",
                        detail={"error_class": type(error).__name__, "claim_count": len(claims)},
                    )
                except Exception:
                    pass
        evidence_repo, results = EvidenceRepository(connection), []
        for claim in claims:
            evidence = [
                {"type": "event", "id": link["evidence_id"]}
                for link in evidence_repo.get_links_for_derived("claim", claim["id"])
            ]
            decoded = json.loads(claim["value_json"])
            text = (
                decoded.get("old_value")
                if isinstance(decoded, dict) and decoded.get("_type") == "superseded_value"
                else decoded
            )
            replacement = None
            if claim.get("superseded_by_id"):
                replacement_claim = ClaimRepository(connection).get_claim(claim["superseded_by_id"])
                if replacement_claim:
                    replacement = {
                        "id": replacement_claim["id"],
                        "text": json.loads(replacement_claim["value_json"]),
                        "valid_from": replacement_claim["valid_from"],
                    }
            results.append(
                {
                    "type": "claim",
                    "id": claim["id"],
                    "text": text,
                    "status": claim["status"],
                    "confidence": claim["confidence"],
                    "valid_from": claim["valid_from"],
                    "replacement": replacement,
                    "evidence": evidence,
                }
            )
        policies = matching_policies(ExperienceService(connection).list_policies("active"), payload.query)
        return {
            "results": results,
            "observations": [],
            "policies": policies,
            "total": len(results),
            "query_id": query_id,
        }

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
        updated = ExperienceService(connection).submit_retrieval_feedback(
            payload.query_id, payload.memory_id, payload.helpful, payload.task_outcome, _now()
        )
        return {"updated": updated}

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
        now, event_id = _now(), new_id()
        text = payload.text or payload.content
        if not text:
            raise HTTPException(422, "text or content is required")
        memory = {
            "text": text,
            "subject": payload.subject,
            "predicate": payload.predicate,
            "qualifiers": payload.qualifiers,
        }
        event = {
            "id": event_id,
            "idempotency_key": None,
            "tenant_id": "default",
            "event_type": "explicit_memory",
            "actor_type": "user",
            "content_json": json.dumps({"text": text, "memory": memory}, ensure_ascii=False),
            "occurred_at": now,
            "recorded_at": now,
        }
        try:
            connection.execute("BEGIN IMMEDIATE")
            EventRepository(connection).insert_event(event, commit=False)
            _queue_event(connection, event_id, now, commit=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        audit.emit(
            "ingest",
            "accepted",
            "queued",
            trace_id=event_id,
            event_id=event_id,
            detail={
                "event_type": "explicit_memory",
                "actor_type": "user",
                "content_chars": len(event["content_json"]),
                "sensitivity": "normal",
            },
        )
        return {"id": event_id}

    @app.delete("/v1/memories/{memory_id}")
    def forget(memory_id: str, connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, Any]:
        repo = ClaimRepository(connection)
        if not repo.get_claim(memory_id):
            raise HTTPException(404, "memory not found")
        repo.retract(memory_id)
        stale_observations(connection, memory_id)
        return {"id": memory_id, "forgotten": True}

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
    def jobs(connection: sqlite3.Connection = Depends(get_connection)) -> dict[str, int]:
        return JobRepository(connection).counts()

    return app


def _queue_event(connection: Any, event_id: str, now: str, commit: bool = True) -> None:
    JobRepository(connection).insert_job(
        {
            "id": new_id(),
            "job_type": "extract_event",
            "payload_json": json.dumps({"event_id": event_id}),
            "idempotency_key": f"extract:{event_id}",
            "created_at": now,
            "updated_at": now,
        },
        commit=commit,
    )


app = create_app()
