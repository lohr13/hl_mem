from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from hl_mem.api.pipeline import hybrid_claims, new_id, stale_observations, store_extracted
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embeddings import Embedder, FakeEmbedder
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import ExtractedClaim, FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
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


class MemoryInput(BaseModel):
    text: str | None = None
    content: str | None = None
    subject: str = "用户"
    predicate: str = "explicit_memory"
    qualifiers: dict[str, Any] = Field(default_factory=dict)


def _make_embedder() -> Any:
    dim = int(os.getenv("EMBEDDING_DIM", "2048"))
    mode = os.getenv("HL_MEM_EMBEDDER", "fake").lower()
    if mode == "fake":
        return FakeEmbedder(dim)
    if mode != "real":
        raise ValueError("HL_MEM_EMBEDDER must be 'fake' or 'real'")
    return Embedder(os.environ["EMBEDDING_API_KEY"], os.getenv(
        "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        os.getenv("EMBEDDING_MODEL", "text-embedding-v4"), dim)


def create_app(database_path: str | Path | None = None) -> FastAPI:
    path = database_path or os.getenv("HL_MEM_DB_PATH", "hl_mem.db")
    database, event_filter, embedder = Database(path), EventFilter(), _make_embedder()
    budget = TokenBudget(int(os.getenv("HL_MEM_DAILY_TOKEN_LIMIT", "500000")), Path(path).with_suffix(".budget.json"))
    extractor_name = os.getenv("HL_MEM_EXTRACTOR", "fake").lower()
    extractor: Any = FakeExtractor() if extractor_name == "fake" else LLMExtractor(
        os.environ["LLM_API_KEY"], os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
        os.getenv("LLM_MODEL", "qwen3.7-plus"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = database
        database.open()
        yield
        database.close()

    app = FastAPI(title="HL-Mem", lifespan=lifespan)
    app.state.db, app.state.token_budget, app.state.extractor = database, budget, extractor

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        database.open().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/v1/events")
    def post_event(payload: EventInput, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        connection, events = database.open(), EventRepository(database.open())
        key = idempotency_key or payload.idempotency_key
        existing = connection.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone() if key else None
        if existing:
            return {"id": existing["id"], "created": False}
        event_id, timestamp = payload.id or new_id(), _now()
        content = payload.content if isinstance(payload.content, dict) else {"text": payload.content}
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        event = payload.model_dump(exclude={"content", "id"})
        event.update(id=event_id, idempotency_key=key, content_json=content_json,
                     occurred_at=payload.occurred_at or timestamp, recorded_at=timestamp,
                     content_hash=hashlib.sha256(content_json.encode()).hexdigest())
        created = events.insert_event(event)
        if created:
            _queue_and_extract(connection, event, content, timestamp, event_filter, budget, extractor, embedder)
        return {"id": event_id, "created": created}

    @app.post("/v1/recall")
    def recall(payload: RecallInput, request: Request) -> dict[str, Any]:
        connection = database.open()
        claims = hybrid_claims(ClaimRepository(connection), payload.query,
                               embedder.embed_one(payload.query), payload.limit, payload.as_of)
        evidence_repo, results = EvidenceRepository(connection), []
        for claim in claims:
            evidence = [{"type": "event", "id": link["evidence_id"]} for link in
                        evidence_repo.get_links_for_derived("claim", claim["id"])]
            results.append({"type": "claim", "id": claim["id"], "text": json.loads(claim["value_json"]),
                            "status": claim["status"], "confidence": claim["confidence"],
                            "valid_from": claim["valid_from"], "evidence": evidence})
        observations = [dict(row) for row in connection.execute(
            "SELECT * FROM derivations WHERE kind='observation' AND status='active'").fetchall()]
        observation_results = [{"type": "observation", "id": item["id"], "text": item["body"],
                                "status": item["status"], "confidence": item["confidence"]}
                               for item in observations]
        return {"results": results + observation_results, "observations": observations,
                "total": len(results) + len(observation_results),
                "query_id": request.headers.get("X-Request-ID", new_id())}

    @app.post("/v1/memories")
    def save_memory(payload: MemoryInput) -> dict[str, str]:
        now, event_id = _now(), new_id()
        text = payload.text or payload.content
        if not text:
            raise HTTPException(422, "text or content is required")
        event = {"id": event_id, "idempotency_key": None, "tenant_id": "default", "event_type": "explicit_memory",
                 "actor_type": "user", "content_json": json.dumps({"text": text}, ensure_ascii=False),
                 "occurred_at": now, "recorded_at": now}
        EventRepository(database.open()).insert_event(event)
        claim = ExtractedClaim(payload.predicate, text, 1.0, "stable", payload.subject, payload.qualifiers)
        return {"id": store_extracted(database.open(), claim, event, now, embedder, "high")}

    @app.delete("/v1/memories/{memory_id}")
    def forget(memory_id: str) -> dict[str, Any]:
        repo = ClaimRepository(database.open())
        if not repo.get_claim(memory_id):
            raise HTTPException(404, "memory not found")
        repo.retract(memory_id)
        stale_observations(database.open(), memory_id)
        return {"id": memory_id, "forgotten": True}

    @app.get("/v1/stats")
    def stats() -> dict[str, Any]:
        connection, token_stats = database.open(), budget.get_stats()
        return {"events": connection.execute("SELECT count(*) FROM events").fetchone()[0],
                "claims": connection.execute("SELECT count(*) FROM claims").fetchone()[0],
                "tokens_today": token_stats["used_tokens"], "jobs_pending": connection.execute(
                    "SELECT count(*) FROM jobs WHERE status='pending'").fetchone()[0]}
    return app


def _queue_and_extract(connection: Any, event: dict[str, Any], content: dict[str, Any], now: str,
                       event_filter: EventFilter, budget: TokenBudget, extractor: Any, embedder: Any) -> None:
    should_extract, reason = event_filter.should_extract({**event, "content": content})
    if not should_extract:
        logging.getLogger(__name__).info("event extraction skipped: %s", reason)
        return
    JobRepository(connection).insert_job({"id": new_id(), "job_type": "extract_event",
        "payload_json": json.dumps({"event_id": event["id"]}), "idempotency_key": f"extract:{event['id']}",
        "created_at": now, "updated_at": now})
    if not budget.can_spend(max(1, len(json.dumps(content, ensure_ascii=False)) // 2)):
        return
    try:
        extracted = extractor.extract(content, event) if isinstance(extractor, LLMExtractor) else extractor.extract(content)
    except Exception:
        logging.getLogger(__name__).exception("event extraction failed; job remains pending")
        return
    if isinstance(extractor, LLMExtractor):
        budget.record_usage(extractor.last_usage_tokens)
        event["extractor"] = "llm"
    for claim in extracted:
        store_extracted(connection, claim, event, now, embedder)


app = create_app()
