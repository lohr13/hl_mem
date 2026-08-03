"""后台任务租约、分派、维护调度与 CLI 入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hl_mem import components
from hl_mem.application.ingest import IngestService
from hl_mem.config_loader import load_settings
from hl_mem.domain.claims.attributes import infer_canonical_attribute
from hl_mem.domain.consolidation_scope import ConsolidationScope
from hl_mem.domain.content import ImagePart, parse_content
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.ingest.pre_filter import ExtractionPreFilter
from hl_mem.observability.audit import AuditLogger, NullAuditLogger, audit_scope
from hl_mem.settings import Settings, is_placeholder_secret, parse_daily_cron
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.consolidate import (
    ConflictConsolidator,
    LLMConflictJudge,
    auto_resolve_conflicts,
    enqueue_daily_consolidation,
)
from hl_mem.workers.decay import cleanup_stale_temporal_claims, decay_claims
from hl_mem.workers.deduplicate import deduplicate_claims, enqueue_daily_deduplication
from hl_mem.workers.discover_relations import discover_relations
from hl_mem.workers.induce_policies import (
    enqueue_daily_policy_induction,
    induce_policies,
)
from hl_mem.workers.mental_models import DerivedMemoryMaintainer
from hl_mem.workers.rebuild_usefulness import rebuild_usefulness
from hl_mem.workers.scheduling import enqueue_daily_job
from hl_mem.workers.ttl import expire_claims

_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_daily_reclassify(connection: Any, now: str, cron: str) -> bool:
    """到达计划时间后幂等创建当天的重分类任务。"""
    return (
        enqueue_daily_job(
            connection,
            now,
            {"cron": cron, "idempotency_prefix": "reclassify"},
            "reclassify_claims",
            {},
            "HL_MEM_RECLASSIFY_CRON",
        )
        is not None
    )


class Worker:
    """Single-job worker intended to run in its own process."""

    def __init__(
        self,
        settings: Settings,
        *,
        event_filter: Any = None,
        pre_filter: Any = None,
        extractor: Any = None,
        image_describer: Any = _UNSET,
        embedder: Any = None,
        budget: Any = None,
        audit_logger: Any = None,
        consolidator: Any = None,
        relation_discoverer: Any = None,
    ) -> None:
        self.settings = settings
        self.db_path = Path(settings.database_path)
        self.dedup_scheduled_minutes = parse_daily_cron(
            self.settings.dedup_cron,
            "HL_MEM_DEDUP_CRON",
        )
        self.database = Database(settings=settings)
        self.connection = self.database.open_worker()
        self.jobs = JobRepository(self.connection)
        self.filter = event_filter or EventFilter()
        self.pre_filter = pre_filter or ExtractionPreFilter()
        self.extractor = extractor or self._make_extractor()
        self.image_describer = (
            image_describer if image_describer is not _UNSET else components.make_image_describer(self.settings)
        )
        self.embedder = embedder or self._make_embedder()
        self.budget = budget or TokenBudget(
            self.settings.daily_token_limit,
            self.db_path.with_suffix(".budget.db"),
        )
        if audit_logger is not None:
            self.audit = audit_logger
        elif self.settings.extract_pre_filter:
            self.audit = AuditLogger(self.db_path)
        else:
            self.audit = NullAuditLogger()
        self.consolidator = consolidator
        self.relation_discoverer = relation_discoverer

    def run_once(self) -> dict[str, Any]:
        now = _now()
        lease = (datetime.now(timezone.utc) + timedelta(minutes=self.settings.worker_job_lease_minutes)).isoformat()
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
            current = self.connection.execute("SELECT status,attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()
            return {
                "status": current["status"] if current else "unknown",
                "job_id": job["id"],
                "attempts": current["attempts"] if current else 0,
                "error": str(error),
            }

    def run_forever(self, poll_interval: float | None = None) -> None:
        """持续处理任务并按统一配置执行维护调度。"""
        effective_poll_interval = poll_interval if poll_interval is not None else self.settings.worker_poll_interval
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

    def _run_maintenance(self) -> None:
        """执行一轮 TTL、衰减、派生记忆、保留策略和定时任务维护。"""
        cleanup_stale_temporal_claims(
            self.connection,
            age_days=self.settings.temporal_cleanup_age_days,
            expiry_days=self.settings.temporal_cleanup_expiry_days,
        )
        expire_claims(
            self.connection,
            feedback_lifecycle_mode=self.settings.feedback_lifecycle_mode,
            slot_short_ttl_seconds=self.settings.slot_short_ttl_seconds,
        )
        decay_claims(
            self.connection,
            temporal_decay_days=self.settings.decay_temporal_days,
            temporal_archive_days=self.settings.archive_temporal_days,
            permanent_decay_days=self.settings.decay_permanent_days,
            permanent_archive_days=self.settings.archive_permanent_days,
            access_bonus_every=self.settings.access_bonus_every,
            access_bonus_days=self.settings.access_bonus_days,
            access_bonus_cap_days=self.settings.access_bonus_cap_days,
            rollout_grace_days=self.settings.decay_rollout_grace_days,
            min_confidence=self.settings.decay_min_confidence,
            feedback_lifecycle_mode=self.settings.feedback_lifecycle_mode,
            feedback_bonus_cap_days=self.settings.feedback_bonus_cap_days,
        )
        maintenance_now = _now()
        maintainer = DerivedMemoryMaintainer(self.connection)
        maintainer.mark_stale_dependencies()
        maintainer.scan_and_build(maintenance_now)
        auto_resolve_conflicts(self.connection, maintenance_now)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days)).isoformat()
        _purge_retained_events(self.connection, cutoff)
        self.audit.cleanup(self.settings.audit_retention_days)
        if not is_placeholder_secret(self.settings.llm_api_key):
            enqueue_daily_consolidation(
                self.connection,
                _now(),
                self.settings.consolidate_cron,
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
            self.settings.induce_policies_cron,
        )
        enqueue_daily_reclassify(
            self.connection,
            _now(),
            self.settings.reclassify_cron,
        )

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
            image_parts = [
                part
                for part in parse_content(
                    content,
                    image_max_bytes=self.settings.image_max_bytes,
                    image_max_parts=self.settings.image_max_parts,
                )
                if isinstance(part, ImagePart)
            ]
            description_event_ids: list[str] = []
            description_texts: list[str] = []
            image_errors: list[str] = []
            if self.image_describer is not None:
                for image_index, image in enumerate(image_parts):
                    try:
                        existing = events.find_image_description(
                            event["id"],
                            image_index,
                            str(
                                getattr(
                                    self.image_describer,
                                    "model",
                                    self.settings.image_describer_model,
                                )
                            ),
                        )
                        if existing is not None:
                            description_event = existing
                            description = existing["content"]
                        else:
                            result = self.image_describer.describe(
                                image,
                                timeout_seconds=self.settings.image_describer_timeout_seconds,
                            )
                            description_event = events.insert_image_description_event(event, image_index, result)
                            description = description_event["content"]
                        locator = description["locator"]
                        uri_hash = (
                            locator.get("sha256") or hashlib.sha256(str(locator.get("uri", "")).encode()).hexdigest()
                        )
                        description_texts.append(
                            f'<image_evidence index="{image_index}" uri_hash="{uri_hash}">\n'
                            f"[caption]\n{description.get('caption', '')}\n"
                            f"[ocr]\n{description.get('ocr_text', '')}\n</image_evidence>"
                        )
                        description_event_ids.append(description_event["id"])
                    except Exception as error:
                        image_errors.append(f"image {image_index}: {type(error).__name__}: {error}")
                        self.audit.emit(
                            "image_description",
                            "evaluated",
                            "error",
                            event_id=event["id"],
                            detail={
                                "image_index": image_index,
                                "error_class": type(error).__name__,
                            },
                        )
            if description_texts:
                textual = "\n".join(part.to_text() for part in parse_content(content) if part.to_text())
                content = {"text": "\n".join(filter(None, [textual, *description_texts]))}
            elif image_errors and not any(part.to_text() for part in parse_content(content)):
                raise RuntimeError("; ".join(image_errors))
            event["_image_description_event_ids"] = description_event_ids
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
            if self.settings.extract_pre_filter:
                started = time.perf_counter_ns()
                try:
                    decision = self.pre_filter.evaluate(event, content)
                except Exception as error:
                    self.audit.emit(
                        "extraction_pre_filter",
                        "evaluated",
                        "error_fallback",
                        duration_us=(time.perf_counter_ns() - started) // 1000,
                        detail={
                            "error_class": type(error).__name__,
                            "rule_version": str(getattr(self.pre_filter, "rule_version", "unknown")),
                        },
                    )
                else:
                    self.audit.emit(
                        "extraction_pre_filter",
                        "evaluated",
                        "allow" if decision.should_extract else "skip",
                        duration_us=(time.perf_counter_ns() - started) // 1000,
                        detail={
                            "reason": decision.reason,
                            "rule_version": str(getattr(self.pre_filter, "rule_version", "unknown")),
                            "event_type": event["event_type"],
                            "actor_type": event["actor_type"],
                            "content_chars": len(event["content_json"]),
                        },
                    )
                    if not decision.should_extract:
                        return {"claims": 0, "pre_filter": decision.reason}
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
                        events.get_recent_events(
                            str(event.get("tenant_id") or "default"),
                            event["session_id"],
                            event,
                            3,
                        )
                        if event.get("session_id")
                        else []
                    )
                    event_context = {
                        "occurred_at": event["occurred_at"],
                        "actor_type": event.get("actor_type"),
                        "session_id": event.get("session_id"),
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
                    relation_discovery_mode=self.settings.relation_discovery_mode,
                    index_text_mode=self.settings.index_text_mode,
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
        judge = LLMConflictJudge(components.make_llm_client(self.settings, self.connection, operation="conflict"))
        return ConflictConsolidator(
            self.connection,
            judge,
            self.settings.consolidate_confidence,
        )


def _handle_extract(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理事件提取任务。"""
    return worker._extract(json.loads(job["payload_json"] or "{}"), job["id"])


