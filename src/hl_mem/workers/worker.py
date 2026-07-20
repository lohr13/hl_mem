from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hl_mem.api.pipeline import store_extracted
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embeddings import Embedder, FakeEmbedder
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import ExtractedClaim, FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.storage.database import Database
from hl_mem.storage.repository import EventRepository, JobRepository
from hl_mem.workers.ttl import expire_claims


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Worker:
    """Single-job worker intended to run in its own process."""

    def __init__(self, db_path: str | Path, config: dict[str, Any] | None = None) -> None:
        self.db_path, self.config = Path(db_path), config or {}
        self.database = Database(self.db_path)
        self.connection = self.database.open()
        self.jobs = JobRepository(self.connection)
        self.filter = self.config.get("event_filter") or EventFilter()
        self.extractor = self.config.get("extractor") or self._make_extractor()
        self.embedder = self.config.get("embedder") or self._make_embedder()
        self.budget = self.config.get("budget") or TokenBudget(
            int(self.config.get("daily_token_limit", os.getenv("HL_MEM_DAILY_TOKEN_LIMIT", "500000"))),
            self.db_path.with_suffix(".budget.json"),
        )

    def run_once(self) -> dict[str, Any]:
        now = _now()
        lease = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        job = self.jobs.lease_job(lease, now)
        if not job:
            return {"status": "idle"}
        try:
            result = self._dispatch(job)
            self.jobs.complete_job(job["id"], _now())
            return {"status": "succeeded", "job_id": job["id"], **result}
        except Exception as error:
            self.jobs.fail_job(job["id"], str(error), _now())
            current = self.connection.execute(
                "SELECT status,attempts FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            return {"status": current["status"], "job_id": job["id"],
                    "attempts": current["attempts"], "error": str(error)}

    def run_forever(self, poll_interval: float = 2.0) -> None:
        next_ttl = 0.0
        try:
            while True:
                current = time.monotonic()
                if current >= next_ttl:
                    expire_claims(self.connection)
                    next_ttl = current + 600.0
                if self.run_once()["status"] == "idle":
                    time.sleep(poll_interval)
        finally:
            self.database.close()

    def _dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        if job["job_type"] == "extract_event":
            return self._extract(json.loads(job["payload_json"] or "{}"))
        if job["job_type"] == "expire_ttl":
            return expire_claims(self.connection)
        if job["job_type"] == "retry_failed":
            cursor = self.connection.execute(
                "UPDATE jobs SET status='pending',last_error=NULL WHERE status='failed'"
            )
            self.connection.commit()
            return {"retried": cursor.rowcount}
        raise ValueError(f"unknown job type: {job['job_type']}")

    def _extract(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = EventRepository(self.connection).get_event(payload["event_id"])
        if not event:
            raise ValueError(f"event not found: {payload['event_id']}")
        content = json.loads(event["content_json"])
        allowed, _ = self.filter.should_extract({**event, "content": content})
        if not allowed:
            return {"claims": 0}
        estimate = max(1, len(event["content_json"]) // 2)
        if not self.budget.can_spend(estimate):
            raise RuntimeError("daily token budget exhausted")
        if event["event_type"] == "explicit_memory" and content.get("memory"):
            memory = content["memory"]
            extracted = [ExtractedClaim(memory["predicate"], memory["text"], 1.0, "stable",
                                        memory["subject"], memory.get("qualifiers") or {})]
        else:
            extracted = self.extractor.extract(content, event) if isinstance(
                self.extractor, LLMExtractor) else self.extractor.extract(content)
        if isinstance(self.extractor, LLMExtractor):
            self.budget.record_usage(self.extractor.last_usage_tokens)
            event["extractor"] = "llm"
        for claim in extracted:
            authority = "high" if event["event_type"] == "explicit_memory" else None
            store_extracted(self.connection, claim, event, _now(), self.embedder, authority)
        return {"claims": len(extracted)}

    def _make_extractor(self) -> Any:
        if self.config.get("extractor_name", os.getenv("HL_MEM_EXTRACTOR", "fake")) == "fake":
            return FakeExtractor()
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            return FakeExtractor()
        return LLMExtractor(api_key, os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"), os.getenv("LLM_MODEL", "qwen3.7-plus"))

    def _make_embedder(self) -> Any:
        dim = int(self.config.get("embedding_dim", os.getenv("EMBEDDING_DIM", "2048")))
        if self.config.get("embedder_name", os.getenv("HL_MEM_EMBEDDER", "fake")) == "fake":
            return FakeEmbedder(dim)
        api_key = os.getenv("EMBEDDING_API_KEY")
        if not api_key:
            return FakeEmbedder(dim)
        return Embedder(api_key, os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"), os.getenv("EMBEDDING_MODEL", "text-embedding-v4"), dim)
