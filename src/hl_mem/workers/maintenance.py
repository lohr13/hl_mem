"""Deterministic maintenance and explicitly enabled semantic scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from hl_mem.application.conflict_repairs import repair_dangling_conflicts
from hl_mem.application.expired_cleanup import maintain_expired_claims
from hl_mem.settings import Settings, is_placeholder_secret, parse_daily_cron
from hl_mem.workers.auto_resolve_conflicts import auto_resolve_conflicts
from hl_mem.workers.automation import semantic_job_enabled
from hl_mem.workers.consolidate import enqueue_daily_consolidation
from hl_mem.workers.decay import cleanup_stale_temporal_claims, decay_claims
from hl_mem.workers.deduplicate import enqueue_daily_deduplication, review_pending_near_duplicates
from hl_mem.workers.deferred import cleanup_recall_side_effect_tasks, process_deferred_tasks
from hl_mem.workers.history_cleanup import HistoryCleanupPolicy, cleanup_operational_history
from hl_mem.workers.induce_policies import enqueue_daily_policy_induction
from hl_mem.workers.job_handlers import purge_retained_events_for_namespaces
from hl_mem.workers.mental_models import DerivedMemoryMaintainer
from hl_mem.workers.plan_fulfillment import plan_maintenance_items
from hl_mem.workers.scheduling import enqueue_daily_job
from hl_mem.workers.ttl import expire_claims


class AuditCleaner(Protocol):
    def cleanup(self, retention_days: int, *, batch_size: int = 2_000) -> dict[str, int | bool]: ...


@dataclass(frozen=True)
class MaintenanceOperation:
    """One named maintenance action executed by the Worker boundary."""

    name: str
    operation: Callable[[], Any]


def build_deterministic_maintenance(
    connection: Any,
    settings: Settings,
    *,
    now: str,
    audit: AuditCleaner,
) -> list[MaintenanceOperation]:
    """Build model-free maintenance in stable execution order."""
    cutoff = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=settings.retention_days)).isoformat()
    operations = [
        MaintenanceOperation("process_deferred_tasks", lambda: process_deferred_tasks(connection, now=now)),
        MaintenanceOperation(
            "cleanup_recall_side_effect_tasks",
            lambda: cleanup_recall_side_effect_tasks(connection, before=cutoff),
        ),
        MaintenanceOperation(
            "cleanup_stale_temporal_claims",
            lambda: cleanup_stale_temporal_claims(
                connection,
                age_days=settings.temporal_cleanup_age_days,
                expiry_days=settings.temporal_cleanup_expiry_days,
            ),
        ),
        MaintenanceOperation(
            "expire_claims",
            lambda: expire_claims(
                connection,
                feedback_lifecycle_mode=settings.feedback_lifecycle_mode,
                slot_short_ttl_seconds=settings.slot_short_ttl_seconds,
            ),
        ),
    ]
    if settings.expired_cleanup_mode != "off":
        operations.append(
            MaintenanceOperation(
                "cleanup_expired_claims",
                lambda: maintain_expired_claims(
                    connection,
                    now=now,
                    retention_days=settings.expired_claim_retention_days,
                    batch_size=settings.expired_cleanup_batch_size,
                    mode=settings.expired_cleanup_mode,
                ),
            )
        )
    operations.extend(
        [
            MaintenanceOperation(
                "decay_claims",
                lambda: decay_claims(
                    connection,
                    temporal_decay_days=settings.decay_temporal_days,
                    temporal_archive_days=settings.archive_temporal_days,
                    permanent_decay_days=settings.decay_permanent_days,
                    permanent_archive_days=settings.archive_permanent_days,
                    access_bonus_every=settings.access_bonus_every,
                    access_bonus_days=settings.access_bonus_days,
                    access_bonus_cap_days=settings.access_bonus_cap_days,
                    rollout_grace_days=settings.decay_rollout_grace_days,
                    min_confidence=settings.decay_min_confidence,
                    feedback_lifecycle_mode=settings.feedback_lifecycle_mode,
                    feedback_bonus_cap_days=settings.feedback_bonus_cap_days,
                    decay_model=settings.decay_model,
                    temporal_half_life_days=settings.decay_temporal_half_life_days,
                    permanent_half_life_days=settings.decay_permanent_half_life_days,
                    identity_half_life_days=settings.decay_identity_half_life_days,
                    halflife_archive_threshold=settings.decay_halflife_archive_threshold,
                    halflife_archive_grace_days=settings.decay_halflife_archive_grace_days,
                ),
            ),
            MaintenanceOperation(
                "mark_stale_dependencies",
                lambda: DerivedMemoryMaintainer(connection).mark_stale_dependencies(),
            ),
            MaintenanceOperation(
                "scan_derived_memories",
                lambda: DerivedMemoryMaintainer(connection).scan_and_build(now),
            ),
        ]
    )
    if settings.dedup_enabled:
        operations.append(
            MaintenanceOperation(
                "review_pending_near_duplicates",
                lambda: review_pending_near_duplicates(
                    connection,
                    threshold=settings.dedup_threshold,
                    limit=settings.dedup_scan_limit,
                ),
            )
        )
    operations.append(
        MaintenanceOperation(
            "repair_dangling_conflicts",
            lambda: repair_dangling_conflicts(connection, source="worker"),
        )
    )
    if settings.conflict_auto_resolve_enabled and settings.conflict_auto_mode != "off":
        operations.append(
            MaintenanceOperation(
                "auto_resolve_conflicts",
                lambda: auto_resolve_conflicts(
                    connection,
                    now,
                    max_cases=settings.conflict_maintenance_max_cases,
                    max_elapsed_ms=settings.conflict_maintenance_budget_ms,
                    failure_backoff_seconds=settings.conflict_failure_backoff_seconds,
                ),
            )
        )
    operations.extend(
        MaintenanceOperation(name, operation)
        for name, operation in plan_maintenance_items(connection, now, settings.plan_fulfillment_mode)
    )
    operations.append(
        MaintenanceOperation(
            "purge_retained_events",
            lambda: purge_retained_events_for_namespaces(connection, cutoff),
        )
    )
    if settings.operational_cleanup_enabled:
        operations.extend(
            [
                MaintenanceOperation(
                    "cleanup_operational_history",
                    lambda: cleanup_operational_history(
                        connection,
                        now,
                        HistoryCleanupPolicy(
                            batch_size=settings.operational_batch_size,
                            job_succeeded_days=settings.job_succeeded_days,
                            job_dead_days=settings.job_dead_days,
                            llm_span_days=settings.llm_span_days,
                            dedup_pair_days=settings.dedup_pair_days,
                            feedback_uninjected_days=settings.feedback_uninjected_days,
                            feedback_unlabeled_days=settings.feedback_unlabeled_days,
                        ),
                    ),
                ),
                MaintenanceOperation(
                    "cleanup_audit_log",
                    lambda: audit.cleanup(
                        settings.audit_retention_days,
                        batch_size=settings.operational_batch_size,
                    ),
                ),
            ]
        )
    return operations


def enqueue_daily_reclassify(connection: Any, now: str, cron: str) -> bool:
    """Idempotently enqueue the day's explicitly enabled reclassification job."""
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


