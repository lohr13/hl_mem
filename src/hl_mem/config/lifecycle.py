"""Lifecycle and maintenance configuration ownership."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

FeedbackLifecycleMode = Literal["off", "observe", "on"]
ExpiredCleanupMode = Literal["off", "observe", "on"]
DecayModel = Literal["legacy_linear", "activation_halflife", "confidence_halflife"]


def _toml_field(default: Any, path: str) -> Any:
    return field(default=default, metadata={"toml": path})


@dataclass(frozen=True)
class LifecycleConfig:
    """Configuration owned by the lifecycle boundary."""

    policy_induction_lookback_days: int = field(
        default=7,
        metadata={"toml": "worker.policy_induction_lookback_days"},
    )

    policy_induction_min_episodes: int = field(
        default=3,
        metadata={"toml": "worker.policy_induction_min_episodes"},
    )

    worker_poll_interval: float = field(default=2.0, metadata={"toml": "worker.poll_interval"})

    worker_maintenance_interval: float = _toml_field(600.0, "worker.maintenance_interval")

    conflict_auto_resolve_enabled: bool = _toml_field(True, "worker.conflict_auto_resolve_enabled")

    conflict_maintenance_max_cases: int = _toml_field(50, "worker.conflict_maintenance_max_cases")

    conflict_maintenance_budget_ms: int = _toml_field(1_000, "worker.conflict_maintenance_budget_ms")

    conflict_failure_backoff_seconds: int = _toml_field(300, "worker.conflict_failure_backoff_seconds")

    conflict_writer_yield_ms: int = field(
        default=25,
        metadata={"toml": "worker.conflict_writer_yield_ms"},
    )

    conflict_auto_resolve_max_candidates: int = field(
        default=8,
        metadata={"toml": "worker.conflict_auto_resolve_max_candidates"},
    )

    worker_job_lease_minutes: int = field(default=5, metadata={"toml": "worker.job_lease_minutes"})

    daily_token_limit: int = field(default=500000, metadata={"toml": "worker.daily_token_limit"})

    audit_retention_days: int = field(default=30, metadata={"toml": "worker.audit_retention_days"})

    retention_days: int = field(default=30, metadata={"toml": "retention.event_days"})

    consolidate_cron: str = field(default="03:30", metadata={"toml": "worker.consolidate_cron"})

    consolidate_batch_size: int = field(default=100, metadata={"toml": "worker.consolidate_batch_size"})

    consolidate_confidence: float = field(
        default=0.8,
        metadata={"toml": "worker.consolidate_confidence"},
    )

    induce_policies_cron: str = field(default="04:00", metadata={"toml": "worker.induce_policies_cron"})

    reclassify_cron: str = field(default="04:30", metadata={"toml": "worker.reclassify_cron"})

    memory_temporal_ttl_days: int = field(
        default=7,
        metadata={"toml": "retention.temporal_ttl_days"},
    )

    operational_cleanup_enabled: bool = field(
        default=True,
        metadata={"toml": "retention.operational_cleanup_enabled"},
    )

    operational_batch_size: int = field(
        default=2_000,
        metadata={"toml": "retention.operational_batch_size"},
    )

    expired_cleanup_mode: ExpiredCleanupMode = field(
        default="observe",
        metadata={"toml": "retention.expired_cleanup_mode"},
    )

    expired_claim_retention_days: int = field(
        default=90,
        metadata={"toml": "retention.expired_claim_retention_days"},
    )

    expired_cleanup_batch_size: int = field(
        default=100,
        metadata={"toml": "retention.expired_cleanup_batch_size"},
    )

    job_succeeded_days: int = field(default=30, metadata={"toml": "retention.job_succeeded_days"})

    job_dead_days: int = field(default=90, metadata={"toml": "retention.job_dead_days"})

    llm_span_days: int = field(default=30, metadata={"toml": "retention.llm_span_days"})

    dedup_pair_days: int = field(default=90, metadata={"toml": "retention.dedup_pair_days"})

    feedback_uninjected_days: int = field(
        default=7,
        metadata={"toml": "retention.feedback_uninjected_days"},
    )

    feedback_unlabeled_days: int = field(
        default=90,
        metadata={"toml": "retention.feedback_unlabeled_days"},
    )

    temporal_ttl_days_low: int = field(
        default=3,
        metadata={"toml": "retention.temporal_ttl_days_low"},
    )

    temporal_ttl_days_normal: int = field(
        default=7,
        metadata={"toml": "retention.temporal_ttl_days_normal"},
    )

    temporal_ttl_days_high: int = field(
        default=14,
        metadata={"toml": "retention.temporal_ttl_days_high"},
    )

    importance_low_threshold: float = field(
        default=0.4,
        metadata={"toml": "retention.importance_low_threshold"},
    )

    importance_high_threshold: float = field(
        default=0.7,
        metadata={"toml": "retention.importance_high_threshold"},
    )

    importance_write_floor: float = field(
        default=0.2,
        metadata={"toml": "retention.importance_write_floor"},
    )

    slot_short_ttl_seconds: int = field(
        default=86400,
        metadata={"toml": "retention.slot_short_ttl_seconds"},
    )

    ttl_backfill_batch_size: int = field(
        default=100,
        metadata={"toml": "retention.ttl_backfill_batch_size"},
    )

    ttl_backfill_grace_hours: int = field(
        default=0,
        metadata={"toml": "retention.ttl_backfill_grace_hours"},
    )

    temporal_cleanup_age_days: int = field(
        default=30,
        metadata={"toml": "retention.temporal_cleanup_age_days"},
    )

    temporal_cleanup_expiry_days: int = field(
        default=90,
        metadata={"toml": "retention.temporal_cleanup_expiry_days"},
    )

    decay_temporal_days: int = field(default=7, metadata={"toml": "retention.decay_temporal_days"})

    archive_temporal_days: int = field(default=30, metadata={"toml": "retention.archive_temporal_days"})

    decay_permanent_days: int = field(default=90, metadata={"toml": "retention.decay_permanent_days"})

    archive_permanent_days: int = field(
        default=180,
        metadata={"toml": "retention.archive_permanent_days"},
    )

    access_bonus_every: int = field(default=5, metadata={"toml": "retention.access_bonus_every"})

    access_bonus_days: int = field(default=1, metadata={"toml": "retention.access_bonus_days"})

    access_bonus_cap_days: int = field(default=30, metadata={"toml": "retention.access_bonus_cap_days"})

    decay_rollout_grace_days: int = field(
        default=7,
        metadata={"toml": "retention.decay_rollout_grace_days"},
    )

    decay_min_confidence: float = field(
        default=0.05,
        metadata={"toml": "retention.decay_min_confidence"},
    )

    decay_model: DecayModel = field(default="activation_halflife", metadata={"toml": "decay.model"})

    decay_temporal_half_life_days: int = field(
        default=45,
        metadata={"toml": "decay.temporal_half_life_days"},
    )

    decay_permanent_half_life_days: int = field(
        default=90,
        metadata={"toml": "decay.permanent_half_life_days"},
    )

    decay_identity_half_life_days: int = field(
        default=365,
        metadata={"toml": "decay.identity_half_life_days"},
    )

    decay_halflife_archive_threshold: float = field(
        default=0.05,
        metadata={"toml": "decay.halflife_archive_threshold"},
    )

    decay_halflife_archive_grace_days: int = field(
        default=7,
        metadata={"toml": "decay.halflife_archive_grace_days"},
    )

    feedback_lifecycle_mode: FeedbackLifecycleMode = field(
        default="observe",
        metadata={"toml": "retention.feedback_lifecycle_mode"},
    )

    feedback_bonus_every: int = field(default=3, metadata={"toml": "retention.feedback_bonus_every"})

    feedback_bonus_days: int = field(default=14, metadata={"toml": "retention.feedback_bonus_days"})

    feedback_bonus_cap_days: int = field(
        default=180,
        metadata={"toml": "retention.feedback_bonus_cap_days"},
    )


__all__ = ["DecayModel", "ExpiredCleanupMode", "FeedbackLifecycleMode", "LifecycleConfig"]
