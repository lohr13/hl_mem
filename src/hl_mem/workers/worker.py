from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hl_mem import components
from hl_mem.application.ingest import IngestService
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.observability.audit import NullAuditLogger, audit_scope
from hl_mem.recall.attribute_map import infer_canonical_attribute
from hl_mem.storage.database import Database, default_database_path
from hl_mem.storage.repository import EventRepository, JobRepository
from hl_mem.workers.consolidate import (
    ConflictConsolidator,
    LLMConflictJudge,
    enqueue_daily_consolidation,
)
from hl_mem.workers.decay import decay_claims
from hl_mem.workers.induce_policies import enqueue_daily_policy_induction, induce_policies
from hl_mem.workers.ttl import expire_claims


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_daily_reclassify(connection: Any, now: str, cron: str) -> bool:
    """到达计划时间后幂等创建当天的重分类任务。"""
    try:
        hour_text, minute_text = cron.split(":", 1)
        scheduled_minutes = int(hour_text) * 60 + int(minute_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("HL_MEM_RECLASSIFY_CRON must use HH:MM format") from error
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not 0 <= scheduled_minutes < 24 * 60:
        raise ValueError("HL_MEM_RECLASSIFY_CRON must use HH:MM format")
    if current.hour * 60 + current.minute < scheduled_minutes:
        return False
    return JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "reclassify_claims",
            "payload_json": "{}",
            "idempotency_key": f"reclassify:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )


class Worker:
    """Single-job worker intended to run in its own process."""

    def __init__(self, db_path: str | Path, config: dict[str, Any] | None = None) -> None:
        self.db_path, self.config = Path(db_path), config or {}
        self.database = Database(self.db_path)
        self.connection = self.database.open_worker()
        self.jobs = JobRepository(self.connection)
        self.filter = self.config.get("event_filter") or EventFilter()
        self.extractor = self.config.get("extractor") or self._make_extractor()
        self.embedder = self.config.get("embedder") or self._make_embedder()
        self.budget = self.config.get("budget") or TokenBudget(
            int(self.config.get("daily_token_limit", os.getenv("HL_MEM_DAILY_TOKEN_LIMIT", "500000"))),
            self.db_path.with_suffix(".budget.db"),
        )
        self.audit = self.config.get("audit") or NullAuditLogger()

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
            current = self.connection.execute("SELECT status,attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()
            return {
                "status": current["status"],
                "job_id": job["id"],
                "attempts": current["attempts"],
                "error": str(error),
            }

    def run_forever(self, poll_interval: float = 2.0) -> None:
        next_ttl = 0.0
        try:
            while True:
                current = time.monotonic()
                if current >= next_ttl:
                    expire_claims(self.connection)
                    decay_claims(self.connection)
                    from hl_mem.security.retention import purge_retained_events

                    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                    purge_retained_events(self.connection, "default", cutoff)
                    self.audit.cleanup(int(self.config.get("audit_retention_days", 30)))
                    enqueue_daily_consolidation(
                        self.connection,
                        _now(),
                        self.config.get("consolidate_cron", os.getenv("HL_MEM_CONSOLIDATE_CRON", "03:30")),
                    )
                    enqueue_daily_policy_induction(
                        self.connection,
                        _now(),
                        self.config.get("induce_policies_cron", os.getenv("HL_MEM_INDUCE_POLICIES_CRON", "04:00")),
                    )
                    enqueue_daily_reclassify(
                        self.connection,
                        _now(),
                        self.config.get("reclassify_cron", os.getenv("HL_MEM_RECLASSIFY_CRON", "04:30")),
                    )
                    next_ttl = current + 600.0
                if self.run_once()["status"] == "idle":
                    time.sleep(poll_interval)
        finally:
            self.audit.close()
            self.database.close()

    def _dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        if job["job_type"] == "extract_event":
            return self._extract(json.loads(job["payload_json"] or "{}"), job["id"])
        if job["job_type"] == "expire_ttl":
            return expire_claims(self.connection)
        if job["job_type"] == "decay_access":
            return decay_claims(self.connection)
        if job["job_type"] == "consolidate_conflicts":
            consolidator = self.config.get("consolidator") or self._make_consolidator()
            payload = json.loads(job["payload_json"] or "{}")
            return consolidator.run_batch(
                int(
                    payload.get(
                        "limit",
                        self.config.get("consolidate_batch_size", os.getenv("HL_MEM_CONSOLIDATE_BATCH_SIZE", "100")),
                    )
                ),
                payload.get("namespace", "default"),
                payload.get("watermark"),
                bool(payload.get("dry_run", False)),
            )
        if job["job_type"] == "induce_policies":
            return induce_policies(self.connection, _now())
        if job["job_type"] == "reclassify_claims":
            from hl_mem.workers.reclassify import reclassify_claims

            return reclassify_claims(self.connection, self._make_extractor())
        if job["job_type"] == "purge_retention":
            from hl_mem.security.retention import purge_retained_events

            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            return {"purged": purge_retained_events(self.connection, "default", cutoff)}
        if job["job_type"] == "retry_failed":
            cursor = self.connection.execute("UPDATE jobs SET status='pending',last_error=NULL WHERE status='failed'")
            self.connection.commit()
            return {"retried": cursor.rowcount}
        raise ValueError(f"unknown job type: {job['job_type']}")

    def _extract(self, payload: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
        events = EventRepository(self.connection)
        event = events.get_event(payload["event_id"])
        if not event:
            raise ValueError(f"event not found: {payload['event_id']}")
        with audit_scope(
            self.audit,
            trace_id=event["id"],
            event_id=event["id"],
            job_id=job_id,
            tenant_id=event.get("tenant_id", "default"),
        ):
            content = json.loads(event["content_json"])
            started = time.perf_counter_ns()
            allowed, reason = self.filter.should_extract({**event, "content": content})
            self.audit.emit(
                "filter",
                "evaluated",
                "allow" if allowed else "reject",
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail={
                    "reason": reason,
                    "event_type": event["event_type"],
                    "actor_type": event["actor_type"],
                    "content_chars": len(event["content_json"]),
                },
            )
            if not allowed:
                return {"claims": 0}
            estimate = max(1, len(event["content_json"]) // 2)
            can_spend = self.budget.can_spend(estimate)
            self.audit.emit(
                "budget",
                "checked",
                "allow" if can_spend else "reject",
                detail={"estimated_tokens": estimate, **self.budget.get_stats()},
            )
            if not can_spend:
                raise RuntimeError("daily token budget exhausted")
            recent: list[dict[str, Any]] = []
            started = time.perf_counter_ns()
            try:
                if event["event_type"] == "explicit_memory" and content.get("memory"):
                    memory = content["memory"]
                    extracted = [
                        ExtractedClaim(
                            predicate=memory["predicate"],
                            value=memory["text"],
                            confidence=1.0,
                            volatility="stable",
                            subject=memory["subject"],
                            qualifiers=memory.get("qualifiers") or {},
                            scope="permanent",
                            importance=1.0,
                            canonical_attribute=infer_canonical_attribute(
                                memory["predicate"],
                                memory["subject"],
                                memory["text"],
                                memory.get("qualifiers") or {},
                            ),
                        )
                    ]
                else:
                    recent = events.get_recent_events(event["session_id"], event, 3) if event.get("session_id") else []
                    event_context = {
                        "occurred_at": event["occurred_at"],
                        "recent_events": [
                            {**item, "content": json.loads(item["content_json"])} for item in reversed(recent)
                        ],
                    }
                    extracted = (
                        self.extractor.extract(content, event_context)
                        if isinstance(self.extractor, LLMExtractor)
                        else self.extractor.extract(content)
                    )
            except Exception as error:
                self.audit.emit(
                    "extraction",
                    "evaluated",
                    "error",
                    duration_us=(time.perf_counter_ns() - started) // 1000,
                    detail={
                        "extractor": type(self.extractor).__name__,
                        "error_class": type(error).__name__,
                        "error": str(error)[:256],
                    },
                )
                raise
            self.audit.emit(
                "extraction",
                "evaluated",
                "claims" if extracted else "no_claims",
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail={
                    "extractor": (
                        "explicit_memory" if event["event_type"] == "explicit_memory" else type(self.extractor).__name__
                    ),
                    "claim_count": len(extracted),
                    "context_event_ids": [item["id"] for item in recent],
                },
            )
            if isinstance(self.extractor, LLMExtractor):
                self.budget.record_usage(self.extractor.last_usage_tokens)
                self.audit.emit(
                    "budget",
                    "recorded",
                    "success",
                    detail={"actual_tokens": self.extractor.last_usage_tokens, **self.budget.get_stats()},
                )
                event["extractor"] = "llm"
            for claim in extracted:
                authority = "high" if event["event_type"] == "explicit_memory" else None
                ttl_days = int(self.config.get("memory_temporal_ttl_days", os.getenv("HL_MEM_TEMPORAL_TTL_DAYS", "7")))
                IngestService.store_extracted(
                    self.connection, claim, event, _now(), self.embedder, authority, ttl_days
                )
            return {"claims": len(extracted)}

    def _make_extractor(self) -> Any:
        return components.make_extractor(self.config)

    def _make_embedder(self) -> Any:
        return components.make_embedder(self.config)

    def _make_consolidator(self) -> ConflictConsolidator:
        """从环境配置构建冲突归并器。"""
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("LLM_API_KEY is required for consolidate_conflicts")
        judge = LLMConflictJudge(
            api_key,
            os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
            os.getenv("LLM_MODEL", "qwen3.7-plus"),
        )
        return ConflictConsolidator(
            self.connection,
            judge,
            float(self.config.get("consolidate_confidence", os.getenv("HL_MEM_CONSOLIDATE_CONFIDENCE", "0.8"))),
        )


def main() -> None:
    """运行 worker、处理单个任务或查看任务队列状态。"""
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.worker")
    parser.add_argument("command", choices=("run", "run-once", "status"))
    parser.add_argument("--db", default=str(default_database_path()))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    if args.command == "status":
        database = Database(args.db)
        try:
            print(json.dumps(JobRepository(database.open()).counts(), sort_keys=True))
        finally:
            database.close()
        return
    worker = Worker(args.db)
    if args.command == "run-once":
        try:
            print(json.dumps(worker.run_once(), ensure_ascii=False, sort_keys=True))
        finally:
            worker.database.close()
    else:
        worker.run_forever(args.poll_interval)


if __name__ == "__main__":
    main()