def _handle_expire(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理 TTL 过期任务。"""
    return expire_claims(
        worker.connection,
        feedback_lifecycle_mode=worker.settings.feedback_lifecycle_mode,
        slot_short_ttl_seconds=worker.settings.slot_short_ttl_seconds,
    )


def _handle_decay(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理访问衰减任务。"""
    return decay_claims(
        worker.connection,
        temporal_decay_days=worker.settings.decay_temporal_days,
        temporal_archive_days=worker.settings.archive_temporal_days,
        permanent_decay_days=worker.settings.decay_permanent_days,
        permanent_archive_days=worker.settings.archive_permanent_days,
        access_bonus_every=worker.settings.access_bonus_every,
        access_bonus_days=worker.settings.access_bonus_days,
        access_bonus_cap_days=worker.settings.access_bonus_cap_days,
        rollout_grace_days=worker.settings.decay_rollout_grace_days,
        min_confidence=worker.settings.decay_min_confidence,
        feedback_lifecycle_mode=worker.settings.feedback_lifecycle_mode,
        feedback_bonus_cap_days=worker.settings.feedback_bonus_cap_days,
    )


def _handle_rebuild_usefulness(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """从 retrieval_feedback 重建 usefulness 聚合。"""
    return rebuild_usefulness(worker.connection, worker.settings)


def _handle_consolidate(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理冲突归并任务。"""
    consolidator = worker.consolidator or worker._make_consolidator()
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
                    worker.settings.consolidate_batch_size,
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
                worker.settings.consolidate_batch_size,
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
    payload = json.loads(job.get("payload_json") or "{}")
    return induce_policies(
        worker.connection,
        _now(),
        worker.settings.policy_induction_lookback_days,
        worker.settings.policy_induction_min_episodes,
        namespace=payload.get("namespace"),
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


def _handle_discover_relations(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """处理单个新 Claim 的关系候选发现任务。"""
    payload = json.loads(job["payload_json"] or "{}")
    discoverer = worker.relation_discoverer or components.make_relation_discoverer(worker.settings, worker.connection)
    if discoverer is None:
        return {
            "candidates": 0,
            "proposals": 0,
            "applied": 0,
            "conflicts": 0,
            "rejected": 0,
        }
    return discover_relations(
        worker.connection,
        discoverer,
        str(payload["claim_id"]),
        mode=worker.settings.relation_discovery_mode,
        pool_limit=worker.settings.relation_discovery_pool_limit,
        max_proposals=worker.settings.relation_discovery_max_proposals,
        auto_apply_confidence=worker.settings.relation_auto_apply_confidence,
        conflict_confidence=worker.settings.relation_conflict_confidence,
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
    payload = json.loads(job.get("payload_json") or "{}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=worker.settings.retention_days)).isoformat()
    return {
        "purged": _purge_retained_events(
            worker.connection,
            cutoff,
            namespace=payload.get("namespace"),
        )
    }


def _purge_retained_events(
    connection: Any,
    cutoff: str,
    namespace: str | None = None,
) -> int:
    """清理一个或全部现存 namespace，避免把维护范围静默固定为 default。"""
    from hl_mem.security.retention import purge_retained_events

    namespaces = (
        [namespace]
        if namespace is not None
        else [
            str(row[0])
            for row in connection.execute("SELECT DISTINCT tenant_id FROM events ORDER BY tenant_id").fetchall()
        ]
    )
    return sum(purge_retained_events(connection, event_namespace, cutoff) for event_namespace in namespaces)


def _handle_retry_failed(worker: Worker, job: dict[str, Any]) -> dict[str, Any]:
    """将失败任务重新置为待处理。"""
    retried = worker.jobs.retry_failed()
    worker.connection.commit()
    return {"retried": retried}


JOB_HANDLERS: dict[str, Callable[[Worker, dict[str, Any]], dict[str, Any]]] = {
    "extract_event": _handle_extract,
    "expire_ttl": _handle_expire,
    "decay_access": _handle_decay,
    "rebuild_usefulness": _handle_rebuild_usefulness,
    "consolidate_conflicts": _handle_consolidate,
    "deduplicate_claims": _handle_deduplicate,
    "discover_relations": _handle_discover_relations,
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
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.worker")
    parser.add_argument("command", choices=("run", "run-once", "status"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db")
    parser.add_argument("--poll-interval", type=float)
    args = parser.parse_args()
    settings = load_settings(args.config, args.env_file)
    if args.db is not None:
        settings = replace(settings, database_path=args.db)
    if args.command == "status":
        database = Database(settings=settings)
        try:
            print(json.dumps(JobRepository(database.open()).counts(), sort_keys=True))
        finally:
            database.close()
        return
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
