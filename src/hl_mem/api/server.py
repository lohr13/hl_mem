from __future__ import annotations

import hashlib
import json
import os
import uuid
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, Request
from pydantic import BaseModel, Field

from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository, EventRepository, EvidenceRepository, JobRepository

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _id() -> str:
    return uuid.uuid4().hex

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

def create_app(database_path: str | Path | None = None) -> FastAPI:
    path = database_path or os.getenv("HL_MEM_DB_PATH", "hl_mem.db")
    database = Database(path)
    budget_path = Path(path).with_suffix(".budget.json")
    budget = TokenBudget(int(os.getenv("HL_MEM_DAILY_TOKEN_LIMIT", "500000")), budget_path)
    event_filter = EventFilter()
    extractor_name = os.getenv("HL_MEM_EXTRACTOR", "fake").lower()
    if extractor_name == "llm":
        extractor: Any = LLMExtractor(
            os.environ["LLM_API_KEY"],
            os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
            os.getenv("LLM_MODEL", "qwen3.7-plus"),
        )
    elif extractor_name == "fake":
        extractor = FakeExtractor()
    else:
        raise ValueError("HL_MEM_EXTRACTOR must be 'fake' or 'llm'")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = database
        database.open()
        yield
        database.close()

    app = FastAPI(title="HL-Mem", lifespan=lifespan)
    app.state.db = database
    app.state.token_budget = budget
    app.state.extractor = extractor

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        database.open().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/v1/events")
    def post_event(payload: EventInput, idempotency_key: str | None = Header(default=None)) -> dict[str, Any]:
        connection = database.open()
        events = EventRepository(connection)
        key = idempotency_key or payload.idempotency_key
        if key:
            existing = connection.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                return {"id": existing["id"], "created": False}
        event_id = payload.id or _id()
        timestamp = _now()
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
        created = events.insert_event(event)
        if created:
            _queue_and_extract(connection, event, content, timestamp, event_filter, budget, extractor)
        return {"id": event_id, "created": created}

    @app.post("/v1/recall")
    def recall(payload: RecallInput, request: Request) -> dict[str, Any]:
        connection = database.open()
        claims = ClaimRepository(connection).search_claims_fts(
            payload.query, payload.limit, payload.as_of
        )
        evidence_repo = EvidenceRepository(connection)
        results = []
        for claim in claims:
            evidence = []
            for link in evidence_repo.get_links_for_derived("claim", claim["id"]):
                event = EventRepository(connection).get_event(link["evidence_id"])
                if event:
                    evidence.append(
                        {"type": "event", "id": event["id"], "occurred_at": event["occurred_at"]}
                    )
            results.append(
                {
                    "type": "claim",
                    "id": claim["id"],
                    "text": json.loads(claim["value_json"]),
                    "status": claim["status"],
                    "confidence": claim["confidence"],
                    "valid_from": claim["valid_from"],
                    "evidence": evidence,
                }
            )
        return {"results": results, "total": len(results), "query_id": request.headers.get("X-Request-ID", _id())}

    @app.get("/v1/stats")
    def stats() -> dict[str, Any]:
        connection = database.open()
        token_stats = budget.get_stats()
        return {
            "events": connection.execute("SELECT count(*) FROM events").fetchone()[0],
            "claims": connection.execute("SELECT count(*) FROM claims").fetchone()[0],
            "tokens_today": token_stats["used_tokens"],
            "jobs_pending": connection.execute(
                "SELECT count(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0],
        }

    return app

def _queue_and_extract(
    connection: Any, event: dict[str, Any], content: dict[str, Any], now: str,
    event_filter: EventFilter, budget: TokenBudget, extractor: Any,
) -> None:
    should_extract, reason = event_filter.should_extract({**event, "content": content})
    if not should_extract:
        logging.getLogger(__name__).info("event extraction skipped: %s", reason)
        return
    job_id = _id()
    JobRepository(connection).insert_job(
        {"id": job_id, "job_type": "extract_event", "payload_json": json.dumps({"event_id": event["id"]}),
         "idempotency_key": f"extract:{event['id']}", "created_at": now, "updated_at": now}
    )
    estimated_tokens = max(1, len(json.dumps(content, ensure_ascii=False)) // 2)
    if not budget.can_spend(estimated_tokens):
        return
    try:
        extracted_claims = extractor.extract(content, event) if isinstance(extractor, LLMExtractor) else extractor.extract(content)
    except Exception:
        if isinstance(extractor, LLMExtractor) and extractor.last_usage_tokens:
            budget.record_usage(extractor.last_usage_tokens)
        logging.getLogger(__name__).exception("event extraction failed; job remains pending")
        return
    if isinstance(extractor, LLMExtractor):
        budget.record_usage(extractor.last_usage_tokens)
    for extracted in extracted_claims:
        claim_id = _id()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat() if extracted.volatility == "ephemeral" else None
        ClaimRepository(connection).insert_claim(
            {"id": claim_id, "predicate": extracted.predicate, "value_json": json.dumps(extracted.value, ensure_ascii=False),
             "qualifiers_json": json.dumps(extracted.qualifiers or {}, ensure_ascii=False),
             "recorded_from": now, "observed_at": event["occurred_at"], "expires_at": expires,
             "volatility": extracted.volatility, "status": "active", "confidence": extracted.confidence,
             "source_authority": "low" if event.get("actor_type") == "assistant" else "medium",
             "extractor_version": "llm-v1" if isinstance(extractor, LLMExtractor) else "fake-v1"}
        )
        EvidenceRepository(connection).add_link(
            {"id": _id(), "derived_type": "claim", "derived_id": claim_id, "evidence_type": "event",
             "evidence_id": event["id"], "relation": "derived_from", "weight": 1.0}
        )


app = create_app()
