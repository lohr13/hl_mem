"""后台任务租约、分派、维护调度与 CLI 入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from hl_mem import components
from hl_mem.application.conflict_repairs import repair_dangling_conflicts
from hl_mem.application.expired_cleanup import maintain_expired_claims
from hl_mem.application.ingest import IngestService
from hl_mem.config_loader import load_settings
from hl_mem.domain.claims.attributes import infer_canonical_attribute
from hl_mem.domain.content import ImagePart, parse_content
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.event_filter import EventFilter
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.ingest.pre_filter import ExtractionPreFilter
from hl_mem.monitoring.worker import DEFAULT_WORKER_RUNTIME, WorkerRuntimeState
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
from hl_mem.workers.deduplicate import (
    enqueue_daily_deduplication,
    review_pending_near_duplicates,
)
from hl_mem.workers.deferred import (
    cleanup_recall_side_effect_tasks,
    complete_deferred_extractions,
    handle_failed_extractions,
    process_deferred_tasks,
    process_recall_side_effect_tasks,
)
from hl_mem.workers.history_cleanup import HistoryCleanupPolicy, cleanup_operational_history
from hl_mem.workers.induce_policies import enqueue_daily_policy_induction
from hl_mem.workers.job_handlers import (
    dispatch_job,
)
from hl_mem.workers.job_handlers import purge_retained_events_for_namespaces as _purge_retained_events
from hl_mem.workers.mental_models import DerivedMemoryMaintainer
from hl_mem.workers.scheduling import (
    enqueue_daily_job,
)
from hl_mem.workers.scheduling import lease_deadline as _lease_deadline
from hl_mem.workers.scheduling import utc_now as _now
from hl_mem.workers.ttl import expire_claims

_UNSET = object()
LOGGER = logging.getLogger(__name__)


class _LeaseHeartbeat:
    """Periodically renew a job lease on a separate database connection."""

    def __init__(
        self,
        database: Database | None,
        settings: Settings,
        job_ids: list[str],
        lease_token: str,
    ) -> None:
        self.database = database
        self.settings = settings
        self.job_ids = job_ids
        self.lease_token = lease_token
        self.interval_seconds = max(1.0, settings.worker_job_lease_minutes * 60.0 / 3.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def start(self) -> None:
        if self.database is None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"hl-mem-lease-{self.job_ids[0]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError(self._error)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            heartbeat_at = _now()
            try:
                assert self.database is not None
                with self.database.connect() as connection:
                    renewed = JobRepository(connection).renew_lease(
                        self.job_ids,
                        self.lease_token,
                        leased_until=_lease_deadline(self.settings),
                        heartbeat_at=heartbeat_at,
                    )
            except Exception as error:  # pragma: no cover - timing and storage dependent
                # A short SQLite writer conflict must not turn completed work
                # into a retry. Keep attempting; terminal ownership is checked
                # by row count both here and before completion.
                LOGGER.warning("job_lease_heartbeat_failed job=%s error=%s", self.job_ids[0], error)
                continue
            if renewed != len(self.job_ids):
                self._error = "job lease ownership lost during execution"
                return


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


def _process_recall_side_effects_safely(connection: Any, now: str) -> dict[str, int]:
    """隔离高频副作用消费异常，避免单次锁竞争终止 worker 主循环。"""
    try:
        return process_recall_side_effect_tasks(connection, now=now)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        LOGGER.exception("recall_side_effect_processing_failed")
        return {"completed": 0, "retried": 0, "abandoned": 0}


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
        worker_runtime: WorkerRuntimeState = DEFAULT_WORKER_RUNTIME,
        connection: Any = None,
    ) -> None:
        self.settings = settings
        self.db_path = Path(settings.database_path)
        self.dedup_scheduled_minutes = parse_daily_cron(
            self.settings.dedup_cron,
            "HL_MEM_DEDUP_CRON",
        )
        if connection is None:
            self.database: Database | None = Database(settings=settings)
            self.connection = self.database.open_worker()
        else:
            self.database = None
            self.connection = connection
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
        self.worker_runtime = worker_runtime

    def run_once(self, *, force_extraction: bool = True) -> dict[str, Any]:
        now = _now()
        self.worker_runtime.heartbeat(now)
        lease = _lease_deadline(self.settings)
        job = self.jobs.lease_job(
            lease,
            now,
            extraction_batch_max_events=self.settings.extraction_batch_max_events,
            extraction_batch_max_wait_seconds=self.settings.extraction_batch_max_wait_seconds,
            force_extraction=force_extraction,
        )
        if not job:
            return {"status": "idle"}
        lease_token = job["lease_token"]
        leased_job_ids = list(job.get("leased_job_ids") or [job["id"]])
        heartbeat = _LeaseHeartbeat(self.database, self.settings, leased_job_ids, lease_token)
        try:
            renewed = self.jobs.renew_lease(
                leased_job_ids,
                lease_token,
                leased_until=lease,
                heartbeat_at=now,
            )
            progress_updated = self.jobs.update_progress(
                job["id"],
                lease_token,
                stage="leased",
                heartbeat_at=now,
            )
            if renewed != len(leased_job_ids) or not progress_updated:
                raise RuntimeError("job lease ownership lost before dispatch")
            heartbeat.start()
            try:
                result = dispatch_job(self, job)
            finally:
                heartbeat.stop()
            heartbeat.raise_if_failed()
            if job["job_type"] == "extract_event":
                try:
                    complete_deferred_extractions(self.connection, list(job["payload"]["event_ids"]), _now())
                except Exception:
                    LOGGER.exception("deferred_extraction_completion_failed job=%s", job["id"])
            completed = self.jobs.complete_jobs(leased_job_ids, _now(), lease_token)
            if completed != len(leased_job_ids):
                raise RuntimeError("job lease ownership lost before completion")
            return {"status": "succeeded", "job_id": job["id"], **result}
        except Exception as error:
            heartbeat.stop()
            failed = self.jobs.fail_jobs(leased_job_ids, str(error), _now(), lease_token)
            placeholders = ",".join("?" for _ in leased_job_ids)
            rows = self.connection.execute(
                f"SELECT id,status,attempts FROM jobs WHERE id IN ({placeholders})",
                leased_job_ids,
            ).fetchall()
            current_by_id = {str(row["id"]): row for row in rows}
            current = current_by_id.get(str(job["id"]))
            if failed and job["job_type"] == "extract_event":
                dead_event_ids = [
                    event_id
                    for job_id, event_id in zip(leased_job_ids, job["payload"]["event_ids"], strict=True)
                    if current_by_id.get(job_id) is not None and current_by_id[job_id]["status"] == "dead"
                ]
                try:
                    handle_failed_extractions(self.connection, dead_event_ids, error, now=_now())
                except Exception:
                    LOGGER.exception("deferred_extraction_registration_failed job=%s", job["id"])
            return {
                "status": current["status"] if failed and current else "lease_lost",
                "job_id": job["id"],
                "attempts": current["attempts"] if current else 0,
                "error": str(error),
            }

    def run_forever(self, poll_interval: float | None = None) -> None:
        """持续处理任务并按统一配置执行维护调度。"""
        effective_poll_interval = poll_interval if poll_interval is not None else self.settings.worker_poll_interval
        next_ttl = 0.0
        self.worker_runtime.mark_started(_now())
        try:
            while True:
                self.worker_runtime.heartbeat(_now())
                _process_recall_side_effects_safely(self.connection, _now())
                current = time.monotonic()
                if current >= next_ttl:
                    self._run_maintenance()
                    next_ttl = current + self.settings.worker_maintenance_interval
                if self.run_once(force_extraction=False)["status"] == "idle":
                    time.sleep(effective_poll_interval)
        finally:
            self.worker_runtime.mark_stopped(_now())
            self.close()

    def close(self) -> None:
        """关闭 Worker 自有资源；外部注入的数据库连接由调用方管理。"""
        self.audit.close()
        if self.database is not None:
            self.database.close()

    def _run_maintenance(self) -> None:
        """执行一轮 TTL、衰减、派生记忆、保留策略和定时任务维护。"""
        maintenance_now = _now()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.settings.retention_days)).isoformat()
        items: list[tuple[str, Callable[[], Any]]] = [
            (
                "process_deferred_tasks",
                lambda: process_deferred_tasks(self.connection, now=maintenance_now),
            ),
            (
                "cleanup_recall_side_effect_tasks",
                lambda: cleanup_recall_side_effect_tasks(self.connection, before=cutoff),
            ),
            (
                "cleanup_stale_temporal_claims",
                lambda: cleanup_stale_temporal_claims(
                    self.connection,
                    age_days=self.settings.temporal_cleanup_age_days,
                    expiry_days=self.settings.temporal_cleanup_expiry_days,
                ),
            ),
            (
                "expire_claims",
                lambda: expire_claims(
                    self.connection,
                    feedback_lifecycle_mode=self.settings.feedback_lifecycle_mode,
                    slot_short_ttl_seconds=self.settings.slot_short_ttl_seconds,
                ),
            ),
            *(
                [
                    (
                        "cleanup_expired_claims",
                        lambda: maintain_expired_claims(
                            self.connection,
                            now=maintenance_now,
                            retention_days=self.settings.expired_claim_retention_days,
                            batch_size=self.settings.expired_cleanup_batch_size,
                            mode=self.settings.expired_cleanup_mode,
                        ),
                    )
                ]
                if self.settings.expired_cleanup_mode != "off"
                else []
            ),
            (
                "decay_claims",
                lambda: decay_claims(
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
                    decay_model=self.settings.decay_model,
                    temporal_half_life_days=self.settings.decay_temporal_half_life_days,
                    permanent_half_life_days=self.settings.decay_permanent_half_life_days,
                    identity_half_life_days=self.settings.decay_identity_half_life_days,
                    halflife_archive_threshold=self.settings.decay_halflife_archive_threshold,
                    halflife_archive_grace_days=self.settings.decay_halflife_archive_grace_days,
                ),
            ),
            (
                "mark_stale_dependencies",
                lambda: DerivedMemoryMaintainer(self.connection).mark_stale_dependencies(),
            ),
            (
                "scan_derived_memories",
                lambda: DerivedMemoryMaintainer(self.connection).scan_and_build(maintenance_now),
            ),
            *(
                [
                    (
                        "review_pending_near_duplicates",
                        lambda: review_pending_near_duplicates(
                            self.connection,
                            threshold=self.settings.dedup_threshold,
                            limit=self.settings.dedup_scan_limit,
                        ),
                    )
                ]
                if self.settings.dedup_enabled
                else []
            ),
            (
                "repair_dangling_conflicts",
                lambda: repair_dangling_conflicts(self.connection, source="worker"),
            ),
            *(
                [
                    (
                        "auto_resolve_conflicts",
                        lambda: auto_resolve_conflicts(
                            self.connection,
                            maintenance_now,
                            max_cases=self.settings.conflict_maintenance_max_cases,
                            max_elapsed_ms=self.settings.conflict_maintenance_budget_ms,
                            failure_backoff_seconds=self.settings.conflict_failure_backoff_seconds,
                        ),
                    )
                ]
                if self.settings.conflict_auto_resolve_enabled
                else []
            ),
            (
                "purge_retained_events",
                lambda: _purge_retained_events(self.connection, cutoff),
            ),
        ]
        if self.settings.operational_cleanup_enabled:
            items.extend(
                [
                    (
                        "cleanup_operational_history",
                        lambda: cleanup_operational_history(
                            self.connection,
                            maintenance_now,
                            HistoryCleanupPolicy(
                                batch_size=self.settings.operational_batch_size,
                                job_succeeded_days=self.settings.job_succeeded_days,
                                job_dead_days=self.settings.job_dead_days,
                                llm_span_days=self.settings.llm_span_days,
                                dedup_pair_days=self.settings.dedup_pair_days,
                                feedback_uninjected_days=self.settings.feedback_uninjected_days,
                                feedback_unlabeled_days=self.settings.feedback_unlabeled_days,
                            ),
                        ),
                    ),
                    (
                        "cleanup_audit_log",
                        lambda: self.audit.cleanup(
                            self.settings.audit_retention_days,
                            batch_size=self.settings.operational_batch_size,
                        ),
                    ),
                ]
            )
        if not is_placeholder_secret(self.settings.llm_api_key):
            items.append(
                (
                    "enqueue_daily_consolidation",
                    lambda: enqueue_daily_consolidation(
                        self.connection,
                        _now(),
                        self.settings.consolidate_cron,
                    ),
                )
            )
            if self.settings.dedup_enabled:
                items.append(
                    (
                        "enqueue_daily_deduplication",
                        lambda: enqueue_daily_deduplication(
                            self.connection,
                            _now(),
                            self.dedup_scheduled_minutes,
                        ),
                    )
                )
        items.extend(
            [
                (
                    "enqueue_daily_policy_induction",
                    lambda: enqueue_daily_policy_induction(
                        self.connection,
                        _now(),
                        self.settings.induce_policies_cron,
                    ),
                ),
                (
                    "enqueue_daily_reclassify",
                    lambda: enqueue_daily_reclassify(
                        self.connection,
                        _now(),
                        self.settings.reclassify_cron,
                    ),
                ),
            ]
        )

        self.worker_runtime.begin_maintenance(maintenance_now)
        try:
            for item, operation in items:
                result = self._run_maintenance_item(item, operation)
                if (
                    item == "auto_resolve_conflicts"
                    and isinstance(result, dict)
                    and int(result.get("scanned", 0)) > 0
                    and self.settings.conflict_writer_yield_ms > 0
                ):
                    threading.Event().wait(self.settings.conflict_writer_yield_ms / 1_000)
        finally:
            self.worker_runtime.finish_maintenance(_now())

    def _run_maintenance_item(self, item: str, operation: Callable[[], Any]) -> Any:
        """隔离单个维护项，清理失败事务并保留可观测失败信息。"""
        result: Any = None
        self.worker_runtime.begin_maintenance_item(item, _now())
        try:
            result = operation()
            return result
        except Exception as error:
            rollback_error: Exception | None = None
            try:
                if self.connection.in_transaction:
                    self.connection.rollback()
            except Exception as caught_rollback_error:  # pragma: no cover - sqlite rollback 极少失败
                rollback_error = caught_rollback_error
            failure_at = _now()
            self.worker_runtime.record_maintenance_failure(item, error, failure_at)
            detail = {
                "item": item,
                "error_class": type(error).__name__,
                "error": str(error)[:256],
            }
            if rollback_error is not None:
                detail["rollback_error"] = f"{type(rollback_error).__name__}: {str(rollback_error)[:256]}"
            LOGGER.exception("worker_maintenance_failed item=%s", item)
            try:
                self.audit.emit(
                    "worker",
                    "maintenance",
                    "error",
                    detail=detail,
                )
            except Exception:
                LOGGER.exception("worker_maintenance_audit_failed item=%s", item)
            return None
        finally:
            self.worker_runtime.finish_maintenance_item(item, result, _now())

    def _extract(
        self,
        payload: dict[str, Any],
        job_id: str | None = None,
        progress_callback: Callable[[str, int, int, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        event_ids = list(payload.get("event_ids") or [payload["event_id"]])
        return self._extract_window(event_ids, job_id, progress_callback)

    def _extract_window(
        self,
        event_ids: list[str],
        job_id: str | None,
        progress_callback: Callable[[str, int, int, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        events = EventRepository(self.connection)
        batch = [events.get_event(event_id) for event_id in event_ids]
        missing = [event_id for event_id, event in zip(event_ids, batch, strict=True) if event is None]
        if missing:
            raise ValueError(f"event not found: {missing[0]}")
        source_batch = [event for event in batch if event is not None]
        first = source_batch[0]

        def report_writes(stage: str, processed: int, total: int, written: int) -> None:
            if progress_callback is None:
                return
            progress_callback(
                stage,
                processed,
                total,
                {
                    "written_claim_count": {
                        "windows": [{"event_ids": event_ids, "written": written}],
                        "total": written,
                    }
                },
            )

        with audit_scope(
            self.audit,
            trace_id=first["id"],
            event_id=first["id"],
            job_id=job_id,
            tenant_id=first.get("tenant_id", "default"),
        ):
            prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
            pre_filter_reasons: list[str] = []
            for event in source_batch:
                content, pre_filter_reason = self._prepare_event(events, event)
                if content is not None:
                    prepared.append((event, content))
                elif pre_filter_reason:
                    pre_filter_reasons.append(pre_filter_reason)
            if not prepared:
                report_writes("claims_written", 0, 0, 0)
                if len(source_batch) == 1 and pre_filter_reasons:
                    return {"claims": 0, "pre_filter": pre_filter_reasons[0]}
                return {
                    "events": len(source_batch),
                    "eligible_events": 0,
                    "claims": 0,
                    "stored": 0,
                    "skipped": 0,
                    "rejections": [],
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                }
            estimate = max(1, sum(len(event["content_json"]) for event, _ in prepared) // 2)
            can_spend = self.budget.can_spend(estimate)
            self.audit.emit(
                "budget",
                "checked",
                "allow" if can_spend else "reject",
                detail={"estimated_tokens": estimate, **self.budget.get_stats()},
            )
            if not can_spend:
                raise RuntimeError("daily token budget exhausted")
            anchor = prepared[0][0]
            recent = (
                events.get_recent_events(
                    str(anchor.get("tenant_id") or "default"),
                    anchor["session_id"],
                    anchor,
                    3,
                )
                if anchor.get("session_id")
                else []
            )
            extraction_sources: list[dict[str, Any]] = []
            messages: list[dict[str, Any]] = []
            for index, (event, content) in enumerate(prepared):
                metadata_value = event.get("metadata")
                metadata = metadata_value if isinstance(metadata_value, dict) else {}
                turn = metadata.get("turn_id", metadata.get("turn_index", index))
                extraction_sources.append(
                    {
                        **event,
                        "event_index": index,
                        "turn": turn,
                        "content": content,
                    }
                )
                messages.append(
                    {
                        "event_index": index,
                        "speaker": str(event.get("actor_type") or "unknown"),
                        "turn": turn,
                        "occurred_at": event.get("occurred_at"),
                        "content": self._event_text(content),
                    }
                )
            started = time.perf_counter_ns()
            explicit_bypass = (
                len(prepared) == 1
                and prepared[0][0]["event_type"] == "explicit_memory"
                and bool(prepared[0][1].get("memory"))
            )
            uses_llm = not explicit_bypass and isinstance(self.extractor, LLMExtractor)
            extractor_hash = self.extractor.prompt_hash if uses_llm else None
            try:
                if explicit_bypass:
                    memory = prepared[0][1]["memory"]
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
                            source_event_indices=(0,),
                        )
                    ]
                elif uses_llm:
                    extracted = self.extractor.extract(
                        {"messages": messages},
                        {
                            "occurred_at": anchor["occurred_at"],
                            "actor_type": "conversation",
                            "event_type": "message",
                            "session_id": anchor.get("session_id"),
                            "recent_events": [
                                {**item, "content": json.loads(item["content_json"])} for item in reversed(recent)
                            ],
                            "_source_events": extraction_sources,
                        },
                    )
                else:
                    extracted = []
                    for index, (_, content) in enumerate(prepared):
                        extracted.extend(
                            replace(claim, source_event_indices=(index,)) for claim in self.extractor.extract(content)
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
                        "source_event_ids": [event["id"] for event, _ in prepared],
                        **({"extractor_hash": extractor_hash} if extractor_hash else {}),
                    },
                )
                raise
            self.audit.emit(
                "extraction",
                "evaluated",
                "claims" if extracted else "no_claims",
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail={
                    "extractor": "explicit_memory" if explicit_bypass else type(self.extractor).__name__,
                    "claim_count": len(extracted),
                    "context_event_ids": [item["id"] for item in recent],
                    "source_event_ids": [event["id"] for event, _ in prepared],
                    **({"extractor_hash": extractor_hash} if extractor_hash else {}),
                },
            )
            input_tokens = int(getattr(self.extractor, "last_input_tokens", 0)) if uses_llm else 0
            output_tokens = int(getattr(self.extractor, "last_output_tokens", 0)) if uses_llm else 0
            total_tokens = int(getattr(self.extractor, "last_usage_tokens", 0)) if uses_llm else 0
            if uses_llm:
                self.budget.record_usage(total_tokens)
                self.audit.emit(
                    "budget",
                    "recorded",
                    "success",
                    detail={"actual_tokens": total_tokens, **self.budget.get_stats()},
                )
                for event in extraction_sources:
                    event["extractor"] = "llm"
                    event["extractor_version"] = self.extractor.extractor_version
            elif explicit_bypass:
                extraction_sources[0]["extractor"] = "explicit"
                extraction_sources[0]["extractor_version"] = "explicit-v1"
            stored = 0
            rejections: list[dict[str, Any]] = []
            for processed, claim in enumerate(extracted, start=1):
                indices = claim.source_event_indices or (0,)
                if any(index < 0 or index >= len(extraction_sources) for index in indices):
                    raise ValueError("extracted claim contains an invalid source_event_index")
                claim_sources = [extraction_sources[index] for index in indices]
                primary = claim_sources[0]
                authority = "high" if primary["event_type"] == "explicit_memory" else None
                result = IngestService.store_extracted(
                    self.connection,
                    claim,
                    primary,
                    _now(),
                    self.embedder,
                    authority,
                    policy=self.settings.retention_policy(),
                    relation_discovery_mode=self.settings.relation_discovery_mode,
                    index_text_mode=self.settings.index_text_mode,
                    source_events=claim_sources,
                )
                if result.status == "skipped":
                    rejections.append({"reason": result.reason, "predicate": claim.predicate})
                else:
                    stored += 1
                report_writes("writing_claims", processed, len(extracted), stored)
            report_writes("claims_written", len(extracted), len(extracted), stored)
            return {
                "events": len(source_batch),
                "eligible_events": len(prepared),
                "claims": len(extracted),
                "stored": stored,
                "skipped": len(rejections),
                "rejections": rejections,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }

    def _prepare_event(
        self,
        events: EventRepository,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """逐 Event 执行多模态准备和两级过滤，供窗口构建复用。"""
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
                        str(getattr(self.image_describer, "model", self.settings.image_describer_model)),
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
                    uri_hash = locator.get("sha256") or hashlib.sha256(str(locator.get("uri", "")).encode()).hexdigest()
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
                        detail={"image_index": image_index, "error_class": type(error).__name__},
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
            event_id=event["id"],
            duration_us=(time.perf_counter_ns() - started) // 1000,
            detail={
                "reason": reason,
                "event_type": event["event_type"],
                "actor_type": event["actor_type"],
                "content_chars": len(event["content_json"]),
            },
        )
        if not allowed:
            return None, None
        if self.settings.extract_pre_filter:
            started = time.perf_counter_ns()
            try:
                decision = self.pre_filter.evaluate(event, content)
            except Exception as error:
                self.audit.emit(
                    "extraction_pre_filter",
                    "evaluated",
                    "error_fallback",
                    event_id=event["id"],
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
                    event_id=event["id"],
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
                    return None, decision.reason
        return content, None

    @staticmethod
    def _event_text(content: dict[str, Any]) -> str:
        return "\n".join(part.to_text() for part in parse_content(content) if part.to_text())

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
            worker.close()
    else:
        worker.run_forever(args.poll_interval)


if __name__ == "__main__":
    main()
