from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hl_mem import components
from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.attributes import infer_canonical_attribute
from hl_mem.domain.consolidation_scope import ConsolidationScope
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.observability.audit import NullAuditLogger, audit_scope
from hl_mem.settings import Settings, parse_daily_cron
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.consolidate import (
    ConflictConsolidator,
    LLMConflictJudge,
    auto_resolve_conflicts,
    enqueue_daily_consolidation,
)
from hl_mem.workers.decay import decay_claims
from hl_mem.workers.deduplicate import deduplicate_claims, enqueue_daily_deduplication
from hl_mem.workers.induce_policies import (
    enqueue_daily_policy_induction,
    induce_policies,
)
from hl_mem.workers.mental_models import DerivedMemoryMaintainer
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
    created = JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "reclassify_claims",
            "payload_json": "{}",
            "idempotency_key": f"reclassify:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )
    connection.commit()
    return created


class Worker:
    """Single-job worker intended to run in its own process."""

    def __init__(
        self,
        settings: Settings | str | Path,
        config: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(settings, Settings):
            self.settings = settings
            self.db_path = Path(settings.database_path)
        else:
            self.settings = Settings.from_env()
            self.db_path = Path(settings)
        self.config = config or {}
        self.dedup_scheduled_minutes = parse_daily_cron(
            str(self.config.get("dedup_cron", self.settings.dedup_cron)),
            "HL_MEM_DEDUP_CRON",
        )
        self.database = Database(self.db_path)
        self.connection = self.database.open_worker()
        self.jobs = JobRepository(self.connection)
        self.filter = self.config.get("event_filter") or EventFilter()
        self.extractor = self.config.get("extractor") or self._make_extractor()
        self.embedder = self.config.get("embedder") or self._make_embedder()
        self.budget = self.config.get("budget") or TokenBudget(
            int(self.config.get("daily_token_limit", self.settings.daily_token_limit)),
            self.db_path.with_suffix(".budget.db"),
        )
        self.audit = self.config.get("audit") or NullAuditLogger()

    def run_once(self) -> dict[str, Any]:
        now = _now()
        lease = (
            datetime.now(timezone.utc)
            + timedelta(minutes=self.settings.worker_job_lease_minutes)
        ).isoformat()
        job = self.jobs.lease_job(lease, now)
        if not job:
            return {"status": "idle"}
        lease_token = job["lease_token"]
        self.jobs.update_progress(
            job["id"],
            lease_token,
            stage="leased",
            heartbeat_at=now,
        )
        try:
            result = dispatch_job(self, job)
            self.jobs.complete_job(job["id"], _now(), lease_token)
            return {"status": "succeeded", "job_id": job["id"], **result}
        except Exception as error:
            self.jobs.fail_job(job["id"], str(error), _now(), lease_token)
            current = self.connection.execute(
                "SELECT status,attempts FROM jobs WHERE id=?", (job["id"],)
            ).fetchone()
            return {
                "status": current["status"] if current else "unknown",
                "job_id": job["id"],
                "attempts": current["attempts"] if current else 0,
                "error": str(error),
            }

    def run_forever(self, poll_interval: float | None = None) -> None:
        """持续处理任务并按统一配置执行维护调度。"""
        effective_poll_interval = (
            poll_interval
            if poll_interval is not None
            else self.settings.worker_poll_interval
        )
        next_ttl = 0.0
        try:
            while True:
                current = time.monotonic()
                if current >= next_ttl:
                    self._run_maintenance()
                    next_ttl = current + self.settings.worker_maintenance_interval
                if self.run_once()["status"] == "idle":
                    time.sleep(effective_poll_interval)
        finally:
            self.audit.close()
            self.database.close()

    def _dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """兼容旧调用；新代码应直接调用模块级 dispatch_job。"""
        return dispatch_job(self, job)

    def _run_maintenance(self) -> None:
        """执行一轮 TTL、衰减、派生记忆、保留策略和定时任务维护。"""
        expire_claims(self.connection)
        decay_claims(self.connection)
        maintenance_now = _now()
        maintainer = DerivedMemoryMaintainer(self.connection)
        maintainer.mark_stale_dependencies()
        maintainer.scan_and_build(maintenance_now)
        auto_resolve_conflicts(self.connection, maintenance_now)
        from hl_mem.security.retention import purge_retained_events

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days)
        ).isoformat()
        purge_retained_events(self.connection, "default", cutoff)
        self.audit.cleanup(
            int(
                self.config.get(
                    "audit_retention_days", self.settings.audit_retention_days
                )
            )
        )
        enqueue_daily_consolidation(
            self.connection,
            _now(),
            self.config.get("consolidate_cron", self.settings.consolidate_cron),
        )
        if self.settings.dedup_enabled:
            enqueue_daily_deduplication(
                self.connection,
                _now(),
                self.dedup_scheduled_minutes,
            )
        enqueue_daily_policy_induction(
            self.connection,
            _now(),
            self.config.get("induce_policies_cron", self.settings.induce_policies_cron),
        )
        enqueue_daily_reclassify(
            self.connection,
            _now(),
            self.config.get("reclassify_cron", self.settings.reclassify_cron),
        )

    def _extract(
        self, payload: dict[str, Any], job_id: str | None = None
    ) -> dict[str, Any]:
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
                    recent = (
                        events.get_recent_events(event["session_id"], event, 3)
                        if event.get("session_id")
                        else []
                    )
                    event_context = {
                        "occurred_at": event["occurred_at"],
                        "recent_events": [
                            {**item, "content": json.loads(item["content_json"])}
                            for item in reversed(recent)
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
                        "explicit_memory"
                        if event["event_type"] == "explicit_memory"
                        else type(self.extractor).__name__
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
                    detail={
                        "actual_tokens": self.extractor.last_usage_tokens,
                        **self.budget.get_stats(),
                    },
                )
                event["extractor"] = "llm"
            stored = 0
            rejections: list[dict[str, Any]] = []
            for claim in extracted:
                authority = "high" if event["event_type"] == "explicit_memory" else None
                result = IngestService.store_extracted(
                    self.connection,
                    claim,
                    event,
                    _now(),
                    self.embedder,
                    authority,
                    policy=self.settings.retention_policy(),
                )
                if result.status == "skipped":
                    rejections.append({"reason": result.reason, "predicate": claim.predicate})
                else:
                    stored += 1
            return {
                "claims": len(extracted),
                "stored": stored,
                "skipped": len(rejections),
                "rejections": rejections,
            }

    def _make_extractor(self) -> Any:
        return components.make_extractor(self.settings, connection=getattr(self, "connection", None))

    def _make_embedder(self) -> Any:
        return components.make_embedder(self.settings)

    def _make_consolidator(self) -> ConflictConsolidator:
        """从环境配置构建冲突归并器。"""
        judge = LLMConflictJudge(
            components.make_llm_client(self.settings, self.connection, operation="conflict")
        )
        return ConflictConsolidator(
            self.connection,
            judge,
            float(
                self.config.get(
                    "consolidate_confidence", self.settings.consolidate_confidence
                )
            ),
        )


def _handle_extract(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理事件提取任务。"""
    return worker._extract(json.loads(job["payload_json"] or "{}"), job["id"])


def _handle_expire(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理 TTL 过期任务。"""
    return expire_claims(worker.connection)


def _handle_decay(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理访问衰减任务。"""
    return decay_claims(worker.connection)


def _handle_consolidate(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理冲突归并任务。"""
    consolidator = worker.config.get("consolidator") or worker._make_consolidator()
    payload = json.loads(job["payload_json"] or "{}")
    scope = ConsolidationScope(
        namespace=payload.get("namespace", "default"),
        slot_filter=payload.get("slot_filter"),
        tag_filter=payload.get("tag_filter"),
        max_pairs=int(
            payload.get(
                "max_pairs",
                payload.get(
                    "limit",
                    worker.config.get("consolidate_batch_size", worker.settings.consolidate_batch_size),
                ),
            )
        ),
        similarity_threshold=float(payload.get("similarity_threshold", 0.72)),
        similarity_ceiling=float(payload.get("similarity_ceiling", 0.95)),
    )
    progress_callback = _job_progress_callback(worker, job)
    return consolidator.run_batch(
        int(
            payload.get(
                "limit",
                worker.config.get(
                    "consolidate_batch_size", worker.settings.consolidate_batch_size
                ),
            )
        ),
        payload.get("namespace", "default"),
        payload.get("watermark"),
        bool(payload.get("dry_run", False)),
        progress_callback,
        scope,
    )


def _handle_induce_policies(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理策略归纳任务。"""
    return induce_policies(
        worker.connection,
        _now(),
        worker.settings.policy_induction_lookback_days,
        worker.settings.policy_induction_min_episodes,
    )


def _handle_deduplicate(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理跨主体语义去重任务。"""
    payload = json.loads(job["payload_json"] or "{}")
    return deduplicate_claims(
        worker.connection,
        components.make_llm_client(worker.settings, worker.connection, operation="dedup"),
        worker.embedder,
        namespace=str(payload.get("namespace", "default")),
        threshold=float(payload.get("threshold", worker.settings.dedup_threshold)),
        audit_only=bool(payload.get("audit_only", worker.settings.dedup_audit_only)),
        auto_merge_min_confidence=float(
            payload.get(
                "auto_merge_min_confidence",
                worker.settings.dedup_auto_merge_min_confidence,
            )
        ),
        limit=int(payload.get("limit", worker.settings.dedup_scan_limit)),
        progress_callback=_job_progress_callback(worker, job),
    )


def _job_progress_callback(worker: Worker, job: dict[str, Any]) -> Callable[[str, int, int], None]:
    """创建受 lease token 保护的任务进度回调。"""

    def update(stage: str, processed: int, total: int) -> None:
        worker.jobs.update_progress(
            job["id"],
            job["lease_token"],
            stage=stage,
            processed=processed,
            total=total,
            heartbeat_at=_now(),
        )

    return update


def _handle_reclassify(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理 claim 重分类任务。"""
    from hl_mem.workers.reclassify import reclassify_claims

    return reclassify_claims(
        worker.connection,
        components.make_llm_client(worker.settings, worker.connection, operation="other"),
        policy=worker.settings.retention_policy(),
    )


def _handle_purge_retention(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理事件保留清理任务。"""
    from hl_mem.security.retention import purge_retained_events

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=worker.settings.retention_days)
    ).isoformat()
    return {"purged": purge_retained_events(worker.connection, "default", cutoff)}


def _handle_retry_failed(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """将失败任务重新置为待处理。"""
    retried = worker.jobs.retry_failed()
    worker.connection.commit()
    return {"retried": retried}


JOB_HANDLERS: dict[str, Callable[[Worker, dict[str, Any]], dict[str, Any]]] = {
    "extract_event": _handle_extract,
    "expire_ttl": _handle_expire,
    "decay_access": _handle_decay,
    "consolidate_conflicts": _handle_consolidate,
    "deduplicate_claims": _handle_deduplicate,
    "induce_policies": _handle_induce_policies,
    "reclassify_claims": _handle_reclassify,
    "purge_retention": _handle_purge_retention,
    "retry_failed": _handle_retry_failed,
}


def dispatch_job(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """通过公开模块级入口独立分派单个后台任务。"""
    handler = JOB_HANDLERS.get(job["job_type"])
    if handler is None:
        raise ValueError(f"unknown job type: {job['job_type']}")
    return handler(worker, job)


def main() -> None:
    """运行 worker、处理单个任务或查看任务队列状态。"""
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.worker")
    parser.add_argument("command", choices=("run", "run-once", "status"))
    parser.add_argument("--db", default=settings.database_path)
    parser.add_argument(
        "--poll-interval", type=float, default=settings.worker_poll_interval
    )
    args = parser.parse_args()
    if args.command == "status":
        database = Database(args.db)
        try:
            print(json.dumps(JobRepository(database.open()).counts(), sort_keys=True))
        finally:
            database.close()
        return
    if args.db != settings.database_path:
        from dataclasses import replace

        settings = replace(settings, database_path=args.db)
    worker = Worker(settings)
    if args.command == "run-once":
        try:
            print(json.dumps(worker.run_once(), ensure_ascii=False, sort_keys=True))
        finally:
            worker.database.close()
    else:
        worker.run_forever(args.poll_interval)


if __name__ == "__main__":
    main()