def build_semantic_schedules(
    connection: Any,
    settings: Settings,
    *,
    now: Callable[[], str],
    dedup_scheduled_minutes: int | None = None,
) -> list[MaintenanceOperation]:
    """Build only explicitly enabled semantic scheduling operations."""
    operations: list[MaintenanceOperation] = []
    llm_configured = not is_placeholder_secret(settings.llm_api_key)
    if llm_configured and semantic_job_enabled(settings, "consolidate_conflicts"):
        operations.append(
            MaintenanceOperation(
                "enqueue_daily_consolidation",
                lambda: enqueue_daily_consolidation(connection, now(), settings.consolidate_cron),
            )
        )
    if llm_configured and semantic_job_enabled(settings, "deduplicate_claims"):
        scheduled_minutes = (
            dedup_scheduled_minutes
            if dedup_scheduled_minutes is not None
            else parse_daily_cron(settings.dedup_cron, "dedup.cron")
        )
        operations.append(
            MaintenanceOperation(
                "enqueue_daily_deduplication",
                lambda: enqueue_daily_deduplication(connection, now(), scheduled_minutes),
            )
        )
    if semantic_job_enabled(settings, "induce_policies"):
        operations.append(
            MaintenanceOperation(
                "enqueue_daily_policy_induction",
                lambda: enqueue_daily_policy_induction(connection, now(), settings.induce_policies_cron),
            )
        )
    if llm_configured and semantic_job_enabled(settings, "reclassify_claims"):
        operations.append(
            MaintenanceOperation(
                "enqueue_daily_reclassify",
                lambda: enqueue_daily_reclassify(connection, now(), settings.reclassify_cron),
            )
        )
    return operations
